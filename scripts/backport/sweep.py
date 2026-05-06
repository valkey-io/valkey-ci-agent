"""Weekly backport sweep across release branches."""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github import Auth, Github

from scripts.backport.conflict_resolver import resolve_conflicts_with_claude
from scripts.backport.main import (
    _resolve_commit_signer,
    _run_git,
    emit_job_summary,
)
from scripts.backport.models import BackportPRContext
from scripts.common.git_auth import GitAuth, github_https_url
from scripts.common.github_client import retry_github_call
from scripts.common.publish_guard import check_publish_allowed

logger = logging.getLogger(__name__)

_DEFAULT_BRANCH_FIELDS = (
    "Backport Branch", "Target Branch", "Release Branch",
    "Branch", "Version", "Release", "Folder",
)
# Only sweep these release branches, even if other N.N branches exist in the repo
_SUPPORTED_RELEASE_BRANCHES = ("7.2", "8.0", "8.1", "9.0", "9.1")
_DEFAULT_RELEASE_BRANCH_PATTERN = r"\d+\.\d+"
_DEFAULT_STATUS_FIELD = "Status"
_DEFAULT_STATUS_VALUE = "To be backported"
_BRANCH_PREFIX = "agent/backport/weekly"

# Well-known per-release-branch backport project boards on valkey-io.
# Each project is scoped to exactly one target branch (no per-item branch
# field), so we can derive the implicit target from the project number alone.
#
# To add a new release branch: add the project number → branch mapping here,
# and add the branch to _SUPPORTED_RELEASE_BRANCHES below.
_VALKEY_IO_PROJECT_TO_BRANCH: dict[int, str] = {
    1: "7.2",
    2: "8.0",
    14: "8.1",
    18: "9.0",
    41: "9.1",
}



@dataclass(frozen=True)
class ProjectBackportCandidate:
    source_pr_number: int
    source_pr_title: str
    source_pr_url: str
    target_branch: str
    merge_commit_sha: str | None = None
    commit_shas: list[str] = field(default_factory=list)


@dataclass
class CandidateResult:
    source_pr_number: int
    source_pr_title: str
    outcome: str  # applied, skipped-existing, skipped-conflict, skipped-test, error
    detail: str = ""


@dataclass
class BranchSweepResult:
    target_branch: str
    candidates_found: int = 0
    results: list[CandidateResult] = field(default_factory=list)
    pr_url: str = ""
    error: str = ""



