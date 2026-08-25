"""Backport pipeline CLI and orchestrator."""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github import Auth, Github
from github.GithubException import GithubException

from scripts.backport.candidate_apply import apply_candidate
from scripts.backport.diff_comments import reconcile_diff_comments
from scripts.backport.git_commands import head_sha
from scripts.backport.git_commands import run_git as _run_git
from scripts.backport.models import (
    DETAIL_EMPTY_ON_TARGET,
    BackportCandidate,
    BackportConfig,
    BackportOutcome,
    BackportPRContext,
    BackportResult,
    CandidateResult,
    CherryPickResult,
    ResolutionResult,
)
from scripts.backport.pr_creator import BackportPRCreator
from scripts.backport.registry import ValidationRule
from scripts.backport.sweep_validation import validate_branch_with_optional_repair
from scripts.backport.utils import build_branch_name
from scripts.backport.validation import changed_paths_since_base, select_validation_commands
from scripts.common.build_validator import run_build_commands
from scripts.common.git_auth import GitAuth, github_https_url
from scripts.common.github_client import retry_github_call
from scripts.common.identity import BOT_EMAIL, BOT_NAME
from scripts.common.job_summary import emit_job_summary

logger = logging.getLogger(__name__)

# Login that authors the AI-diff comments. Defaults to the bot, but the comment
# author follows the token identity, so a fork run using a personal PAT sets
# CI_AGENT_DIFF_COMMENT_LOGIN to that user so the ownership gate still matches.
DIFF_COMMENT_LOGIN = os.environ.get("CI_AGENT_DIFF_COMMENT_LOGIN") or BOT_NAME


def build_summary(result: BackportResult) -> str:
    """Generate a human-readable summary string for a backport run.

    Contains: commits cherry-picked, conflicting files, files resolved,
    and files unresolved.

    """
    lines = [
        f"- Outcome: `{result.outcome}`",
        f"- Commits cherry-picked: {result.commits_cherry_picked}",
        f"- Conflicting files: {result.files_conflicted}",
        f"- Conflict files resolved: {result.files_resolved}",
        f"- Files unresolved: {result.files_unresolved}",
    ]
    return "\n".join(lines)


def _skipped_existing_message(
    result: CandidateResult,
    target_branch: str,
) -> str:
    """Describe why a skipped candidate does not need a backport PR."""

    if result.detail == DETAIL_EMPTY_ON_TARGET:
        reason = (
            result.skip_reason
            or "The change produces no net change on this branch."
        )
        return (
            f"Source PR #{result.source_pr_number} does not require a "
            f"backport to `{target_branch}`; no backport PR was "
            f"created. {reason}"
        )
    return (
        f"Source PR #{result.source_pr_number} is already applied to "
        f"`{target_branch}`; no backport PR was created."
    )


