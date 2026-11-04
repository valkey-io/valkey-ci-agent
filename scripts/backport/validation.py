"""Path-based validation command selection for backport branches."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from shlex import quote
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from scripts.backport.registry import ValidationRule

UNMAPPED_TEST_PATHS_PREFIX = "BACKPORT_UNMAPPED_TEST_PATHS="
SKIP_REQ_RES_VALIDATION_ENV = "BACKPORT_SKIP_REQ_RES_VALIDATION"

logger = logging.getLogger(__name__)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


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
_UNIT_TEST_SOURCE_SUFFIXES = (".c", ".cc", ".cpp")
# Valkey's clang-format workflow runs inside `src/`, and the only
# `.clang-format` in the tree is `src/.clang-format`. Files outside `src/` -
# `tests/modules/*.c`, anything vendored under `deps/` - are therefore never
# formatted upstream, and checking them here would fall back to clang's
# built-in LLVM style and fail a candidate whose upstream CI is green.
_FORMATTED_PREFIX = "src/"
_DIFF_HUNK_RE = re.compile(
    r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@"
)
_CLANG_FORMAT_INVOCATION_RE = re.compile(
    r"^[ \t]*(?:run:[ \t]*)?clang-format(?:-18)?[ \t]+-i(?P<args>[^\n]*)$",
    re.MULTILINE,
)


def _valkey_fast_validation_commands(
    changed_paths: tuple[str, ...],
    repo_dir: str,
    base_ref: str,
) -> tuple[str, ...]:
    """Return the cheap checks that should fail a candidate before any build."""
    diff_range = f" {quote(base_ref)}...HEAD" if base_ref else ""
    commands = [f"git diff --check{diff_range}"]
    formatted_suffixes = _valkey_clang_format_suffixes(repo_dir)
    c_family = tuple(
        path
        for path in changed_paths
        if path.startswith(_FORMATTED_PREFIX)
        and path.endswith(formatted_suffixes)
        and _path_exists(repo_dir, path)
    )
    if (
        formatted_suffixes
        and c_family
        and _path_exists(repo_dir, "src/.clang-format")
    ):
        for path in c_family:
            line_ranges = _changed_line_ranges(repo_dir, base_ref, path)
            if line_ranges == ():
                # A deletion-only change introduces no new line to format.
                continue
            range_args = (
                ""
                if line_ranges is None
                else " " + " ".join(
                    f"--lines={start}:{end}" for start, end in line_ranges
                )
            )
            commands.append(
                f"clang-format --dry-run --Werror{range_args} -- {quote(path)}"
            )
    return tuple(commands)


def _changed_line_ranges(
    repo_dir: str,
    base_ref: str,
    path: str,
) -> tuple[tuple[int, int], ...] | None:
    """Return new-file line ranges changed by ``base_ref...HEAD``.

    ``None`` means no comparison range was supplied, so callers should retain
    the historical whole-file check. An empty tuple means a real diff contained
    only deletions and therefore introduced no line for clang-format to judge.
    """
    if not repo_dir or not base_ref:
        return None
    result = subprocess.run(
        [
            "git",
            "diff",
            "--unified=0",
            f"{base_ref}...HEAD",
            "--",
            path,
        ],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    ranges: list[tuple[int, int]] = []
    for line in result.stdout.splitlines():
        match = _DIFF_HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group("start"))
        count = int(match.group("count") or "1")
        if count:
            ranges.append((start, start + count - 1))
    return tuple(ranges)


def _valkey_clang_format_suffixes(repo_dir: str) -> tuple[str, ...]:
    """Return exactly the suffixes formatted by this release branch's CI.

    Valkey 8.0 has ``src/.clang-format`` but no formatting workflow, while 8.1
    and 9.0 format only C headers/sources and newer branches also format C++.
    The workflow is therefore the capability signal; the style file alone
    cannot distinguish those release layouts.
    """
    if not repo_dir:
        return _C_FAMILY_SUFFIXES

    workflows_dir = Path(repo_dir, ".github/workflows")
    suffixes: set[str] = set()
    workflows = sorted(
        (*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml"))
    )
    for workflow in workflows:
        try:
            body = workflow.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for invocation in _CLANG_FORMAT_INVOCATION_RE.finditer(body):
            args = invocation.group("args")
            suffixes.update(
                suffix
                for suffix in _C_FAMILY_SUFFIXES
                if f"**/*{suffix}" in args
            )
    return tuple(
        suffix for suffix in _C_FAMILY_SUFFIXES if suffix in suffixes
    )


_MODULE_SOURCE_PREFIX = "tests/modules/"
_TLS_ONLY_UNITS = frozenset({"unit/tls"})

# A `tests/support/*` or `tests/test_helper.tcl` change has to prove the harness
# still loads and can drive servers; it does not need all 170 default units.
# Upstream budgets 24 hours and three shards for one serialized whole-suite run
# (valkey `.github/workflows/daily.yml`), which cannot fit the per-command cap in
# `scripts/common/build_validator.py`. These five units are ~3% of the suite and
# between them cover the fragile helpers: `csvdump`/`debug_digest`/
# `restart_server` (other), `assert_encoding` plus a non-slow `start_cluster`
# block that reaches `cluster_util.tcl` (keyspace), `attach_to_replication_stream`
# and nested `start_server` (dump), `valkey.tcl`/`response_transformers.tcl`
# parsing (protocol), and dense cheap round-trips (type/incr). All five exist on
# every branch in the registry.
_HARNESS_SMOKE_UNITS = (
    "unit/other",
    "unit/keyspace",
    "unit/dump",
    "unit/protocol",
    "unit/type/incr",
)


def _moduleapi_units_for_module_sources(
    repo_dir: str,
    changed_paths: tuple[str, ...],
) -> tuple[tuple[str, ...], bool]:
    """Map changed ``tests/modules/*.c`` onto the moduleapi units that load them.

    Every moduleapi test names its module as the literal string
    ``tests/modules/<name>.so``, so grepping for that literal in the checked-out
    target branch is exact. Basenames are not: ``defragtest.c`` is only exercised
    by ``defrag.tcl``, and ``auth.c`` is loaded by four different units. The
    mapping has to be recomputed per branch because modules are added and
    retired between release lines.

    Returns the resolved units and whether the whole suite is still required --
    a non-``.c`` path under ``tests/modules/`` is build infrastructure, and a
    ``.c`` with no consumer cannot be narrowed, so both fail open to everything.
    """
    sources = tuple(
        path
        for path in changed_paths
        if path.startswith(_MODULE_SOURCE_PREFIX) and path.endswith(".c")
    )
    infrastructure = tuple(
        path
        for path in changed_paths
        if path.startswith(_MODULE_SOURCE_PREFIX) and not path.endswith(".c")
    )
    if not sources and not infrastructure:
        return (), False

    tests_dir = Path(repo_dir, "tests/unit/moduleapi") if repo_dir else None
    if infrastructure or tests_dir is None or not tests_dir.is_dir():
        return (), True

    bodies = {
        path: path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(tests_dir.glob("*.tcl"))
    }
    units: set[str] = set()
    for source in sources:
        needle = f"{_MODULE_SOURCE_PREFIX}{Path(source).stem}.so"
        hits = {
            f"unit/moduleapi/{path.stem}"
            for path, body in bodies.items()
            if needle in body
        }
        if not hits:
            return (), True
        units |= hits
    return tuple(sorted(units)), False


def _valkey_test_validation_commands(
    changed_paths: tuple[str, ...],
    repo_dir: str,
) -> tuple[str, ...]:
    """Map one candidate diff onto the narrowest test runs that can fail it.

    Each command has to finish inside the per-command cap in
    ``scripts/common/build_validator.py``, so nothing here may expand to a whole
    serialized suite. A changed test runs as its own unit, a changed module
    source runs only the moduleapi units that load it, and a harness change runs
    a fixed smoke set. The reply-schema block is appended last because it needs
    a ``-DLOG_REQ_RES`` rebuild that would otherwise poison the cheaper checks.
    """
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
    covered_test_paths: set[str] = set()
    for _path, unit in regular_tests:
        commands.append(
            f"./runtest --single {quote(unit)} --clients 1"
            f"{_runtest_mode_args(unit)}"
        )
        covered_test_paths.add(_path)
    # Moduleapi units need their test modules built first. Newer wrappers accept
    # caller selection; legacy wrappers hard-code the full suite.
    for _path, unit in module_tests:
        commands.append(_targeted_moduleapi_command(repo_dir, (unit,)))
        covered_test_paths.add(_path)

    if any(path.startswith("src/unit/") for path in changed_paths):
        commands.append("make -C src test-unit")
        covered_test_paths.update(
            path
            for path in changed_paths
            if (
                path.startswith("src/unit/")
                and _is_valkey_test_path(path)
                and _unit_test_source_is_built(repo_dir, path)
            )
        )

    legacy_cluster_tests = tuple(
        path
        for path in changed_paths
        if path.startswith("tests/cluster/tests/")
        and path.endswith(".tcl")
        and _path_exists(repo_dir, path)
    )
    for path in legacy_cluster_tests:
        pattern = path.removeprefix("tests/cluster/tests/").removesuffix(".tcl")
        commands.append(f"./runtest-cluster --single {quote(pattern)}")
        covered_test_paths.add(path)

    cluster_changed = any(
        path.startswith("tests/unit/cluster/")
        or path.startswith("tests/cluster/")
        or fnmatch(path, "src/cluster*.c")
        or fnmatch(path, "src/cluster*.h")
        for path in changed_paths
    )
    if cluster_changed and not any(unit.startswith("unit/cluster/") for unit in direct_tests):
        cluster_unit = _first_existing_unit(
            repo_dir,
            ("unit/cluster/base", "unit/cluster/misc"),
        )
        commands.append(f"./runtest --single {quote(cluster_unit)} --clients 1")
    if cluster_changed:
        covered_test_paths.update(
            path
            for path in changed_paths
            if path.startswith("tests/cluster/") and _is_valkey_test_path(path)
        )

    if any(
        path.startswith("tests/sentinel/")
        or path in {"src/sentinel.c", "src/sentinel.h"}
        for path in changed_paths
    ):
        commands.append("./runtest-sentinel")
        covered_test_paths.update(
            path
            for path in changed_paths
            if path.startswith("tests/sentinel/") and _is_valkey_test_path(path)
        )

    module_units, module_suite_needed = _moduleapi_units_for_module_sources(
        repo_dir,
        changed_paths,
    )
    if module_suite_needed:
        # The fallback deliberately keeps the harness default client count: the
        # whole moduleapi suite serialized at one client does not fit the
        # per-command cap, and upstream runs it in parallel too.
        commands.append("./runtest-moduleapi")
        covered_test_paths.update(
            path
            for path in changed_paths
            if path.startswith(_MODULE_SOURCE_PREFIX) and _is_valkey_test_path(path)
        )
    else:
        extra_module_units = tuple(
            unit for unit in module_units if unit not in set(direct_tests)
        )
        if extra_module_units:
            commands.append(_targeted_moduleapi_command(repo_dir, extra_module_units))

    harness_paths = tuple(
        path
        for path in changed_paths
        if (
            path.startswith("tests/support/")
            or path.startswith("tests/helpers/")
            or path in {"tests/instances.tcl", "tests/test_helper.tcl"}
        )
    )
    if harness_paths:
        units = " ".join(f"--single {quote(unit)}" for unit in _HARNESS_SMOKE_UNITS)
        commands.append(f"./runtest {units} --clients 1 --tags -slow")
        covered_test_paths.update(
            path
            for path in harness_paths
            if _is_valkey_test_path(path)
        )

    reply_tests = tuple(
        unit
        for path, unit in regular_tests
        if not _top_level_skips_reply_logging(repo_dir, path)
    )
    regular_reply_tests = tuple(
        unit for unit in reply_tests if unit not in _TLS_ONLY_UNITS
    )
    tls_reply_tests = tuple(
        unit for unit in reply_tests if unit in _TLS_ONLY_UNITS
    )
    # A changed module source reaches the reply-schema run through the units that
    # load it, so a module-only diff no longer degrades to the whole suite here.
    reply_module_tests = tuple(
        dict.fromkeys(
            tuple(
                unit
                for path, unit in module_tests
                if not _top_level_skips_reply_logging(repo_dir, path)
            )
            + tuple(
                unit
                for unit in module_units
                if not _top_level_skips_reply_logging(repo_dir, f"tests/{unit}.tcl")
            )
        )
    )
    if reply_tests or reply_module_tests or module_suite_needed:
        commands.append("make -j$(nproc) BUILD_TLS=yes SERVER_CFLAGS='-Werror -DLOG_REQ_RES'")
        wrote_reply_logs = False
        for selected_reply_tests, mode_args in (
            (regular_reply_tests, ""),
            (tls_reply_tests, " --tls"),
        ):
            if not selected_reply_tests:
                continue
            # `--tags -slow` matches the upstream reply-schema job. Blocks it has
            # never run under `--log-req-res --force-resp3` have never had their
            # logged replies schema-checked, so running them here can fail a
            # candidate for a pre-existing upstream violation.
            units = " ".join(f"--single {quote(unit)}" for unit in selected_reply_tests)
            keep_logs = " --dont-pre-clean" if wrote_reply_logs else ""
            commands.append(
                "./runtest "
                f"{units} --clients 1 --tags -slow --log-req-res --no-latency "
                f"--dont-clean{keep_logs} --force-resp3{mode_args}"
            )
            wrote_reply_logs = True
        # `--dont-pre-clean` only exists to keep the logs the `./runtest` leg
        # above just wrote. Without that leg the startup wipe has to run, or a
        # previous candidate's `tests/tmp/**/*.reqres` is validated against this
        # branch's schemas and fails for a reason unrelated to the diff.
        keep_logs = " --dont-pre-clean" if wrote_reply_logs else ""
        if module_suite_needed:
            commands.append(
                "CFLAGS='-Werror' ./runtest-moduleapi --log-req-res --no-latency "
                f"--dont-clean{keep_logs} --force-resp3"
            )
        elif reply_module_tests:
            commands.append(
                _targeted_moduleapi_command(
                    repo_dir,
                    reply_module_tests,
                    suffix=(
                        " --log-req-res --no-latency --dont-clean"
                        f"{keep_logs} --force-resp3"
                    ),
                    werror=True,
                )
            )
        if _env_flag(SKIP_REQ_RES_VALIDATION_ENV):
            logger.warning(
                "%s is enabled; skipping request/response schema validation",
                SKIP_REQ_RES_VALIDATION_ENV,
            )
        else:
            commands.append(
                "./utils/req-res-log-validator.py --verbose --fail-missing-reply-schemas"
            )

    uncovered_tests = tuple(
        path
        for path in changed_paths
        if _path_exists(repo_dir, path)
        and _is_valkey_test_path(path)
        and path not in covered_test_paths
    )
    if uncovered_tests:
        marker = UNMAPPED_TEST_PATHS_PREFIX + json.dumps(
            uncovered_tests,
            separators=(",", ":"),
        )
        message = (
            "changed test path(s) have no explicit validation mapping: "
            + ", ".join(uncovered_tests)
        )
        diagnostic = f"{marker}\n{message}\n"
        commands.append(
            f"python3 -c "
            f"{quote(f'import sys; sys.stderr.write({diagnostic!r}); sys.exit(1)')}",
        )

    return tuple(commands)


def _targeted_moduleapi_command(
    repo_dir: str,
    units: tuple[str, ...],
    *,
    suffix: str = "",
    werror: bool = False,
) -> str:
    """Run only selected moduleapi units on both legacy and current wrappers.

    The 7.2 and 8.0 wrappers hard-code every moduleapi unit before appending
    caller arguments, so adding ``--single`` to those wrappers still runs the
    whole suite. On those branches, build the test modules explicitly and call
    the general test harness, whose repeatable ``--single`` option accepts
    moduleapi units directly.
    """
    unit_args = " ".join(f"--single {quote(unit)}" for unit in units)
    if _moduleapi_wrapper_accepts_selection(repo_dir):
        prefix = "CFLAGS='-Werror' " if werror else ""
        return f"{prefix}./runtest-moduleapi {unit_args} --clients 1{suffix}"

    make_prefix = "CFLAGS='-Werror' " if werror else ""
    return f"{make_prefix}make -C tests/modules && ./runtest {unit_args} --clients 1{suffix}"


def _moduleapi_wrapper_accepts_selection(repo_dir: str) -> bool:
    """Whether ``runtest-moduleapi`` delegates selection to ``--moduleapi``."""
    if not repo_dir:
        return True
    try:
        body = Path(repo_dir, "runtest-moduleapi").read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        # A partial fixture should retain the current command shape; real Valkey
        # checkouts always contain the wrapper.
        return True
    return "--moduleapi" in body


def _first_existing_unit(repo_dir: str, units: tuple[str, ...]) -> str:
    """Choose a smoke unit that exists on the checked-out release branch."""
    if repo_dir:
        for unit in units:
            if Path(repo_dir, f"tests/{unit}.tcl").is_file():
                return unit
    return units[0]


def _is_direct_runtest(path: str) -> bool:
    return path.endswith(".tcl") and (
        path.startswith("tests/unit/") or path.startswith("tests/integration/")
    )


def _is_valkey_test_path(path: str) -> bool:
    return (
        path.startswith("tests/")
        and path.endswith(".tcl")
    ) or (
        path.startswith("src/unit/test_")
        and path.endswith(_UNIT_TEST_SOURCE_SUFFIXES)
    )


def _unit_test_source_is_built(repo_dir: str, path: str) -> bool:
    """Whether the target branch's Makefiles compile this unit-test suffix.

    Valkey 8.0-9.0 discovers ``src/unit/*.c`` from ``src/Makefile``. Valkey
    9.1+ instead discovers ``src/unit/*.cpp`` from ``src/unit/Makefile``, while
    7.2 has no source-unit harness. A clean cherry-pick across that transition
    can otherwise pass ``make test-unit`` while silently ignoring the new file.
    """
    if not repo_dir:
        return True
    suffix = Path(path).suffix.lower()
    marker = {
        ".c": "wildcard unit/*.c",
        ".cc": "wildcard *.cc",
        ".cpp": "wildcard *.cpp",
    }.get(suffix)
    if marker is None:
        return False
    for makefile in (Path(repo_dir, "src/Makefile"), Path(repo_dir, "src/unit/Makefile")):
        try:
            body = " ".join(makefile.read_text(encoding="utf-8", errors="replace").split())
        except OSError:
            continue
        if marker in body:
            return True
    return False


def _runtest_unit(path: str) -> str:
    """Turn ``tests/unit/type/list.tcl`` into the ``--single`` name ``unit/type/list``.

    Returns ``""`` for anything the harness cannot run as a unit, so callers can
    filter on falsiness instead of re-checking the path shape.
    """
    if not _is_direct_runtest(path):
        return ""
    return path.removeprefix("tests/").removesuffix(".tcl")


def _runtest_mode_args(unit: str) -> str:
    """Return harness flags required for a unit to execute meaningful tests."""
    return " --tls" if unit in _TLS_ONLY_UNITS else ""


def _path_exists(repo_dir: str, path: str) -> bool:
    return not repo_dir or Path(repo_dir, path).is_file()


def _top_level_skips_reply_logging(repo_dir: str, path: str) -> bool:
    """Report whether a test opts out of reply logging at its top level.

    ``logreqres:skip`` is a tag on the outermost block, so only the head of the
    file matters; a match deeper down belongs to one nested block and does not
    exempt the unit. Running a skipped unit under ``--log-req-res`` produces no
    usable logs, so it is left out of the reply-schema commands entirely.
    """
    if not repo_dir:
        return False
    try:
        with Path(repo_dir, path).open(encoding="utf-8", errors="replace") as handle:
            prefix = "".join(next(handle, "") for _ in range(40))
    except OSError:
        return False
    return "logreqres:skip" in prefix