class GitHubGraphQLClient:
    def __init__(self, token: str) -> None:
        self._token = token

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables}).encode()
        # Retry transient 5xx / network failures with exponential backoff.
        # A single GraphQL hiccup shouldn't abort an entire branch sweep.
        import random as _random
        import time as _time
        last_exc: Exception | None = None
        for attempt in range(4):
            request = urllib.request.Request(
                "https://api.github.com/graphql",
                data=payload,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    body = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    last_exc = exc
                    wait = _random.uniform(0, min(8.0, 1.0 * (2 ** attempt)))
                    logger.warning(
                        "GraphQL %d retry after %.2fs: %s",
                        exc.code, wait, details[:200],
                    )
                    _time.sleep(wait)
                    continue
                raise RuntimeError(f"GraphQL failed: {exc.code} {details}") from exc
            except urllib.error.URLError as exc:
                if attempt < 3:
                    last_exc = exc
                    wait = _random.uniform(0, min(8.0, 1.0 * (2 ** attempt)))
                    logger.warning("GraphQL URL error retry after %.2fs: %s", wait, exc)
                    _time.sleep(wait)
                    continue
                raise
        else:
            if last_exc is not None:
                raise last_exc

        data = json.loads(body)
        if data.get("errors"):
            msgs = "; ".join(str(e.get("message", e)) for e in data["errors"])
            raise RuntimeError(f"GraphQL errors: {msgs}")
        return data.get("data", {})



class ProjectBackportDiscovery:
    def __init__(self, gql: GitHubGraphQLClient, *, project_owner: str,
                 project_number: int, project_owner_type: str = "organization",
                 status_field: str = _DEFAULT_STATUS_FIELD,
                 status_value: str = _DEFAULT_STATUS_VALUE,
                 branch_fields: list[str] | None = None,
                 implicit_target_branch: str | None = None) -> None:
        self._gql = gql
        self._owner = project_owner
        self._number = project_number
        self._owner_type = project_owner_type
        self._status_field = status_field
        self._status_value = status_value
        self._branch_fields = branch_fields or list(_DEFAULT_BRANCH_FIELDS)
        # If set, every candidate on this project goes to this branch
        # (used for per-release-version project boards like valkey-io/projects/14 → 8.1)
        self._implicit_target = implicit_target_branch

    def discover(self, release_branches: list[str]) -> dict[str, list[ProjectBackportCandidate]]:
        by_branch: dict[str, list[ProjectBackportCandidate]] = {b: [] for b in release_branches}
        for item in self._iter_items():
            c = self._candidate_from_item(item, release_branches)
            if c:
                by_branch.setdefault(c.target_branch, []).append(c)
        return by_branch

    def _iter_items(self) -> list[dict[str, Any]]:
        owner_field = "user" if self._owner_type == "user" else "organization"
        query = _project_items_query(owner_field)
        cursor = None
        items: list[dict[str, Any]] = []
        while True:
            data = self._gql.execute(query, {"owner": self._owner, "number": self._number, "cursor": cursor})
            project = (data.get(owner_field) or {}).get("projectV2")
            if not project:
                raise RuntimeError(f"Project {self._owner}/{self._number} not found")
            page = project.get("items") or {}
            items.extend(page.get("nodes") or [])
            pi = page.get("pageInfo") or {}
            if not pi.get("hasNextPage"):
                return items
            cursor = pi.get("endCursor")

    def _candidate_from_item(self, item: dict[str, Any], branches: list[str]) -> ProjectBackportCandidate | None:
        content = item.get("content") or {}
        if content.get("__typename") != "PullRequest" or not content.get("merged"):
            return None
        fields = _extract_field_values(item)
        if not _field_has_value(fields, self._status_field, self._status_value):
            return None
        # Determine target branch: either from project (implicit) or from a field
        target: str | None
        if self._implicit_target:
            target = self._implicit_target
        else:
            target = _matching_release_branch(fields, self._branch_fields, branches)
            if not target:
                return None
        commits = [n.get("commit", {}).get("oid", "") for n in (content.get("commits", {}).get("nodes") or [])]
        merge_sha = (content.get("mergeCommit") or {}).get("oid")
        return ProjectBackportCandidate(
            source_pr_number=int(content["number"]),
            source_pr_title=str(content.get("title") or ""),
            source_pr_url=str(content.get("url") or ""),
            target_branch=target,
            merge_commit_sha=merge_sha,
            commit_shas=[s for s in commits if s],
        )


def discover_release_branches(repo: Any, pattern: str) -> list[str]:
    regex = re.compile(pattern)
    branches = [b.name for b in retry_github_call(lambda: list(repo.get_branches()), retries=3, description="list branches")]
    matched = sorted([b for b in branches if regex.fullmatch(b) and b in _SUPPORTED_RELEASE_BRANCHES], key=_release_branch_sort_key)
    logger.info("Discovered release branches: %s", matched)
    return matched



def run_backport_sweep(
    *,
    repo_full_name: str,
    github_token: str,
    project_owner: str,
    project_number: int,
    project_owner_type: str = "organization",
    status_field: str = _DEFAULT_STATUS_FIELD,
    status_value: str = _DEFAULT_STATUS_VALUE,
    branch_fields: list[str] | None = None,
    push_repo: str | None = None,
    only_branch: str | None = None,
    test_commands: list[str] | None = None,
    discover_only: bool = False,
    implicit_target_branch: str | None = None,
    max_candidates: int = 0,
) -> list[BranchSweepResult]:
    gh = Github(auth=Auth.Token(github_token))
    repo = retry_github_call(lambda: gh.get_repo(repo_full_name), retries=3, description=f"get {repo_full_name}")
    release_branches = discover_release_branches(repo, _DEFAULT_RELEASE_BRANCH_PATTERN)

    # Auto-derive implicit_target_branch for well-known valkey-io per-release
    # project boards when caller did not pass one explicitly.
    if (
        not implicit_target_branch
        and project_owner == "valkey-io"
        and project_owner_type == "organization"
        and project_number in _VALKEY_IO_PROJECT_TO_BRANCH
    ):
        implicit_target_branch = _VALKEY_IO_PROJECT_TO_BRANCH[project_number]
        logger.info(
            "Derived implicit target branch %s from valkey-io project %d",
            implicit_target_branch, project_number,
        )

    if only_branch:
        release_branches = [b for b in release_branches if b == only_branch]
    if implicit_target_branch and implicit_target_branch not in release_branches:
        # User-specified target takes precedence even if not in pattern match
        release_branches = [implicit_target_branch]

    discovery = ProjectBackportDiscovery(
        GitHubGraphQLClient(github_token),
        project_owner=project_owner, project_number=project_number,
        project_owner_type=project_owner_type, status_field=status_field,
        status_value=status_value, branch_fields=branch_fields,
        implicit_target_branch=implicit_target_branch,
    )
    candidates_by_branch = discovery.discover(release_branches)

    results: list[BranchSweepResult] = []
    for branch in release_branches:
        candidates = candidates_by_branch.get(branch, [])
        if max_candidates > 0:
            logger.info("Branch %s: %d candidate(s) found, will apply up to %d", branch, len(candidates), max_candidates)
        else:
            logger.info("Branch %s: %d candidate(s)", branch, len(candidates))
        if discover_only:
            for c in candidates:
                logger.info("  PR #%d: %s (%s)", c.source_pr_number, c.source_pr_title, c.merge_commit_sha or "no merge sha")
            results.append(BranchSweepResult(target_branch=branch, candidates_found=len(candidates)))
            continue
        if not candidates:
            results.append(BranchSweepResult(target_branch=branch))
            continue
        results.append(_process_branch(
            gh=gh, repo=repo, repo_full_name=repo_full_name,
            github_token=github_token, target_branch=branch,
            candidates=candidates, push_repo=push_repo or repo_full_name,
            test_commands=test_commands or [],
            max_applied=max_candidates,
        ))

    summary = _build_summary(results)
    emit_job_summary(summary)
    return results


def _process_branch(
    *, gh: Any, repo: Any, repo_full_name: str, github_token: str,
    target_branch: str, candidates: list[ProjectBackportCandidate],
    push_repo: str, test_commands: list[str],
    max_applied: int = 0,
) -> BranchSweepResult:
    result = BranchSweepResult(target_branch=target_branch, candidates_found=len(candidates))
    tmpdir = tempfile.mkdtemp(prefix=f"backport-{target_branch}-")

    try:
        with GitAuth(github_token, prefix="backport-sweep-git-askpass-") as git_auth:
            git_env = git_auth.env()
            check_publish_allowed(target_repo=push_repo, action="git_push", context=f"{_BRANCH_PREFIX}/{target_branch}")
            # Clone
            clone_url = github_https_url(repo_full_name)
            _run_git(tmpdir, "clone", "--branch", target_branch, clone_url, tmpdir, env=git_env)
            _run_git(tmpdir, "config", "user.name", "valkey-ci-agent")
            _run_git(tmpdir, "config", "user.email", "ci-agent@valkey.io")

            # Sync push_repo's copy of target_branch to match source before
            # we start cherry-picking. Without this, if the fork's release
            # branch drifts behind upstream, the resulting PR diff ends up
            # including everything between fork's branch and upstream, not
            # just the cherry-picked commits. Only fast-forward — if the
            # fork has diverged (local commits), we abort to avoid clobbering.
            if push_repo != repo_full_name:
                _sync_target_branch_to_source(gh, push_repo, repo_full_name, target_branch)

            # Check for existing backport branch on push_repo
            backport_branch = f"{_BRANCH_PREFIX}/{target_branch}"
            existing_pr = _find_existing_pr(gh, push_repo, backport_branch)

            if existing_pr:
                logger.info("Found existing PR #%d for %s, fetching branch...", existing_pr.number, target_branch)
                push_url = github_https_url(push_repo)
                _run_git(tmpdir, "remote", "add", "push_target", push_url, env=git_env)
                _run_git(tmpdir, "fetch", "push_target", backport_branch, env=git_env)
                _run_git(tmpdir, "checkout", f"push_target/{backport_branch}")
                _run_git(tmpdir, "checkout", "-B", backport_branch)
                # Rebase onto refreshed target so the backport branch is
                # based on the current release-branch HEAD, not a stale one.
                # Without this, cherry-picks stack on old history and the PR
                # diff vs target includes every commit between old and new
                # target head + the new cherry-picks.
                rebase_result = subprocess.run(
                    ["git", "rebase", f"origin/{target_branch}"],
                    cwd=tmpdir, capture_output=True, text=True,
                )
                if rebase_result.returncode != 0:
                    subprocess.run(["git", "rebase", "--abort"], cwd=tmpdir, capture_output=True)
                    raise RuntimeError(
                        f"Could not rebase existing backport branch "
                        f"{backport_branch} onto origin/{target_branch}. "
                        f"The existing backport PR #{existing_pr.number} "
                        f"likely has conflicts with the refreshed release "
                        f"branch. Rebase manually or close the PR before "
                        f"the next sweep. Git stderr: "
                        f"{rebase_result.stderr.strip()[:300]}"
                    )
            else:
                # No open PR. If a stale backport branch still exists on
                # push_repo (e.g., because a previous PR was closed without
                # merging), delete it so we start from a clean target branch
                # state instead of stacking new commits on old history.
                _delete_stale_backport_branch(gh, push_repo, backport_branch)
                _run_git(tmpdir, "checkout", "-b", backport_branch)
                push_url = github_https_url(push_repo)
                _run_git(tmpdir, "remote", "add", "push_target", push_url, env=git_env)

            # Find already-applied PRs
            already_applied = _list_already_applied(tmpdir, target_branch, backport_branch)
            logger.info("Already applied on %s: %s", backport_branch, already_applied)

            signer, require_dco_signoff = _resolve_commit_signer()

            applied_count = 0
            for candidate in candidates:
                if max_applied > 0 and applied_count >= max_applied:
                    logger.info(
                        "Branch %s: reached cap of %d applied backport(s); deferring remaining %d candidate(s) to next sweep",
                        target_branch, max_applied, len(candidates) - candidates.index(candidate),
                    )
                    break
                if str(candidate.source_pr_number) in already_applied:
                    result.results.append(CandidateResult(
                        source_pr_number=candidate.source_pr_number,
                        source_pr_title=candidate.source_pr_title,
                        outcome="skipped-existing",
                        detail="already on backport branch",
                    ))
                    continue

                cr = _apply_candidate(tmpdir, candidate, signer, repo_full_name, git_env, require_dco_signoff=require_dco_signoff)
                result.results.append(cr)
                if cr.outcome == "applied":
                    applied_count += 1

            # Push if we applied anything and validation passes.
            applied = [r for r in result.results if r.outcome == "applied"]
            if applied:
                ok, output = _run_test_commands(tmpdir, test_commands)
                if not ok:
                    for item in applied:
                        item.outcome = "skipped-test"
                        item.detail = output[:500]
                    logger.warning("Validation failed for %s; not pushing branch.", target_branch)
                    return result
                check_publish_allowed(target_repo=push_repo, action="git_push", context=backport_branch)
                _run_git(tmpdir, "push", "push_target", backport_branch, env=git_env)
                logger.info("Pushed %d commit(s) to %s/%s", len(applied), push_repo, backport_branch)

                # Upsert PR
                pr_url = _upsert_pr(gh, push_repo, target_branch, backport_branch, result, existing_pr)
                result.pr_url = pr_url

    except Exception as exc:
        logger.exception("Error processing branch %s", target_branch)
        result.error = str(exc)
        result.results.append(CandidateResult(
            source_pr_number=0,
            source_pr_title=f"Branch {target_branch}",
            outcome="error",
            detail=str(exc),
        ))
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    return result


def _apply_candidate(
    repo_dir: str, candidate: ProjectBackportCandidate,
    signer: object,
    repo_full_name: str, git_env: dict[str, str],
    require_dco_signoff: bool = False,
) -> CandidateResult:
    sha = candidate.merge_commit_sha
    if not sha:
        return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "error", "no merge SHA")

    try:
        # Fetch the merge commit
        _run_git(repo_dir, "fetch", "origin", sha, env=git_env)
        # Cherry-pick directly without re-checkout (we're already on the backport branch)
        result = subprocess.run(
            ["git", "cherry-pick", "-m", "1", sha],
            cwd=repo_dir, capture_output=True, text=True,
        )
    except Exception as exc:
        return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "error", str(exc))

    if result.returncode == 0:
        return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "applied")

    # Detect every unmerged path. Porcelain status has several conflict
    # forms (UU, DU, UD, AU, UA, AA, DD); diff-filter=U covers all of them
    # without depending on two-letter status parsing.
    conflict_result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    conflicting_paths = [
        line.strip()
        for line in conflict_result.stdout.splitlines()
        if line.strip()
    ]
    if not conflicting_paths:
        subprocess.run(["git", "cherry-pick", "--abort"], cwd=repo_dir, capture_output=True)
        stderr = result.stderr[:500]
        if "cherry-pick is now empty" in result.stderr or "nothing to commit" in result.stderr:
            return CandidateResult(
                candidate.source_pr_number,
                candidate.source_pr_title,
                "skipped-existing",
                "already applied or empty cherry-pick",
            )
        return CandidateResult(
            candidate.source_pr_number,
            candidate.source_pr_title,
            "error",
            f"cherry-pick failed: {stderr}",
        )

    logger.info("Found %d conflicting file(s): %s", len(conflicting_paths), conflicting_paths)
    # Build ConflictedFile list with real target/source content for the whitespace-only fast path
    from scripts.backport.models import ConflictedFile
    conflicting_files = []
    target_missing_paths: set[str] = set()
    for path in conflicting_paths:
        # :2:<path> = ours (target branch, where we're cherry-picking TO)
        # :3:<path> = theirs (source commit being cherry-picked)
        target_content = _read_index_stage(repo_dir, path, 2)
        source_content = _read_index_stage(repo_dir, path, 3)
        if not _index_stage_exists(repo_dir, path, 2):
            target_missing_paths.add(path)
        conflicting_files.append(ConflictedFile(
            path=path,
            target_branch_content=target_content,
            source_branch_content=source_content,
        ))
    if target_missing_paths:
        subprocess.run(["git", "cherry-pick", "--abort"], cwd=repo_dir, capture_output=True)
        paths = ", ".join(sorted(target_missing_paths))
        return CandidateResult(
            candidate.source_pr_number,
            candidate.source_pr_title,
            "skipped-conflict",
            f"target branch lacks conflicted file(s): {paths}",
        )

    pr_context = BackportPRContext(
        source_pr_number=candidate.source_pr_number,
        source_pr_title=candidate.source_pr_title,
        source_pr_url=candidate.source_pr_url,
        source_pr_diff="",
        target_branch=candidate.target_branch,
        commits=candidate.commit_shas,
    )

    resolutions = resolve_conflicts_with_claude(repo_dir, conflicting_files, pr_context)
    unresolved = [r for r in resolutions if r.resolved_content is None]
    if unresolved:
        # Abort cherry-pick
        subprocess.run(["git", "cherry-pick", "--abort"], cwd=repo_dir, capture_output=True)
        paths = ", ".join(r.path for r in unresolved)
        return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "skipped-conflict", f"unresolved: {paths}")

    # Apply resolutions and commit. If Claude's resolution ended up matching
    # the target branch exactly, treat it as already satisfied rather than
    # failing the whole batch on an empty commit.
    for r in resolutions:
        if r.resolved_content is not None:
            resolved_path = Path(repo_dir, r.path)
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            resolved_path.write_text(r.resolved_content, encoding="utf-8")
            _run_git(repo_dir, "add", r.path)
    if not _has_staged_changes(repo_dir):
        subprocess.run(["git", "cherry-pick", "--abort"], cwd=repo_dir, capture_output=True)
        return CandidateResult(
            candidate.source_pr_number,
            candidate.source_pr_title,
            "skipped-existing",
            "resolution was already satisfied on target branch",
        )

    # Complete the cherry-pick so git preserves the original author from
    # CHERRY_PICK_HEAD. A plain `git commit` would set the author to the
    # local user.name/user.email, breaking backport authorship. Set
    # core.editor=true so git doesn't try to open an editor in CI.
    commit_result = subprocess.run(
        [
            "git",
            "-c", "core.editor=true",
            "cherry-pick", "--continue",
        ],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if commit_result.returncode != 0:
        stderr_lower = (commit_result.stderr or "").lower()
        stdout_lower = (commit_result.stdout or "").lower()
        if "nothing to commit" in stderr_lower or "nothing to commit" in stdout_lower:
            subprocess.run(["git", "cherry-pick", "--abort"], cwd=repo_dir, capture_output=True)
            return CandidateResult(
                candidate.source_pr_number, candidate.source_pr_title,
                "skipped-existing",
                "resolution was already satisfied on target branch",
            )
        subprocess.run(["git", "cherry-pick", "--abort"], cwd=repo_dir, capture_output=True)
        return CandidateResult(
            candidate.source_pr_number, candidate.source_pr_title,
            "skipped-conflict",
            f"commit failed: {(commit_result.stderr or commit_result.stdout).strip()[:200]}",
        )
    if require_dco_signoff:
        amend_result = subprocess.run(
            ["git", "commit", "--amend", "--no-edit", "--signoff"],
            cwd=repo_dir, capture_output=True, text=True,
        )
        if amend_result.returncode != 0:
            # Cherry-pick already succeeded; log and continue. Missing DCO
            # sign-off will surface downstream (e.g., in the PR's CLA check)
            # rather than silently discarding the backport.
            logger.warning(
                "Failed to amend commit with DCO sign-off for #%d: %s",
                candidate.source_pr_number,
                (amend_result.stderr or amend_result.stdout).strip()[:200],
            )

    # Sanity check: reject commits that are wildly larger than upstream.
    # Claude Code conflict resolution sometimes over-applies when a file
    # doesn't exist on the target branch — it creates the whole file
    # instead of skipping it. Compare HEAD's stats to the source commit's
    # stats; if additions exceed 3x upstream or >300 extra lines, revert.
    issue = _check_applied_commit_size(repo_dir, candidate)
    if issue:
        logger.warning(
            "Reverting cherry-pick for #%d: %s",
            candidate.source_pr_number, issue,
        )
        subprocess.run(["git", "reset", "--hard", "HEAD^"], cwd=repo_dir, capture_output=True)
        return CandidateResult(
            candidate.source_pr_number, candidate.source_pr_title,
            "skipped-conflict",
            f"rejected after over-application: {issue}",
        )

    return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "applied", "conflicts resolved by Claude Code")