def run_backport(
    repo_full_name: str,
    source_pr_number: int,
    target_branch: str,
    config: BackportConfig,
    github_token: str,
    push_repo: str | None = None,
    language: str = "c",
    build_commands: list[str] | None = None,
    validation_setup_commands: list[str] | None = None,
    validation_rules: list[ValidationRule] | None = None,
    validation_profile: str = "",
    generated_file_rules: list[object] | None = None,
    repair_validation_failures: bool = False,
    test_path_patterns: tuple[str, ...] | list[str] | None = None,
) -> BackportResult:
    """Execute the backport pipeline end-to-end.

    Returns a :class:`BackportResult` with outcome details.
    """
    if push_repo and push_repo.split("/", 1)[0] == repo_full_name.split("/", 1)[0]:
        return BackportResult(
            outcome="error",
            error_message=(
                "push_repo must be a different-owner fork; omit push_repo "
                "for direct upstream pushes"
            ),
        )
    effective_push_repo = push_repo or repo_full_name

    gh = Github(auth=Auth.Token(github_token))
    try:
        repo = retry_github_call(
            lambda: gh.get_repo(repo_full_name),
            retries=3,
            description=f"get repo {repo_full_name}",
        )

        logger.info("Validating target branch %s exists.", target_branch)
        try:
            retry_github_call(
                lambda: repo.get_branch(target_branch),
                retries=3,
                description=f"get branch {target_branch}",
            )
        except GithubException as exc:
            if exc.status == 404:
                msg = f"Target branch `{target_branch}` does not exist."
                logger.warning(msg)
                _post_comment(repo, source_pr_number, f"Backport skipped: {msg}")
                return BackportResult(outcome="branch-missing", error_message=msg)
            raise

        logger.info("Checking for duplicate backport PR.")
        pr_creator = BackportPRCreator(
            gh,
            base_repo=repo_full_name,
            push_repo=effective_push_repo,
            backport_label=config.backport_label,
            llm_conflict_label=config.llm_conflict_label,
        )
        existing_url = pr_creator.check_duplicate(source_pr_number, target_branch)
        if existing_url:
            msg = (
                f"A backport PR already exists for #{source_pr_number} → "
                f"`{target_branch}`: {existing_url}"
            )
            logger.info(msg)
            _post_comment(repo, source_pr_number, f"Backport skipped: {msg}")
            return BackportResult(outcome="duplicate", backport_pr_url=existing_url)

        logger.info("Fetching source PR #%d metadata.", source_pr_number)
        try:
            source_pr = retry_github_call(
                lambda: repo.get_pull(source_pr_number),
                retries=3,
                description=f"get PR #{source_pr_number}",
            )
        except GithubException as exc:
            msg = f"Failed to fetch source PR #{source_pr_number}: {exc}"
            logger.error(msg)
            _post_comment(repo, source_pr_number, f"Backport failed: {msg}")
            return BackportResult(outcome="error", error_message=msg)

        if not bool(getattr(source_pr, "merged", False)):
            msg = f"Source PR #{source_pr_number} is not merged."
            logger.warning(msg)
            _post_comment(repo, source_pr_number, f"Backport skipped: {msg}")
            return BackportResult(outcome="pr-not-merged", error_message=msg)

        commits = [
            c.sha
            for c in retry_github_call(
                lambda: list(source_pr.get_commits()),
                retries=3,
                description=f"get commits for PR #{source_pr_number}",
            )
        ]
        merge_commit_sha = source_pr.merge_commit_sha

        # Fetch PR diff
        try:
            # PyGithub doesn't have a direct diff method, but we can get
            # the patch/diff from the PR's files
            pr_files = retry_github_call(
                lambda: list(source_pr.get_files()),
                retries=3,
                description=f"get files for PR #{source_pr_number}",
            )
            diff_parts = []
            for f in pr_files:
                if f.patch:
                    diff_parts.append(
                        f"diff --git a/{f.filename} b/{f.filename}\n"
                        f"--- a/{f.filename}\n+++ b/{f.filename}\n{f.patch}"
                    )
            diff_content = "\n".join(diff_parts)
        except GithubException as exc:
            logger.warning("Could not fetch PR diff for #%s: %s", source_pr_number, exc)
            diff_content = ""

        candidate = BackportCandidate(
            source_pr_number=source_pr_number,
            source_pr_title=source_pr.title or "",
            source_pr_url=source_pr.html_url,
            source_pr_diff=diff_content,
            target_branch=target_branch,
            merge_commit_sha=merge_commit_sha,
            commit_shas=commits,
        )
        pr_context = candidate.to_pr_context()

        logger.info("Executing cherry-pick onto %s.", target_branch)
        branch_name = build_branch_name(source_pr_number, target_branch)
        with tempfile.TemporaryDirectory() as tmp_dir:
            with GitAuth(github_token, prefix="backport-git-askpass-") as git_auth:
                git_env = git_auth.env()
                # Clone the repo with full history for cherry-pick
                _clone_repo(
                    repo_full_name,
                    tmp_dir,
                    target_branch,
                    git_env=git_env,
                )

                if validation_setup_commands:
                    setup_ok, setup_output = run_build_commands(
                        tmp_dir,
                        validation_setup_commands,
                    )
                    if not setup_ok:
                        msg = (
                            "Validation setup failed: "
                            + (setup_output[:500] or "setup command failed")
                        )
                        logger.error(msg)
                        _post_comment(
                            repo,
                            source_pr_number,
                            f"Backport failed: {msg}",
                        )
                        return BackportResult(
                            outcome="error",
                            error_message=msg,
                        )

                # Create the backport branch locally from target branch HEAD
                _run_git(tmp_dir, "checkout", "-b", branch_name)

                application_result = apply_candidate(
                    tmp_dir,
                    candidate,
                    repo_full_name,
                    git_env,
                    language=language,
                    build_commands=build_commands,
                    validation_rules=validation_rules,
                    test_path_patterns=test_path_patterns,
                    max_conflicting_files=config.max_conflicting_files,
                )
                resolution_results = application_result.resolutions or None
                resolved_commit_sha = application_result.resolved_commit_sha
                cherry_result = CherryPickResult(
                    success=not application_result.conflicting_files,
                    conflicting_files=application_result.conflicting_files,
                    applied_commits=application_result.applied_commits,
                    conflicting_commit_sha=(
                        application_result.conflicting_commit_sha
                    ),
                )

                if application_result.outcome == "skipped-existing":
                    msg = _skipped_existing_message(
                        application_result,
                        target_branch,
                    )
                    logger.info(msg)
                    _post_comment(repo, source_pr_number, f"Backport skipped: {msg}")
                    return BackportResult(
                        outcome="already-applied",
                        error_message=msg,
                    )
                if application_result.outcome == "error":
                    msg = application_result.detail or "Candidate application failed."
                    logger.error(msg)
                    _post_comment(repo, source_pr_number, f"Backport failed: {msg}")
                    return BackportResult(
                        outcome="error",
                        error_message=msg,
                    )
                if application_result.outcome == "skipped-conflict":
                    conflict_paths = {
                        item.path
                        for item in application_result.conflicting_files
                    }
                    resolved_paths = {
                        item.path
                        for item in application_result.resolutions
                        if item.resolved_content is not None
                        and item.path in conflict_paths
                    }
                    unresolved_paths = conflict_paths - resolved_paths
                    result = BackportResult(
                        outcome="conflicts-unresolved",
                        files_conflicted=len(conflict_paths),
                        files_resolved=len(resolved_paths),
                        files_unresolved=len(unresolved_paths),
                        error_message=application_result.detail,
                    )
                    summary_text = build_summary(result)
                    _post_comment(
                        repo,
                        source_pr_number,
                        "## Backport Result\n\n"
                        "Backport could not be completed automatically.\n\n"
                        f"### Overview\n{summary_text}",
                    )
                    emit_job_summary(
                        "## Backport Result: conflicts-unresolved\n\n"
                        f"- Source PR: #{source_pr_number}\n"
                        f"- Target branch: `{target_branch}`\n\n"
                        f"### Overview\n{summary_text}"
                    )
                    return result
                if application_result.outcome != "applied":
                    msg = (
                        "Candidate application returned unexpected outcome: "
                        f"{application_result.outcome}"
                    )
                    logger.error(msg)
                    return BackportResult(
                        outcome="error",
                        error_message=msg,
                    )

                validation_outcome = None
                validation_ok = True
                validation_output = ""
                if validation_profile or generated_file_rules:
                    validation_outcome = validate_branch_with_optional_repair(
                        tmp_dir,
                        target_branch,
                        build_commands or [],
                        validation_rules or [],
                        repair=repair_validation_failures,
                        validation_profile=validation_profile,
                        generated_file_rules=generated_file_rules,
                        run_git=_run_git,
                    )
                    validation_ok = validation_outcome.ok
                    validation_output = validation_outcome.output
                elif build_commands or validation_rules:
                    commands = select_validation_commands(
                        build_commands or [],
                        validation_rules or [],
                        changed_paths_since_base(tmp_dir, f"origin/{target_branch}"),
                    )
                    validation_ok, validation_output = run_build_commands(tmp_dir, commands)

                if not validation_ok:
                    msg = f"Build validation failed: {validation_output[:500]}"
                    logger.error(msg)
                    _post_comment(repo, source_pr_number, f"Backport skipped: {msg}")
                    return BackportResult(
                        outcome="error",
                        commits_cherry_picked=len(cherry_result.applied_commits),
                        files_conflicted=len(cherry_result.conflicting_files),
                        error_message=msg,
                    )
                if validation_outcome is not None and validation_outcome.amended_commit_sha:
                    application_result.resolved_commit_sha = validation_outcome.amended_commit_sha
                    resolved_commit_sha = validation_outcome.amended_commit_sha
                if validation_outcome is not None and validation_outcome.resolutions:
                    application_result.resolutions.extend(validation_outcome.resolutions)
                    resolution_results = application_result.resolutions
                    application_result.resolved_by_ai = True
                    application_result.ai_summary = validation_outcome.ai_summary
                    resolved_commit_sha = head_sha(tmp_dir)

                # Push the backport branch to the remote
                push_remote = "origin"
                if effective_push_repo != repo_full_name:
                    push_remote = "push_target"
                    push_url = github_https_url(effective_push_repo)
                    _run_git(tmp_dir, "remote", "add", push_remote, push_url, env=git_env)
                    # Sync the staging fork's target branch from upstream so the
                    # PR doesn't show unrelated commits. This only updates a
                    # *fork's* copy of the release branch — never the upstream
                    # release branch itself.
                    if effective_push_repo.split("/", 1)[0] == repo_full_name.split("/", 1)[0]:
                        raise RuntimeError(
                            f"Refusing to push to release branch on same-owner repo: "
                            f"{effective_push_repo} (target_branch={target_branch}). "
                            f"push_repo must be a different-owner fork."
                        )
                    logger.info("Syncing %s:%s to upstream.", effective_push_repo, target_branch)
                    _run_git(tmp_dir, "push", push_remote, f"{target_branch}:{target_branch}", env=git_env)
                # Sanity check: the agent only pushes to branches it owns.
                # branch_name comes from build_branch_name() which always
                # produces 'backport/<pr>-to-<target>'.
                if not branch_name.startswith("backport/"):
                    raise RuntimeError(
                        f"Refusing to push to non-namespaced branch: {branch_name!r}. "
                        f"Agent push targets must start with 'backport/'."
                    )
                logger.info("Pushing branch %s to %s.", branch_name, effective_push_repo)
                _run_git(tmp_dir, "push", "--force-with-lease", push_remote, branch_name, env=git_env)
        logger.info("Creating backport PR.")
        try:
            backport_pr_url = pr_creator.create_backport_pr(
                pr_context,
                cherry_result,
                resolution_results,
                branch_name,
                ai_involved=application_result.resolved_by_ai,
                ai_summary=application_result.ai_summary,
            )
        except (GithubException, subprocess.CalledProcessError) as exc:
            msg = f"Failed to create backport PR: {exc}"
            logger.error(msg)
            _post_comment(repo, source_pr_number, f"Backport failed: {msg}")
            return BackportResult(outcome="error", error_message=msg)

        # Post AI-resolution details as PR comments (best-effort). The
        # backport already succeeded; a comment failure must never change that.
        if resolution_results:
            _reconcile_diff_comments_best_effort(
                repo, backport_pr_url, pr_context, cherry_result, resolution_results,
                resolved_commit_sha=resolved_commit_sha,
                ai_involved=application_result.resolved_by_ai,
                ai_summary=application_result.ai_summary,
            )

        files_resolved = 0
        files_unresolved = 0
        if resolution_results:
            files_resolved = sum(
                1 for r in resolution_results if r.resolved_content is not None
            )
            files_unresolved = sum(
                1 for r in resolution_results if r.resolved_content is None
            )

        outcome: BackportOutcome = (
            "success" if files_unresolved == 0 else "conflicts-unresolved"
        )
        result = BackportResult(
            outcome=outcome,
            backport_pr_url=backport_pr_url,
            commits_cherry_picked=len(cherry_result.applied_commits),
            files_conflicted=len(cherry_result.conflicting_files),
            files_resolved=files_resolved,
            files_unresolved=files_unresolved,
        )

        summary_text = build_summary(result)
        comment_body = (
            "## Backport Result\n\n"
            f"Backport PR created: [view PR]({backport_pr_url})\n\n"
            f"### Overview\n{summary_text}"
        )
        _post_comment(repo, source_pr_number, comment_body)


        job_summary = (
            f"## Backport Result: {result.outcome}\n\n"
            f"- Source PR: #{source_pr_number}\n"
            f"- Target branch: `{target_branch}`\n"
            f"- Backport PR: [view PR]({backport_pr_url})\n\n"
            f"### Overview\n{summary_text}"
        )
        emit_job_summary(job_summary)

        logger.info("Backport complete: %s", result.outcome)
        return result
    except Exception as exc:
        logger.exception("Backport pipeline failed")
        return BackportResult(outcome="error", error_message=str(exc))




