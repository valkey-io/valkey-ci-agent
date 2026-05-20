"""Daily backport sweep across registered release branches."""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github import Auth, Github
from github.GithubException import GithubException

from scripts.backport.cherry_pick import is_non_merge_mainline_error
from scripts.backport.conflict_resolver import resolve_conflicts_with_claude
from scripts.backport.main import (
    _run_git,
)
from scripts.backport.models import BackportPRContext
from scripts.backport.pr_creator import (
    build_pull_search_head_ref,
    create_pull_from_push_repo,
    pull_matches_push_repo,
)
from scripts.backport.validation import (
    changed_paths_since_base,
    select_validation_commands,
)
from scripts.common.git_auth import GitAuth, github_https_url
from scripts.common.github_client import retry_github_call
from scripts.common.job_summary import emit_job_summary

if TYPE_CHECKING:
    from scripts.backport.registry import BranchEntry, RepoEntry  # noqa: F401

logger = logging.getLogger(__name__)

_DEFAULT_BRANCH_FIELDS = (
    "Backport Branch", "Target Branch", "Release Branch",
    "Branch", "Version", "Release", "Folder",
)
_DEFAULT_STATUS_FIELD = "Status"
_DEFAULT_STATUS_VALUE = "To be backported"
_BRANCH_PREFIX = "agent/backport/sweep"



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


# Detail string used when a candidate PR is already cherry-picked onto the
# backport sweep branch (detected by _list_already_applied scanning the
# branch's commit log for the (#NNNN) marker). The PR-body builder treats
# this case as "on the branch" and lists it under Applied, distinct from
# other "skipped-existing" detail strings (e.g. "already applied or empty
# cherry-pick") which mean the change is on the *release* branch and not
# committed to the sweep branch.
DETAIL_ALREADY_ON_SWEEP_BRANCH = "already on backport branch"


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
        body: str | None = None
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

        if body is None:
            # All retries exhausted without a successful response. Surface
            # the last captured exception instead of letting json.loads
            # raise an opaque UnboundLocalError.
            raise RuntimeError(
                "GraphQL request failed after retries"
            ) from last_exc

        data = json.loads(body)
        if data.get("errors"):
            msgs = "; ".join(str(e.get("message", e)) for e in data["errors"])
            raise RuntimeError(f"GraphQL errors: {msgs}")
        return data.get("data", {})



class ProjectBackportDiscovery:
    def __init__(self, gql: GitHubGraphQLClient, *, project_owner: str,
                 project_number: int, source_repo: str,
                 project_owner_type: str = "organization",
                 status_field: str = _DEFAULT_STATUS_FIELD,
                 status_value: str = _DEFAULT_STATUS_VALUE,
                 branch_fields: list[str] | None = None,
                 implicit_target_branch: str | None = None) -> None:
        self._gql = gql
        self._owner = project_owner
        self._number = project_number
        self._owner_type = project_owner_type
        self._source_repo = source_repo
        self._status_field = status_field
        self._status_value = status_value
        self._branch_fields = branch_fields or list(_DEFAULT_BRANCH_FIELDS)
        # If set, every candidate on this project goes to this branch.
        # Each project board maps to exactly one release branch.
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
        # Project boards aggregate PRs across the whole org. Drop anything
        # that didn't originate in the repo we're sweeping into — otherwise
        # we'd try to cherry-pick e.g. a valkey-io.github.io blog-post merge
        # commit into valkey-io/valkey, which fails at `git fetch <sha>`.
        item_repo = (content.get("repository") or {}).get("nameWithOwner")
        if item_repo and item_repo != self._source_repo:
            logger.debug(
                "Skipping project item PR #%s from %s (sweep target is %s)",
                content.get("number"), item_repo, self._source_repo,
            )
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