def _has_staged_changes(repo_dir: str) -> bool:
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    return result.returncode == 1


def _index_stage_exists(repo_dir: str, path: str, stage: int) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f":{stage}:{path}"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    return result.returncode == 0


def _check_applied_commit_size(
    repo_dir: str, candidate: ProjectBackportCandidate,
) -> str | None:
    """Return an error string if the applied cherry-pick is wildly larger
    than the upstream source commit. Returns None if the commit looks sane.

    Guards against Claude Code over-application when files don't exist on
    the target branch.
    """
    source_sha = candidate.merge_commit_sha or (candidate.commit_shas[0] if candidate.commit_shas else None)
    if not source_sha:
        return None  # Nothing to compare against

    try:
        # Fetch the upstream commit so we can diff-stat it
        subprocess.run(
            ["git", "fetch", "origin", source_sha],
            cwd=repo_dir, capture_output=True, text=True, check=False,
        )
        # Upstream commit additions
        upstream_stats = subprocess.run(
            ["git", "show", "--stat", "--format=", source_sha],
            cwd=repo_dir, capture_output=True, text=True, check=False,
        )
        upstream_additions = _parse_additions_from_stat(upstream_stats.stdout)
        # Applied commit (HEAD) additions
        applied_stats = subprocess.run(
            ["git", "show", "--stat", "--format=", "HEAD"],
            cwd=repo_dir, capture_output=True, text=True, check=False,
        )
        applied_additions = _parse_additions_from_stat(applied_stats.stdout)
    except Exception:
        return None  # If the check fails, don't block the backport

    if upstream_additions <= 0:
        return None  # Can't compare; allow through

    # Reject only when both:
    #  - applied additions are >= 3x upstream AND
    #  - applied additions exceed upstream by more than 100 lines
    # The 100-line floor prevents false positives on small PRs where a
    # legitimate branch adaptation (e.g., 5 lines -> 15 lines) trips the
    # 3x ratio. Over-application typically produces hundreds of extra
    # lines (re-creating whole files), so the floor cleanly separates the
    # two cases.
    extra = applied_additions - upstream_additions
    if applied_additions >= upstream_additions * 3 and extra > 100:
        return (
            f"applied +{applied_additions} vs upstream +{upstream_additions} "
            f"(+{extra} extra lines, "
            f"{applied_additions / max(1, upstream_additions):.1f}x)"
        )
    # Absolute floor: a >300-line over-application is suspicious even when
    # the ratio is mild (e.g., upstream +200, applied +500).
    if extra > 300:
        return (
            f"applied +{applied_additions} vs upstream +{upstream_additions} "
            f"(+{extra} extra lines, "
            f"{applied_additions / max(1, upstream_additions):.1f}x)"
        )
    return None


