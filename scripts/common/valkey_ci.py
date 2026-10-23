"""What the bots know about Valkey's CI: its scheduled workflow and the
result lines its test runners print.

``runtest`` prints ``[ok]: <name> (<ms> ms)`` for a passing test and
``[err]: <name> in tests/<file>.tcl`` (or ``[TIMEOUT]``) for a failing one,
again prefixed ``*** `` in its closing summary. A C unit test (Google Test,
``make test-unit``, ``src/unit/*.cpp``) prints ``[       OK ] Suite.Test (<ms>
ms)`` or ``[  FAILED  ] Suite.Test (<ms> ms)`` once it ends, after the
``file:line: Failure`` of each failed assertion; the list gtest prints at the
end repeats a failed name without a time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from scripts.common.text_utils import strip_ansi


class Source(str, Enum):
    DAILY = "daily"

    @property
    def workflow_file(self) -> str:
        return f"{self.value}.yml"

    @property
    def label(self) -> str:
        return self.value.capitalize()


def source_for_run(run: Any, *, scheduled_only: bool = True) -> Source | None:
    """The source ``run`` executed, by its workflow file, or None for any other.

    ``scheduled_only`` also requires a scheduled run, as a fix does; the
    detector reads a manually dispatched run of the same workflow the same way.
    """
    if scheduled_only and str(getattr(run, "event", "") or "") != "schedule":
        return None
    path = str(getattr(run, "path", "") or "")
    for source in Source:
        if path == f".github/workflows/{source.workflow_file}":
            return source
    return None


# --- runtest result lines -----------------------------------------------------

_LOG_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ")
_RESULT_LINE_RE = re.compile(r"\[(?P<status>ok|err|TIMEOUT|exception)\]: (?P<data>.+)$")
_TEST_IN_FILE_RE = re.compile(r"^(?P<name>.+) in (?P<file>tests/\S+\.tcl)$")
_OK_RE = re.compile(r"^(?P<name>.+) \(\d+ ms\)$")
# A test client that times out outside a test is reported by its state, not a
# test name (test_helper.tcl: "pid:74389", "port 21581", "ASSIGNED: sock5f0
# (unit/x)", "SLEEPING, ..."). Those change every run, so the failure is keyed
# by this stable name for its file instead.
_CLIENT_STATE_RE = re.compile(r"^(?:pid:\d+|port \d+|ASSIGNED: .*|SLEEPING\b.*)$")
FILE_TIMEOUT_TEST_NAME = "test client timed out"
# What a failing runtest or gtest run prints; a build break or a missing Tcl does not.
FAILURE_MARKERS = (
    "[err]:", "[TIMEOUT]:", "[exception]:", "Valgrind error", "Sanitizer error", "[  FAILED  ]",
    "[  TIMEOUT ]",  # gtest-parallel's, for a test killed by its per-test watchdog
)

# --- Google Test result lines -------------------------------------------------

_GTEST_RUN_RE = re.compile(r"^\[ RUN      \] (?P<test>\S+)$")
_GTEST_OK_RE = re.compile(r"^\[       OK \] (?P<test>[^,\s]+)(?:, where .*)? \(\d+ ms\)$")
_GTEST_FAILED_RE = re.compile(r"^\[  FAILED  \] (?P<test>[^,\s]+)(?:, where .*)? \(\d+ ms\)$")
# gtest-parallel (``make test-unit``) runs each test in its own process and
# prints one line per test: its name and time when it passed, else how it
# ended (``returned with exit code 1``, ``killed by signal SIGSEGV``,
# ``aborted after``), or ``[  TIMEOUT ]`` when its watchdog killed it.
_GTEST_PARALLEL_RE = re.compile(r"^\[\d+/\d+\] (?P<test>\S+) (?P<rest>.+)$")
_GTEST_PARALLEL_OK_RE = re.compile(r"^\(\d+ ms\)$")
_GTEST_PARALLEL_TIMEOUT_RE = re.compile(r"^\[  TIMEOUT \] (?P<test>\S+) timed out after \d+ s$")
_GTEST_LOCATION_RE = re.compile(r"^(?P<file>[\w./-]+\.(?:c|cc|cpp|h|hpp)):\d+: Failure$")
UNIT_TEST_FILE_SUFFIXES = (".c", ".cc", ".cpp", ".h", ".hpp")


def is_unit_test_file(file: str) -> bool:
    """Whether ``file`` names a C unit test's source, not a Tcl test."""
    return file.endswith(UNIT_TEST_FILE_SUFFIXES)