def run_backport_sweep(
    *,
    repo_entry: "RepoEntry",
    branch_entry: "BranchEntry",
    github_token: str,
    status_field: str = _DEFAULT_STATUS_FIELD,
    status_value: str = _DEFAULT_STATUS_VALUE,
    branch_fields: list[str] | None = None,
    test_commands_override: list[str] | None = None,
    discover_only: bool = False,
    max_candidates: int = 5,
) -> BranchSweepResult:
    """Run backport sweep for a single repo+branch from the registry.

    Returns a single BranchSweepResult.
    """
    repo_full_name = repo_entry.repo
    push_repo = repo_entry.effective_push_repo
    target_branch = branch_entry.branch
    project_number = branch_entry.project_number
    test_commands = test_commands_override if test_commands_override is not None else list(repo_entry.build_commands)
    validation_rules = [] if test_commands_override is not None else list(repo_entry.validation_rules)

    gh = Github(auth=Auth.Token(github_token))
    repo = retry_github_call(lambda: gh.get_repo(repo_full_name), retries=3, description=f"get {repo_full_name}")

    discovery = ProjectBackportDiscovery(
        GitHubGraphQLClient(github_token),
        project_owner=repo_entry.project_owner,
        project_number=project_number,
        source_repo=repo_full_name,
        project_owner_type=repo_entry.project_owner_type,
        status_field=status_field,
        status_value=status_value,
        branch_fields=branch_fields,
        implicit_target_branch=target_branch,
    )
    candidates_by_branch = discovery.discover([target_branch])
    candidates = candidates_by_branch.get(target_branch, [])

    if max_candidates > 0:
        logger.info(
            "Branch %s: %d candidate(s) found, will apply up to %d successful cherry-pick(s)",
            target_branch, len(candidates), max_candidates,
        )
    else:
        logger.info("Branch %s: %d candidate(s)", target_branch, len(candidates))

    if discover_only:
        for c in candidates:
            logger.info("  PR #%d: %s (%s)", c.source_pr_number, c.source_pr_title, c.merge_commit_sha or "no merge sha")
        result = BranchSweepResult(target_branch=target_branch, candidates_found=len(candidates))
        emit_job_summary(_build_summary([result]))
        return result

    if not candidates:
        result = BranchSweepResult(target_branch=target_branch)
        emit_job_summary(_build_summary([result]))
        return result

    result = _process_branch(
        gh=gh, repo=repo, repo_full_name=repo_full_name,
        github_token=github_token, target_branch=target_branch,
        candidates=candidates, push_repo=push_repo,
        test_commands=test_commands,
        max_applied=max_candidates,
        language=repo_entry.language,
        build_commands=list(repo_entry.build_commands) or None,
        validation_rules=validation_rules,
    )
    emit_job_summary(_build_summary([result]))
    return result


def _process_branch(
    *, gh: Any, repo: Any, repo_full_name: str, github_token: str,
    target_branch: str, candidates: list[ProjectBackportCandidate],
    push_repo: str, test_commands: list[str],
    max_applied: int = 0,
    language: str = "c",
    build_commands: list[str] | None = None,
    validation_rules: list[Any] | None = None,
) -> BranchSweepResult:
    result = BranchSweepResult(target_branch=target_branch, candidates_found=len(candidates))
    tmpdir = tempfile.mkdtemp(prefix=f"backport-{_safe_tmp_component(target_branch)}-")

    try:
        with GitAuth(github_token, prefix="backport-sweep-git-askpass-") as git_auth:
            git_env = git_auth.env()
            _clone_target_branch(repo_full_name, target_branch, tmpdir, git_env)
            _run_git(tmpdir, "config", "user.name", "valkeyrie-bot[bot]")
            _run_git(tmpdir, "config", "user.email", "3692572+valkeyrie-bot[bot]@users.noreply.github.com")

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
            # _find_existing_pr intentionally raises on transient GitHub
            # errors (network, 5xx) rather than returning None, so that a
            # flaky "list PRs" call can't be mistaken for "no open PR" and
            # trigger _delete_stale_backport_branch below. The propagated
            # exception is caught by the outer try/except on this branch.
            existing_pr = _find_existing_pr(gh, repo_full_name, push_repo, backport_branch)

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
                        detail=DETAIL_ALREADY_ON_SWEEP_BRANCH,
                    ))
                    continue

                cr = _apply_candidate(
                    tmpdir, candidate, repo_full_name, git_env,
                    language=language, build_commands=build_commands,
                    validation_rules=validation_rules,
                )
                result.results.append(cr)
                if cr.outcome == "applied":
                    applied_count += 1

            # Push if we applied anything and validation passes.
            applied = [r for r in result.results if r.outcome == "applied"]
            if applied:
                commands = select_validation_commands(
                    test_commands,
                    validation_rules or [],
                    changed_paths_since_base(tmpdir, f"origin/{target_branch}"),
                )
                ok, output = _run_test_commands(tmpdir, commands)
                if not ok:
                    for item in applied:
                        item.outcome = "skipped-test"
                        item.detail = output[:500]
                    logger.warning("Validation failed for %s; not pushing branch.", target_branch)
                    return result
                _push_backport_branch(
                    tmpdir,
                    backport_branch,
                    git_env,
                    force_with_lease=existing_pr is not None,
                )
                logger.info("Pushed %d commit(s) to %s/%s", len(applied), push_repo, backport_branch)

                # Upsert PR
                pr_url = _upsert_pr(
                    gh, repo_full_name, push_repo, target_branch,
                    backport_branch, result, existing_pr,
                    gql=GitHubGraphQLClient(github_token),
                )
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
        shutil.rmtree(tmpdir, ignore_errors=True)

    return result


