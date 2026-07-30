"""Recover gtest failure output from a job's console log.

gtest-parallel's JSON dump records only a per-test verdict (PASS, FAIL,
TIMEOUT) and timings, so the extraction action can report no more than which
test failed. The assertion that failed, its file and line, and the expected and
actual values are printed to the console instead. This reads that block back out
of the job log so an issue carries the diagnostic a maintainer needs.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# gtest-parallel brackets each test it runs with a progress line, and closes a
# failing one by repeating the header with the exit code:
#
#   [6/298] DummyTest.IntentionalFailure (2 ms)
#   ... the test's own gtest output ...
#   [6/298] DummyTest.IntentionalFailure returned with exit code 1 (2 ms)
#
# Anchoring on the closing line rather than the next test's header keeps the
# block intact when tests run in parallel and their output interleaves.
_GTEST_BLOCK_START_RE = re.compile(
    r"^\[\d+/\d+\]\s+(?P<test>[\w./:]+(?:\.[\w./:]+)?)\s+\([\d.]+\s*m?s\)\s*$"
)
_GTEST_BLOCK_END_RE = re.compile(
    r"^\[\d+/\d+\]\s+(?P<test>[\w./:]+)\s+returned with exit code\s+\d+"
)

# A GitHub Actions log line is prefixed with an ISO timestamp, and gtest colours
# its output. Both are noise in an issue body, and the timestamp would also make
# two runs of one failure compare unequal in the recurrence check.
_LOG_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?")
_ANSI_RE = re.compile(r"\033?\[[0-9;]*m")

# A block longer than this is not a test diagnostic: a test that logs in a loop
# can print thousands of lines before failing. The head holds the assertion, so
# the cap keeps that and drops the rest.
_MAX_BLOCK_LINES = 200


def _clean(line: str) -> str:
    return _ANSI_RE.sub("", _LOG_TIMESTAMP_RE.sub("", line)).rstrip()


def parse_gtest_failures_from_log(log_content: bytes) -> dict[str, str]:
    """Map test name to its console output, for tests that failed in *log*.

    Only tests whose block is closed by a non-zero exit code are returned, so a
    test that passed on a retry contributes nothing. Returns an empty mapping
    when the log holds no gtest blocks, which is the normal case for a job that
    runs no unit tests.
    """
    try:
        text = log_content.decode("utf-8", errors="replace")
    except Exception:
        logger.warning("Could not decode a job log while recovering gtest output")
        return {}

    failures: dict[str, str] = {}
    # gtest-parallel buffers each test's output and prints it in one piece
    # between that test's own progress lines, so an output line belongs to the
    # most recently opened block. Appending to every open block instead would
    # copy one test's output into another's issue.
    open_blocks: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in text.split("\n"):
        line = _clean(raw_line)

        end = _GTEST_BLOCK_END_RE.match(line)
        if end is not None:
            test = end.group("test")
            body = open_blocks.pop(test, None)
            if body is not None:
                failures[test] = "\n".join([*body, line]).strip()
            if current == test:
                current = None
            continue

        start = _GTEST_BLOCK_START_RE.match(line)
        if start is not None:
            current = start.group("test")
            open_blocks[current] = [line]
            continue

        if current is None:
            continue
        body = open_blocks[current]
        if len(body) < _MAX_BLOCK_LINES:
            body.append(line)

    if failures:
        logger.info(
            "Recovered console output for %d gtest failure(s)", len(failures)
        )
    return failures
