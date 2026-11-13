"""Daily backport sweep across registered release branches."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github import Auth, Github
from github.GithubException import GithubException

from scripts.backport import ci_sweep
from scripts.backport.candidate_apply import apply_candidate
from scripts.backport.git_commands import head_sha
from scripts.backport.git_commands import run_git as _run_git
from scripts.backport.mark_done import prs_present_in_history
from scripts.backport.models import CandidateOutcome, ResolutionResult
from scripts.backport.source_plan import SourceChangeError, SourceChangePlan, prepare_source_change
from scripts.backport.sweep_git import (
    branch_has_changes,
    clone_target_branch,
    has_changes_since,
    list_already_applied,
    list_applied_prs_on_branch,
    push_backport_branch,
    safe_tmp_component,
)
from scripts.backport.sweep_graphql import GitHubGraphQLClient
from scripts.backport.sweep_models import (
    DETAIL_ALREADY_ON_SWEEP_BRANCH,
    DETAIL_ALREADY_ON_TARGET,
    DETAIL_EMPTY_ON_TARGET,
    DETAIL_RESOLVED_BY_AI,
    BranchSweepResult,
    CandidateResult,
    PreparedBranchSweep,
    ProjectBackportCandidate,
)
from scripts.backport.sweep_prs import find_existing_pr, upsert_pr
from scripts.backport.sweep_reporting import (
    build_summary,
    result_is_on_backport_branch,
    validation_failure_detail,
)
from scripts.backport.sweep_validation import (
    run_test_commands,
    validate_branch_with_optional_repair,
)
from scripts.ci_fix.agent import fix_still_needed
from scripts.common.git_auth import GitAuth, github_https_url
from scripts.common.job_summary import emit_job_summary
from scripts.common.logging_utils import (
    annotate,
    compact_log_value,
    configure_logging,
    log_group,
    log_highlight,
)

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


def _log_existing_sweep_pr(
    existing_pr: Any,
    target_branch: str,
    backport_branch: str,
) -> None:
    log_highlight(
        logger,
        "BACKPORT SWEEP PR: resuming PR #%d | %s | target=%s | branch=%s",
        existing_pr.number,
        compact_log_value(getattr(existing_pr, "title", "")),
        target_branch,
        backport_branch,
    )


class ProjectBackportDiscovery:
    def __init__(
        self,
        gql: GitHubGraphQLClient,
        *,
        project_owner: str,
        project_number: int,
        source_repo: str,
        project_owner_type: str = "organization",
        status_field: str = _DEFAULT_STATUS_FIELD,
        status_value: str = _DEFAULT_STATUS_VALUE,
        branch_fields: list[str] | None = None,
        implicit_target_branch: str | None = None,
    ) -> None:
        self._gql = gql
        self._owner = project_owner
        self._number = project_number
        self._owner_type = project_owner_type
        self._source_repo = source_repo
        self._status_field = status_field
        self._status_value = status_value
        self._branch_fields = branch_fields or list(_DEFAULT_BRANCH_FIELDS)
        self._implicit_target = implicit_target_branch

    def discover(
        self,
        release_branches: list[str],
    ) -> dict[str, list[ProjectBackportCandidate]]:
        by_branch: dict[str, list[ProjectBackportCandidate]] = {
            branch: [] for branch in release_branches
        }
        for item in self._iter_items():
            candidate = self._candidate_from_item(item, release_branches)
            if candidate:
                by_branch.setdefault(candidate.target_branch, []).append(candidate)
        return by_branch

    def _iter_items(self) -> list[dict[str, Any]]:
        owner_field = "user" if self._owner_type == "user" else "organization"
        query = _project_items_query(owner_field)
        cursor = None
        items: list[dict[str, Any]] = []
        while True:
            data = self._gql.execute(
                query,
                {"owner": self._owner, "number": self._number, "cursor": cursor},
            )
            project = (data.get(owner_field) or {}).get("projectV2")
            if not project:
                raise RuntimeError(f"Project {self._owner}/{self._number} not found")
            page = project.get("items") or {}
            items.extend(page.get("nodes") or [])
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return items
            cursor = page_info.get("endCursor")

    def _candidate_from_item(
        self,
        item: dict[str, Any],
        branches: list[str],
    ) -> ProjectBackportCandidate | None:
        content = item.get("content") or {}
        if content.get("__typename") != "PullRequest" or not content.get("merged"):
            return None

        item_repo = (content.get("repository") or {}).get("nameWithOwner")
        if item_repo and item_repo != self._source_repo:
            logger.debug(
                "Skipping project item PR #%s from %s (sweep target is %s)",
                content.get("number"),
                item_repo,
                self._source_repo,
            )
            return None

        fields = _extract_field_values(item)
        if not _field_has_value(fields, self._status_field, self._status_value):
            return None

        if self._implicit_target is not None:
            target_branch = self._implicit_target
        else:
            matched_branch = _matching_release_branch(
                fields,
                self._branch_fields,
                branches,
            )
            if not matched_branch:
                return None
            target_branch = matched_branch

        commits = [
            node.get("commit", {}).get("oid", "")
            for node in (content.get("commits", {}).get("nodes") or [])
        ]
        commits_page = content.get("commits") or {}
        merge_sha = (content.get("mergeCommit") or {}).get("oid")
        return ProjectBackportCandidate(
            source_pr_number=int(content["number"]),
            source_pr_title=str(content.get("title") or ""),
            source_pr_url=str(content.get("url") or ""),
            target_branch=target_branch,
            merge_commit_sha=merge_sha,
            commit_shas=[sha for sha in commits if sha],
            merged_at=str(content.get("mergedAt") or ""),
            source_commits_complete=not bool(
                (commits_page.get("pageInfo") or {}).get("hasNextPage")
            ),
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
    """Run both phases with one token for compatibility with direct callers."""
    result, prepared = prepare_backport_sweep(
        repo_entry=repo_entry,
        branch_entry=branch_entry,
        github_token=github_token,
        status_field=status_field,
        status_value=status_value,
        branch_fields=branch_fields,
        test_commands_override=test_commands_override,
        discover_only=discover_only,
        max_candidates=max_candidates,
    )
    if prepared is not None:
        result = publish_prepared_sweep(prepared, github_token)
    emit_job_summary(build_summary([result]))
    return result


def prepare_backport_sweep(
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
    work_root: str | None = None,
) -> tuple[BranchSweepResult, PreparedBranchSweep | None]:
    """Prepare and validate a local branch without publishing it."""
    repo_full_name = repo_entry.repo
    target_branch = branch_entry.branch
    test_commands = (
        test_commands_override
        if test_commands_override is not None
        else list(repo_entry.build_commands)
    )
    validation_setup_commands = (
        [] if test_commands_override is not None
        else list(repo_entry.validation_setup_commands)
    )
    validation_rules = (
        [] if test_commands_override is not None
        else list(repo_entry.validation_rules)
    )

    logger.info(
        "Backport sweep of %s %s from project %s/%d (pushing to %s, up to %s PR(s), "
        "%d validation command(s), validation repair %s)",
        repo_full_name, target_branch, repo_entry.project_owner, branch_entry.project_number,
        repo_entry.effective_push_repo, max_candidates if max_candidates > 0 else "unlimited",
        len(test_commands), "on" if repo_entry.repair_validation_failures else "off",
    )
    discovery = ProjectBackportDiscovery(
        GitHubGraphQLClient(github_token),
        project_owner=repo_entry.project_owner,
        project_number=branch_entry.project_number,
        source_repo=repo_full_name,
        project_owner_type=repo_entry.project_owner_type,
        status_field=status_field,
        status_value=status_value,
        branch_fields=branch_fields,
        implicit_target_branch=target_branch,
    )
    candidates = discovery.discover([target_branch]).get(target_branch, [])
    candidates.sort(key=lambda candidate: candidate.merged_at or "")

    if max_candidates > 0:
        logger.info(
            "Branch %s: %d candidate(s) found, will apply up to %d successful cherry-pick(s)",
            target_branch,
            len(candidates),
            max_candidates,
        )
    else:
        logger.info("Branch %s: %d candidate(s)", target_branch, len(candidates))

    if discover_only:
        for candidate in candidates:
            logger.info(
                "  PR #%d: %s (%s)",
                candidate.source_pr_number,
                candidate.source_pr_title,
                candidate.merge_commit_sha or "no merge sha",
            )
        return BranchSweepResult(
            target_branch=target_branch,
            candidates_found=len(candidates),
        ), None

    if not candidates:
        logger.info("Branch %s: no PR in %r status on project %d; nothing to sweep",
                    target_branch, status_value, branch_entry.project_number)
        return BranchSweepResult(target_branch=target_branch), None

    return _prepare_branch(
        gh=Github(auth=Auth.Token(github_token)),
        repo_full_name=repo_full_name,
        github_token=github_token,
        target_branch=target_branch,
        candidates=candidates,
        push_repo=repo_entry.effective_push_repo,
        test_commands=test_commands,
        validation_setup_commands=validation_setup_commands,
        max_applied=max_candidates,
        language=repo_entry.language,
        build_commands=list(repo_entry.build_commands) or None,
        validation_rules=validation_rules,
        validation_profile=repo_entry.validation_profile,
        generated_file_rules=list(repo_entry.generated_file_rules),
        test_path_patterns=repo_entry.test_path_patterns,
        repair_validation_failures=repo_entry.repair_validation_failures,
        max_conflicting_files=repo_entry.max_conflicting_files,
        backport_label=repo_entry.backport_label,
        llm_conflict_label=repo_entry.llm_conflict_label,
        work_root=work_root,
    )


def _process_branch(
    *,
    gh: Any,
    repo_full_name: str,
    github_token: str,
    target_branch: str,
    candidates: list[ProjectBackportCandidate],
    push_repo: str,
    test_commands: list[str],
    validation_setup_commands: list[str] | None = None,
    max_applied: int = 0,
    language: str = "c",
    build_commands: list[str] | None = None,
    validation_rules: list[Any] | None = None,
    validation_profile: str = "",
    generated_file_rules: list[Any] | None = None,
    test_path_patterns: tuple[str, ...] | list[str] | None = None,
    repair_validation_failures: bool = False,
    max_conflicting_files: int = 100,
    backport_label: str = "backport",
    llm_conflict_label: str = "ai-resolved-conflicts",
) -> BranchSweepResult:
    """Compatibility wrapper for existing direct callers and tests."""
    result, prepared = _prepare_branch(
        gh=gh,
        repo_full_name=repo_full_name,
        github_token=github_token,
        target_branch=target_branch,
        candidates=candidates,
        push_repo=push_repo,
        test_commands=test_commands,
        validation_setup_commands=validation_setup_commands,
        max_applied=max_applied,
        language=language,
        build_commands=build_commands,
        validation_rules=validation_rules,
        validation_profile=validation_profile,
        generated_file_rules=generated_file_rules,
        test_path_patterns=test_path_patterns,
        repair_validation_failures=repair_validation_failures,
        max_conflicting_files=max_conflicting_files,
        backport_label=backport_label,
        llm_conflict_label=llm_conflict_label,
    )
    if prepared is not None:
        return publish_prepared_sweep(prepared, github_token, gh=gh)
    return result


def _prepare_branch(
    *,
    gh: Any,
    repo_full_name: str,
    github_token: str,
    target_branch: str,
    candidates: list[ProjectBackportCandidate],
    push_repo: str,
    test_commands: list[str],
    validation_setup_commands: list[str] | None = None,
    max_applied: int = 0,
    language: str = "c",
    build_commands: list[str] | None = None,
    validation_rules: list[Any] | None = None,
    validation_profile: str = "",
    generated_file_rules: list[Any] | None = None,
    test_path_patterns: tuple[str, ...] | list[str] | None = None,
    repair_validation_failures: bool = False,
    max_conflicting_files: int = 100,
    backport_label: str = "backport",
    llm_conflict_label: str = "ai-resolved-conflicts",
    work_root: str | None = None,
) -> tuple[BranchSweepResult, PreparedBranchSweep | None]:
    """Build one sweep branch locally and return it for a later publish step.

    Nothing is pushed here. The clone is validated with the preparation token
    already discarded -- everything from the setup commands onward runs against
    repository code with no credential in the environment -- so publishing needs
    a second, separately scoped token. A candidate that fails validation is reset
    out of the branch rather than carried, keeping the published branch green.
    """
    result = BranchSweepResult(
        target_branch=target_branch,
        candidates_found=len(candidates),
    )
    tmpdir = tempfile.mkdtemp(
        prefix=f"backport-{safe_tmp_component(target_branch)}-",
        dir=work_root,
    )
    preserve_repo = False

    try:
        backport_branch = f"{_BRANCH_PREFIX}/{target_branch}"
        with GitAuth(
            github_token,
            prefix="backport-sweep-prepare-git-askpass-",
        ) as git_auth:
            git_env = git_auth.env()
            clone_target_branch(repo_full_name, target_branch, tmpdir, git_env)
            target_head = head_sha(tmpdir)
            existing_pr = find_existing_pr(
                gh,
                repo_full_name,
                push_repo,
                backport_branch,
            )
            expected_pr_number = _expected_pr_number(existing_pr, target_branch)
            expected_remote_head = _remote_branch_sha(
                gh,
                push_repo,
                backport_branch,
            )

            push_url = github_https_url(push_repo)
            _run_git(tmpdir, "remote", "add", "push_target", push_url, env=git_env)
            ci_pull = _fetch_ci_sweep(gh, repo_full_name, push_repo, target_branch, tmpdir, git_env)
            if existing_pr is not None:
                if expected_remote_head is None:
                    raise RuntimeError(
                        f"Open backport PR #{existing_pr.number} has no remote branch "
                        f"{backport_branch}"
                    )
                _log_existing_sweep_pr(
                    existing_pr,
                    target_branch,
                    backport_branch,
                )
                logger.info("Fetching existing backport sweep branch...")
                _run_git(tmpdir, "fetch", "push_target", backport_branch, env=git_env)
                _run_git(tmpdir, "checkout", f"push_target/{backport_branch}")
                _run_git(tmpdir, "checkout", "-B", backport_branch)
                rebase_result = subprocess.run(
                    ["git", "rebase", f"origin/{target_branch}"],
                    cwd=tmpdir,
                    capture_output=True,
                    text=True,
                )
                if rebase_result.returncode != 0:
                    _run_git(tmpdir, "rebase", "--abort")
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
                _run_git(tmpdir, "checkout", "-b", backport_branch)

            already_applied = list_already_applied(
                tmpdir,
                target_branch,
                backport_branch,
            )
            # The board lags a merged sweep until the next reconcile, so the
            # release branch history, not the board, decides what is missing.
            on_target = prs_present_in_history(
                tmpdir,
                f"origin/{target_branch}",
                {
                    candidate.source_pr_number: candidate.merged_at
                    for candidate in candidates
                    if candidate.merged_at
                    and str(candidate.source_pr_number) not in already_applied
                },
            )
            source_plans, plan_errors = _prepare_source_plans(
                tmpdir,
                candidates,
                already_applied | {str(number) for number in on_target},
                git_env,
            )

        # Everything after this point is local; the preparation token and
        # askpass helper no longer exist while repository code is validated.
        setup_ok, setup_output = run_test_commands(
            tmpdir,
            validation_setup_commands or [],
        )
        if not setup_ok:
            logger.warning("Validation setup failed for %s", target_branch)
            with log_group(f"Validation setup output for {target_branch} (last 4000 chars)"):
                logger.warning("%s", setup_output[-4000:])
            raise RuntimeError(
                "validation setup failed: "
                + (setup_output[:500] or "setup command failed")
            )

        logger.info("Already on %s: %s", backport_branch,
                    ", ".join(f"#{n}" for n in sorted(already_applied, key=int)) or "nothing yet")
        applied_count = 0
        for index, candidate in enumerate(candidates):
            if max_applied > 0 and applied_count >= max_applied:
                logger.info(
                    "Branch %s: reached cap of %d applied backport(s); deferring remaining %d candidate(s) to next sweep",
                    target_branch,
                    max_applied,
                    len(candidates) - index,
                )
                break

            if str(candidate.source_pr_number) in already_applied:
                logger.info(
                    "BACKPORT SKIPPED: PR #%d | %s | already present on %s",
                    candidate.source_pr_number,
                    compact_log_value(candidate.source_pr_title),
                    backport_branch,
                )
                result.results.append(
                    CandidateResult(
                        source_pr_number=candidate.source_pr_number,
                        source_pr_title=candidate.source_pr_title,
                        outcome="skipped-existing",
                        detail=DETAIL_ALREADY_ON_SWEEP_BRANCH,
                    )
                )
                continue

            if candidate.source_pr_number in on_target:
                logger.info(
                    "BACKPORT SKIPPED: PR #%d | %s | already present on %s",
                    candidate.source_pr_number,
                    compact_log_value(candidate.source_pr_title),
                    target_branch,
                )
                result.results.append(
                    CandidateResult(
                        source_pr_number=candidate.source_pr_number,
                        source_pr_title=candidate.source_pr_title,
                        outcome="skipped-existing",
                        detail=DETAIL_ALREADY_ON_TARGET,
                    )
                )
                continue

            plan_error = plan_errors.get(candidate.source_pr_number)
            if plan_error is not None:
                logger.warning(
                    "BACKPORT ERROR: PR #%d | %s | %s",
                    candidate.source_pr_number,
                    compact_log_value(candidate.source_pr_title),
                    compact_log_value(plan_error.detail, limit=500),
                )
                result.results.append(plan_error)
                continue

            pre_candidate_head = head_sha(tmpdir)
            # The cherry-pick and any AI conflict resolution are folded; the
            # one-line BACKPORT result below stays visible.
            with log_group(f"PR #{candidate.source_pr_number} "
                           f"({compact_log_value(candidate.source_pr_title, limit=120)}): "
                           f"apply to {target_branch}"):
                candidate_result = apply_candidate(
                    tmpdir,
                    candidate,
                    repo_full_name,
                    {},
                    language=language,
                    build_commands=build_commands,
                    validation_rules=validation_rules,
                    test_path_patterns=test_path_patterns,
                    max_conflicting_files=max_conflicting_files,
                    source_plan=source_plans[candidate.source_pr_number],
                )
            result.results.append(candidate_result)

            if not candidate_result.worktree_restored:
                raise RuntimeError(
                    f"candidate #{candidate.source_pr_number} could not "
                    "restore the worktree; aborting this branch"
                )
            if candidate_result.outcome != "applied":
                logger.log(
                    logging.ERROR if candidate_result.outcome == "error" else logging.WARNING,
                    "BACKPORT NOT APPLIED: PR #%d | %s | %s%s",
                    candidate.source_pr_number,
                    compact_log_value(candidate.source_pr_title),
                    _OUTCOME_WORDS.get(candidate_result.outcome, candidate_result.outcome),
                    f" | {compact_log_value(candidate_result.detail, limit=500)}"
                    if candidate_result.detail else "",
                )
                continue

            with log_group(f"PR #{candidate.source_pr_number}: validate {target_branch}"):
                validation_outcome = validate_branch_with_optional_repair(
                    tmpdir,
                    target_branch,
                    test_commands,
                    validation_rules or [],
                    repair=repair_validation_failures,
                    validation_profile=validation_profile,
                    generated_file_rules=generated_file_rules,
                    base_ref=pre_candidate_head,
                    candidate=candidate,
                    language=language,
                    test_path_patterns=test_path_patterns,
                    run_git=_run_git,
                )
            ok, output = validation_outcome
            if not ok:
                candidate_result.outcome = "skipped-validation-failed"
                candidate_result.detail = validation_failure_detail(output)
                _run_git(tmpdir, "reset", "--hard", pre_candidate_head)
                logger.warning(
                    "BACKPORT REJECTED: PR #%d | %s | target=%s | "
                    "validation failed; removed candidate and continuing | %s",
                    candidate.source_pr_number,
                    compact_log_value(candidate.source_pr_title),
                    target_branch,
                    compact_log_value(candidate_result.detail, limit=500),
                )
                continue

            # A repair can undo the whole cherry-pick (e.g. re-stripping the
            # whitespace a resolution reintroduced). Keeping that pair would
            # publish two commits with no net change.
            if not has_changes_since(tmpdir, pre_candidate_head):
                _run_git(tmpdir, "reset", "--hard", pre_candidate_head)
                candidate_result.outcome = "skipped-existing"
                candidate_result.detail = DETAIL_EMPTY_ON_TARGET
                candidate_result.skip_reason = (
                    "Validation repair reverted the entire cherry-pick, so it "
                    "adds no net change to this branch."
                )
                candidate_result.resolved_commit_sha = None
                logger.warning(
                    "BACKPORT NOT APPLIED: PR #%d | %s | no net change after "
                    "validation; removed candidate and continuing",
                    candidate.source_pr_number,
                    compact_log_value(candidate.source_pr_title),
                )
                continue

            if validation_outcome.amended_commit_sha:
                candidate_result.resolved_commit_sha = validation_outcome.amended_commit_sha

            repair_resolutions = list(validation_outcome.resolutions)
            if repair_resolutions:
                candidate_result.resolutions.extend(repair_resolutions)
                candidate_result.resolved_by_ai = True
                candidate_result.resolved_commit_sha = head_sha(tmpdir)
                if validation_outcome.ai_summary:
                    candidate_result.ai_summary = validation_outcome.ai_summary
                if DETAIL_RESOLVED_BY_AI not in candidate_result.detail:
                    candidate_result.detail = "; ".join(
                        part
                        for part in (
                            candidate_result.detail,
                            DETAIL_RESOLVED_BY_AI,
                        )
                        if part
                    )
            logger.info(
                "BACKPORT ACCEPTED: PR #%d | %s | target=%s",
                candidate.source_pr_number,
                compact_log_value(candidate.source_pr_title),
                target_branch,
            )
            applied_count += 1

        committed = [
            item for item in result.results
            if result_is_on_backport_branch(item)
        ]
        if committed and branch_has_changes(tmpdir, target_branch):
            prepared_head = head_sha(tmpdir)
            prepared = PreparedBranchSweep(
                repo_full_name=repo_full_name,
                push_repo=push_repo,
                target_branch=target_branch,
                backport_branch=backport_branch,
                repo_dir=tmpdir,
                target_head=target_head,
                prepared_head=prepared_head,
                expected_remote_head=expected_remote_head,
                expected_pr_number=expected_pr_number,
                result=result,
                backport_label=backport_label,
                llm_conflict_label=llm_conflict_label,
                ci_sweep=_plan_ci_sweep(tmpdir, ci_pull, prepared_head, expected_pr_number),
            )
            preserve_repo = True
            return result, prepared
        logger.info(
            "Not publishing %s: %s", backport_branch,
            "no candidate was committed" if not committed
            else f"the branch has no changes against {target_branch}",
        )

    except Exception as exc:
        logger.exception("Error preparing branch %s", target_branch)
        _record_sweep_error(result, exc)
    finally:
        if not preserve_repo:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return result, None


def _prepare_source_plans(
    repo_dir: str,
    candidates: list[ProjectBackportCandidate],
    already_applied: set[str],
    git_env: dict[str, str],
) -> tuple[dict[int, SourceChangePlan], dict[int, CandidateResult]]:
    plans: dict[int, SourceChangePlan] = {}
    errors: dict[int, CandidateResult] = {}
    for candidate in candidates:
        if str(candidate.source_pr_number) in already_applied:
            continue
        try:
            plans[candidate.source_pr_number] = prepare_source_change(
                repo_dir,
                candidate.source_pr_number,
                candidate.merge_commit_sha,
                candidate.commit_shas,
                source_commits_complete=candidate.source_commits_complete,
                git_env=git_env,
            )
        except (SourceChangeError, subprocess.CalledProcessError) as exc:
            errors[candidate.source_pr_number] = CandidateResult(
                candidate.source_pr_number,
                candidate.source_pr_title,
                "error",
                str(exc),
            )
    return plans, errors


def publish_prepared_sweep(
    prepared: PreparedBranchSweep,
    github_token: str,
    *,
    gh: Any | None = None,
) -> BranchSweepResult:
    """Publish exactly the validated commit with a fresh token."""
    result = prepared.result
    gh = gh or Github(auth=Auth.Token(github_token))
    try:
        if head_sha(prepared.repo_dir) != prepared.prepared_head:
            raise RuntimeError("Prepared backport branch changed before publication")

        current_target = _remote_branch_sha(
            gh,
            prepared.repo_full_name,
            prepared.target_branch,
        )
        if current_target != prepared.target_head:
            raise RuntimeError(
                f"Target branch {prepared.target_branch} changed during preparation"
            )

        current_remote = _remote_branch_sha(
            gh,
            prepared.push_repo,
            prepared.backport_branch,
        )
        if current_remote != prepared.expected_remote_head:
            raise RuntimeError("Backport branch changed during preparation")

        existing_pr = find_existing_pr(
            gh,
            prepared.repo_full_name,
            prepared.push_repo,
            prepared.backport_branch,
        )
        if _expected_pr_number(existing_pr, prepared.target_branch) != prepared.expected_pr_number:
            raise RuntimeError("Backport PR changed during preparation")

        with GitAuth(
            github_token,
            prefix="backport-sweep-publish-git-askpass-",
        ) as git_auth:
            push_backport_branch(
                prepared.repo_dir,
                prepared.backport_branch,
                git_auth.env(),
                push_repo=prepared.push_repo,
                prepared_head=prepared.prepared_head,
                expected_remote_head=prepared.expected_remote_head,
            )

        result.pr_url = upsert_pr(
            gh,
            prepared.repo_full_name,
            prepared.push_repo,
            prepared.target_branch,
            prepared.backport_branch,
            result,
            existing_pr,
            gql=GitHubGraphQLClient(github_token),
            branch_applied=list_applied_prs_on_branch(
                prepared.repo_dir,
                prepared.target_branch,
                prepared.backport_branch,
            ),
            backport_label=prepared.backport_label,
            llm_conflict_label=prepared.llm_conflict_label,
        )
        _apply_ci_sweep(gh, prepared, github_token)
    except Exception as exc:
        logger.exception("Error publishing branch %s", prepared.target_branch)
        for item in result.results:
            if item.outcome == "applied":
                item.outcome = "error"
                item.detail = f"publication failed: {exc}"
        _record_sweep_error(result, exc)
    finally:
        shutil.rmtree(prepared.repo_dir, ignore_errors=True)
    return result


def _fetch_ci_sweep(
    gh: Any, repo_full_name: str, push_repo: str, target_branch: str, repo_dir: str, git_env: dict[str, str],
) -> Any | None:
    """The open CI-sweep PR into this sweep, its branch fetched to ``ci_sweep.PLANNED_REF``; never fails the sweep."""
    branch = ci_sweep.ci_sweep_branch(target_branch)
    try:
        pull = find_existing_pr(gh, repo_full_name, push_repo, branch)
        if pull is not None:
            _run_git(repo_dir, "fetch", "push_target", f"+refs/heads/{branch}:{ci_sweep.PLANNED_REF}", env=git_env)
        return pull
    except Exception as exc:  # noqa: BLE001 - the CI-sweep PR is rebuilt on the next sweep
        logger.warning("Could not read the CI-sweep PR of %s: %s", target_branch, exc)
        return None


def _plan_ci_sweep(repo_dir: str, pull: Any | None, sweep_head: str, sweep_pr: int | None) -> ci_sweep.Plan | None:
    """How the CI-sweep PR goes onto the new sweep head, with its fixes reviewed; never fails the sweep."""
    if pull is None:
        return None

    def review(fix: ci_sweep.Fix, backports: str) -> tuple[bool, str]:
        return fix_still_needed(repo_dir, fix.sha, fix.job, backports)

    try:
        return ci_sweep.plan(
            repo_dir, pull=pull, ci_sweep=ci_sweep.PLANNED_REF, sweep_head=sweep_head, sweep_pr=sweep_pr,
            review=review,
        )
    except Exception as exc:  # noqa: BLE001 - the CI-sweep PR is rebuilt on the next sweep
        logger.warning("Could not plan the CI-sweep PR #%s: %s", getattr(pull, "number", "?"), exc)
        return None


def _apply_ci_sweep(gh: Any, prepared: PreparedBranchSweep, github_token: str) -> None:
    """Rebuild, push, describe or close the CI-sweep PR as planned; never fails the sweep.

    The sweep PR's description, which the sweep has just rewritten, names the
    CI-sweep PR again whenever one is open, planned or not.
    """
    branch = ci_sweep.ci_sweep_branch(prepared.target_branch)
    try:
        sweep_pr = find_existing_pr(gh, prepared.repo_full_name, prepared.push_repo, prepared.backport_branch)
        if prepared.ci_sweep is not None:
            plan = prepared.ci_sweep
            with GitAuth(github_token, prefix="ci-sweep-publish-git-askpass-") as git_auth:
                def push(worktree: str, head: str, expected: str) -> None:
                    push_backport_branch(
                        worktree, branch, git_auth.env(), push_repo=prepared.push_repo, prepared_head=head,
                        expected_remote_head=expected, branch_prefix=ci_sweep.BRANCH_PREFIX,
                    )

                ci_sweep.apply(
                    gh, prepared.repo_dir, repo_full_name=prepared.repo_full_name, release=prepared.target_branch,
                    plan=plan, sweep_head=prepared.prepared_head,
                    sweep_pr=int(sweep_pr.number) if sweep_pr is not None else None, push=push,
                )
        ci_pull = find_existing_pr(gh, prepared.repo_full_name, prepared.push_repo, branch)
        if ci_pull is not None and sweep_pr is not None:
            text = ci_sweep.with_sweep_line(str(getattr(sweep_pr, "body", "") or ""), int(ci_pull.number))
            if text != sweep_pr.body:
                sweep_pr.edit(body=text)
    except Exception as exc:  # noqa: BLE001 - the CI-sweep PR is rebuilt on the next sweep
        logger.warning("Could not update the CI-sweep PR of %s: %s", prepared.target_branch, exc)


def _remote_branch_sha(gh: Any, repo_name: str, branch: str) -> str | None:
    try:
        return gh.get_repo(repo_name).get_branch(branch).commit.sha
    except GithubException as exc:
        if exc.status == 404:
            return None
        raise


def _expected_pr_number(pr: Any | None, target_branch: str) -> int | None:
    if pr is None:
        return None
    base_ref = getattr(getattr(pr, "base", None), "ref", None)
    if isinstance(base_ref, str) and base_ref != target_branch:
        raise RuntimeError(
            f"Open backport PR #{pr.number} targets {base_ref}, not {target_branch}"
        )
    return int(pr.number)


def _record_sweep_error(result: BranchSweepResult, exc: Exception) -> None:
    result.error = str(exc)
    result.results.append(
        CandidateResult(
            source_pr_number=0,
            source_pr_title=f"Branch {result.target_branch}",
            outcome="error",
            detail=str(exc),
        )
    )


def write_prepared_sweep(path: str, prepared: PreparedBranchSweep) -> None:
    """Atomically persist the small, token-free publication handoff."""
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "identity": {
            "repo": prepared.repo_full_name,
            "push_repo": prepared.push_repo,
            "target_branch": prepared.target_branch,
            "backport_branch": prepared.backport_branch,
        },
        "worktree": Path(prepared.repo_dir).name,
        "target_head": prepared.target_head,
        "prepared_head": prepared.prepared_head,
        "expected_remote_head": prepared.expected_remote_head,
        "expected_pr_number": prepared.expected_pr_number,
        "ci_sweep": dataclasses.asdict(prepared.ci_sweep) if prepared.ci_sweep is not None else None,
        "candidates_found": prepared.result.candidates_found,
        "results": [_result_to_dict(item) for item in prepared.result.results],
    }
    fd, temporary = tempfile.mkstemp(
        dir=state_path.parent,
        prefix=f".{state_path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_path, state_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def load_prepared_sweep(
    path: str,
    *,
    repo_full_name: str,
    push_repo: str,
    target_branch: str,
    backport_branch: str,
    backport_label: str,
    llm_conflict_label: str,
) -> PreparedBranchSweep:
    state_path = Path(path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    identity = {
        "repo": repo_full_name,
        "push_repo": push_repo,
        "target_branch": target_branch,
        "backport_branch": backport_branch,
    }
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("invalid prepared sweep state")
    if payload.get("identity") != identity:
        raise ValueError("prepared sweep identity does not match registry")

    worktree = _required_string(payload, "worktree")
    root = state_path.parent.resolve()
    repo_dir = root / worktree
    if (
        Path(worktree).name != worktree
        or repo_dir.is_symlink()
        or not repo_dir.is_dir()
        or repo_dir.resolve().parent != root
    ):
        raise ValueError("invalid prepared sweep worktree")

    result = BranchSweepResult(
        target_branch=target_branch,
        candidates_found=payload["candidates_found"],
        results=[_result_from_dict(item) for item in payload["results"]],
    )
    return PreparedBranchSweep(
        repo_full_name=repo_full_name,
        push_repo=push_repo,
        target_branch=target_branch,
        backport_branch=backport_branch,
        repo_dir=str(repo_dir),
        target_head=_required_string(payload, "target_head"),
        prepared_head=_required_string(payload, "prepared_head"),
        expected_remote_head=payload["expected_remote_head"],
        expected_pr_number=payload["expected_pr_number"],
        result=result,
        backport_label=backport_label,
        llm_conflict_label=llm_conflict_label,
        ci_sweep=_plan_from_dict(payload.get("ci_sweep")),
    )


def _plan_from_dict(data: Any) -> ci_sweep.Plan | None:
    if not isinstance(data, dict):
        return None
    return ci_sweep.Plan(
        pr_number=int(data["pr_number"]),
        head=str(data["head"]),
        keep=tuple(data.get("keep") or ()),
        dropped=tuple(
            (str(sha), str(subject), str(job), str(why)) for sha, subject, job, why in data.get("dropped") or ()
        ),
        close=str(data.get("close") or ""),
        foreign=bool(data.get("foreign")),
    )


_RESULT_FIELDS = (
    "source_pr_number", "source_pr_title", "outcome", "detail",
    "resolved_by_ai", "skip_reason", "resolved_commit_sha",
)


def _result_to_dict(result: CandidateResult) -> dict[str, Any]:
    payload = {name: getattr(result, name) for name in _RESULT_FIELDS}
    payload["resolutions"] = [
        {"path": item.path, "llm_summary": item.llm_summary}
        for item in result.resolutions
        if item.resolved_content is not None
        and (item.reviewer_diff or item.resolution_diff)
    ]
    return payload


def _result_from_dict(payload: Any) -> CandidateResult:
    if not isinstance(payload, dict):
        raise ValueError("invalid prepared candidate result")
    values = {name: payload[name] for name in _RESULT_FIELDS}
    values["outcome"] = cast(CandidateOutcome, values["outcome"])
    values["resolutions"] = [
        ResolutionResult(
            path=item["path"],
            resolved_content="",
            resolution_summary="",
            resolution_diff="available",
            llm_summary=item.get("llm_summary"),
        )
        for item in payload["resolutions"]
    ]
    return CandidateResult(**cast(Any, values))

def _required_string(payload: dict[str, Any], name: str) -> str:
    value = payload[name]
    if not isinstance(value, str) or not value:
        raise ValueError("invalid prepared sweep state")
    return value

def _normalize(value: object) -> str:
    return str(value or "").strip().lower()


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
              number title url merged mergedAt
              repository {{ nameWithOwner }}
              mergeCommit {{ oid }}
              commits(first: 100) {{
                pageInfo {{ hasNextPage endCursor }}
                nodes {{ commit {{ oid }} }}
              }}
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
    values: dict[str, list[str]] = defaultdict(list)
    for field_value in (item.get("fieldValues") or {}).get("nodes") or []:
        name = (field_value.get("field") or {}).get("name")
        if not name:
            continue
        values[_normalize(name)].extend(_field_value_strings(field_value))
    return dict(values)


def _field_value_strings(field_value: dict[str, Any]) -> list[str]:
    type_name = field_value.get("__typename")
    if type_name == "ProjectV2ItemFieldTextValue":
        return [str(field_value.get("text") or "")]
    if type_name == "ProjectV2ItemFieldSingleSelectValue":
        return [str(field_value.get("name") or "")]
    if type_name == "ProjectV2ItemFieldNumberValue":
        number = field_value.get("number")
        return [] if number is None else [str(number)]
    if type_name == "ProjectV2ItemFieldIterationValue":
        return [str(field_value.get("title") or "")]
    return []


def _field_has_value(
    fields: dict[str, list[str]],
    field_name: str,
    expected: str,
) -> bool:
    return any(
        _normalize(value) == _normalize(expected)
        for value in fields.get(_normalize(field_name), [])
    )


def _matching_release_branch(
    fields: dict[str, list[str]],
    branch_fields: list[str],
    branches: list[str],
) -> str | None:
    for field_name in branch_fields:
        values = fields.get(_normalize(field_name), [])
        for branch in branches:
            normalized_branch = _normalize(branch)
            if any(
                _normalize(value) == normalized_branch
                or _normalize(value) == f"backport {normalized_branch}"
                for value in values
            ):
                return branch
    return None


def _print_result(result: BranchSweepResult) -> None:
    print(json.dumps({
        "branch": result.target_branch,
        "found": result.candidates_found,
        "applied": result.applied_count,
        "pr": result.pr_url,
    }, indent=2))


_OUTCOME_WORDS = {
    "applied": "applied",
    "skipped-existing": "already on the branch",
    "skipped-empty": "empty on the target",
    "skipped-conflict": "unresolved conflicts",
    "skipped-validation-failed": "failed validation",
    "error": "errored",
}


def sweep_outcome(result: BranchSweepResult) -> tuple[int, str]:
    """The sweep's result as one sentence and the log level it deserves."""
    if result.error:
        return logging.ERROR, f"Backport sweep of {result.target_branch} failed: {result.error}"
    if not result.candidates_found:
        return logging.INFO, f"Backport sweep of {result.target_branch}: nothing to backport"
    counts: dict[str, int] = {}
    for item in result.results:
        counts[item.outcome] = counts.get(item.outcome, 0) + 1
    errored = [f"#{item.source_pr_number}" for item in result.results
               if item.outcome == "error" and item.source_pr_number]
    parts = [f"{n} {_OUTCOME_WORDS.get(k, k)}" for k, n in sorted(counts.items())]
    text = (f"Backport sweep of {result.target_branch}: {result.candidates_found} "
            f"candidate(s)" + (f", {', '.join(parts)}" if parts else "")
            + (f" (errored: {', '.join(errored)})" if errored else "")
            + (f"; PR {result.pr_url}" if result.pr_url else "; no PR change"))
    if errored and len(errored) == len(result.results):
        return logging.ERROR, text
    needs_look = errored or counts.get("skipped-conflict") or counts.get("skipped-validation-failed")
    return (logging.WARNING if needs_look else logging.INFO), text


