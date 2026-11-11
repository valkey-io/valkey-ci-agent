"""Adapt upstream tests that do not exist on an older target branch."""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from scripts.ai.runtime import AgentRunResult, run_agent
from scripts.backport.git_commands import (
    index_stage_exists,
    read_index_stage,
)
from scripts.backport.git_commands import (
    run_git as run_git_default,
)
from scripts.backport.models import (
    DETAIL_PORTED_TARGET_MISSING_TEST_PREFIX,
    BackportCandidate,
    ResolutionResult,
)
from scripts.backport.utils import has_conflict_markers

logger = logging.getLogger(__name__)

MAX_TEST_CONTEXT_CHARS = 12000
MAX_EXISTING_TEST_PATHS = 120
DEFAULT_TEST_PATH_PATTERNS = (
    "tests/*.tcl",
    "src/unit/test_*.c",
    "src/unit/test_*.cc",
    "src/unit/test_*.cpp",
)

RunGit = Callable[..., Any]
RunProcess = Callable[..., subprocess.CompletedProcess[str]]
RunAgent = Callable[..., AgentRunResult]


@dataclass
class MissingTestAdaptationResult:
    adapted_paths: list[str] = field(default_factory=list)
    resolutions: list[ResolutionResult] = field(default_factory=list)
    summary: str = ""
    fatal: bool = False


@dataclass(frozen=True)
class FileSnapshot:
    state: str
    digest: str = ""
    content: bytes | None = None


def is_test_path(
    path: str,
    test_path_patterns: tuple[str, ...] | list[str] | None = None,
) -> bool:
    normalized = path.replace("\\", "/").strip("/").lower()
    patterns = test_path_patterns or DEFAULT_TEST_PATH_PATTERNS
    return any(
        fnmatch.fnmatchcase(normalized, pattern.replace("\\", "/").lower())
        for pattern in patterns
    )


