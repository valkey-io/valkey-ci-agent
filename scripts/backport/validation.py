"""Path-based validation command selection for backport branches."""

from __future__ import annotations

import subprocess
from fnmatch import fnmatch
from pathlib import Path
from shlex import quote
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from scripts.backport.registry import ValidationRule


def changed_paths_since_base(repo_dir: str, base_ref: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def select_validation_commands(
    base_commands: Iterable[str],
    validation_rules: Iterable["ValidationRule"],
    changed_paths: Iterable[str],
    *,
    validation_profile: str = "",
    repo_dir: str = "",
    base_ref: str = "",
) -> list[str]:
    """Return ordered, de-duplicated checks for one candidate diff.

    Registry rules remain the generic mechanism. A named profile may add
    deterministic repository-aware checks that cannot be expressed safely as
    static shell strings (for example, one command per changed Tcl test).
    """
    commands: list[str] = []
    seen: set[str] = set()
    paths = tuple(changed_paths)

    if validation_profile == "valkey-core":
        _append_commands(
            commands,
            seen,
            _valkey_fast_validation_commands(paths, repo_dir, base_ref),
        )
    for command in base_commands:
        if command not in seen:
            commands.append(command)
            seen.add(command)

    for rule in validation_rules:
        if not _rule_matches(rule.paths, paths):
            continue
        _append_commands(commands, seen, rule.commands)

    if validation_profile == "valkey-core":
        _append_commands(
            commands,
            seen,
            _valkey_test_validation_commands(paths, repo_dir),
        )
    return commands


def _rule_matches(patterns: Iterable[str], changed_paths: Iterable[str]) -> bool:
    return any(fnmatch(path, pattern) for path in changed_paths for pattern in patterns)


def _append_commands(commands: list[str], seen: set[str], additions: Iterable[str]) -> None:
    for command in additions:
        if command not in seen:
            commands.append(command)
            seen.add(command)


_C_FAMILY_SUFFIXES = (".c", ".h", ".cpp", ".hpp")


def _valkey_fast_validation_commands(
    changed_paths: tuple[str, ...],
    repo_dir: str,
    base_ref: str,
) -> tuple[str, ...]:
    diff_range = f" {quote(base_ref)}...HEAD" if base_ref else ""
    commands = [f"git diff --check{diff_range}"]
    c_family = tuple(
        path
        for path in changed_paths
        if path.endswith(_C_FAMILY_SUFFIXES) and _path_exists(repo_dir, path)
    )
    if c_family:
        arguments = " ".join(quote(path) for path in c_family)
        commands.append(f"clang-format-18 --dry-run --Werror -- {arguments}")
    return tuple(commands)


def _valkey_test_validation_commands(
    changed_paths: tuple[str, ...],
    repo_dir: str,
) -> tuple[str, ...]:
    commands: list[str] = []

    direct_test_paths = tuple(
        path
        for path in changed_paths
        if _is_direct_runtest(path) and _path_exists(repo_dir, path)
    )
    direct_tests = tuple(_runtest_unit(path) for path in direct_test_paths)
    regular_tests = tuple(
        (path, unit)
        for path, unit in zip(direct_test_paths, direct_tests)
        if not path.startswith("tests/unit/moduleapi/")
    )
    module_tests = tuple(
        (path, unit)
        for path, unit in zip(direct_test_paths, direct_tests)
        if path.startswith("tests/unit/moduleapi/")
    )
    for _path, unit in regular_tests:
        commands.append(f"./runtest --single {quote(unit)} --clients 1")
    for _path, unit in module_tests:
        commands.append(f"./runtest-moduleapi --single {quote(unit)} --clients 1")

    if any(path.startswith("src/unit/") for path in changed_paths):
        commands.append("make -C src test-unit")

    cluster_changed = any(
        path.startswith("tests/unit/cluster/")
        or fnmatch(path, "src/cluster*.c")
        or fnmatch(path, "src/cluster*.h")
        for path in changed_paths
    )
    if cluster_changed and not any(unit.startswith("unit/cluster/") for unit in direct_tests):
        commands.append("./runtest-cluster --single unit/cluster/base --clients 1")

    if any(
        path.startswith("tests/sentinel/")
        or path in {"src/sentinel.c", "src/sentinel.h"}
        for path in changed_paths
    ):
        commands.append("./runtest-sentinel")

    module_sources_changed = any(path.startswith("tests/modules/") for path in changed_paths)
    if module_sources_changed:
        commands.append("./runtest-moduleapi --clients 1")

    if any(path.startswith("tests/support/") or path == "tests/test_helper.tcl" for path in changed_paths):
        commands.append("./runtest --clients 1")

    reply_tests = tuple(
        unit
        for path, unit in regular_tests
        if not _top_level_skips_reply_logging(repo_dir, path)
    )
    reply_module_tests = tuple(
        unit
        for path, unit in module_tests
        if not _top_level_skips_reply_logging(repo_dir, path)
    )
    if reply_tests or reply_module_tests or module_sources_changed:
        commands.append("make -j$(nproc) BUILD_TLS=yes SERVER_CFLAGS='-Werror -DLOG_REQ_RES'")
        if reply_tests:
            units = " ".join(f"--single {quote(unit)}" for unit in reply_tests)
            commands.append(
                "./runtest "
                f"{units} --clients 1 --log-req-res --no-latency --dont-clean "
                "--force-resp3"
            )
        if reply_module_tests or module_sources_changed:
            units = " ".join(f"--single {quote(unit)}" for unit in reply_module_tests)
            commands.append(
                "CFLAGS='-Werror' ./runtest-moduleapi "
                f"{units} --clients 1 --log-req-res --no-latency --dont-clean "
                "--dont-pre-clean --force-resp3"
            )
        commands.append(
            "./utils/req-res-log-validator.py --verbose --fail-missing-reply-schemas"
        )

    return tuple(commands)


def _is_direct_runtest(path: str) -> bool:
    return path.endswith(".tcl") and (
        path.startswith("tests/unit/") or path.startswith("tests/integration/")
    )


def _runtest_unit(path: str) -> str:
    if not _is_direct_runtest(path):
        return ""
    return path.removeprefix("tests/").removesuffix(".tcl")


def _path_exists(repo_dir: str, path: str) -> bool:
    return not repo_dir or Path(repo_dir, path).is_file()


def _top_level_skips_reply_logging(repo_dir: str, path: str) -> bool:
    if not repo_dir:
        return False
    try:
        with Path(repo_dir, path).open(encoding="utf-8", errors="replace") as handle:
            prefix = "".join(next(handle, "") for _ in range(40))
    except OSError:
        return False
    return "logreqres:skip" in prefix
