"""Backport pipeline CLI and orchestrator."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github import Auth, Github
from github.GithubException import GithubException

from scripts.backport.cherry_pick import cherry_pick
from scripts.backport.conflict_resolver import resolve_conflicts_with_claude
from scripts.backport.models import (
    BackportConfig,
    BackportOutcome,
    BackportPRContext,
    BackportResult,
    ResolutionResult,
)
from scripts.backport.pr_creator import BackportPRCreator
from scripts.backport.registry import ValidationRule
from scripts.backport.utils import build_branch_name
from scripts.backport.validation import (
    changed_paths_since_base,
    select_validation_commands,
)
from scripts.common.git_auth import GitAuth, github_https_url
from scripts.common.github_client import retry_github_call
from scripts.common.job_summary import emit_job_summary

logger = logging.getLogger(__name__)




def build_summary(result: BackportResult) -> str:
    """Generate a human-readable summary string for a backport run.

    Contains: commits cherry-picked, conflicting files, files resolved,
    and files unresolved.

    """
    lines = [
        f"- Outcome: `{result.outcome}`",
        f"- Commits cherry-picked: {result.commits_cherry_picked}",
        f"- Conflicting files: {result.files_conflicted}",
        f"- Files resolved by LLM: {result.files_resolved}",
        f"- Files unresolved: {result.files_unresolved}",
    ]
    return "\n".join(lines)


def run_backport(
    repo_full_name: str,
    source_pr_number: int,
    target_branch: str,
    config: BackportConfig,
    github_token: str,
    push_repo: str | None = None,
    language: str = "c",
    build_commands: list[str] | None = None,
    validation_rules: list[ValidationRule] | None = None,
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
        except Exception as exc:
            logger.warning("Could not fetch PR diff for #%s: %s", source_pr_number, exc)
            diff_content = ""

        pr_context = BackportPRContext(
            source_pr_number=source_pr_number,
            source_pr_title=source_pr.title or "",
            source_pr_url=source_pr.html_url,
            source_pr_diff=diff_content,
            target_branch=target_branch,
            commits=commits,
        )

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

                # Create the backport branch locally from target branch HEAD
                _run_git(tmp_dir, "checkout", "-b", branch_name)

                try:
                    cherry_result = cherry_pick(
                        tmp_dir, branch_name, merge_commit_sha, commits,
                    )
                except Exception as exc:
                    msg = f"Cherry-pick failed: {exc}"
                    logger.error(msg)
                    _post_comment(repo, source_pr_number, f"Backport failed: {msg}")
                    return BackportResult(outcome="error", error_message=msg)

                resolution_results = None
                if cherry_result.success and not cherry_result.applied_commits:
                    msg = (
                        f"Source PR #{source_pr_number} is already applied to "
                        f"`{target_branch}`; no backport PR was created."
                    )
                    logger.info(msg)
                    _post_comment(repo, source_pr_number, f"Backport skipped: {msg}")
                    return BackportResult(
                        outcome="already-applied",
                        error_message=msg,
                    )
                if not cherry_result.success and not cherry_result.conflicting_files:
                    msg = (
                        "Cherry-pick failed without conflicted files; refusing to "
                        "push an unresolved or unchanged backport branch."
                    )
                    logger.error(msg)
                    _post_comment(repo, source_pr_number, f"Backport failed: {msg}")
                    return BackportResult(
                        outcome="error",
                        commits_cherry_picked=len(cherry_result.applied_commits),
                        error_message=msg,
                    )
                if not cherry_result.success and cherry_result.conflicting_files:
                    if len(cherry_result.conflicting_files) > config.max_conflicting_files:
                        msg = (
                            f"Too many conflicting files "
                            f"({len(cherry_result.conflicting_files)} > "
                            f"max_conflicting_files={config.max_conflicting_files}). "
                            f"Refusing to invoke conflict resolver."
                        )
                        logger.warning(msg)
                        _post_comment(repo, source_pr_number, f"Backport skipped: {msg}")
                        return BackportResult(
                            outcome="conflicts-unresolved",
                            commits_cherry_picked=len(cherry_result.applied_commits),
                            files_conflicted=len(cherry_result.conflicting_files),
                            files_unresolved=len(cherry_result.conflicting_files),
                            error_message=msg,
                        )
                    logger.info(
                        "Cherry-pick produced %d conflict(s). Invoking conflict resolver.",
                        len(cherry_result.conflicting_files),
                    )
                    resolver_validation_commands = select_validation_commands(
                        build_commands or [],
                        validation_rules or [],
                        [f.path for f in cherry_result.conflicting_files],
                    )
                    resolution_results = resolve_conflicts_with_claude(
                        tmp_dir,
                        cherry_result.conflicting_files,
                        pr_context,
                        language=language,
                        build_commands=resolver_validation_commands or None,
                    )
                    unresolved = [
                        r for r in resolution_results
                        if r.resolved_content is None
                    ]
                    if unresolved:
                        files_resolved = len(resolution_results) - len(unresolved)
                        files_unresolved = len(unresolved)
                        result = BackportResult(
                            outcome="conflicts-unresolved",
                            commits_cherry_picked=len(cherry_result.applied_commits),
                            files_conflicted=len(cherry_result.conflicting_files),
                            files_resolved=files_resolved,
                            files_unresolved=files_unresolved,
                            error_message=(
                                "Unresolved conflict(s): "
                                + ", ".join(r.path for r in unresolved)
                            ),
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
                            f"## Backport Result: conflicts-unresolved\n\n"
                            f"- Source PR: #{source_pr_number}\n"
                            f"- Target branch: `{target_branch}`\n\n"
                            f"### Overview\n{summary_text}"
                        )
                        return result

                    # Apply resolved files to the working tree and commit
                    _apply_resolutions(
                        tmp_dir,
                        resolution_results,
                    )

                commands: list[str] = []
                if build_commands or validation_rules:
                    commands = select_validation_commands(
                        build_commands or [],
                        validation_rules or [],
                        changed_paths_since_base(tmp_dir, f"origin/{target_branch}"),
                    )
                if commands:
                    from scripts.common.build_validator import run_build_commands
                    ok, output = run_build_commands(tmp_dir, commands)
                    if not ok:
                        msg = f"Build validation failed: {output[:500]}"
                        logger.error(msg)
                        _post_comment(repo, source_pr_number, f"Backport skipped: {msg}")
                        return BackportResult(
                            outcome="error",
                            commits_cherry_picked=len(cherry_result.applied_commits),
                            files_conflicted=len(cherry_result.conflicting_files),
                            error_message=msg,
                        )

                # Push the backport branch to the remote
                push_remote = "origin"
                if effective_push_repo != repo_full_name:
                    push_remote = "push_target"
                    push_url = github_https_url(effective_push_repo)
                    _run_git(tmp_dir, "remote", "add", push_remote, push_url, env=git_env)
                    # Sync the fork's target branch to upstream so the PR
                    # doesn't show unrelated commits.
                    logger.info("Syncing %s:%s to upstream.", effective_push_repo, target_branch)
                    _run_git(tmp_dir, "push", push_remote, f"{target_branch}:{target_branch}", env=git_env)
                logger.info("Pushing branch %s to %s.", branch_name, effective_push_repo)
                _run_git(tmp_dir, "push", "--force-with-lease", push_remote, branch_name, env=git_env)
        logger.info("Creating backport PR.")
        try:
            backport_pr_url = pr_creator.create_backport_pr(
                pr_context, cherry_result, resolution_results, branch_name,
            )
        except Exception as exc:
            msg = f"Failed to create backport PR: {exc}"
            logger.error(msg)
            _post_comment(repo, source_pr_number, f"Backport failed: {msg}")
            return BackportResult(outcome="error", error_message=msg)

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
        ["git", "config", "user.name", "valkey-ci-agent[bot]"],
        cwd=dest_dir, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "valkey-ci-agent[bot]@users.noreply.github.com"],
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


def _run_git(repo_dir: str, *args: str, env: dict[str, str] | None = None) -> None:
    """Run a git command in *repo_dir*, raising on failure."""
    cmd = ["git", *args]
    logger.debug("Running: %s (cwd=%s)", " ".join(cmd), repo_dir)
    subprocess.run(cmd, cwd=repo_dir, check=True, capture_output=True, text=True, env=env)


def _apply_resolutions(
    repo_dir: str,
    resolution_results: list[ResolutionResult],
) -> None:
    """Write resolved file contents to the working tree and commit.

    For each successfully resolved file, writes the content, stages it
    with ``git add``, then aborts the failed cherry-pick and commits
    the resolved state.
    """
    any_resolved = False
    for result in resolution_results:
        if result.resolved_content is not None:
            file_path = os.path.join(repo_dir, result.path)
            parent = os.path.dirname(file_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as fh:
                fh.write(result.resolved_content)
            _run_git(repo_dir, "add", result.path)
            any_resolved = True
        else:
            raise ValueError(f"Cannot apply unresolved conflict for {result.path}")

    if not any_resolved:
        # Nothing staged means nothing to commit; caller already verified the
        # input, so this is a defensive check rather than an expected path.
        return
    # Complete the cherry-pick with resolved content.
    # Set core.editor=true to prevent git from opening an editor
    # in the non-interactive CI environment.
    try:
        _run_git(
            repo_dir,
            "-c", "core.editor=true",
            "cherry-pick",
            "--continue",
        )
    except Exception as exc:
        # If cherry-pick --continue fails, something is wrong with the
        # resolution (e.g., all files matched target-branch content so
        # there's nothing to commit). Don't create a pointless empty
        # commit — let the caller see the error and skip this candidate.
        logger.warning("cherry-pick --continue failed: %s", exc)
        raise




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
        validation_rules=list(repo_entry.validation_rules),
    )

    logger.info("Backport outcome: %s", result.outcome)
    if result.outcome == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
