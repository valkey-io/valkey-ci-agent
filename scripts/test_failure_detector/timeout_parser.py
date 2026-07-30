"""Parse test timeouts from CI console logs.

The runner records timeouts in the artifact, but its watchdog can kill the run
before ``write_test_failures`` executes, leaving a timed-out job with no timeout
entry. This module recovers those from the console log, where they appear as::

    [TIMEOUT]: clients state report follows.
    ...
    [TIMEOUT]: <test_name> in <test_file>

and later in the summary::

    *** [TIMEOUT]: <test_name> in <test_file>
"""

from __future__ import annotations

import logging
import re
from typing import Any

from scripts.test_failure_detector.download import normalize_job_name
from scripts.test_failure_detector.parse_failures import (
    _VOLATILE_TEST_NAME_RE,
    FailureType,
    JobReference,
    UniqueFailure,
)

logger = logging.getLogger(__name__)

# Matches the timeout failure lines printed by the Tcl test runner.
# Both the inline report and the final "*** [TIMEOUT]:" summary use this form.
# ANSI color codes may be stripped or present depending on how the logs are stored.
_TIMEOUT_RE = re.compile(
    r"\[(?:\x1b\[[^m]*m)?TIMEOUT(?:\x1b\[[^m]*m)?\]"
    r":\s*(.+?)\s+in\s+(tests/\S+\.tcl)",
)

# On a timeout the runner prints what each test client was doing when the
# watchdog fired:
#
#   [TIMEOUT]: clients state report follows.
#   sock56195920b920 => (IN PROGRESS) dummy-timeout - intentional hang ...
#
# That naming of the in-progress test is the only diagnostic a timeout has, so
# it is captured for the issue body. The report is followed by a dump of every
# server's log, which is long and mostly startup banner, so collection stops at
# the first server-log header.
_CLIENTS_REPORT_START_RE = re.compile(r"\[TIMEOUT\]:\s*clients state report follows")
_SERVER_LOG_HEADER_RE = re.compile(r"^===\s+Server log\b")
_LOG_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?")
_ANSI_RE = re.compile(r"\x1b?\[[0-9;]*m")

# The report lists one line per test client. A run uses 16 by default, so this
# keeps the whole report while bounding a pathological one.
_MAX_REPORT_LINES = 40


def _clean_log_line(line: str) -> str:
    """Strip the Actions timestamp prefix and ANSI colouring from *line*."""
    return _ANSI_RE.sub("", _LOG_TIMESTAMP_RE.sub("", line))


def clients_state_report(text: str) -> str:
    """The runner's clients state report for a timeout, or "".

    Names the test each client was running when the watchdog fired, which is
    what tells a reader where the run hung.
    """
    lines = text.split("\n")
    for index, raw_line in enumerate(lines):
        # Match the cleaned line: the runner colours the TIMEOUT tag whenever
        # TERM says the terminal supports it, which splits the literal tag this
        # pattern looks for. Cleaning first also drops the Actions timestamp.
        if not _CLIENTS_REPORT_START_RE.search(_clean_log_line(raw_line)):
            continue
        collected: list[str] = []
        for follow_raw in lines[index : index + _MAX_REPORT_LINES]:
            follow = _clean_log_line(follow_raw).rstrip()
            if _SERVER_LOG_HEADER_RE.match(follow):
                break
            if follow:
                collected.append(follow)
        if len(collected) > 1:
            return "\n".join(collected)
    return ""