def build_missing_test_context(
    repo_dir: str,
    path: str,
    source_content: str,
    *,
    run_process: RunProcess = subprocess.run,
) -> str:
    if index_stage_exists(repo_dir, path, 1, run_process=run_process):
        base_content = read_index_stage(
            repo_dir,
            path,
            1,
            run_process=run_process,
        )
        diff = "".join(
            difflib.unified_diff(
                base_content.splitlines(keepends=True),
                source_content.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        return "Changed upstream test hunk:\n" + (
            diff or "(no textual diff)"
        )
    return "Full upstream test content for a new missing test file:\n" + source_content


def adapt_target_missing_tests_with_claude(
    repo_dir: str,
    candidate: BackportCandidate,
    missing_test_sources: dict[str, str],
    *,
    language: str,
    test_path_patterns: tuple[str, ...] | list[str] | None = None,
    excluded_test_paths: Iterable[str] = (),
    run_git: RunGit = run_git_default,
    run_process: RunProcess = subprocess.run,
    run_agent_func: RunAgent = run_agent,
) -> MissingTestAdaptationResult:
    excluded = set(excluded_test_paths) | set(missing_test_sources)
    existing_test_paths = set(
        list_existing_test_paths(
            repo_dir,
            limit=None,
            test_path_patterns=test_path_patterns,
            run_process=run_process,
        )
    ) - excluded

    try:
        with tempfile.TemporaryDirectory(
            prefix="valkey-test-adaptation-",
        ) as temp_dir:
            prompt = build_test_adaptation_prompt(
                repo_dir,
                candidate,
                missing_test_sources,
                language=language,
                test_path_patterns=test_path_patterns,
                excluded_test_paths=excluded,
                run_process=run_process,
                # Beside the sandbox, not in it: nothing there is part of the adaptation.
                context_dir=Path(temp_dir, "upstream"),
            )
            sandbox_dir = Path(temp_dir, "repo")
            copy_worktree_for_adaptation(repo_dir, sandbox_dir)
            for path in sorted(excluded):
                candidate_path = safe_regular_file(sandbox_dir, path)
                if candidate_path is not None:
                    candidate_path.unlink()
            sandbox_before = snapshot_regular_files(sandbox_dir)

            logger.info(
                "Calling Claude Code to adapt %d target-missing test file(s) "
                "for PR #%d onto %s...",
                len(missing_test_sources),
                candidate.source_pr_number,
                candidate.target_branch,
            )
            agent_result = run_agent_func(
                "test_adaptation_edit_only",
                prompt,
                cwd=str(sandbox_dir),
            )
            result_text = extract_agent_result_text(agent_result)
            logger.info(
                "Claude Code test adaptation finished (rc=%d). Result: %s",
                agent_result.returncode,
                result_text[:200] if result_text else "(no result text)",
            )

            sandbox_after = snapshot_regular_files(sandbox_dir)
            changed_paths = changed_snapshot_paths(
                sandbox_before,
                sandbox_after,
            )
            if agent_result.returncode != 0:
                detail = (
                    agent_result.stderr
                    or result_text
                    or "Claude Code returned non-zero"
                )
                return MissingTestAdaptationResult(
                    summary=(
                        "test adaptation not applied: Claude Code failed: "
                        f"{detail[:200]}"
                    ),
                    fatal=True,
                )
            if not changed_paths:
                return MissingTestAdaptationResult(
                    summary=(
                        "test adaptation not applied: no branch-native "
                        "test changes"
                    ),
                )

            removed_paths = [
                path
                for path in changed_paths
                if path in sandbox_before
                and not Path(sandbox_dir, path).exists()
                and not Path(sandbox_dir, path).is_symlink()
            ]
            if removed_paths:
                return MissingTestAdaptationResult(
                    summary=(
                        "test adaptation not applied: removed existing test "
                        "path(s): " + ", ".join(removed_paths[:10])
                    ),
                    fatal=True,
                )

            invalid_paths = invalid_sandbox_test_paths(
                sandbox_dir,
                changed_paths,
                sandbox_before=sandbox_before,
                existing_test_paths=existing_test_paths,
                test_path_patterns=test_path_patterns,
            )
            if invalid_paths:
                return MissingTestAdaptationResult(
                    summary=(
                        "test adaptation not applied: invalid generated "
                        "test path(s): " + ", ".join(invalid_paths[:10])
                    ),
                    fatal=True,
                )

            unsafe_destination_paths = [
                path
                for path in changed_paths
                if safe_regular_file(Path(repo_dir), path) is None
            ]
            if unsafe_destination_paths:
                return MissingTestAdaptationResult(
                    summary=(
                        "test adaptation not applied: unsafe repository "
                        "test path(s): "
                        + ", ".join(unsafe_destination_paths[:10])
                    ),
                    fatal=True,
                )

            import_snapshots = {
                path: snapshot_path(
                    Path(repo_dir, path), include_content=True
                )
                for path in changed_paths
            }
            import_index_entries = index_entries_for_paths(
                repo_dir,
                set(changed_paths),
                run_process=run_process,
            )
            try:
                for path in changed_paths:
                    source = safe_regular_file(sandbox_dir, path)
                    destination = safe_regular_file(Path(repo_dir), path)
                    if source is None or destination is None:
                        raise RuntimeError(f"unsafe test adaptation path: {path}")
                    destination.write_bytes(source.read_bytes())
                    run_git(repo_dir, "add", path)
            except Exception as exc:  # noqa: BLE001
                restore_paths(
                    repo_dir,
                    import_snapshots,
                    index_entries=import_index_entries,
                    run_git=run_git,
                    run_process=run_process,
                )
                return MissingTestAdaptationResult(
                    summary=(
                        "test adaptation import failed: "
                        f"{str(exc)[:200]}"
                    ),
                    fatal=True,
                )

            return MissingTestAdaptationResult(
                adapted_paths=changed_paths,
                resolutions=[
                    _adaptation_resolution(
                        path,
                        (import_snapshots[path].content or b"").decode(
                            "utf-8",
                            errors="replace",
                        ),
                        Path(repo_dir, path).read_text(
                            encoding="utf-8",
                            errors="replace",
                        ),
                        result_text,
                    )
                    for path in changed_paths
                ],
                summary=(
                    f"{DETAIL_PORTED_TARGET_MISSING_TEST_PREFIX} "
                    + ", ".join(changed_paths)
                ),
            )
    except Exception as exc:  # noqa: BLE001
        return MissingTestAdaptationResult(
            summary=f"test adaptation failed unexpectedly: {str(exc)[:200]}",
            fatal=True,
        )


def build_test_adaptation_prompt(
    repo_dir: str,
    candidate: BackportCandidate,
    missing_test_sources: dict[str, str],
    *,
    language: str,
    test_path_patterns: tuple[str, ...] | list[str] | None = None,
    excluded_test_paths: Iterable[str] = (),
    run_process: RunProcess = subprocess.run,
    context_dir: Path | None = None,
) -> str:
    excluded = set(excluded_test_paths)
    source_sections = "\n\n".join(
        f"### Missing upstream test or harness file: {path}\n"
        f"```\n{_shown(content, index, path, context_dir)}\n```"
        for index, (path, content) in enumerate(sorted(missing_test_sources.items()))
    )
    existing_tests = "\n".join(
        f"- {path}"
        for path in list_existing_test_paths(
            repo_dir,
            test_path_patterns=test_path_patterns,
            run_process=run_process,
        )
        if path not in excluded
    )
    return (
        f"You are adapting test coverage for a {language} backport.\n\n"
        f'Source PR #{candidate.source_pr_number}: "{candidate.source_pr_title}"\n'
        f"URL: {candidate.source_pr_url}\n"
        f"Target branch: {candidate.target_branch}\n\n"
        f"The upstream PR changed test or test-harness file(s) that do not exist "
        f"on this target branch. The cherry-pick has already kept those missing "
        f"files absent. "
        f"Your task is to decide whether equivalent coverage can be added using "
        f"the target branch's existing test format.\n\n"
        f"Missing upstream test and harness context:\n{source_sections}\n\n"
        f"Existing test files on the target branch include:\n"
        f"{existing_tests or '- (none found)'}\n\n"
        f"CRITICAL constraints:\n"
        f"- Edit existing test files only. Do not edit source, build, workflow, "
        f"or metadata files.\n"
        f"- Prefer modifying an existing test file that matches the target "
        f"branch's conventions. Do not create new files.\n"
        f"- Preserve the source PR's test intent only. Do not add unrelated "
        f"coverage or new product behavior.\n"
        f"- Before using a helper, command, macro, fixture, or test harness, "
        f"verify it exists on this target branch.\n"
        f"- Do not recreate the missing upstream file path.\n"
        f"- Do not run `git add`, `git commit`, or any network command.\n"
        f"- If equivalent branch-native coverage is not practical, make no file "
        f"changes and explain that in your final result.\n\n"
        f"Do not wrap output in markdown. Edit files directly when safe."
    )


def _shown(content: str, index: int, path: str, context_dir: Path | None) -> str:
    """``content`` as the prompt shows it: whole, or its start and where the whole text is.

    The start alone was the first 12,000 characters, with nothing to say that
    a longer test file went on: the agent was asked for equivalent coverage of
    tests it never saw.
    """
    if len(content) <= MAX_TEST_CONTEXT_CHARS:
        return content
    shown = content[:MAX_TEST_CONTEXT_CHARS]
    if context_dir is None:
        return f"{shown}\n[... cut here: {len(content) - len(shown):,} more characters are not shown ...]"
    context_dir.mkdir(parents=True, exist_ok=True)
    whole = context_dir / f"{index:02d}-{Path(path).name}"
    whole.write_text(content, encoding="utf-8")
    return (
        f"{shown}\n[... cut here: this is the first {len(shown):,} of {len(content):,} characters. "
        f"Read the whole text at {whole} before you decide what coverage to add ...]"
    )


def list_existing_test_paths(
    repo_dir: str,
    *,
    limit: int | None = MAX_EXISTING_TEST_PATHS,
    test_path_patterns: tuple[str, ...] | list[str] | None = None,
    run_process: RunProcess = subprocess.run,
) -> list[str]:
    result = run_process(
        ["git", "ls-files"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    paths = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and is_test_path(line.strip(), test_path_patterns)
    ]
    return paths if limit is None else paths[:limit]


def extract_agent_result_text(agent_result: AgentRunResult) -> str:
    result_text = ""
    for line in agent_result.stdout.strip().splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if event.get("type") != "result" or "result" not in event:
            continue
        raw_result = event.get("result")
        if isinstance(raw_result, str):
            result_text = raw_result
        elif raw_result is not None:
            result_text = json.dumps(
                raw_result,
                sort_keys=True,
                default=str,
            )
    return result_text


def copy_worktree_for_adaptation(repo_dir: str, sandbox_dir: Path) -> None:
    shutil.copytree(
        repo_dir,
        sandbox_dir,
        ignore=_ignore_git_and_symlinks,
        symlinks=True,
    )


def _ignore_git_and_symlinks(directory: str, names: list[str]) -> set[str]:
    """Keep repository symlinks out of the agent's writable sandbox."""
    ignored = {".git"} if ".git" in names else set()
    ignored.update(
        name for name in names if Path(directory, name).is_symlink()
    )
    return ignored


def safe_regular_file(root: Path, relative_path: str) -> Path | None:
    """Return a non-symlink regular file contained by *root*, or ``None``."""
    normalized = Path(*relative_path.replace("\\", "/").split("/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        return None
    candidate = root / normalized
    current = root
    for part in normalized.parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def snapshot_regular_files(root: Path) -> dict[str, FileSnapshot]:
    snapshots: dict[str, FileSnapshot] = {}
    for path in root.rglob("*"):
        if not path.is_symlink() and path.is_file():
            snapshots[path.relative_to(root).as_posix()] = snapshot_path(path)
    return snapshots


def snapshot_path(path: Path, *, include_content: bool = False) -> FileSnapshot:
    """Snapshot a path for change detection or restoration.

    Change detection across a whole worktree only needs a digest, so file
    contents are hashed in chunks instead of being buffered in memory.
    ``include_content`` additionally captures the bytes; it is meant for the
    small set of paths that ``restore_paths`` may need to write back.
    """
    if not path.exists():
        return FileSnapshot("absent")
    if not path.is_file():
        return FileSnapshot("special")
    try:
        if include_content:
            content = path.read_bytes()
            return FileSnapshot(
                "file",
                hashlib.sha256(content).hexdigest(),
                content,
            )
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return FileSnapshot("file", digest.hexdigest())
    except OSError:
        return FileSnapshot("unreadable")


def changed_snapshot_paths(
    before: dict[str, FileSnapshot],
    after: dict[str, FileSnapshot],
) -> list[str]:
    return sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def _adaptation_resolution(
    path: str,
    before: str,
    after: str,
    summary: str,
) -> ResolutionResult:
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path} (target branch)",
            tofile=f"b/{path} (adapted test coverage)",
        )
    ).rstrip("\n")
    return ResolutionResult(
        path=path,
        resolved_content=after,
        resolution_summary="ported upstream test intent to a branch-native test",
        resolution_diff=diff or None,
        reviewer_diff=diff or None,
        llm_summary=summary or "Adapted upstream test coverage for the target branch.",
    )


def invalid_sandbox_test_paths(
    sandbox_dir: Path,
    changed_paths: list[str],
    *,
    sandbox_before: dict[str, FileSnapshot],
    existing_test_paths: set[str],
    test_path_patterns: tuple[str, ...] | list[str] | None = None,
) -> list[str]:
    invalid_paths = []
    for path in changed_paths:
        file_path = Path(sandbox_dir, path)
        if (
            path not in sandbox_before
            or path not in existing_test_paths
            or not is_test_path(path, test_path_patterns)
        ):
            invalid_paths.append(path)
            continue
        if safe_regular_file(sandbox_dir, path) is None:
            invalid_paths.append(path)
            continue
        content = file_path.read_text(encoding="utf-8", errors="replace")
        if has_conflict_markers(content):
            invalid_paths.append(path)
    return invalid_paths


def index_entries_for_paths(
    repo_dir: str,
    paths: set[str],
    *,
    run_process: RunProcess = subprocess.run,
) -> dict[str, tuple[str, ...]]:
    entries: dict[str, tuple[str, ...]] = {}
    for path in sorted(paths):
        result = run_process(
            ["git", "ls-files", "--stage", "--", path],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"could not inspect index entry for {path}: "
                + (
                    (result.stderr or "").strip()[:300]
                    or "git ls-files failed"
                )
            )
        entries[path] = tuple(
            line for line in result.stdout.splitlines() if line
        )
    return entries


def restore_index_entries(
    repo_dir: str,
    entries_by_path: dict[str, tuple[str, ...]],
    *,
    run_git: RunGit = run_git_default,
    run_process: RunProcess = subprocess.run,
) -> None:
    for path, entries in entries_by_path.items():
        current_entries = index_entries_for_paths(
            repo_dir,
            {path},
            run_process=run_process,
        ).get(path, ())
        if current_entries:
            run_git(repo_dir, "reset", "-q", "HEAD", "--", path)
        if not entries:
            continue
        result = run_process(
            ["git", "update-index", "--index-info"],
            cwd=repo_dir,
            input="\n".join(entries) + "\n",
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"could not restore index entry for {path}: "
                + (
                    (result.stderr or "").strip()[:300]
                    or "git update-index failed"
                )
            )


def restore_paths(
    repo_dir: str,
    snapshots: dict[str, FileSnapshot],
    *,
    index_entries: dict[str, tuple[str, ...]],
    run_git: RunGit = run_git_default,
    run_process: RunProcess = subprocess.run,
) -> None:
    for path, snapshot in snapshots.items():
        file_path = Path(repo_dir, path)
        if snapshot.state == "file" and snapshot.content is not None:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(snapshot.content)
        elif snapshot.state == "absent" and file_path.exists():
            if file_path.is_dir():
                shutil.rmtree(file_path)
            else:
                file_path.unlink()
    restore_index_entries(
        repo_dir,
        index_entries,
        run_git=run_git,
        run_process=run_process,
    )
