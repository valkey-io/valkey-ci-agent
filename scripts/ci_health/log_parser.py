"""Log parser for Valkey CI job logs.

Extracts test failure information from Valkey's Tcl test framework output,
Google Test (gtest) unit test output, sentinel/cluster test output, and
Tcl exception stack traces.

GitHub Actions prepends ISO timestamps to every log line
(e.g. ``2026-04-01T01:05:06.5089664Z ``).  The parser strips these before
applying patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from scripts.ci_health.weekly import parse_branch_job


@dataclass
class TestFailure:
    """A single test failure extracted from a CI job log."""

    test_name: str
    error_summary: str
    log_lines: str


# ---------------------------------------------------------------------------
# Timestamp stripping
# ---------------------------------------------------------------------------

# GitHub Actions timestamp prefix: "2026-04-01T01:05:06.5089664Z "
_GHA_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s?")


def _strip_timestamps(raw_log: str) -> str:
    """Remove GitHub Actions timestamp prefixes from every line."""
    return "\n".join(_GHA_TIMESTAMP_RE.sub("", line) for line in raw_log.splitlines())


# ---------------------------------------------------------------------------
# Regex patterns for Valkey CI log formats
# ---------------------------------------------------------------------------

# Tcl test framework: "[err]: <test_name> in <file>"
# This appears inline during test execution.
_TCL_ERR_RE = re.compile(r"^\[err\]:\s*(.+?)\s+in\s+(tests/.+\.tcl)\s*$", re.MULTILINE)

# Summary lines at the end: "*** [err]: <test_name> in <file>"
_SUMMARY_ERR_RE = re.compile(
    r"^\*\*\*\s+\[err\]:\s*(.+?)\s+in\s+(tests/.+\.tcl)\s*$", re.MULTILINE
)

# Timeout marker: "[TIMEOUT]: ..."
_TIMEOUT_RE = re.compile(r"^\[TIMEOUT\]:\s*(.+)", re.MULTILINE)

# Timeout summary: "*** [TIMEOUT]: test_name in tests/file.tcl"
_SUMMARY_TIMEOUT_RE = re.compile(
    r"^\*\*\*\s+\[TIMEOUT\]:\s*(.+?)\s+in\s+(tests/.+\.tcl)\s*$", re.MULTILINE
)

# In-progress client line after timeout: "sock<hex> => (IN PROGRESS) test_name"
_IN_PROGRESS_RE = re.compile(
    r"^sock[0-9a-f]+\s+=>\s+\(IN PROGRESS\)\s+(.+)", re.MULTILINE
)

# Google Test failure marker: "[  FAILED  ] TestSuite.TestName"
_GTEST_FAILED_RE = re.compile(r"^\[\s+FAILED\s+\]\s+(\S+)", re.MULTILINE)

# Sentinel / cluster test "FAILED:" lines
_SENTINEL_FAILED_RE = re.compile(r"^(.+?):\s*FAILED:\s*(.+)", re.MULTILINE)

# Tcl exception: "[exception]: Executing test client: <message>"
_TCL_EXCEPTION_RE = re.compile(r"^\[exception\]:\s*(.+)", re.MULTILINE)

# Tcl file reference in stack trace: '(file "tests/unit/foo.tcl" line 123)'
_TCL_FILE_REF_RE = re.compile(r'\(file\s+"(tests/.+?\.tcl)"\s+line\s+\d+\)')

# Tcl test name in stack trace: 'test "test name here" {' or '"test "name" {'
_TCL_STACK_TEST_RE = re.compile(r'(?:^"|^)test\s+"([^"]+)"', re.MULTILINE)

# Tcl procedure call with test name: 'test_slave_buffers {name here} args'
_TCL_PROC_CALL_RE = re.compile(r'^"?(\w+)\s+\{([^}]+)\}', re.MULTILINE)

# GitHub Actions error annotation: "##[error]Process completed with exit code N"
_GHA_ERROR_RE = re.compile(r"^##\[error\](.+)", re.MULTILINE)

# Valkey crash log signature: "# valkey 255.255.255 crashed by signal: 11, si_code: 0"
_CRASH_RE = re.compile(r"crashed by signal:\s*(\d+)")
_CRASH_ADDR_RE = re.compile(r"Accessing address:\s*(\S+)")


# ---------------------------------------------------------------------------
# Noise filtering / normalization (issue #1)
# ---------------------------------------------------------------------------

# Generic CI-runner banners that are never a test identity. Matched
# case-insensitively as substrings.
_GENERIC_NOISE = (
    "clients state report follows",
    "process completed with exit code",
    "the runner has received a shutdown signal",
    "the operation was canceled",
)


def _scrub_volatile(text: str) -> str:
    """Replace run-specific volatile tokens with stable placeholders.

    Two otherwise-identical failures reported on different days must not
    split into separate rows just because a pid/port/address differs.  This
    is applied when building a *name* from free-form error text so repeated
    incidents aggregate.
    """
    s = text
    s = re.sub(r"\d{1,3}(?:\.\d{1,3}){3}:\d+", "<host:port>", s)  # ip:port
    s = re.sub(r"\bpid[:=]\s*\d+", "pid:<pid>", s)  # pid:12345 / pid=12345
    s = re.sub(r"\bpid \d+", "pid <pid>", s)  # pid 12345
    s = re.sub(r"\bProcess \d+", "Process <pid>", s)  # "Process 12345" (crash)
    s = re.sub(r"\bsock[0-9a-f]{6,}", "sock<id>", s)  # sock55f0a1b2c3d0
    s = re.sub(r"server\.\d+\.\d+", "server.<pid>.<n>", s)  # tmp dir server.10822.103
    s = re.sub(r"0x[0-9a-fA-F]{4,}", "0x<addr>", s)  # memory addresses
    return s


def _is_valid_test_name(name: str) -> bool:
    """Return False for strings that are not real test identities.

    Filters generic runner banners and volatile-only tokens (e.g. a bare
    ``pid:12345``) that would otherwise pollute the failure heatmap.  Kept
    deliberately narrow: a genuine Tcl/GTest/sentinel test name never
    contains a ``:`` in its name portion, so these rules cannot reject one.
    """
    n = name.strip()
    if not n:
        return False
    low = n.lower()
    if any(sub in low for sub in _GENERIC_NOISE):
        return False
    if n.startswith("pid:") or re.fullmatch(r"pid[:=]\s*\d+", n):
        return False
    return True


def sanitize_cached_failure(test_name: str) -> Optional[str]:
    """Clean a failure name already stored in the cache for display.

    Mirrors the parser's own filtering so rows written by an older parser
    version render cleanly at report time without requiring a full re-parse:

    * volatile tokens (pids/ports/addresses) are normalized so incidents
      aggregate into one row
    * generic runner banners and volatile-only tokens are dropped (return
      None) — the run still shows as failed via its job conclusion

    Returns the cleaned name, or None if the row should be dropped as noise.
    """
    cleaned = _scrub_volatile(test_name)
    # Rows parsed before weekly release-branch splitting carry the fan-out
    # job prefix (e.g. "run-daily-for-release-branches (8.1) / test-x:
    # unattributed failure"); strip it so historical and freshly-parsed
    # rows aggregate into the same heatmap row.
    parsed = parse_branch_job(cleaned)
    if parsed is not None:
        cleaned = parsed[1]
    if not _is_valid_test_name(cleaned):
        return None
    return cleaned


def _context_window(lines: List[str], center: int, radius: int = 5) -> str:
    """Return a window of *radius* lines around *center*, joined."""
    start = max(0, center - radius)
    end = min(len(lines), center + radius + 1)
    return "\n".join(lines[start:end])


def _find_line_index(lines: List[str], substring: str) -> int:
    """Return the index of the first line containing *substring*, or -1."""
    for i, line in enumerate(lines):
        if substring in line:
            return i
    return -1


def _extract_error_block(lines: List[str], start: int) -> str:
    """Extract the error detail lines following a failure marker.

    Collects non-empty lines after *start* until we hit another status
    marker (``[ok]``, ``[err]``, ``[skip]``, ``Passed``, ``Testing``,
    or a blank line after content).
    """
    detail_lines: List[str] = []
    # Status markers that signal the start of a new test result
    stop_markers = (
        "[ok]",
        "[err]",
        "[skip]",
        "[ignore]",
        "[TIMEOUT]",
        "Passed ",
        "Testing ",
        "*** [",
    )
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            # Allow one blank line, but stop on a second consecutive blank
            if detail_lines and not detail_lines[-1].strip():
                break
            detail_lines.append(line)
            continue
        if any(stripped.startswith(m) for m in stop_markers):
            break
        detail_lines.append(line)
        # Cap at a reasonable number of lines
        if len(detail_lines) > 30:
            break
    # Trim trailing blank lines
    while detail_lines and not detail_lines[-1].strip():
        detail_lines.pop()
    return "\n".join(detail_lines)


def parse_job_log(raw_log: str, job_name: Optional[str] = None) -> List[TestFailure]:
    """Parse a raw CI job log and return extracted test failures.

    Handles:
    * Valkey Tcl test framework ``[err]`` markers
    * End-of-run ``*** [err]`` summary lines
    * ``[TIMEOUT]`` markers
    * Google Test ``[  FAILED  ]`` markers
    * Sentinel/cluster ``FAILED:`` lines
    * Tcl ``[exception]`` stack traces with file references
    * A per-job "unattributed failure" fallback when the log clearly failed
      but no test name could be extracted (see issue #1)

    *job_name*, when provided, is used to key the unattributed-failure
    fallback bucket so those failures aggregate per job across days instead
    of splitting on a volatile runner message.

    Returns an empty list when no recognisable failure patterns are found.
    """
    if not raw_log or not raw_log.strip():
        return []

    # Strip GitHub Actions timestamp prefixes so ^-anchored regexes work
    cleaned = _strip_timestamps(raw_log)
    lines = cleaned.splitlines()
    seen: set[str] = set()  # deduplicate by (test_name)
    failures: List[TestFailure] = []

    # Did the log contain *any* failure signal?  Used to decide whether an
    # unattributed-failure bucket is warranted when no name could be parsed.
    signal_seen = bool(
        _TCL_ERR_RE.search(cleaned)
        or _SUMMARY_ERR_RE.search(cleaned)
        or _TIMEOUT_RE.search(cleaned)
        or _SUMMARY_TIMEOUT_RE.search(cleaned)
        or _TCL_EXCEPTION_RE.search(cleaned)
        or _SENTINEL_FAILED_RE.search(cleaned)
        or _GHA_ERROR_RE.search(cleaned)
        or re.search(r"^\[\s+FAILED\s+\]", cleaned, re.MULTILINE)
    )

    def _add(test_name: str, error_summary: str, log_lines: str) -> None:
        key = test_name.strip()
        if not _is_valid_test_name(key):
            return
        if key not in seen:
            seen.add(key)
            failures.append(
                TestFailure(
                    test_name=key,
                    error_summary=error_summary.strip(),
                    log_lines=log_lines.strip(),
                )
            )

    # 1. Tcl [err] markers (inline during test run)
    for m in _TCL_ERR_RE.finditer(cleaned):
        test_name = m.group(1).strip()
        test_file = m.group(2).strip()
        full_name = f"{test_name} in {test_file}"
        idx = _find_line_index(lines, m.group(0).strip())
        if idx >= 0:
            error_detail = _extract_error_block(lines, idx)
            context = _context_window(lines, idx, radius=8)
        else:
            error_detail = ""
            context = m.group(0)
        _add(full_name, error_detail or test_name, context)

    # 2. Summary *** [err] lines (end-of-run summary)
    for m in _SUMMARY_ERR_RE.finditer(cleaned):
        test_name = m.group(1).strip()
        test_file = m.group(2).strip()
        full_name = f"{test_name} in {test_file}"
        idx = _find_line_index(lines, m.group(0).strip())
        context = _context_window(lines, idx, radius=3) if idx >= 0 else m.group(0)
        # Only add if not already captured by the inline [err]
        _add(full_name, test_name, context)

    # 3. TIMEOUT markers
    #    First pass: collect *** [TIMEOUT] summary lines (have test name + file)
    timeout_summary_names: set[str] = set()
    for m in _SUMMARY_TIMEOUT_RE.finditer(cleaned):
        test_name = m.group(1).strip()
        test_file = m.group(2).strip()
        # Strip spawn-timeout pid tokens (not a real test name):
        #   "pid:NNNNN - tests/file.tcl"  (pid + inline file), or
        #   "pid:NNNNN"                    (bare pid; file is in group 2)
        if re.match(r"pid:\d+\s*-\s*", test_name) or re.fullmatch(
            r"pid:\d+", test_name
        ):
            test_name = "spawn timeout"
        full_name = f"{test_name} in {test_file}"
        timeout_summary_names.add(full_name)
        idx = _find_line_index(lines, m.group(0).strip())
        context = _context_window(lines, idx, radius=3) if idx >= 0 else m.group(0)
        _add(full_name, f"TIMEOUT: {test_name}", context)

    #    Second pass: handle bare [TIMEOUT] markers
    for m in _TIMEOUT_RE.finditer(cleaned):
        detail = m.group(1).strip()
        idx = _find_line_index(lines, m.group(0).strip())

        # If detail is the generic "clients state report follows." message,
        # look at the next line for "(IN PROGRESS) <real test name>" or "(SPAWNED SERVER)"
        if "clients state report follows" in detail.lower():
            if idx >= 0 and idx + 1 < len(lines):
                next_line = lines[idx + 1]
                prog_match = _IN_PROGRESS_RE.match(next_line)
                if prog_match:
                    detail = prog_match.group(1).strip()
                else:
                    # Try (SPAWNED SERVER) pattern: "sock<hex> => (SPAWNED SERVER) pid:N - file"
                    spawn_match = re.match(
                        r"sock[0-9a-f]+\s+=>\s+\(SPAWNED SERVER\)\s+pid:\d+\s*-\s*(.+)",
                        next_line,
                    )
                    if spawn_match:
                        detail = f"spawn timeout in {spawn_match.group(1).strip()}"

        # Bare spawn-timeout marker "pid:NNNNN - tests/file.tcl": attribute to
        # the file rather than leaking the volatile pid as a "test name".
        pid_spawn = re.match(r"pid:\d+\s*-\s*(.+)$", detail)
        if pid_spawn:
            detail = f"spawn timeout in {pid_spawn.group(1).strip()}"

        # Skip if the *** [TIMEOUT] summary already captured this test
        if any(detail in name for name in timeout_summary_names):
            continue

        test_name = detail
        context = _context_window(lines, idx, radius=5) if idx >= 0 else m.group(0)
        _add(test_name, f"TIMEOUT: {detail}", context)

    # 4. Google Test [  FAILED  ] markers
    #    GTest prints a summary block at the end listing all failed tests.
    #    We want the summary entries, not the inline ones (which duplicate).
    gtest_summary = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Detect the GTest summary section
        if re.match(r"^\[\s+FAILED\s+\]\s+\d+\s+test", stripped):
            gtest_summary = True
            continue
        if gtest_summary and stripped.startswith("[  FAILED  ]"):
            gm = _GTEST_FAILED_RE.match(stripped)
            if gm:
                test_name = gm.group(1).strip()
                # Remove trailing timing info like " (123 ms)"
                test_name = re.sub(r"\s*\(\d+\s*ms\)\s*$", "", test_name)
                context = _context_window(lines, i, radius=3)
                _add(test_name, f"GTest FAILED: {test_name}", context)

    # If no summary section found, pick up inline [  FAILED  ] markers
    if not gtest_summary:
        for m in _GTEST_FAILED_RE.finditer(cleaned):
            test_name = m.group(1).strip()
            test_name = re.sub(r"\s*\(\d+\s*ms\)\s*$", "", test_name)
            if test_name and not re.match(r"^\d+\s+test", test_name):
                idx = _find_line_index(lines, m.group(0).strip())
                context = (
                    _context_window(lines, idx, radius=5) if idx >= 0 else m.group(0)
                )
                _add(test_name, f"GTest FAILED: {test_name}", context)

    # 5. Sentinel/cluster "FAILED:" lines
    for m in _SENTINEL_FAILED_RE.finditer(cleaned):
        prefix = m.group(1).strip()
        message = m.group(2).strip()
        # The prefix often contains a timestamp and test name
        # e.g. "00:36:47>Master reboot in very short time"
        # Strip leading timestamps
        test_name = re.sub(r"^\d{2}:\d{2}:\d{2}\s*>\s*", "", prefix)
        if not test_name:
            test_name = message
        idx = _find_line_index(lines, m.group(0).strip())
        context = _context_window(lines, idx, radius=8) if idx >= 0 else m.group(0)
        _add(test_name, message, context)

    # 6. Tcl [exception] stack traces
    for m in _TCL_EXCEPTION_RE.finditer(cleaned):
        error_msg = m.group(1).strip()
        idx = _find_line_index(lines, m.group(0).strip())
        test_file = ""
        test_name_from_stack = ""
        if idx >= 0:
            stack_window = lines[idx : min(idx + 60, len(lines))]
            stack_text = "\n".join(stack_window)

            # Look for file reference
            file_match = _TCL_FILE_REF_RE.search(stack_text)
            if file_match:
                test_file = file_match.group(1)

            # Look for test name in the stack trace.
            # Strategy: find the procedure call line just before the
            # (file "...") reference — that has the resolved test name.
            # e.g. 'test_slave_buffers {slave buffer are counted correctly} 1000000 10 0 1'
            #       ^^^ procedure        ^^^ test name in braces
            # Skip Tcl control structures (foreach, if, while, etc.)
            _TCL_CONTROL = {
                "foreach",
                "if",
                "while",
                "for",
                "switch",
                "start_server",
                "cluster_setup",
            }
            if test_file:
                # Find the line index of the file reference within the window
                for wi, wline in enumerate(stack_window):
                    if test_file in wline and "(file" in wline:
                        # Look backwards from here for a quoted procedure call with {name}
                        for bi in range(wi - 1, -1, -1):
                            bline = stack_window[bi].strip().strip('"')
                            proc_m = re.match(r"(\w+)\s+\{([^}]+)\}", bline)
                            if proc_m:
                                proc_name = proc_m.group(1)
                                candidate = proc_m.group(2).strip()
                                if (
                                    proc_name not in _TCL_CONTROL
                                    and "$" not in candidate
                                    and len(candidate) > 2
                                ):
                                    test_name_from_stack = candidate
                                    break
                        break

            # Fallback: try 'test "name" {' — even with $variables, it groups well
            if not test_name_from_stack:
                test_match = _TCL_STACK_TEST_RE.search(stack_text)
                if test_match:
                    test_name_from_stack = test_match.group(1).strip()

            context = _context_window(lines, idx, radius=10)
        else:
            context = m.group(0)

        # Normalize: strip dynamic values (ports, IPs, pids, addresses) so
        # repeated incidents aggregate into one row instead of one per run.
        normalized_error = _scrub_volatile(error_msg)

        # Strip trailing Tcl $variable suffixes like "- $type"
        if test_name_from_stack:
            display_name = re.sub(r"\s*-\s*\$\w+\s*$", "", test_name_from_stack).strip()
        else:
            display_name = ""

        if display_name and test_file:
            test_name = f"{display_name} in {test_file}"
        elif display_name:
            test_name = f"{display_name} (exception)"
        elif test_file:
            test_name = f"Exception in {test_file}"
        else:
            test_name = f"Exception: {normalized_error[:80]}"
        _add(test_name, error_msg, context)

    # 7. Fallback: the job clearly failed but no test name could be extracted.
    #    Do NOT invent a fake test row from a generic runner message. Record
    #    exactly ONE stable per-job "unattributed failure" bucket so the failed
    #    job stays visible in the counts and aggregates across days (instead of
    #    splitting by exit code / pid). Keyed by job name when available.
    if not failures and signal_seen:
        crash = _CRASH_RE.search(cleaned)
        if crash:
            # A server crash with no attributable test -> dedicated crash bucket.
            # A log may contain several "crashed by signal" lines (intentional
            # crash-tests that pass earlier in the run); the one that actually
            # ended the run is the LAST. Derive signal + address from that block.
            crash_line_idxs = [
                i for i, line in enumerate(lines) if "crashed by signal:" in line
            ]
            cidx = crash_line_idxs[-1]
            # The line was selected by the same substring _CRASH_RE anchors on,
            # so a miss here means the regex and the selector disagree.
            crash_m = _CRASH_RE.search(lines[cidx])
            signal_no = crash_m.group(1) if crash_m else "?"
            block = "\n".join(lines[cidx : cidx + 8])
            addr_m = _CRASH_ADDR_RE.search(block)
            addr = f", address {addr_m.group(1)}" if addr_m else ""
            summary_msg = f"crashed by signal: {signal_no}{addr}"
            context = _context_window(lines, cidx, radius=15)
            bucket = f"{job_name}: server crash" if job_name else "server crash"
        else:
            gha = _GHA_ERROR_RE.search(cleaned)
            tmo = _TIMEOUT_RE.search(cleaned)
            if gha:
                summary_msg = gha.group(1).strip()
            elif tmo:
                summary_msg = f"TIMEOUT: {tmo.group(1).strip()}"
            else:
                summary_msg = ""
            idx = _find_line_index(lines, "##[error]")
            context = (
                _context_window(lines, idx, radius=15) if idx >= 0 else summary_msg
            )
            bucket = (
                f"{job_name}: unattributed failure"
                if job_name
                else "unattributed failure"
            )
        failures.append(
            TestFailure(
                test_name=bucket,
                error_summary=summary_msg,
                log_lines=context.strip(),
            )
        )

    return failures
