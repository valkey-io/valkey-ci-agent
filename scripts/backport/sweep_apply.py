"""Apply a single project-board backport candidate to a sweep branch."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from scripts.backport.cherry_pick import is_non_merge_mainline_error
from scripts.backport.conflict_resolver import resolve_conflicts_with_claude
from scripts.backport.main import _run_git as run_git_default
from scripts.backport.models import BackportPRContext, ConflictedFile, ResolutionResult
from scripts.backport.sweep_git import changed_paths_in_index_or_worktree
from scripts.backport.sweep_models import CandidateResult, ProjectBackportCandidate
from scripts.backport.validation import select_validation_commands

logger = logging.getLogger(__name__)

RunGit = Callable[..., Any]
RunProcess = Callable[..., subprocess.CompletedProcess[str]]
ResolveConflicts = Callable[..., list[ResolutionResult]]


def _abort_cherry_pick(repo_dir: str, run_git: RunGit) -> None:
    run_git(repo_dir, "cherry-pick", "--abort")


def apply_candidate(
    repo_dir: str,
    candidate: ProjectBackportCandidate,
    repo_full_name: str,
    git_env: dict[str, str],
    *,
    language: str = "c",
    build_commands: list[str] | None = None,
    validation_rules: list[Any] | None = None,
    run_git: RunGit = run_git_default,
    resolve_conflicts: ResolveConflicts = resolve_conflicts_with_claude,
    run_process: RunProcess = subprocess.run,
) -> CandidateResult:
    sha = candidate.merge_commit_sha
    if not sha:
        return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "error", "no merge SHA")

    try:
        run_git(repo_dir, "fetch", "origin", sha, env=git_env)
        result = run_process(
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
            result = run_process(
                ["git", "cherry-pick", sha],
                cwd=repo_dir, capture_output=True, text=True,
            )
    except subprocess.CalledProcessError as exc:
        return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "error", str(exc))

    if result.returncode == 0:
        return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "applied")

    conflict_result = run_process(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    conflicting_paths = [
        line.strip()
        for line in conflict_result.stdout.splitlines()
        if line.strip()
    ]
    if not conflicting_paths:
        _abort_cherry_pick(repo_dir, run_git)
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
    conflicting_files = []
    target_missing_paths: set[str] = set()
    for path in conflicting_paths:
        target_content = read_index_stage(repo_dir, path, 2, run_process=run_process)
        source_content = read_index_stage(repo_dir, path, 3, run_process=run_process)
        # Binary files have no line-level merge, so the resolver can't act on
        # them (git marks binary content with a NUL byte). Skip them rather
        # than feeding them to the resolver. A candidate left with only binary
        # conflicts has no resolvable files and is skipped below.
        if "\x00" in target_content or "\x00" in source_content:
            logger.warning("Skipping binary conflict: %s", path)
            continue
        if not index_stage_exists(repo_dir, path, 2, run_process=run_process):
            target_missing_paths.add(path)
        conflicting_files.append(ConflictedFile(
            path=path,
            target_branch_content=target_content,
            source_branch_content=source_content,
        ))
    if not conflicting_files:
        _abort_cherry_pick(repo_dir, run_git)
        return CandidateResult(
            candidate.source_pr_number,
            candidate.source_pr_title,
            "skipped-conflict",
            "only binary file conflicts; nothing the resolver can act on",
        )
    if target_missing_paths:
        _abort_cherry_pick(repo_dir, run_git)
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
    worktree_paths = changed_paths_in_index_or_worktree(repo_dir, run_process=run_process)
    allowed_resolution_paths = sorted(set(conflicting_paths) | set(worktree_paths))
    resolutions = resolve_conflicts(
        repo_dir, conflicting_files, pr_context,
        language=language, build_commands=resolver_validation_commands or None,
        allowed_paths=allowed_resolution_paths,
    )
    unresolved = [r for r in resolutions if r.resolved_content is None]
    if unresolved:
        _abort_cherry_pick(repo_dir, run_git)
        details = "; ".join(
            f"{r.path}: {(r.resolution_summary or 'unresolved')[:200]}"
            for r in unresolved
        )
        return CandidateResult(
            candidate.source_pr_number,
            candidate.source_pr_title,
            "skipped-conflict",
            f"unresolved - {details}",
        )

    for r in resolutions:
        if r.resolved_content is not None:
            resolved_path = Path(repo_dir, r.path)
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            resolved_path.write_text(r.resolved_content, encoding="utf-8")
            run_git(repo_dir, "add", r.path)
    if not has_staged_changes(repo_dir, run_process=run_process):
        _abort_cherry_pick(repo_dir, run_git)
        return CandidateResult(
            candidate.source_pr_number,
            candidate.source_pr_title,
            "skipped-existing",
            "resolution was already satisfied on target branch",
        )

    commit_result = run_process(
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
            _abort_cherry_pick(repo_dir, run_git)
            return CandidateResult(
                candidate.source_pr_number, candidate.source_pr_title,
                "skipped-existing",
                "resolution was already satisfied on target branch",
            )
        _abort_cherry_pick(repo_dir, run_git)
        return CandidateResult(
            candidate.source_pr_number, candidate.source_pr_title,
            "skipped-conflict",
            f"commit failed: {(commit_result.stderr or commit_result.stdout).strip()[:200]}",
        )

    issue = check_applied_commit_size(repo_dir, candidate, run_process=run_process)
    if issue:
        logger.warning(
            "Reverting cherry-pick for #%d: %s",
            candidate.source_pr_number, issue,
        )
        run_git(repo_dir, "reset", "--hard", "HEAD^")
        return CandidateResult(
            candidate.source_pr_number, candidate.source_pr_title,
            "skipped-conflict",
            f"rejected after over-application: {issue}",
        )

    return CandidateResult(
        candidate.source_pr_number,
        candidate.source_pr_title,
        "applied",
        "conflicts resolved by Claude Code",
    )


def has_staged_changes(repo_dir: str, *, run_process: RunProcess = subprocess.run) -> bool:
    result = run_process(
        ["git", "diff", "--cached", "--quiet"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    raise RuntimeError(
        "could not inspect staged changes: "
        + ((result.stderr or "").strip()[:300] or "git diff failed")
    )


def index_stage_exists(
    repo_dir: str,
    path: str,
    stage: int,
    *,
    run_process: RunProcess = subprocess.run,
) -> bool:
    result = run_process(
        ["git", "cat-file", "-e", f":{stage}:{path}"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    return result.returncode == 0


def check_applied_commit_size(
    repo_dir: str,
    candidate: ProjectBackportCandidate,
    *,
    run_process: RunProcess = subprocess.run,
) -> str | None:
    source_sha = candidate.merge_commit_sha or (candidate.commit_shas[0] if candidate.commit_shas else None)
    if not source_sha:
        return None

    run_process(
        ["git", "fetch", "origin", source_sha],
        cwd=repo_dir, capture_output=True, text=True, check=False,
    )
    # Diff against the first parent rather than `git show --stat`, which emits
    # no diffstat for merge commits and would silently disable this guard.
    upstream_stats = run_process(
        ["git", "diff", "--stat", f"{source_sha}^1", source_sha],
        cwd=repo_dir, capture_output=True, text=True, check=False,
    )
    upstream_additions = parse_additions_from_stat(upstream_stats.stdout)
    if upstream_additions <= 0:
        return None

    applied_stats = run_process(
        ["git", "show", "--stat", "--format=", "HEAD"],
        cwd=repo_dir, capture_output=True, text=True, check=False,
    )
    applied_additions = parse_additions_from_stat(applied_stats.stdout)

    extra = applied_additions - upstream_additions
    ratio = applied_additions / upstream_additions
    over_applied = (applied_additions >= upstream_additions * 3 and extra > 100) or extra > 300
    if not over_applied:
        return None
    return (
        f"applied +{applied_additions} vs upstream +{upstream_additions} "
        f"(+{extra} extra lines, {ratio:.1f}x)"
    )


def parse_additions_from_stat(stat_output: str) -> int:
    match = re.search(r"(\d+) insertion", stat_output)
    return int(match.group(1)) if match else 0


def read_index_stage(
    repo_dir: str,
    path: str,
    stage: int,
    *,
    run_process: RunProcess = subprocess.run,
) -> str:
    result = run_process(
        ["git", "show", f":{stage}:{path}"],
        cwd=repo_dir, capture_output=True, text=True, errors="replace",
    )
    return result.stdout if result.returncode == 0 else ""
