"""Deterministic preparation and protected publication without controller state."""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import re
from typing import Any, NamedTuple

from github.GithubException import GithubException

from scripts.common.github_client import retry_github_call
from scripts.release.authorize import ensure_authorized
from scripts.release.checks import require_green_checks
from scripts.release.models import DerivedRelease, PublishPlan, ReleaseIntent, ReleasePolicy
from scripts.release.policy import validate_branch
from scripts.release.versioning import derive_version
from scripts.release_notes.release_format import parse_version
from scripts.release_notes.version_bump import current_release_state

logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DATED_SECTION_RE = re.compile(r"^Valkey\s+\d+\.\d+\.\d+", re.MULTILINE)


class ReleaseError(Exception):
    """A release request is invalid or no longer matches live repository state."""


class TagRulesetVerdict(NamedTuple):
    protected: bool | None
    bypass_integration_ids: tuple[int, ...] | None = None


def prepare_release(
    gh: Any,
    policy: ReleasePolicy,
    *,
    branch: str,
    intent: ReleaseIntent,
    actor: str,
) -> DerivedRelease:
    """Authorize and derive the release identity; perform no writes."""
    branch = validate_branch(policy, branch)
    ensure_authorized(gh, policy, actor)
    repo = _repo(gh, policy.repo)
    tags = retry_github_call(
        lambda: [tag.name for tag in repo.get_tags()],
        retries=2,
        description="list release tags",
    )
    return derive_version(branch, intent, tags)


def plan_publication(
    gh: Any,
    policy: ReleasePolicy,
    *,
    branch: str,
    candidate_sha: str,
) -> PublishPlan:
    """Recompute the complete publication plan from one exact branch head."""
    branch = validate_branch(policy, branch)
    candidate_sha = candidate_sha.strip().lower()
    if not _SHA_RE.fullmatch(candidate_sha):
        raise ReleaseError("candidate_sha must be a full lowercase 40-character SHA")
    repo = _repo(gh, policy.repo)

    head = retry_github_call(
        lambda: repo.get_branch(branch).commit.sha,
        retries=2,
        description=f"read {branch} head",
    )
    if head != candidate_sha:
        raise ReleaseError(
            f"{branch} is at {head}, not candidate {candidate_sha}; qualify and approve the current head"
        )

    version_h = _read_text(repo, "src/version.h", candidate_sha)
    version, stage = current_release_state(version_h)
    if ".".join(version.split(".")[:2]) != branch:
        raise ReleaseError(f"version.h records {version}, which does not belong to branch {branch}")
    if stage == "dev":
        raise ReleaseError("version.h still records development stage 'dev'")
    tag = version if stage == "ga" else f"{version}-{stage}"

    _require_merged_preparation_pr(repo, policy.repo, branch, version, stage, candidate_sha)

    existing_release = _find_release(repo, tag)
    if existing_release is not None:
        raise ReleaseError(f"release {tag} already exists: {existing_release.html_url}")
    existing_tag_sha = resolve_tag_commit(repo, tag)
    if existing_tag_sha and existing_tag_sha != candidate_sha:
        raise ReleaseError(f"tag {tag} already points at {existing_tag_sha}, not {candidate_sha}")

    # Candidate CI is useful maintainer context, but the exact no-publish
    # qualification matrix is the technical publication gate. A stale or
    # renamed advisory check must not strand a fully qualified release.
    try:
        require_green_checks(repo, policy, candidate_sha)
        candidate_ci = "green"
    except ValueError as exc:
        candidate_ci = f"informational warning: {exc}"
    notes = _read_text(repo, "00-RELEASENOTES", candidate_sha)
    body = _extract_release_section(notes, tag)
    if body is None:
        raise ReleaseError(f"00-RELEASENOTES at {candidate_sha[:12]} has no section for Valkey {tag}")
    body += _changelog_footer(repo, policy.repo, tag, version, stage)

    verdict = tag_ruleset_protected(repo, tag)
    if verdict.protected is not True:
        raise ReleaseError(f"cannot verify an active immutable-tag ruleset for {tag}; refusing publication")
    if verdict.bypass_integration_ids is not None and len(verdict.bypass_integration_ids) != 1:
        raise ReleaseError(
            f"the immutable-tag ruleset for {tag} must name exactly one Integration bypass "
            f"(found {len(verdict.bypass_integration_ids)})"
        )
    return PublishPlan(
        branch=branch,
        tag=tag,
        version=version,
        stage=stage,
        sha=candidate_sha,
        body=body,
        prerelease=stage != "ga",
        make_latest=_make_latest(repo, version, stage),
        tag_protected=verdict.protected,
        tag_bypass_integration_ids=verdict.bypass_integration_ids,
        candidate_ci=candidate_ci,
    )


