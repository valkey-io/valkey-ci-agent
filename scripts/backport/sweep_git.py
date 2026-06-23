"""Git workspace operations for scheduled backport sweeps."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any, Callable

from github.GithubException import GithubException

from scripts.backport.main import BOT_EMAIL, BOT_NAME
from scripts.backport.main import _run_git as run_git_default
from scripts.backport.sweep_models import (
    DETAIL_ALREADY_ON_SWEEP_BRANCH,
    BranchAppliedPr,
    CandidateResult,
)
from scripts.backport.utils import pr_numbers_from_commit_subjects
from scripts.common.git_auth import github_https_url
from scripts.common.github_client import retry_github_call

logger = logging.getLogger(__name__)


RunGit = Callable[..., Any]

BRANCH_PREFIX = "agent/backport/sweep"


def safe_tmp_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "branch"


def clone_target_branch(
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
    run_git_default(dest_dir, "config", "user.name", BOT_NAME)
    run_git_default(dest_dir, "config", "user.email", BOT_EMAIL)


def push_backport_branch(
    repo_dir: str,
    branch: str,
    git_env: dict[str, str],
    *,
    force_with_lease: bool,
    branch_prefix: str = BRANCH_PREFIX,
    run_git: RunGit = run_git_default,
) -> None:
    if not branch.startswith(f"{branch_prefix}/"):
        raise RuntimeError(
            f"Refusing to push to non-namespaced branch: {branch!r}. "
            f"Agent push targets must start with {branch_prefix}/."
        )
    args = ["push", "push_target", branch]
    if force_with_lease:
        args.insert(1, "--force-with-lease")
    run_git(repo_dir, *args, env=git_env)


def list_already_applied(repo_dir: str, base_branch: str, backport_branch: str) -> set[str]:
    return {
        str(applied.source_pr_number)
        for applied in list_branch_applied_prs(repo_dir, base_branch, backport_branch)
    }


def list_branch_applied_prs(
    repo_dir: str,
    base_branch: str,
    backport_branch: str,
) -> list[BranchAppliedPr]:
    """Return the PRs on the sweep branch, in branch order, with commit SHAs."""
    result = subprocess.run(
        [
            "git",
            "log",
            "--reverse",
            f"origin/{base_branch}..{backport_branch}",
            "--format=%H%x00%s",
        ],
        cwd=repo_dir, capture_output=True, text=True, check=True,
    )
    applied: list[BranchAppliedPr] = []
    seen: set[int] = set()
    for line in result.stdout.strip().splitlines():
        if "\0" not in line:
            continue
        commit_sha, subject = line.split("\0", 1)
        matched = pr_numbers_from_commit_subjects([subject])
        if not matched:
            continue
        pr_number = next(iter(matched))
        if pr_number in seen:
            continue
        seen.add(pr_number)
        title = re.sub(r"\s*\(#\d+\)\s*$", "", subject).strip() or subject.strip()
        applied.append(
            BranchAppliedPr(
                source_pr_number=pr_number,
                source_pr_title=title,
                commit_sha=commit_sha,
            )
        )
    return applied


def list_applied_prs_on_branch(
    repo_dir: str,
    base_branch: str,
    backport_branch: str,
) -> list[CandidateResult]:
    return [
        CandidateResult(
            source_pr_number=applied.source_pr_number,
            source_pr_title=applied.source_pr_title,
            outcome="skipped-existing",
            detail=DETAIL_ALREADY_ON_SWEEP_BRANCH,
        )
        for applied in list_branch_applied_prs(repo_dir, base_branch, backport_branch)
    ]


RunProcess = Callable[..., subprocess.CompletedProcess[Any]]


def changed_paths_in_index_or_worktree(
    repo_dir: str,
    *,
    run_process: RunProcess = subprocess.run,
) -> tuple[str, ...]:
    """Return staged, unstaged, and untracked paths with exact git path names."""
    return collect_git_paths_z(
        repo_dir,
        (
            ("git", "diff", "--name-only", "-z"),
            ("git", "diff", "--cached", "--name-only", "-z"),
            ("git", "ls-files", "--others", "--exclude-standard", "-z"),
        ),
        run_process=run_process,
    )


def worktree_changed_paths(
    repo_dir: str,
    *,
    run_process: RunProcess = subprocess.run,
) -> tuple[str, ...]:
    return collect_git_paths_z(
        repo_dir,
        (
            ("git", "diff", "--name-only", "-z", "HEAD"),
            ("git", "ls-files", "--others", "--exclude-standard", "-z"),
        ),
        run_process=run_process,
    )


def collect_git_paths_z(
    repo_dir: str,
    commands: tuple[tuple[str, ...], ...],
    *,
    run_process: RunProcess = subprocess.run,
) -> tuple[str, ...]:
    paths: set[str] = set()
    for command in commands:
        result = run_process(
            list(command),
            cwd=repo_dir,
            capture_output=True,
            text=False,
        )
        if result.returncode != 0:
            stderr = os.fsdecode(result.stderr).strip()
            raise RuntimeError(
                f"could not collect changed paths with {' '.join(command)} "
                f"(exit {result.returncode}): "
                + (stderr[:300] or "git command failed")
            )
        stdout = result.stdout
        parts = stdout.split(b"\0") if isinstance(stdout, bytes) else str(stdout).split("\0")
        paths.update(os.fsdecode(path) for path in parts if path)
    return tuple(sorted(paths))


def branch_has_changes(repo_dir: str, target_branch: str) -> bool:
    result = subprocess.run(
        ["git", "diff", "--quiet", f"origin/{target_branch}...HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    raise RuntimeError(
        f"could not compare branch to origin/{target_branch}: "
        + (result.stderr.strip()[:300] or "git diff failed")
    )


def sync_target_branch_to_source(
    gh: Any, push_repo: str, source_repo: str, target_branch: str,
) -> None:
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

    try:
        push_sha = retry_github_call(
            lambda: push_repo_obj.get_branch(target_branch).commit.sha,
            retries=2, description=f"get {push_repo}:{target_branch} head",
        )
    except GithubException as exc:
        if exc.status != 404:
            raise
        logger.info(
            "Creating missing fork branch %s:%s at %s",
            push_repo, target_branch, source_sha[:8],
        )
        retry_github_call(
            lambda: push_repo_obj.create_git_ref(
                ref=f"refs/heads/{target_branch}",
                sha=source_sha,
            ),
            retries=2,
            description=f"create {push_repo}:{target_branch}",
        )
        return

    if push_sha == source_sha:
        logger.info("push_repo %s:%s already in sync with %s", push_repo, target_branch, source_repo)
        return

    compare = retry_github_call(
        lambda: gh.get_repo(source_repo).compare(push_sha, source_sha),
        retries=2, description=f"compare {push_sha[:8]}..{source_sha[:8]}",
    )

    if compare.status in ("identical", "ahead"):
        logger.info(
            "Fast-forwarding %s:%s from %s to %s (behind by %d)",
            push_repo, target_branch, push_sha[:8], source_sha[:8], compare.ahead_by,
        )
        ref = retry_github_call(
            lambda: gh.get_repo(push_repo).get_git_ref(f"heads/{target_branch}"),
            retries=2, description=f"get ref {target_branch}",
        )
        retry_github_call(
            lambda: ref.edit(source_sha, force=False),
            retries=2, description=f"fast-forward {target_branch}",
        )
    elif compare.status in ("diverged", "behind"):
        raise RuntimeError(
            f"{push_repo}:{target_branch} has diverged from "
            f"{source_repo}:{target_branch} (ahead={compare.ahead_by}, "
            f"behind={compare.behind_by}). Cannot safely fast-forward. "
            "Resolve the divergence manually before running the sweep."
        )