def _post_comment(repo: object, pr_number: int, body: str) -> None:
    """Post a comment on a pull request (best-effort)."""
    try:
        pr = retry_github_call(
            lambda: repo.get_pull(pr_number),  # type: ignore[attr-defined]
            retries=3,
            description=f"get PR #{pr_number} for comment",
        )
        retry_github_call(
            lambda: pr.create_issue_comment(body),
            retries=3,
            description=f"post comment on PR #{pr_number}",
        )
        logger.info("Posted comment on PR #%d.", pr_number)
    except Exception as exc:
        logger.warning("Failed to post comment on PR #%d: %s", pr_number, exc)


def _reconcile_diff_comments_best_effort(
    repo: object,
    backport_pr_url: str,
    pr_context: BackportPRContext,
    cherry_result: CherryPickResult,
    resolution_results: list[ResolutionResult],
    *,
    resolved_commit_sha: str | None = None,
    ai_involved: bool = False,
    ai_summary: str = "",
) -> None:
    """Post AI-resolution diff comments, then link them from the PR body.

    Best-effort: the backport already succeeded, so neither the comment
    reconcile nor the body re-edit may raise into the caller.
    """
    match = re.search(r"/pull/(\d+)", backport_pr_url)
    if not match:
        logger.warning("Could not parse PR number from %s; skipping diff comments.", backport_pr_url)
        return
    pr_number = int(match.group(1))
    try:
        pr = retry_github_call(
            lambda: repo.get_pull(pr_number),  # type: ignore[attr-defined]
            retries=3,
            description=f"get backport PR #{pr_number} for diff comments",
        )
        # Key the comment markers on the source PR (not the backport PR), so the
        # identity matches the sweep path and the module's documented scheme.
        comment_links = reconcile_diff_comments(
            pr,
            pr_context.source_pr_number,
            resolution_results,
            source_title=pr_context.source_pr_title,
            cherry_pick_sha=cherry_result.conflicting_commit_sha,
            resolved_commit_sha=resolved_commit_sha,
            bot_login=DIFF_COMMENT_LOGIN,
        )
        logger.info("Reconciled AI-diff comments on PR #%d.", pr_number)
    except Exception as exc:
        logger.warning("Failed to reconcile diff comments on PR #%d: %s", pr_number, exc)
        return

    if not comment_links:
        return

    # Second pass: rebuild the body with links now that comment URLs exist.
    try:
        linked_body = BackportPRCreator.build_pr_body(
            pr_context,
            not cherry_result.success,
            resolution_results,
            applied_commits=cherry_result.applied_commits,
            comment_links=comment_links,
            ai_involved=ai_involved,
            ai_summary=ai_summary,
        )
        retry_github_call(
            lambda: pr.edit(body=linked_body),
            retries=3,
            description=f"link AI-diff comments in PR #{pr_number} body",
        )
        logger.info("Linked AI-diff comments in PR #%d body.", pr_number)
    except Exception as exc:
        logger.warning("Failed to link diff comments in PR #%d body: %s", pr_number, exc)