def gtest_failure(line: str) -> str | None:
    """The test a ``[  FAILED  ] Suite.Test (N ms)`` line reports, or None.

    Not the closing list, which repeats the name without a time.
    """
    match = _GTEST_FAILED_RE.match(strip_log_prefix(line))
    return match.group("test") if match else None


def gtest_file_of(test: str, before: list[str]) -> str:
    """The base name of the file ``test``'s first failed assertion is in, or "".

    ``before`` holds the (prefix-stripped) log lines up to the test's
    ``[  FAILED  ]`` line. The base name, not the path, and no line: a fix
    elsewhere in the file moves the line, and the compiler saw the path
    differently in another job's build, so either would make the same failure
    look like a new one.
    """
    file = ""
    for line in reversed(before):
        if (run := _GTEST_RUN_RE.match(line)) and run.group("test") == test:
            break
        if location := _GTEST_LOCATION_RE.match(line):
            file = location.group("file").rsplit("/", 1)[-1]
    return file


@dataclass(frozen=True)
class ResultLine:
    """One ``[status]: ...`` line: ``name`` and ``file`` when it names a test."""

    status: str
    name: str = ""
    file: str = ""


def strip_log_prefix(line: str) -> str:
    """``line`` without its Actions timestamp, colours, or surrounding space."""
    return _LOG_TIMESTAMP_RE.sub("", strip_ansi(line)).strip()


def test_in_file(data: str) -> tuple[str, str] | None:
    """``(name, file)`` for ``<name> in tests/<file>.tcl``, client states folded."""
    match = _TEST_IN_FILE_RE.match(data)
    if match is None:
        return None
    name = match.group("name")
    if _CLIENT_STATE_RE.fullmatch(name):
        name = FILE_TIMEOUT_TEST_NAME
    return name, match.group("file")


def parse_result_line(line: str) -> ResultLine | None:
    """The runtest or gtest result a (possibly timestamped) log line reports, or None.

    A gtest line names no file: the test's identity is its ``Suite.Test`` name.
    """
    plain = strip_log_prefix(line)
    if passed := _GTEST_OK_RE.match(plain):
        return ResultLine("ok", name=passed.group("test"))
    if failed := _GTEST_FAILED_RE.match(plain):
        return ResultLine("err", name=failed.group("test"))
    if parallel := _GTEST_PARALLEL_RE.match(plain):
        status = "ok" if _GTEST_PARALLEL_OK_RE.match(parallel.group("rest")) else "err"
        return ResultLine(status, name=parallel.group("test"))
    if timed_out := _GTEST_PARALLEL_TIMEOUT_RE.match(plain):
        return ResultLine("TIMEOUT", name=timed_out.group("test"))
    match = _RESULT_LINE_RE.search(plain)
    if match is None:
        return None
    status, data = match.group("status"), match.group("data").strip()
    if status == "ok":
        passed = _OK_RE.match(data)
        return ResultLine(status, name=passed.group("name") if passed else data)
    test = test_in_file(data)
    return ResultLine(status, *(test or ("", "")))


def failed_tests_in_log(log: str) -> list[tuple[str, str]]:
    """``(test name, file)`` for each test a log shows failing, in order.

    A Tcl test's ``tests/...tcl`` file; a C unit test's file by base name (as
    its first failed assertion printed it), or "" when none did.
    """
    found: list[tuple[str, str]] = []
    plain = [strip_log_prefix(raw) for raw in (log or "").splitlines()]
    for index, line in enumerate(plain):
        if (unit_test := gtest_failure(line)) is not None:
            entry = (unit_test, gtest_file_of(unit_test, plain[:index]))
        else:
            result = parse_result_line(line)
            if result is None or result.status not in ("err", "TIMEOUT") or not result.file:
                continue
            entry = (result.name, result.file)
        if entry not in found:
            found.append(entry)
    return found