def jobs_needing_log_scan(
    all_failures: dict[str, Any],
    failed_job_names: set[str],
) -> set[str]:
    """Identify failed jobs whose artifact captured no timeout entry.

    These are candidates for timeout recovery. The runner's
    ``write_test_failures`` excludes timeouts from the artifact, so captured
    assertions or other failures say nothing about whether the job also timed
    out; only an explicit timeout-type entry (from a runner that does capture
    them) makes log scanning redundant for that job.

    The run-logs archive is downloaded once per run, so scanning extra jobs
    costs only regex passes, not API calls.

    ``failed_job_names`` holds API job names, while ``all_failures`` is keyed by
    the artifact's spelling. For a matrix job those differ (``base (value)`` vs
    ``base-value``), so the lookup below tries the normalized name too.
    """
    needs_scan: set[str] = set()
    for job_name in failed_job_names:
        suites = all_failures.get(job_name)
        if suites is None:
            suites = all_failures.get(normalize_job_name(job_name))
        if not isinstance(suites, dict):
            # Job absent from the artifact (upload skipped) or malformed.
            needs_scan.add(job_name)
            continue
        has_timeout_entry = any(
            isinstance(entry, dict) and entry.get("type") == "timeout"
            for entries in suites.values()
            if isinstance(entries, list)
            for entry in entries
        )
        if not has_timeout_entry:
            needs_scan.add(job_name)
    return needs_scan


def parse_timeouts_from_log(
    log_content: bytes,
    job_name: str,
    job_url: str = "",
) -> list[UniqueFailure]:
    """Extract timeout failures from a single job's console log.

    Returns one UniqueFailure per distinct (test_name, test_file) pair found
    in [TIMEOUT] lines.
    """
    try:
        text = log_content.decode("utf-8", errors="replace")
    except Exception:
        logger.warning("Could not decode log for job %s", job_name)
        return []

    # The report is per-run, not per-test, so it is extracted once and shared by
    # every timeout recovered from this job's log.
    report = clients_state_report(text)

    seen: dict[tuple[str, str], UniqueFailure] = {}
    for match in _TIMEOUT_RE.finditer(text):
        test_name = match.group(1).strip()
        test_file = match.group(2).strip()
        if not test_name or not test_file:
            continue

        # Volatile names (bare PIDs, "hang") carry no stable test identity.
        if _VOLATILE_TEST_NAME_RE.fullmatch(test_name):
            test_name = ""

        key = (test_name, test_file)
        if key in seen:
            continue

        error = "Test timed out (no progress for the configured timeout period)"
        if report:
            error = f"{error}\n\n{report}"

        seen[key] = UniqueFailure(
            test_name=test_name,
            test_file=test_file,
            failure_type=FailureType.TIMEOUT,
            error=error,
            jobs=[JobReference(job=job_name, suite="timeout", url=job_url)],
        )

    if seen:
        logger.info(
            "Recovered %d timeout failure(s) from logs of job %s",
            len(seen), job_name,
        )
    return list(seen.values())


def find_job_log(
    logs: dict[str, bytes],
    job_name: str,
) -> bytes | None:
    """Find the console log for a specific job in the run-logs zip contents.

    GitHub's run-log zip names files as ``<number>_<job_name>.txt``. The job
    name in the filename matches the job name from the API, including any
    spaces and parentheses.
    """
    # Try exact match first (most common)
    for filename, content in logs.items():
        # Strip the leading number prefix: "3_test-valgrind-test.txt" -> "test-valgrind-test"
        if not filename.endswith(".txt"):
            continue
        # Skip system.txt files in subdirectories
        if "/" in filename:
            continue
        name_part = re.sub(r"^\d+_", "", filename)
        name_part = name_part.removesuffix(".txt")
        if name_part == job_name:
            return content

    # Fallback: case-insensitive or partial match
    job_lower = job_name.lower()
    for filename, content in logs.items():
        if "/" in filename:
            continue
        if not filename.endswith(".txt"):
            continue
        name_part = re.sub(r"^\d+_", "", filename).removesuffix(".txt").lower()
        if name_part == job_lower:
            return content

    logger.debug("No log file found for job %s", job_name)
    return None


def clients_state_report_from_log(log_content: bytes) -> str:
    """The clients state report in a job's raw log, or "".

    Wrapper for callers holding undecoded log bytes.
    """
    try:
        text = log_content.decode("utf-8", errors="replace")
    except Exception:
        logger.warning("Could not decode a job log while reading its timeout report")
        return ""
    return clients_state_report(text)