def _parse_additions_from_stat(stat_output: str) -> int:
    """Parse 'N insertions(+)' from git show --stat output."""
    match = re.search(r"(\d+) insertion", stat_output)
    return int(match.group(1)) if match else 0



def _read_index_stage(repo_dir: str, path: str, stage: int) -> str:
    """Read the content of a file from a specific merge stage.
    Stage 1 = common ancestor, 2 = ours (target), 3 = theirs (source).
    Returns empty string if the stage doesn't exist (e.g., add/add conflict).
    """
    try:
        result = subprocess.run(
            ["git", "show", f":{stage}:{path}"],
            cwd=repo_dir, capture_output=True, text=True,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return ""


def _run_test_commands(repo_dir: str, test_commands: list[str]) -> tuple[bool, str]:
    if not test_commands:
        return True, ""
    for command in test_commands:
        logger.info("Running backport validation command: %s", command)
        result = subprocess.run(
            command,
            cwd=repo_dir,
            shell=True,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if result.returncode != 0:
            output = "\n".join(
                part for part in [result.stdout[-2000:], result.stderr[-2000:]]
                if part
            ).strip()
            return False, output or f"`{command}` failed with exit code {result.returncode}"
    return True, ""


def _sync_target_branch_to_source(
    gh: Any, push_repo: str, source_repo: str, target_branch: str,
) -> None:
    """Fast-forward push_repo's copy of target_branch to source_repo's head.

    If push_repo is a fork of source_repo, its release branches can drift
    behind. That makes the resulting backport PR compare diff include every
    commit between fork and upstream — not just the cherry-picked ones.
    Before cherry-picking, fast-forward the fork's branch to match source.

    Only fast-forwards. If the fork has diverged (local commits not on
    source), this raises a RuntimeError so the caller aborts the branch
    sweep before cherry-picking on top of a potentially wrong base.
    """
    try:
        source_sha = retry_github_call(
            lambda: gh.get_repo(source_repo).get_branch(target_branch).commit.sha,
            retries=2, description=f"get {source_repo}:{target_branch} head",
        )
        push_sha = retry_github_call(
            lambda: gh.get_repo(push_repo).get_branch(target_branch).commit.sha,
            retries=2, description=f"get {push_repo}:{target_branch} head",
        )
    except Exception as exc:
        raise RuntimeError(
            f"Could not resolve branch heads for sync of {push_repo}:{target_branch} "
            f"against {source_repo}: {exc}"
        ) from exc

    if push_sha == source_sha:
        logger.info("push_repo %s:%s already in sync with %s", push_repo, target_branch, source_repo)
        return

    # Check if push_sha is an ancestor of source_sha (i.e. fast-forward is safe)
    try:
        compare = retry_github_call(
            lambda: gh.get_repo(source_repo).compare(push_sha, source_sha),
            retries=2, description=f"compare {push_sha[:8]}..{source_sha[:8]}",
        )
    except Exception as exc:
        raise RuntimeError(
            f"Could not compare {push_repo}:{target_branch} to {source_repo}:{target_branch}: {exc}"
        ) from exc

    if compare.status in ("identical", "ahead"):
        # push_repo is behind source_repo — safe to fast-forward
        logger.info(
            "Fast-forwarding %s:%s from %s to %s (behind by %d)",
            push_repo, target_branch, push_sha[:8], source_sha[:8], compare.ahead_by,
        )
        check_publish_allowed(
            target_repo=push_repo, action="update_ref",
            context=f"fast-forward {target_branch} to source head",
        )
        try:
            ref = retry_github_call(
                lambda: gh.get_repo(push_repo).get_git_ref(f"heads/{target_branch}"),
                retries=2, description=f"get ref {target_branch}",
            )
            retry_github_call(
                lambda: ref.edit(source_sha, force=False),
                retries=2, description=f"fast-forward {target_branch}",
            )
        except Exception as exc:
            raise RuntimeError(
                f"Fast-forward of {push_repo}:{target_branch} to "
                f"{source_repo}:{target_branch} failed: {exc}"
            ) from exc
    elif compare.status in ("diverged", "behind"):
        raise RuntimeError(
            f"{push_repo}:{target_branch} has diverged from "
            f"{source_repo}:{target_branch} (ahead={compare.ahead_by}, "
            f"behind={compare.behind_by}). Cannot safely fast-forward. "
            "Resolve the divergence manually before running the sweep."
        )


def _find_existing_pr(gh: Any, push_repo: str, branch: str) -> Any | None:
    """Return the open backport PR for *branch* on *push_repo*, or None.

    Only returns None when GitHub confirmed no matching PR exists.
    Transient errors (network, auth, 5xx) propagate so the caller can
    distinguish "no PR" from "couldn't check" and avoid deleting an
    active backport branch on a transient failure.
    """
    repo = retry_github_call(lambda: gh.get_repo(push_repo), retries=2, description=f"get {push_repo}")
    pulls = retry_github_call(
        lambda: list(repo.get_pulls(state="open", head=f"{push_repo.split('/')[0]}:{branch}")),
        retries=2, description="list PRs",
    )
    return pulls[0] if pulls else None


def _delete_stale_backport_branch(gh: Any, push_repo: str, branch: str) -> None:
    """Delete a backport branch on push_repo if it exists without an open PR.

    Guards against scenario where a prior backport PR was closed without
    merging and the branch is still sitting there. If we simply created a
    new PR from that stale branch, we'd carry over whatever bad history was
    on it. Deleting forces the next sweep to start fresh from target.
    """
    try:
        repo = retry_github_call(lambda: gh.get_repo(push_repo), retries=2, description=f"get {push_repo}")
        ref = retry_github_call(lambda: repo.get_git_ref(f"heads/{branch}"), retries=1, description=f"check ref {branch}")
        if ref is None:
            return
        logger.info("Deleting stale backport branch %s on %s (no open PR)", branch, push_repo)
        check_publish_allowed(target_repo=push_repo, action="delete_branch", context=branch)
        retry_github_call(lambda: ref.delete(), retries=2, description=f"delete ref {branch}")
    except Exception as exc:
        # Branch not found is fine — nothing to prune. Any other error
        # we log but don't abort; the next cherry-pick push will surface
        # a real problem clearly.
        msg = str(exc).lower()
        if "not found" in msg or "404" in msg:
            return
        logger.warning("Could not prune stale backport branch %s: %s", branch, exc)


def _upsert_pr(gh: Any, push_repo: str, target_branch: str, head_branch: str,
               result: BranchSweepResult, existing_pr: Any | None) -> str:
    repo = retry_github_call(lambda: gh.get_repo(push_repo), retries=2, description=f"get {push_repo}")
    body = _build_pr_body(result)
    title = f"[backport] Weekly backport sweep for {target_branch}"

    if existing_pr:
        check_publish_allowed(target_repo=push_repo, action="edit_pull", context=f"PR #{existing_pr.number}")
        retry_github_call(lambda: existing_pr.edit(title=title, body=body), retries=2, description="update PR")
        logger.info("Updated PR #%d on %s", existing_pr.number, push_repo)
        return existing_pr.html_url

    check_publish_allowed(target_repo=push_repo, action="create_pull", context=head_branch)
    owner = push_repo.split("/")[0]
    pr = retry_github_call(
        lambda: repo.create_pull(title=title, body=body, head=f"{owner}:{head_branch}", base=target_branch, draft=True),
        retries=2, description="create PR",
    )
    logger.info("Created PR #%d on %s", pr.number, push_repo)
    return pr.html_url


def _list_already_applied(repo_dir: str, base_branch: str, backport_branch: str) -> set[str]:
    """Extract source PR numbers from commit messages on the backport branch.

    Returns the set of PR numbers that already appear as cherry-picks on
    the backport branch ahead of the release branch. Errors (e.g., a
    stale ref that can't be resolved) propagate — returning an empty
    set on error would make the sweep re-apply commits and create
    duplicate history.
    """
    result = subprocess.run(
        ["git", "log", f"origin/{base_branch}..{backport_branch}", "--format=%s"],
        cwd=repo_dir, capture_output=True, text=True, check=True,
    )
    pr_nums: set[str] = set()
    for line in result.stdout.strip().splitlines():
        m = re.search(r"\(#(\d+)\)", line)
        if m:
            pr_nums.add(m.group(1))
    return pr_nums



def _build_pr_body(result: BranchSweepResult) -> str:
    lines = [
        f"# Weekly backport sweep for {result.target_branch}",
        "",
        "Automated cherry-picks from PRs marked \"To be backported\".",
        "",
    ]
    applied = [r for r in result.results if r.outcome == "applied"]
    skipped = [r for r in result.results if r.outcome != "applied"]

    if applied:
        lines.extend(["## Applied", "", "| Source PR | Title | Detail |", "|---|---|---|"])
        for r in applied:
            lines.append(f"| `#{r.source_pr_number}` | {_esc(r.source_pr_title)} | {_esc(r.detail)} |")
        lines.append("")

    if skipped:
        lines.extend([
            f"<details><summary>Skipped / unresolved ({len(skipped)})</summary>",
            "",
            "| Source PR | Title | Reason |",
            "|---|---|---|",
        ])
        for r in skipped:
            lines.append(f"| `#{r.source_pr_number}` | {_esc(r.source_pr_title)} | {r.outcome}: {_esc(r.detail)} |")
        lines.extend(["", "</details>", ""])

    lines.extend(["---", "*Generated by valkey-ci-agent using Claude Code.*"])
    return "\n".join(lines)


def _build_summary(results: list[BranchSweepResult]) -> str:
    lines = ["## Backport Sweep", ""]
    for r in results:
        applied = sum(1 for c in r.results if c.outcome == "applied")
        suffix = f" — [PR]({r.pr_url})" if r.pr_url else ""
        if r.error:
            suffix += f" — error: {r.error}"
        lines.append(f"- `{r.target_branch}`: {applied}/{r.candidates_found} applied" + suffix)
    return "\n".join(lines)



def _normalize(value: object) -> str:
    return str(value or "").strip().lower()


def _esc(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _release_branch_sort_key(name: str) -> tuple[int, ...]:
    parts = []
    for p in name.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(-1)
    return tuple(parts)


def _project_items_query(owner_field: str) -> str:
    return f"""
query($owner: String!, $number: Int!, $cursor: String) {{
  {owner_field}(login: $owner) {{
    projectV2(number: $number) {{
      items(first: 100, after: $cursor) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          content {{
            __typename
            ... on PullRequest {{
              number title url merged
              mergeCommit {{ oid }}
              commits(first: 100) {{ nodes {{ commit {{ oid }} }} }}
            }}
          }}
          fieldValues(first: 50) {{
            nodes {{
              __typename
              ... on ProjectV2ItemFieldTextValue {{ text field {{ ... on ProjectV2FieldCommon {{ name }} }} }}
              ... on ProjectV2ItemFieldSingleSelectValue {{ name field {{ ... on ProjectV2FieldCommon {{ name }} }} }}
              ... on ProjectV2ItemFieldNumberValue {{ number field {{ ... on ProjectV2FieldCommon {{ name }} }} }}
              ... on ProjectV2ItemFieldIterationValue {{ title field {{ ... on ProjectV2FieldCommon {{ name }} }} }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""


def _extract_field_values(item: dict[str, Any]) -> dict[str, list[str]]:
    vals: dict[str, list[str]] = defaultdict(list)
    for v in (item.get("fieldValues") or {}).get("nodes") or []:
        name = (v.get("field") or {}).get("name")
        if not name:
            continue
        vals[_normalize(name)].extend(_field_value_strings(v))
    return dict(vals)


def _field_value_strings(v: dict[str, Any]) -> list[str]:
    t = v.get("__typename")
    if t == "ProjectV2ItemFieldTextValue":
        return [str(v.get("text") or "")]
    if t == "ProjectV2ItemFieldSingleSelectValue":
        return [str(v.get("name") or "")]
    if t == "ProjectV2ItemFieldNumberValue":
        n = v.get("number")
        return [] if n is None else [str(n)]
    if t == "ProjectV2ItemFieldIterationValue":
        return [str(v.get("title") or "")]
    return []


def _field_has_value(fields: dict[str, list[str]], field_name: str, expected: str) -> bool:
    return any(_normalize(v) == _normalize(expected) for v in fields.get(_normalize(field_name), []))


def _matching_release_branch(fields: dict[str, list[str]], branch_fields: list[str], branches: list[str]) -> str | None:
    for fn in branch_fields:
        vals = fields.get(_normalize(fn), [])
        for b in branches:
            if any(_normalize(v) == _normalize(b) or _normalize(v) == f"backport {_normalize(b)}" for v in vals):
                return b
    return None



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--target-token", required=True)
    parser.add_argument("--project-owner", required=True)
    parser.add_argument("--project-number", required=True, type=int)
    parser.add_argument("--project-owner-type", default="organization")
    parser.add_argument("--push-repo", default="")
    parser.add_argument("--status-field", default=_DEFAULT_STATUS_FIELD)
    parser.add_argument("--status-value", default=_DEFAULT_STATUS_VALUE)
    parser.add_argument("--branch-fields", default=",".join(_DEFAULT_BRANCH_FIELDS))
    parser.add_argument("--test-commands", default="")
    parser.add_argument("--only-branch", default="")
    parser.add_argument("--implicit-target-branch", default="",
                        help="When the project implies the branch (e.g., project 14 → 8.1), set this to override the field-based lookup")
    parser.add_argument("--max-candidates", type=int, default=0,
                        help="Cap the number of candidates per branch (0 = unlimited)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    results = run_backport_sweep(
        repo_full_name=args.repo,
        github_token=args.target_token,
        project_owner=args.project_owner,
        project_number=args.project_number,
        project_owner_type=args.project_owner_type,
        status_field=args.status_field,
        status_value=args.status_value,
        branch_fields=[f.strip() for f in args.branch_fields.split(",") if f.strip()] or None,
        push_repo=args.push_repo or None,
        only_branch=args.only_branch or None,
        test_commands=[c.strip() for c in args.test_commands.split("\n") if c.strip()] or None,
        discover_only=args.discover_only or args.dry_run,
        implicit_target_branch=args.implicit_target_branch or None,
        max_candidates=args.max_candidates,
    )

    print(json.dumps([{"branch": r.target_branch, "found": r.candidates_found, "applied": sum(1 for c in r.results if c.outcome == "applied"), "pr": r.pr_url} for r in results], indent=2))
    if args.discover_only or args.dry_run:
        return

    # Fail closed on:
    #  1. Any BranchSweepResult.error (outer exception)
    #  2. Any branch where candidates were found but all per-candidate
    #     outcomes were "error" (per-candidate errors got swallowed)
    failed_branches: list[str] = []
    for r in results:
        if r.error:
            failed_branches.append(f"{r.target_branch}: {r.error}")
            continue
        if r.candidates_found > 0 and r.results:
            errored = [c for c in r.results if c.outcome == "error"]
            if len(errored) == len(r.results):
                failed_branches.append(
                    f"{r.target_branch}: all {len(errored)} candidates errored"
                )
    if failed_branches:
        logger.error("Backport sweep failures: %s", "; ".join(failed_branches))
        sys.exit(1)


if __name__ == "__main__":
    main()
