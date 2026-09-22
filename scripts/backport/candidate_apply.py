"""Apply one backport candidate to a local target branch."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from scripts.backport.conflict_resolver import resolve_conflicts_with_claude
from scripts.backport.git_commands import (
    has_staged_changes,
    head_sha,
    index_stage_exists,
    read_index_stage,
)
from scripts.backport.git_commands import (
    run_git as run_git_default,
)
from scripts.backport.missing_test_adaptation import (
    MissingTestAdaptationResult,
    adapt_target_missing_tests_with_claude,
    build_missing_test_context,
    is_test_path,
)
from scripts.backport.models import (
    DETAIL_DROPPED_TARGET_MISSING_TEST_PREFIX,
    DETAIL_EMPTY_ON_TARGET,
    DETAIL_RESOLVED_BY_AI,
    BackportCandidate,
    CandidateOutcome,
    CandidateResult,
    ConflictedFile,
    ResolutionResult,
)
from scripts.backport.source_plan import (
    SourceChangeError,
    SourceChangePlan,
    prepare_source_change,
)
from scripts.backport.sweep_git import (
    changed_paths_in_index_or_worktree,
    untracked_paths,
)
from scripts.backport.validation import select_validation_commands
from scripts.common.logging_utils import compact_log_value, log_highlight

logger = logging.getLogger(__name__)

RunGit = Callable[..., Any]
RunProcess = Callable[..., subprocess.CompletedProcess[str]]
ResolveConflicts = Callable[..., list[ResolutionResult]]


AdaptMissingTests = Callable[..., MissingTestAdaptationResult]


def _abort_cherry_pick(repo_dir: str, run_process: RunProcess) -> bool:
    """Abort any in-progress cherry-pick, guaranteeing a clean tree.

    A cherry-pick can fail before creating sequencer state (e.g. an
    untracked file would be overwritten), in which case ``--abort`` itself
    exits non-zero. Callers on their happy path (empty pick, no-net-change)
    continue applying further commits, so they must never proceed on a
    dirty tree: when the abort fails, fall back to ``reset --hard HEAD``,
    which clears CHERRY_PICK_HEAD and any unmerged index while preserving
    applied commits and untracked files.

    Returns ``True`` when the tree is verifiably clean. ``False`` means
    both cleanup commands failed; the caller must treat the candidate as
    an error, not continue or report success.
    """
    result = run_process(
        ["git", "cherry-pick", "--abort"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True
    reset = run_process(
        ["git", "reset", "--hard", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if reset.returncode == 0:
        return True
    logger.error(
        "Could not clean worktree after failed cherry-pick: abort rc=%d, "
        "reset rc=%d: %s",
        result.returncode,
        reset.returncode,
        (reset.stderr or "").strip()[:300],
    )
    return False


_DIRTY_TREE_ERROR = (
    "could not clean the worktree after a failed cherry-pick; "
    "aborting this candidate"
)


def _empty_skip_reason(
    conflicting_files: list[ConflictedFile],
    resolutions: list[ResolutionResult],
) -> str:
    """A deterministic reason a resolved cherry-pick produced no net change.

    Derived only from provable facts, never from the resolver's prose. When the
    resolution of every conflicted file matched the target branch's existing
    content, the source PR's change does not apply on this branch (the code it
    modifies differs or is absent here), so the cherry-pick is a no-op.
    """
    target_by_path = {cf.path: cf.target_branch_content for cf in conflicting_files}
    matched_target = [
        r.path
        for r in resolutions
        if r.resolved_content is not None and r.path in target_by_path and r.resolved_content == target_by_path[r.path]
    ]
    if matched_target and len(matched_target) == len([r for r in resolutions if r.resolved_content is not None]):
        return (
            "The change does not apply to this branch: resolving the conflict "
            "matched the existing code, so the cherry-pick added nothing."
        )
    return "The cherry-pick produced no net change on this branch, so there is nothing to backport."


@dataclass
class _ApplyState:
    """Everything a partially applied candidate has accumulated.

    Owned by ``apply_candidate`` so its failure handler can report the
    conflicts and resolutions gathered before an unexpected error, and
    roll back to ``starting_head``.
    """

    starting_head: str
    starting_worktree_paths: set[str] = field(default_factory=set)
    starting_untracked_files: dict[str, bytes] = field(default_factory=dict)
    applied_commits: list[str] = field(default_factory=list)
    conflicts: list[ConflictedFile] = field(default_factory=list)
    resolutions: list[ResolutionResult] = field(default_factory=list)
    conflict_paths_seen: set[str] = field(default_factory=set)
    detail_parts: list[str] = field(default_factory=list)


def apply_candidate(
    repo_dir: str,
    candidate: BackportCandidate,
    repo_full_name: str,
    git_env: dict[str, str],
    *,
    language: str = "c",
    build_commands: list[str] | None = None,
    validation_rules: list[Any] | None = None,
    test_path_patterns: tuple[str, ...] | list[str] | None = None,
    max_conflicting_files: int = 100,
    run_git: RunGit = run_git_default,
    resolve_conflicts: ResolveConflicts = resolve_conflicts_with_claude,
    adapt_missing_tests: AdaptMissingTests | None = None,
    run_process: RunProcess = subprocess.run,
    source_plan: SourceChangePlan | None = None,
) -> CandidateResult:
    """Apply a complete candidate, rolling back on any failure."""

    if adapt_missing_tests is None:
        adapt_missing_tests = adapt_target_missing_tests_with_claude

    try:
        plan = source_plan or prepare_source_change(
            repo_dir,
            candidate.source_pr_number,
            candidate.merge_commit_sha,
            candidate.commit_shas,
            source_commits_complete=candidate.source_commits_complete,
            git_env=git_env,
        )
    except (SourceChangeError, subprocess.CalledProcessError) as exc:
        return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "error", str(exc))

    log_highlight(
        logger,
        "BACKPORT ATTEMPT: PR #%d | %s | source=%s | plan=%s | commits=%d",
        candidate.source_pr_number,
        compact_log_value(candidate.source_pr_title),
        repo_full_name,
        plan.strategy,
        len(plan.commits),
    )
    try:
        state = _ApplyState(
            head_sha(repo_dir, run_process=run_process),
            starting_worktree_paths=set(
                changed_paths_in_index_or_worktree(
                    repo_dir,
                    run_process=run_process,
                )
            ),
            starting_untracked_files=_snapshot_untracked_files(
                repo_dir,
                untracked_paths(repo_dir, run_process=run_process),
            ),
        )
    except RuntimeError as exc:
        return _application_result(candidate, "error", str(exc))

    try:
        result = _apply_plan(
            repo_dir,
            candidate,
            plan,
            state,
            language=language,
            build_commands=build_commands,
            validation_rules=validation_rules,
            test_path_patterns=test_path_patterns,
            max_conflicting_files=max_conflicting_files,
            run_git=run_git,
            resolve_conflicts=resolve_conflicts,
            adapt_missing_tests=adapt_missing_tests,
            run_process=run_process,
        )
        if result.outcome == "applied":
            amended_sha = _add_source_pr_trailer(
                repo_dir,
                candidate.source_pr_number,
                run_process=run_process,
            )
            result.resolved_commit_sha = amended_sha
        return result
    except Exception as exc:  # noqa: BLE001 - never strand a partial candidate
        detail = f"unexpected failure while applying: {str(exc)[:300]}"
        worktree_restored = True
        try:
            _abort_and_rollback(
                repo_dir,
                state.starting_head,
                run_git,
                run_process,
                state.starting_untracked_files,
            )
        except Exception as cleanup_exc:  # noqa: BLE001 - preserve both failures
            worktree_restored = False
            detail += f"; cleanup failed: {str(cleanup_exc)[:300]}"
        return _application_result(
            candidate,
            "error",
            detail,
            resolutions=state.resolutions,
            conflicting_files=state.conflicts,
            worktree_restored=worktree_restored,
        )


def _apply_plan(
    repo_dir: str,
    candidate: BackportCandidate,
    plan: SourceChangePlan,
    state: _ApplyState,
    *,
    language: str,
    build_commands: list[str] | None,
    validation_rules: list[Any] | None,
    test_path_patterns: tuple[str, ...] | list[str] | None,
    max_conflicting_files: int,
    run_git: RunGit,
    resolve_conflicts: ResolveConflicts,
    adapt_missing_tests: AdaptMissingTests,
    run_process: RunProcess,
) -> CandidateResult:
    last_resolved_sha: str | None = None
    no_change_reason = ""
    adapted_by_ai = False
    ai_summaries: list[str] = []
    first_conflicting_sha: str | None = None

    for index, sha in enumerate(plan.commits):
        command = ["git", "cherry-pick", sha]
        if plan.strategy == "merge" and index == 0:
            command[2:2] = ["-m", "1"]
        result = run_process(
            command,
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            state.applied_commits.append(sha)
            continue

        conflict_result = run_process(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if conflict_result.returncode != 0:
            _abort_and_rollback(
                repo_dir, state.starting_head, run_git, run_process,
                state.starting_untracked_files,
            )
            return _application_result(
                candidate,
                "error",
                "could not inspect cherry-pick conflicts: "
                + ((conflict_result.stderr or "").strip()[:300] or "git diff failed"),
            )

        conflicting_paths = [
            line.strip()
            for line in conflict_result.stdout.splitlines()
            if line.strip()
        ]
        if not conflicting_paths:
            cleaned = _abort_cherry_pick(repo_dir, run_process)
            if cleaned and _is_empty_cherry_pick(result):
                continue
            run_git(repo_dir, "reset", "--hard", state.starting_head)
            if not cleaned and _is_empty_cherry_pick(result):
                return _application_result(candidate, "error", _DIRTY_TREE_ERROR)
            return _application_result(
                candidate,
                "error",
                f"cherry-pick failed: {(result.stderr or result.stdout).strip()[:500]}",
            )

        logger.info(
            "Found %d conflict(s) while applying %s: %s",
            len(conflicting_paths),
            sha,
            conflicting_paths,
        )
        if first_conflicting_sha is None:
            first_conflicting_sha = sha
        conflicting_files: list[ConflictedFile] = []
        binary_conflicts: list[ConflictedFile] = []
        target_missing_paths: set[str] = set()
        target_missing_test_contexts: dict[str, str] = {}
        for path in conflicting_paths:
            target_content = read_index_stage(
                repo_dir,
                path,
                2,
                run_process=run_process,
            )
            source_content = read_index_stage(
                repo_dir,
                path,
                3,
                run_process=run_process,
            )
            conflicted_file = ConflictedFile(
                path=path,
                target_branch_content=target_content,
                source_branch_content=source_content,
            )
            if "\x00" in target_content or "\x00" in source_content:
                logger.warning("Cannot resolve binary conflict: %s", path)
                binary_conflicts.append(conflicted_file)
                continue
            if not index_stage_exists(
                repo_dir,
                path,
                2,
                run_process=run_process,
            ):
                target_missing_paths.add(path)
                if is_test_path(path, test_path_patterns):
                    target_missing_test_contexts[path] = build_missing_test_context(
                        repo_dir,
                        path,
                        source_content,
                        run_process=run_process,
                    )
            conflicting_files.append(conflicted_file)

        if binary_conflicts:
            state.conflicts.extend([*conflicting_files, *binary_conflicts])
            state.conflict_paths_seen.update(
                item.path for item in state.conflicts
            )
            _abort_and_rollback(
                repo_dir, state.starting_head, run_git, run_process,
                state.starting_untracked_files,
            )
            paths = ", ".join(item.path for item in binary_conflicts)
            return _application_result(
                candidate,
                "skipped-conflict",
                "binary file conflict(s) cannot be resolved automatically: "
                + paths,
                conflicting_files=state.conflicts,
            )

        state.conflicts.extend(conflicting_files)
        state.conflict_paths_seen.update(item.path for item in conflicting_files)
        if len(state.conflict_paths_seen) > max_conflicting_files:
            _abort_and_rollback(
                repo_dir, state.starting_head, run_git, run_process,
                state.starting_untracked_files,
            )
            detail = (
                f"Too many conflicting files ({len(state.conflict_paths_seen)} > "
                f"max_conflicting_files={max_conflicting_files}). "
                "Refusing to invoke conflict resolver."
            )
            return _application_result(
                candidate,
                "skipped-conflict",
                detail,
                conflicting_files=state.conflicts,
            )

        if target_missing_paths:
            non_test_missing_paths = sorted(
                path
                for path in target_missing_paths
                if not is_test_path(path, test_path_patterns)
            )
            if non_test_missing_paths:
                _abort_and_rollback(
                    repo_dir, state.starting_head, run_git, run_process,
                    state.starting_untracked_files,
                )
                paths = ", ".join(non_test_missing_paths)
                return _application_result(
                    candidate,
                    "skipped-conflict",
                    f"target branch lacks conflicted file(s): {paths}",
                    conflicting_files=state.conflicts,
                )

            for path in sorted(target_missing_paths):
                logger.info(
                    "Dropping target-missing test file from cherry-pick: %s",
                    path,
                )
                run_git(
                    repo_dir,
                    "rm",
                    "-f",
                    "--ignore-unmatch",
                    "--",
                    path,
                )
            conflicting_paths = [
                path
                for path in conflicting_paths
                if path not in target_missing_paths
            ]
            conflicting_files = [
                item
                for item in conflicting_files
                if item.path not in target_missing_paths
            ]

        resolutions: list[ResolutionResult] = []
        if conflicting_files:
            resolver_validation_commands = select_validation_commands(
                build_commands or [],
                validation_rules or [],
                conflicting_paths,
            )
            worktree_paths = set(changed_paths_in_index_or_worktree(
                repo_dir,
                run_process=run_process,
            )) - state.starting_worktree_paths
            allowed_resolution_paths = sorted(
                set(conflicting_paths) | set(worktree_paths)
            )
            resolutions = resolve_conflicts(
                repo_dir,
                conflicting_files,
                candidate.to_pr_context(),
                language=language,
                build_commands=resolver_validation_commands or None,
                allowed_paths=allowed_resolution_paths,
            )
            state.resolutions.extend(resolutions)

        unresolved = [
            resolution
            for resolution in resolutions
            if resolution.resolved_content is None
        ]
        if unresolved:
            _abort_and_rollback(
                repo_dir, state.starting_head, run_git, run_process,
                state.starting_untracked_files,
            )
            details = "; ".join(
                f"{item.path}: {(item.resolution_summary or 'unresolved')[:200]}"
                for item in unresolved
            )
            return _application_result(
                candidate,
                "skipped-conflict",
                f"unresolved - {details}",
                resolutions=state.resolutions,
                conflicting_files=state.conflicts,
            )

        for resolution in resolutions:
            if resolution.resolved_content is None:
                continue
            resolved_path = Path(repo_dir, resolution.path)
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            resolved_path.write_text(
                resolution.resolved_content,
                encoding="utf-8",
            )
            run_git(repo_dir, "add", resolution.path)

        test_adaptation = MissingTestAdaptationResult()
        if target_missing_test_contexts:
            try:
                test_adaptation = adapt_missing_tests(
                    repo_dir,
                    candidate,
                    target_missing_test_contexts,
                    language=language,
                    test_path_patterns=test_path_patterns,
                    run_git=run_git,
                    run_process=run_process,
                )
            except Exception as exc:  # noqa: BLE001 - adapter failures fail closed
                test_adaptation = MissingTestAdaptationResult(
                    summary=(
                        "test adaptation failed unexpectedly: "
                        f"{str(exc)[:200]}"
                    ),
                    fatal=True,
                )
            if test_adaptation.fatal:
                _abort_and_rollback(
                    repo_dir, state.starting_head, run_git, run_process,
                    state.starting_untracked_files,
                )
                return _application_result(
                    candidate,
                    "skipped-conflict",
                    test_adaptation.summary,
                    resolutions=state.resolutions,
                    conflicting_files=state.conflicts,
                )
            if not test_adaptation.adapted_paths:
                _abort_and_rollback(
                    repo_dir, state.starting_head, run_git, run_process,
                    state.starting_untracked_files,
                )
                return _application_result(
                    candidate,
                    "skipped-conflict",
                    test_adaptation.summary
                    or "test adaptation not applied: no branch-native test changes",
                    resolutions=state.resolutions,
                    conflicting_files=state.conflicts,
                )
            adapted_by_ai = adapted_by_ai or bool(
                test_adaptation.adapted_paths
            )
            if test_adaptation.adapted_paths:
                state.resolutions.extend(test_adaptation.resolutions)
                _append_detail(ai_summaries, test_adaptation.summary)

        if _has_llm_resolutions(resolutions):
            _append_detail(state.detail_parts, DETAIL_RESOLVED_BY_AI)
        if target_missing_paths:
            paths = ", ".join(sorted(target_missing_paths))
            _append_detail(
                state.detail_parts,
                f"{DETAIL_DROPPED_TARGET_MISSING_TEST_PREFIX} {paths}",
            )
        if test_adaptation.summary:
            _append_detail(state.detail_parts, test_adaptation.summary)

        if not has_staged_changes(repo_dir, run_process=run_process):
            if not _abort_cherry_pick(repo_dir, run_process):
                _abort_and_rollback(
                    repo_dir, state.starting_head, run_git, run_process,
                    state.starting_untracked_files,
                )
                return _application_result(
                    candidate,
                    "error",
                    _DIRTY_TREE_ERROR,
                    resolutions=state.resolutions,
                    conflicting_files=state.conflicts,
                )
            if target_missing_paths:
                paths = ", ".join(sorted(target_missing_paths))
                no_change_reason = (
                    "Only target-missing test file(s) were absent on this "
                    f"branch: {paths}"
                )
            else:
                no_change_reason = _empty_skip_reason(
                    conflicting_files,
                    resolutions,
                )
            continue

        commit_result = run_process(
            ["git", "-c", "core.editor=true", "cherry-pick", "--continue"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if commit_result.returncode != 0:
            output = f"{commit_result.stdout}\n{commit_result.stderr}"
            if "nothing to commit" in output.lower():
                if not _abort_cherry_pick(repo_dir, run_process):
                    _abort_and_rollback(
                        repo_dir, state.starting_head, run_git, run_process,
                        state.starting_untracked_files,
                    )
                    return _application_result(
                        candidate,
                        "error",
                        _DIRTY_TREE_ERROR,
                        resolutions=state.resolutions,
                        conflicting_files=state.conflicts,
                    )
                no_change_reason = (
                    "The cherry-pick produced no net change on this branch, "
                    "so there is nothing to backport."
                )
                continue
            _abort_and_rollback(
                repo_dir, state.starting_head, run_git, run_process,
                state.starting_untracked_files,
            )
            return _application_result(
                candidate,
                "skipped-conflict",
                f"commit failed: {output.strip()[:200]}",
                resolutions=state.resolutions,
                conflicting_files=state.conflicts,
            )

        last_resolved_sha = head_sha(
            repo_dir,
            run_process=run_process,
        )

        state.applied_commits.append(sha)

    if not state.applied_commits:
        detail = (
            DETAIL_EMPTY_ON_TARGET
            if no_change_reason
            else "already applied or empty cherry-pick"
        )
        return _application_result(
            candidate,
            "skipped-existing",
            detail,
            resolutions=state.resolutions,
            resolved_by_ai=(
                adapted_by_ai or _has_llm_resolutions(state.resolutions)
            ),
            skip_reason=no_change_reason,
            conflicting_files=state.conflicts,
            ai_summary="; ".join(ai_summaries),
        )

    return _application_result(
        candidate,
        "applied",
        "; ".join(state.detail_parts),
        resolutions=state.resolutions,
        resolved_by_ai=(
            adapted_by_ai or _has_llm_resolutions(state.resolutions)
        ),
        resolved_commit_sha=last_resolved_sha,
        applied_commits=state.applied_commits,
        conflicting_files=state.conflicts,
        conflicting_commit_sha=first_conflicting_sha,
        ai_summary="; ".join(ai_summaries),
    )


def _application_result(
    candidate: BackportCandidate,
    outcome: CandidateOutcome,
    detail: str,
    *,
    resolutions: list[ResolutionResult] | None = None,
    resolved_by_ai: bool = False,
    skip_reason: str = "",
    resolved_commit_sha: str | None = None,
    applied_commits: list[str] | None = None,
    conflicting_files: list[ConflictedFile] | None = None,
    conflicting_commit_sha: str | None = None,
    ai_summary: str = "",
    worktree_restored: bool = True,
) -> CandidateResult:
    return CandidateResult(
        source_pr_number=candidate.source_pr_number,
        source_pr_title=candidate.source_pr_title,
        outcome=outcome,
        detail=detail,
        resolutions=list(resolutions or []),
        resolved_by_ai=resolved_by_ai,
        skip_reason=skip_reason,
        resolved_commit_sha=resolved_commit_sha,
        applied_commits=list(applied_commits or []),
        conflicting_files=list(conflicting_files or []),
        conflicting_commit_sha=conflicting_commit_sha,
        ai_summary=ai_summary,
        worktree_restored=worktree_restored,
    )


def _abort_and_rollback(
    repo_dir: str,
    starting_head: str,
    run_git: RunGit,
    run_process: RunProcess,
    starting_untracked_files: dict[str, bytes],
) -> None:
    _abort_cherry_pick(repo_dir, run_process)
    run_git(repo_dir, "reset", "--hard", starting_head)
    created_paths = sorted(
        set(untracked_paths(repo_dir, run_process=run_process))
        - set(starting_untracked_files)
    )
    if created_paths:
        run_git(repo_dir, "clean", "-f", "--", *created_paths)
    for path, content in starting_untracked_files.items():
        destination = _safe_restore_path(Path(repo_dir), path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def _snapshot_untracked_files(
    repo_dir: str,
    paths: tuple[str, ...],
) -> dict[str, bytes]:
    snapshots: dict[str, bytes] = {}
    root = Path(repo_dir).resolve()
    for path in paths:
        if Path(root, path).is_symlink():
            continue
        candidate = _safe_restore_path(root, path)
        if not candidate.is_file():
            continue
        snapshots[path] = candidate.read_bytes()
    return snapshots


def _safe_restore_path(root: Path, relative_path: str) -> Path:
    normalized = Path(*relative_path.replace("\\", "/").split("/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise RuntimeError(f"unsafe worktree path: {relative_path}")
    root = root.resolve()
    current = root
    for part in normalized.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"symlinked worktree parent: {relative_path}")
    destination = root / normalized
    if destination.is_symlink():
        destination.unlink()
    return destination


def _append_detail(parts: list[str], detail: str) -> None:
    if detail and detail not in parts:
        parts.append(detail)


def _has_llm_resolutions(resolutions: list[ResolutionResult]) -> bool:
    return any(
        resolution.resolved_content is not None
        and resolution.source == "llm"
        for resolution in resolutions
    )


def _add_source_pr_trailer(
    repo_dir: str,
    source_pr_number: int,
    *,
    run_process: RunProcess = subprocess.run,
) -> str:
    """Persist source-PR identity independently of repository merge style."""
    result = run_process(
        [
            "git",
            "-c",
            "core.editor=true",
            "commit",
            "--amend",
            "--no-edit",
            "--trailer",
            f"Backport-Source-PR: {source_pr_number}",
        ],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "could not record source PR identity: "
            + ((result.stderr or result.stdout).strip()[:300] or "git commit failed")
        )
    return head_sha(repo_dir, run_process=run_process)


def _is_empty_cherry_pick(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout}\n{result.stderr}".lower()
    return any(
        marker in output
        for marker in (
            "cherry-pick is now empty",
            "previous cherry-pick is now empty",
            "nothing to commit",
            "patch is empty",
        )
    )