def _clone_repo(
    repo_full_name: str,
    dest_dir: str,
    target_branch: str,
    *,
    git_env: dict[str, str],
) -> dict[str, str]:
    """Clone the repository with full history into *dest_dir*.

    Uses a git credential helper to supply the token, avoiding
    embedding credentials in the clone URL (which would persist in
    ``.git/config`` and be visible via ``git remote -v``).
    """
    logger.info("Cloning %s into %s.", repo_full_name, dest_dir)

    clone_url = github_https_url(repo_full_name)
    subprocess.run(
        ["git", "clone", "--no-single-branch", "--branch", target_branch, clone_url, "."],
        cwd=dest_dir,
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    )
    # Configure git identity for cherry-pick commits
    subprocess.run(
        ["git", "config", "user.name", BOT_NAME],
        cwd=dest_dir, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", BOT_EMAIL],
        cwd=dest_dir, check=True, capture_output=True, text=True,
    )
    # Fetch all branches so cherry-pick can reference any commit
    subprocess.run(
        ["git", "fetch", "--all"],
        cwd=dest_dir,
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    )
    return git_env


def main() -> None:
    """CLI entry point for the backport agent."""
    parser = argparse.ArgumentParser(description="Backport Agent Pipeline")
    parser.add_argument(
        "--repo", required=True, help="Repository full name (owner/repo)",
    )
    parser.add_argument(
        "--pr-number", type=int, required=True, help="Source PR number",
    )
    parser.add_argument(
        "--target-branch", required=True, help="Target release branch",
    )
    parser.add_argument(
        "--registry",
        default="repos.yml",
        help="Path to registry YAML (default: repos.yml)",
    )
    parser.add_argument(
        "--token",
        default="",
        help=(
            "GitHub token. Prefer BACKPORT_GITHUB_TOKEN or GITHUB_TOKEN in CI "
            "to avoid putting secrets in process arguments."
        ),
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging",
    )
    parser.add_argument(
        "--push-repo",
        default="",
        help="Override push_repo with a different-owner fork (emergency/testing use only)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    github_token = (
        args.token
        or os.environ.get("BACKPORT_GITHUB_TOKEN", "")
        or os.environ.get("GITHUB_TOKEN", "")
    )
    if not github_token:
        parser.error(
            "GitHub token is required via --token, BACKPORT_GITHUB_TOKEN, or GITHUB_TOKEN."
        )

    from scripts.backport.registry import load_registry
    registry = load_registry(args.registry)
    try:
        repo_entry, _branch_entry = registry.get_branch(args.repo, args.target_branch)
    except KeyError as exc:
        parser.error(str(exc))

    result = run_backport(
        repo_full_name=args.repo,
        source_pr_number=args.pr_number,
        target_branch=args.target_branch,
        config=BackportConfig(
            backport_label=repo_entry.backport_label,
            llm_conflict_label=repo_entry.llm_conflict_label,
            max_conflicting_files=repo_entry.max_conflicting_files,
        ),
        github_token=github_token,
        push_repo=args.push_repo or repo_entry.push_repo,
        language=repo_entry.language,
        build_commands=list(repo_entry.build_commands) or None,
        validation_setup_commands=list(repo_entry.validation_setup_commands),
        validation_rules=list(repo_entry.validation_rules),
        validation_profile=repo_entry.validation_profile,
        generated_file_rules=list(repo_entry.generated_file_rules),
        repair_validation_failures=repo_entry.repair_validation_failures,
        test_path_patterns=repo_entry.test_path_patterns,
    )

    logger.info("Backport outcome: %s", result.outcome)
    if result.outcome == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