def _clone_target_branch(
    repo_full_name: str,
    target_branch: str,
    dest_dir: str,
    git_env: dict[str, str],
) -> None:
    clone_url = github_https_url(repo_full_name)
    subprocess.run(
        ["git", "clone", "--branch", target_branch, clone_url, dest_dir],
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    )


def _push_backport_branch(
    repo_dir: str,
    branch: str,
    git_env: dict[str, str],
    *,
    force_with_lease: bool,
) -> None:
    # Defense in depth: only ever push to branches under the agent's reserved
    # namespace. This is checked at the lowest level so that no caller — and no
    # future change to the sweep flow — can accidentally push to a release
    # branch (e.g., 9.1, unstable). Combined with the always-create-fresh
    # branch logic in _process_branch, this guarantees release branches and
    # unstable are never written to by the agent.
    if not branch.startswith(f"{_BRANCH_PREFIX}/"):
        raise RuntimeError(
            f"Refusing to push to non-namespaced branch: {branch!r}. "
            f"Agent push targets must start with {_BRANCH_PREFIX}/."
        )
    args = ["push", "push_target", branch]
    if force_with_lease:
        args.insert(1, "--force-with-lease")
    _run_git(repo_dir, *args, env=git_env)


def _apply_candidate(
    repo_dir: str, candidate: ProjectBackportCandidate,
    repo_full_name: str, git_env: dict[str, str],
    language: str = "c",
    build_commands: list[str] | None = None,
    validation_rules: list[Any] | None = None,
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
        if result.returncode != 0 and is_non_merge_mainline_error(
            f"{result.stdout}\n{result.stderr}"
        ):
            logger.info(
                "%s is not a merge commit; retrying cherry-pick without -m",
                sha,
            )
            result = subprocess.run(
                ["git", "cherry-pick", sha],
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

    resolver_validation_commands = select_validation_commands(
        build_commands or [],
        validation_rules or [],
        conflicting_paths,
    )
    resolutions = resolve_conflicts_with_claude(
        repo_dir, conflicting_files, pr_context,
        language=language, build_commands=resolver_validation_commands or None,
    )
    unresolved = [r for r in resolutions if r.resolved_content is None]
    if unresolved:
        # Abort cherry-pick
        subprocess.run(["git", "cherry-pick", "--abort"], cwd=repo_dir, capture_output=True)
        # Surface why each file couldn't be resolved (truncated for readability).
        details = "; ".join(
            f"{r.path}: {(r.resolution_summary or 'unresolved')[:200]}"
            for r in unresolved
        )
        return CandidateResult(
            candidate.source_pr_number,
            candidate.source_pr_title,
            "skipped-conflict",
            f"unresolved — {details}",
        )

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
    from scripts.common.build_validator import run_build_commands
    return run_build_commands(repo_dir, test_commands)


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
        source_repo_obj = retry_github_call(
            lambda: gh.get_repo(source_repo),
            retries=2, description=f"get {source_repo}",
        )
        push_repo_obj = retry_github_call(
            lambda: gh.get_repo(push_repo),
            retries=2, description=f"get {push_repo}",
        )
        source_sha = retry_github_call(
            lambda: source_repo_obj.get_branch(target_branch).commit.sha,
            retries=2, description=f"get {source_repo}:{target_branch} head",
        )
    except Exception as exc:
        raise RuntimeError(
            f"Could not resolve branch heads for sync of {push_repo}:{target_branch} "
            f"against {source_repo}: {exc}"
        ) from exc

    try:
        push_sha = retry_github_call(
            lambda: push_repo_obj.get_branch(target_branch).commit.sha,
            retries=2, description=f"get {push_repo}:{target_branch} head",
        )
    except GithubException as exc:
        if exc.status != 404:
            raise RuntimeError(
                f"Could not resolve branch heads for sync of "
                f"{push_repo}:{target_branch} against {source_repo}: {exc}"
            ) from exc
        logger.info(
                "Creating missing fork branch %s:%s at %s",
            push_repo, target_branch, source_sha[:8],
        )
        try:
            retry_github_call(
                lambda: push_repo_obj.create_git_ref(
                    ref=f"refs/heads/{target_branch}",
                    sha=source_sha,
                ),
                retries=2,
                description=f"create {push_repo}:{target_branch}",
            )
        except Exception as create_exc:
            raise RuntimeError(
                f"Could not create missing fork branch "
                f"{push_repo}:{target_branch}: {create_exc}"
            ) from create_exc
        return
    except Exception as exc:
        raise RuntimeError(
            f"Could not resolve branch heads for sync of "
            f"{push_repo}:{target_branch} against {source_repo}: {exc}"
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


def _find_existing_pr(gh: Any, base_repo: str, push_repo: str, branch: str) -> Any | None:
    """Return the open backport PR for *branch* on *base_repo*, or None.

    Only returns None when GitHub confirmed no matching PR exists.
    Transient errors (network, auth, 5xx) propagate so the caller can
    distinguish "no PR" from "couldn't check" and avoid deleting an
    active backport branch on a transient failure.
    """
    repo = retry_github_call(lambda: gh.get_repo(base_repo), retries=2, description=f"get {base_repo}")
    head_ref = build_pull_search_head_ref(base_repo, push_repo, branch)
    pulls = retry_github_call(
        lambda: list(repo.get_pulls(state="open", head=head_ref)),
        retries=2, description="list PRs",
    )
    for pull in pulls:
        if pull_matches_push_repo(pull, push_repo):
            return pull
    return None


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
        retry_github_call(lambda: ref.delete(), retries=2, description=f"delete ref {branch}")
    except Exception as exc:
        # Branch not found is fine — nothing to prune. Any other error
        # we log but don't abort; the next cherry-pick push will surface
        # a real problem clearly.
        msg = str(exc).lower()
        if "not found" in msg or "404" in msg:
            return
        logger.warning("Could not prune stale backport branch %s: %s", branch, exc)


def _upsert_pr(gh: Any, base_repo: str, push_repo: str, target_branch: str, head_branch: str,
               result: BranchSweepResult, existing_pr: Any | None,
               gql: GitHubGraphQLClient | None = None) -> str:
    repo = retry_github_call(lambda: gh.get_repo(base_repo), retries=2, description=f"get {base_repo}")
    body = _build_pr_body(result)
    title = f"[backport] Backport sweep for {target_branch}"

    if existing_pr:
        retry_github_call(lambda: existing_pr.edit(title=title, body=body), retries=2, description="update PR")
        # Promote existing draft sweep PRs to "ready for review". Earlier
        # versions of this script created PRs as drafts; we now want them
        # open by default, and want any leftover drafts to be promoted on
        # the next sweep run so maintainers see them in the active queue.
        # The GitHub REST API has no endpoint for this transition, so use
        # the GraphQL markPullRequestReadyForReview mutation.
        if getattr(existing_pr, "draft", False) and gql is not None:
            node_id = getattr(existing_pr, "node_id", None)
            if node_id:
                _mark_pr_ready_for_review(gql, node_id)
                logger.info(
                    "Marked PR #%d on %s ready for review",
                    existing_pr.number, base_repo,
                )
        logger.info("Updated PR #%d on %s", existing_pr.number, base_repo)
        return existing_pr.html_url

    pr = retry_github_call(
        lambda: create_pull_from_push_repo(
            repo,
            base_repo=base_repo,
            push_repo=push_repo,
            title=title,
            body=body,
            head_branch=head_branch,
            base_branch=target_branch,
            draft=False,
        ),
        retries=2, description="create PR",
    )
    logger.info("Created PR #%d on %s", pr.number, base_repo)
    return pr.html_url


def _mark_pr_ready_for_review(gql: GitHubGraphQLClient, pr_node_id: str) -> None:
    """Convert a draft pull request to ready-for-review via GraphQL.

    The REST API does not expose this transition, so we use the
    markPullRequestReadyForReview mutation. Errors surface to the caller
    so a sweep run that can't promote a PR fails loudly rather than
    silently leaving the PR in draft.
    """
    mutation = """
    mutation($id: ID!) {
      markPullRequestReadyForReview(input: {pullRequestId: $id}) {
        pullRequest { isDraft }
      }
    }
    """
    gql.execute(mutation, {"id": pr_node_id})


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
        f"# Backport sweep for {result.target_branch}",
        "",
        "Automated cherry-picks from PRs marked \"To be backported\".",
        "",
    ]
    # The Applied table reflects the cumulative state of the backport
    # branch: PRs cherry-picked in this run AND PRs already on the branch
    # from prior sweeps. This way the PR description always matches the
    # commits in the PR, regardless of how many sweep runs contributed.
    #
    # `skipped-existing` is emitted in three different situations and
    # only the first means "the commit is on the backport branch":
    #   1. _list_already_applied found the source PR # in the branch's
    #      commit log -> detail == DETAIL_ALREADY_ON_SWEEP_BRANCH.
    #   2. git cherry-pick produced an empty commit -> detail starts
    #      with "already applied" (the change is already in the *release*
    #      branch, so nothing was committed onto the sweep branch).
    #   3. Conflict resolution collapsed to a no-op -> detail mentions
    #      "already satisfied on target branch".
    # Only #1 belongs in the Applied table; the others would mislead a
    # maintainer into thinking those commits ride on the backport branch.
    # `Needs attention` continues to surface only this run's failures.
    applied = [
        r for r in result.results
        if r.outcome == "applied"
        or (
            r.outcome == "skipped-existing"
            and r.detail == DETAIL_ALREADY_ON_SWEEP_BRANCH
        )
    ]
    failed = [
        r for r in result.results
        if r.outcome not in {"applied", "skipped-existing"}
    ]

    if applied:
        lines.extend(["## Applied", "", "| Source PR | Title | Detail |", "|---|---|---|"])
        for r in applied:
            lines.append(
                f"| #{r.source_pr_number} | {_esc(r.source_pr_title)} | {_esc(r.detail)} |",
            )
        lines.append("")

    if failed:
        lines.extend([
            "## Needs attention",
            "",
            "These candidates could not be applied automatically and need a maintainer to follow up.",
            "",
            f"<details><summary>{len(failed)} candidate(s)</summary>",
            "",
            "| Source PR | Title | Outcome | Reason |",
            "|---|---|---|---|",
        ])
        for r in failed:
            lines.append(
                f"| #{r.source_pr_number} | {_esc(r.source_pr_title)} | "
                f"{r.outcome} | {_esc(r.detail)} |",
            )
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


def _safe_tmp_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "branch"


def _esc(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")




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
              repository {{ nameWithOwner }}
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
    parser.add_argument("--registry", default="repos.yml",
                        help="Path to registry YAML (default: repos.yml)")
    parser.add_argument("--repo", required=True,
                        help="Repository full name (must exist in registry)")
    parser.add_argument("--branch", required=True,
                        help="Target branch (must exist in registry for this repo)")
    parser.add_argument("--target-token", required=True)
    parser.add_argument("--status-field", default=_DEFAULT_STATUS_FIELD)
    parser.add_argument("--status-value", default=_DEFAULT_STATUS_VALUE)
    parser.add_argument("--branch-fields", default=",".join(_DEFAULT_BRANCH_FIELDS))
    parser.add_argument("--test-commands", default="",
                        help="Override test commands (newline-separated). Empty = use registry.")
    parser.add_argument("--max-candidates", type=int, default=5,
                        help="Cap the number of applied cherry-picks per branch (0 = unlimited)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from scripts.backport.registry import load_registry
    registry = load_registry(args.registry)
    repo_entry, branch_entry = registry.get_branch(args.repo, args.branch)

    test_commands_override = None
    if args.test_commands:
        test_commands_override = [c.strip() for c in args.test_commands.split("\n") if c.strip()]

    result = run_backport_sweep(
        repo_entry=repo_entry,
        branch_entry=branch_entry,
        github_token=args.target_token,
        status_field=args.status_field,
        status_value=args.status_value,
        branch_fields=[f.strip() for f in args.branch_fields.split(",") if f.strip()] or None,
        test_commands_override=test_commands_override,
        discover_only=args.discover_only or args.dry_run,
        max_candidates=args.max_candidates,
    )

    print(json.dumps({"branch": result.target_branch, "found": result.candidates_found, "applied": sum(1 for c in result.results if c.outcome == "applied"), "pr": result.pr_url}, indent=2))
    if args.discover_only or args.dry_run:
        return

    if result.error:
        logger.error("Backport sweep failure: %s: %s", result.target_branch, result.error)
        sys.exit(1)
    if result.candidates_found > 0 and result.results:
        errored = [c for c in result.results if c.outcome == "error"]
        if len(errored) == len(result.results):
            logger.error("Backport sweep failure: %s: all %d candidates errored", result.target_branch, len(errored))
            sys.exit(1)


if __name__ == "__main__":
    main()