def _exit_for_result(result: BranchSweepResult) -> None:
    level, text = sweep_outcome(result)
    logger.log(level, "%s", " ".join(text.split()))
    annotate(level, text)
    if level == logging.ERROR:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        default="repos.yml",
        help="Path to registry YAML (default: repos.yml)",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Repository full name (must exist in registry)",
    )
    parser.add_argument(
        "--branch",
        required=True,
        help="Target branch (must exist in registry for this repo)",
    )
    parser.add_argument(
        "--target-token", default="", help="GitHub token (defaults to TARGET_TOKEN)"
    )
    parser.add_argument("--status-field", default=_DEFAULT_STATUS_FIELD)
    parser.add_argument("--status-value", default=_DEFAULT_STATUS_VALUE)
    parser.add_argument("--branch-fields", default=",".join(_DEFAULT_BRANCH_FIELDS))
    parser.add_argument(
        "--test-commands",
        default="",
        help="Override test commands (newline-separated). Empty = use registry.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=5,
        help="Cap the number of applied cherry-picks per branch (0 = unlimited)",
    )
    state_mode = parser.add_mutually_exclusive_group()
    state_mode.add_argument(
        "--prepare-state", default="", help="Prepare locally and write publication state"
    )
    state_mode.add_argument(
        "--publish-state", default="", help="Publish state with a fresh token"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.publish_state and (args.dry_run or args.discover_only):
        parser.error("--publish-state cannot be combined with non-writing modes")

    configure_logging(verbose=args.verbose)

    env_token = os.environ.pop("TARGET_TOKEN", "")
    github_token = args.target_token or env_token
    if not github_token:
        parser.error("--target-token or TARGET_TOKEN is required")

    from scripts.backport.registry import load_registry

    registry = load_registry(args.registry)
    repo_entry, branch_entry = registry.get_branch(args.repo, args.branch)

    if args.publish_state:
        loaded = load_prepared_sweep(
            args.publish_state,
            repo_full_name=repo_entry.repo,
            push_repo=repo_entry.effective_push_repo,
            target_branch=branch_entry.branch,
            backport_branch=f"{_BRANCH_PREFIX}/{branch_entry.branch}",
            backport_label=repo_entry.backport_label,
            llm_conflict_label=repo_entry.llm_conflict_label,
        )
        result = publish_prepared_sweep(loaded, github_token)
        emit_job_summary(build_summary([result]))
        _print_result(result)
        _exit_for_result(result)
        return

    test_commands_override = None
    if args.test_commands:
        test_commands_override = [
            command.strip()
            for command in args.test_commands.split("\n")
            if command.strip()
        ]
    common_args = {
        "repo_entry": repo_entry,
        "branch_entry": branch_entry,
        "github_token": github_token,
        "status_field": args.status_field,
        "status_value": args.status_value,
        "branch_fields": [
            field.strip()
            for field in args.branch_fields.split(",")
            if field.strip()
        ] or None,
        "test_commands_override": test_commands_override,
        "discover_only": args.discover_only or args.dry_run,
        "max_candidates": args.max_candidates,
    }

    if args.prepare_state:
        state_path = Path(args.prepare_state)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.unlink(missing_ok=True)
        result, prepared = prepare_backport_sweep(
            **common_args,
            work_root=str(state_path.parent),
        )
        if prepared is not None:
            write_prepared_sweep(args.prepare_state, prepared)
        else:
            emit_job_summary(build_summary([result]))
    else:
        result = run_backport_sweep(**common_args)

    _print_result(result)
    if args.discover_only or args.dry_run:
        annotate(logging.INFO, f"Discovery only: {result.candidates_found} backport "
                               f"candidate(s) for {result.target_branch}")
        return
    if args.prepare_state and sweep_outcome(result)[0] != logging.ERROR:
        # Preparation publishes nothing; the publish step reports the outcome.
        # Failures still end the run here, as before.
        logger.info("Prepared %s: %d candidate(s), %d applied locally; publication follows",
                    result.target_branch, result.candidates_found, result.applied_count)
        return
    _exit_for_result(result)


if __name__ == "__main__":
    main()