def plan_digest(plan: PublishPlan) -> str:
    payload = "\n".join(
        (
            "release-plan-v1",
            f"branch={plan.branch}",
            f"tag={plan.tag}",
            f"sha={plan.sha}",
            f"prerelease={plan.prerelease}",
            f"make_latest={plan.make_latest}",
            f"body={hashlib.sha256(plan.body.encode()).hexdigest()}",
            f"tag_protected={plan.tag_protected}",
            "tag_bypasses="
            + (
                "not-visible"
                if plan.tag_bypass_integration_ids is None
                else ",".join(map(str, plan.tag_bypass_integration_ids))
            ),
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def render_plan(plan: PublishPlan) -> str:
    protection = "verified" if plan.tag_protected else "NOT verified"
    bypasses = (
        "not visible to the read-only token"
        if plan.tag_bypass_integration_ids is None
        else ", ".join(map(str, plan.tag_bypass_integration_ids)) or "none"
    )
    return "\n".join(
        (
            "## Release publication plan",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| Branch | `{plan.branch}` |",
            f"| Tag | `{plan.tag}` |",
            f"| Candidate | `{plan.sha}` |",
            f"| Prerelease | `{'yes' if plan.prerelease else 'no'}` |",
            f"| Make latest | `{plan.make_latest}` |",
            f"| Immutable-tag ruleset | {protection} (App bypass ids: {bypasses}) |",
            f"| Candidate CI | {plan.candidate_ci} |",
            f"| Plan digest | `{plan_digest(plan)}` |",
            "",
            "Qualification must pass before the approval gate. Publication then recomputes this plan and refuses any drift.",
            "",
            "<details><summary>Release notes</summary>",
            "",
            plan.body.rstrip(),
            "",
            "</details>",
        )
    )


def publish_release(
    gh: Any,
    policy: ReleasePolicy,
    *,
    branch: str,
    candidate_sha: str,
    actor: str,
    expected_digest: str,
    expected_bypass_integration_id: int,
) -> str:
    """Revalidate an approved plan, atomically bind its tag, and publish."""
    ensure_authorized(gh, policy, actor)
    plan = plan_publication(gh, policy, branch=branch, candidate_sha=candidate_sha)
    if plan.tag_bypass_integration_ids != (expected_bypass_integration_id,):
        raise ReleaseError(
            "the immutable-tag ruleset bypass is not the configured publication App "
            f"(expected {expected_bypass_integration_id}, found {plan.tag_bypass_integration_ids})"
        )
    actual_digest = plan_digest(plan)
    if not expected_digest or actual_digest != expected_digest:
        raise ReleaseError(
            "publication plan changed after approval "
            f"(expected {expected_digest[:12] or '<empty>'}, got {actual_digest[:12]}); rerun"
        )

    repo = _repo(gh, policy.repo)
    _ensure_tag(repo, plan.tag, plan.sha)
    try:
        release = repo.create_git_release(
            plan.tag,
            name=plan.tag,
            message=plan.body,
            draft=False,
            prerelease=plan.prerelease,
            target_commitish=plan.sha,
            make_latest=plan.make_latest,
        )
    except Exception:
        release = _find_release(repo, plan.tag)
        if release is None or resolve_tag_commit(repo, plan.tag) != plan.sha:
            raise
        logger.warning("release creation response was lost; recovered %s", plan.tag)

    if resolve_tag_commit(repo, plan.tag) != plan.sha:
        raise ReleaseError(f"release {plan.tag} was not created at approved SHA {plan.sha}")
    return release.html_url


def resolve_tag_commit(repo: Any, tag: str) -> str:
    try:
        ref = retry_github_call(
            lambda: repo.get_git_ref(f"tags/{tag}"),
            retries=2,
            description=f"resolve tag {tag}",
        )
    except GithubException as exc:
        if exc.status == 404:
            return ""
        raise
    obj = ref.object
    if obj.type == "commit":
        return obj.sha
    if obj.type != "tag":
        return ""
    annotated = retry_github_call(
        lambda: repo.get_git_tag(obj.sha),
        retries=2,
        description=f"peel tag {tag}",
    )
    return annotated.object.sha if annotated.object.type == "commit" else ""


def _ensure_tag(repo: Any, tag: str, sha: str) -> None:
    try:
        retry_github_call(
            lambda: repo.create_git_ref(ref=f"refs/tags/{tag}", sha=sha),
            retries=2,
            description=f"create tag {tag}",
        )
        return
    except GithubException as exc:
        if exc.status != 422:
            raise
    existing = resolve_tag_commit(repo, tag)
    if existing != sha:
        raise ReleaseError(f"tag {tag} already points at {existing or '<unknown>'}, not {sha}")


def _repo(gh: Any, name: str) -> Any:
    return retry_github_call(lambda: gh.get_repo(name), retries=2, description=f"get {name}")


def _require_merged_preparation_pr(
    repo: Any,
    repo_name: str,
    branch: str,
    version: str,
    stage: str,
    candidate_sha: str,
) -> None:
    prep_branch = f"agent/release-cut/{version}-{stage}"
    pulls = retry_github_call(
        lambda: list(repo.get_commit(candidate_sha).get_pulls()),
        retries=2,
        description=f"find PRs associated with candidate {candidate_sha[:12]}",
    )
    matching = next(
        (
            pr
            for pr in pulls
            if getattr(pr, "merged", False)
            and (getattr(pr, "merge_commit_sha", "") or "").lower() == candidate_sha
            and getattr(getattr(pr, "base", None), "ref", "") == branch
            and getattr(getattr(pr, "head", None), "ref", "") == prep_branch
            and getattr(getattr(getattr(pr, "head", None), "repo", None), "full_name", "") == repo_name
        ),
        None,
    )
    if matching is None:
        raise ReleaseError(
            f"candidate {candidate_sha} is not the merge commit of the canonical "
            f"{prep_branch} release preparation PR into {branch}"
        )


def _read_text(repo: Any, path: str, ref: str) -> str:
    content = retry_github_call(
        lambda: repo.get_contents(path, ref=ref),
        retries=2,
        description=f"read {path} at {ref[:12]}",
    )
    if isinstance(content, list):
        raise ReleaseError(f"{path} is not a file")
    return content.decoded_content.decode("utf-8")


def _find_release(repo: Any, tag: str) -> Any | None:
    releases = retry_github_call(lambda: repo.get_releases(), retries=2, description="list releases")
    return next((release for release in releases if release.tag_name == tag), None)


def _extract_release_section(notes: str, tag: str) -> str | None:
    match = re.search(rf"^Valkey\s+{re.escape(tag)}\s", notes, re.MULTILINE)
    if match is None:
        return None
    next_section = _DATED_SECTION_RE.search(notes, match.end())
    end = next_section.start() if next_section else len(notes)
    return notes[match.start() : end].rstrip() + "\n"


def _changelog_footer(repo: Any, repo_name: str, tag: str, version: str, stage: str) -> str:
    tags = {item.name for item in retry_github_call(lambda: repo.get_tags(), retries=2, description="list tags")}
    previous: str | None = None
    rc = re.fullmatch(r"rc([1-9]\d*)", stage)
    if rc and int(rc.group(1)) > 1:
        candidate = f"{version}-rc{int(rc.group(1)) - 1}"
        previous = candidate if candidate in tags else None
    elif stage == "ga":
        major, minor, patch = parse_version(version)
        if patch > 0 and f"{major}.{minor}.{patch - 1}" in tags:
            previous = f"{major}.{minor}.{patch - 1}"
        elif patch == 0:
            rcs = [
                int(match.group(1))
                for item in tags
                if (match := re.fullmatch(rf"{re.escape(version)}-rc([1-9]\d*)", item))
            ]
            if rcs:
                previous = f"{version}-rc{max(rcs)}"
    return f"\n**Full Changelog**: https://github.com/{repo_name}/compare/{previous}...{tag}\n" if previous else ""


def _make_latest(repo: Any, version: str, stage: str) -> str:
    if stage != "ga":
        return "false"
    best: tuple[int, int, int] | None = None
    for release in retry_github_call(lambda: repo.get_releases(), retries=2, description="list releases"):
        if getattr(release, "draft", False) or getattr(release, "prerelease", False):
            continue
        try:
            parsed = parse_version(release.tag_name)
        except ValueError:
            continue
        best = parsed if best is None or parsed > best else best
    return "true" if best is None or parse_version(version) >= best else "false"


def _ref_matches(ref: str, patterns: list[str]) -> bool:
    return any(pattern == "~ALL" or fnmatch.fnmatchcase(ref, pattern) for pattern in patterns)


def tag_ruleset_protected(repo: Any, tag: str) -> TagRulesetVerdict:
    """Report an immutable tag ruleset; never infer protection on API failure."""
    ref = f"refs/tags/{tag}"
    try:
        _, listing = repo._requester.requestJsonAndCheck("GET", f"{repo.url}/rulesets?per_page=100")
        for entry in listing or []:
            if entry.get("target") != "tag" or entry.get("enforcement") != "active":
                continue
            _, ruleset = repo._requester.requestJsonAndCheck("GET", f"{repo.url}/rulesets/{entry['id']}")
            conditions = (ruleset.get("conditions") or {}).get("ref_name") or {}
            if not (
                _ref_matches(ref, conditions.get("include") or [])
                and not _ref_matches(ref, conditions.get("exclude") or [])
            ):
                continue
            rule_types = {rule.get("type") for rule in ruleset.get("rules") or []}
            if not {"creation", "update", "deletion"} <= rule_types:
                continue
            if "bypass_actors" not in ruleset:
                # Immutability alone is insufficient: an unknown bypass actor
                # could move or delete the release tag. Validation tokens must
                # have Administration:read so this list is visible.
                return TagRulesetVerdict(None, None)
            ids: list[int] = []
            for actor in ruleset.get("bypass_actors") or []:
                actor_id = actor.get("actor_id") if isinstance(actor, dict) else None
                if (
                    not isinstance(actor, dict)
                    or actor.get("actor_type") != "Integration"
                    or not isinstance(actor_id, int)
                    or isinstance(actor_id, bool)
                ):
                    return TagRulesetVerdict(True, ())
                ids.append(actor_id)
            return TagRulesetVerdict(True, tuple(ids))
        return TagRulesetVerdict(False)
    except Exception:
        logger.warning("cannot inspect tag rulesets for %s", tag, exc_info=True)
        return TagRulesetVerdict(None)
