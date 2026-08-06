"""Parse and deduplicate test failures from the consolidated artifact.

Handles multiple failure types: assertion errors (the original case),
sanitizer/valgrind memory errors, timeouts, exceptions, server startup
failures, and gtest unit-test failures.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class FailureType(str, Enum):
    """Classification of a CI test failure."""

    ASSERTION = "assertion"
    SANITIZER = "sanitizer"
    VALGRIND = "valgrind"
    TIMEOUT = "timeout"
    STARTUP = "startup"
    EXCEPTION = "exception"
    MEMORY_LEAK = "memory-leak"
    UNITTEST = "unittest"


# The datestamp a server log line opens with, in either of the two shapes the
# server emits: "943:C 29 Jul 04:15:32.117" and "2026-07-29 04:15:32.117". None
# of it has a run of four digits for the bare-number pattern below to catch, so
# without this a startup failure whose reason is a log line (the runner hands
# over the whole log when the server never came up) gets a fresh identity, and a
# fresh issue, every night.
#
# Exported: the title has to scrub at least what the identity does, or one issue
# is retitled on every recurrence.
TIMESTAMP_PATTERNS = (
    re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?"),
    # A log line's "<pid>:<role> <day> <month> <clock>" prefix.
    re.compile(
        r"\b\d+:[A-Za-z]\s+\d{1,2}\s+[A-Z][a-z]{2}\s+\d{1,2}:\d{2}:\d{2}(?:\.\d+)?"
    ),
    re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b"),
)

# Patterns stripped from error text to produce a stable identity across runs.
_VOLATILE_PATTERNS = (
    # ANSI escape codes
    re.compile(r"\033\[[0-9;]*m"),
    *TIMESTAMP_PATTERNS,
    # Valgrind PID annotations: ==12345==
    re.compile(r"==\d+==\s*"),
    # Hex addresses: 0xDEADBEEF
    re.compile(r"0x[0-9a-fA-F]+"),
    # Temp paths: /tmp/foo/bar.123
    re.compile(r"/tmp/[^\s:]+"),
    # PID/port annotations: pid 12345, port=6379, port 6379
    re.compile(r"\b(?:pid|port)[=\s]+\d+", re.IGNORECASE),
    # Bare large numbers (>=4 digits) that are likely PIDs/timestamps/addresses.
    # A number directly behind a source file extension is a line number, not
    # noise: it is often the only thing distinguishing two bugs in one function
    # (a use-after-free at cluster_legacy.c:3421 versus one at :5109), and a
    # four-digit line is common in this codebase.
    re.compile(r"(?<!\.c:)(?<!\.h:)(?<!\.cc:)(?<!\.cpp:)(?<!\.hpp:)\b\d{4,}\b"),
)

# Lines containing these keywords are considered "significant" for identity.
_SIGNIFICANT_KEYWORDS = (
    "error:", "Error:", "ERROR:",
    "Invalid", "Mismatched", "uninitialized",
    "runtime error", "Sanitizer", "SUMMARY:",
    "fishy", "overlap", "Can't start",
    "heap-buffer-overflow", "heap-use-after-free",
    "stack-buffer-overflow", "use-after-poison",
    "definitely lost", "LEAK SUMMARY",
)

# Report boilerplate that matches a significance keyword but carries no bug
# identity. The "error:" keyword would otherwise match the tool banner via the
# runner's wrapper prefix ("Valgrind error: ==123== Memcheck, ..."). Valgrind's
# "ERROR SUMMARY: N errors from N contexts" matches "SUMMARY:" but its count
# varies run to run and its position drifts, so it must not reach the identity
# (two runs of one leak whose summary lands inside vs outside the line window
# would otherwise fingerprint differently). The sanitizer's meaningful summary
# is "SUMMARY: AddressSanitizer: ...", which has no "ERROR" prefix and survives.
_BOILERPLATE_SUBSTRINGS = (
    "Memcheck, a memory error detector",
    "HEAP SUMMARY:",
    "LEAK SUMMARY:",
    "ERROR SUMMARY:",
)

# The width of a bad access ("Invalid read of size 4"). One out-of-bounds
# access is reported at whatever width the compiler chose for that load, so the
# same bug reads as size 4 in one build and size 8 in another. The width is
# dropped but the diagnostic is kept, since it names the error class; the stack
# site below is what identifies which access it was.
_ACCESS_WIDTH_RE = re.compile(r"\b(Invalid (?:read|write) of size)\s+\d+")

# Heap-layout coordinates and allocation sizes: the same leak moves between
# loss records and can vary in size run to run, so these must not feed the
# fingerprint identity. The unit list spans all three leak detectors, since
# each words its counts differently: valgrind "41 bytes in 1 blocks",
# LeakSanitizer "41 byte(s) in 1 object(s)" and "1 allocation(s)", and
# /usr/bin/leaks "1 leak for 48 total leaked bytes".
_VOLATILE_COUNT_PATTERNS = (
    re.compile(r"\bin loss record \d[\d,]* of \d[\d,]*"),
    # The "(s)" forms are a separate alternative, not extra branches alongside
    # the bare words: a trailing \b cannot match after ")" (the next character
    # is punctuation or end of line, so there is no word boundary there), and
    # "bytes?" would otherwise match the "byte" inside "byte(s)" first and
    # strand "(s)" in the identity.
    re.compile(
        r"\b\d[\d,]*\s+(?:byte|object|allocation|leak)\(s\)"
        r"|\b\d[\d,]*\s+(?:bytes?|blocks?|leaks?)\b"
    ),
    # Any remaining thousands-separated number is a count/size.
    re.compile(r"\b\d{1,3}(?:,\d{3})+\b"),
)

# A macOS /usr/bin/leaks root-leak line: "1 (48 bytes) ROOT LEAK: <malloc in
# sdsnewlen 0x600001d1c100> [48]". The only lines in a leaks report that name
# the allocation site, so they anchor the identity and keep two leaks in one
# test file as two issues. Addresses and sizes are scrubbed before this runs.
#
# Anchored on the leading count: with stack logging the report also prints a
# heading quoting the same text ("STACK OF 1 INSTANCE OF 'ROOT LEAK: <...>':"),
# which an unanchored match read as a second, differently-spelled site.
_ROOT_LEAK_RE = re.compile(
    r"^\s*\d+\s+\([^)]*\)\s+ROOT LEAK:\s*(?P<site>[^\[]+)", re.MULTILINE
)

# The startup blob's config dump: "Can't start <exe>\nCONFIGURATION:\n<full
# config file>\nERROR:\n<reason>". The config is dozens of lines shared by
# every startup failure; left in place it fills the significant-line window
# before the ERROR: reason is reached, collapsing all startup causes into one
# identity. The reason after ERROR: is the identity; the dump is not.
_STARTUP_CONFIG_SECTION_RE = re.compile(
    r"\nCONFIGURATION:\n.*?\nERROR:\n", re.DOTALL
)

# The startup blob opens with the server executable's absolute path, which is
# the runner's workspace layout rather than anything about the bug: the same
# failure reads "/home/runner/work/..." on a Linux runner, "/Users/runner/..."
# on macOS, and "/__w/..." in a container job. Left verbatim it splits one
# startup bug into an issue per platform. Only the path is reduced; the
# "Can't start" prefix stays, since it is what marks the blob as a startup
# failure downstream.
_STARTUP_EXE_PATH_RE = re.compile(r"(Can't start )\S*/([^/\s]+)")

# The startup blob's captured stderr is prefixed with runner and server
# progress lines that carry no cause: the harness's "### Starting server for
# test" marker, the "*** FATAL CONFIG FILE ERROR ***" banner (identical for
# every config error), and the ">>> '<directive>'" echo of the offending line.
# Skipping these reaches the fatal reason itself ("Bad directive or wrong
# number of arguments"), which is what tells two startup causes apart.
_STARTUP_NOISE_PREFIXES = ("***", "###", ">>>")

# The server's config loader prints the fatal reason last, behind a fixed
# banner and, only when it knows the offending line, a position line and an
# echo of that line. The banner is identical across causes and the position
# moves whenever the config file changes, so neither can be the identity; the
# reason after them is. Nothing before the banner qualifies either: under
# valgrind the capture opens with the tool's own startup banner.
_FATAL_CONFIG_BANNER_PREFIX = "*** FATAL CONFIG FILE ERROR"
_CONFIG_POSITION_PREFIX = "Reading the configuration file, at line"


def startup_reason_from_lines(lines: list[str]) -> str:
    """The fatal reason from a startup blob's stderr lines, or "".

    Expects lines already scrubbed of volatile tokens and stripped of
    surrounding whitespace.
    """
    for index, line in enumerate(lines):
        if not line.startswith(_FATAL_CONFIG_BANNER_PREFIX):
            continue
        for following in lines[index + 1 :]:
            if not following or following.startswith(
                (_CONFIG_POSITION_PREFIX, ">>>")
            ):
                continue
            return following
        return ""

    # No config-file diagnostic: the reason is the first line that is neither
    # blank nor progress/banner noise (e.g. the harness's own summary message).
    for line in lines:
        if line and not line.startswith(_STARTUP_NOISE_PREFIXES):
            return line
    return ""


# A stack frame after volatile stripping. Valgrind: "at : malloc (...)" or
# "by : sdsdup (sds.c:190)". Sanitizer: "#1  in ztrymalloc_usable_internal
# /.../zmalloc.c:172" (the "#N 0xADDR in func" shape with the address
# scrubbed). The function name plus its source location is the stable identity
# anchor; addresses, sizes, and loss records around them are not.
_STACK_FRAME_RE = re.compile(
    r"^(?:at|by)\s*:\s*(?P<func>[^\s(]+)\s*"
    r"(?:\((?P<file>[^():]+):(?P<line>\d+)?)?"
    r"|^#\d+\s+in\s+(?P<san_func>\S+)"
    r"(?:\s+(?P<san_file>\S+?):(?P<san_line>\d+))?"
)

# Allocation wrappers in zmalloc.c and sds.c that every heap operation passes
# through. Which of them appear depends on the toolchain, not the bug: clang
# inlines sdsnewlen/sdsdup into their caller while gcc emits separate frames, so
# an anchor that keeps them makes one leak look like two bugs across compilers.
#
# Matched by name, not by file: those files also hold ordinary code that can be
# the faulting frame itself, and dropping the whole file reduced two distinct
# defects in one file (a bad write in sdscatlen and one in sdsrange) to the same
# anchor.
_ALLOC_WRAPPER_FUNCS = frozenset({
    "zmalloc", "zcalloc", "zrealloc", "zfree",
    "ztrymalloc", "ztrycalloc", "ztryrealloc",
    "zmalloc_usable", "ztrymalloc_usable",
    "zmalloc_usable_internal", "ztrymalloc_usable_internal",
    "zrealloc_usable", "ztryrealloc_usable", "ztryrealloc_usable_internal",
    "sdsnewlen", "_sdsnewlen", "sdstrynewlen", "sdsnew", "sdsempty",
    "sdsdup", "sdsgrowzero", "sdsMakeRoomFor", "sdsMakeRoomForExact",
})
_PLUMBING_FRAME_FILES = frozenset({"zmalloc.c", "sds.c"})
_SANITIZER_RUNTIME_PATH_RE = re.compile(
    r"libsanitizer|sanitizer_common|/(?:asan|lsan|ubsan|tsan|msan)[_/]"
)
# Valgrind's own replacement and preload sources. A bad access is reported
# with valgrind's interceptor on top ("memcpy (vg_replace_strmem.c:1035)"),
# whose line number moves when valgrind is upgraded. Left in, that line is the
# one the anchor keeps, so every such issue refiles on a valgrind bump; and it
# names valgrind's source, never valkey's, so it identifies nothing.
_VALGRIND_RUNTIME_FILE_RE = re.compile(r"^(?:vg_replace_|vg_preloaded|vgpreload)")

# The allocator entry point is the top frame of every heap report, so it never
# distinguishes two leaks. It is matched by name because the frame's location
# is not reliably a source path: gcc resolves the interceptor into the
# sanitizer's own sources, while clang reports a binary offset
# ("malloc (/path/to/valkey-server+0x20de33)") that no file check can catch.
_ALLOCATOR_FRAME_FUNCS = frozenset(
    {"malloc", "calloc", "realloc", "valloc", "operator new", "_Znwm", "_Znam"}
)


def is_plumbing_frame(func: str, source_path: str) -> bool:
    """Whether a stack frame is shared allocation/interceptor plumbing.

    These frames are present or absent depending on the toolchain's inlining
    rather than on the bug, so they must not reach the identity, and the title
    must not name them either. ``source_path`` may be "" for a frame whose
    location is a binary offset rather than a source file.
    """
    if func in _ALLOCATOR_FRAME_FUNCS:
        return True
    if not source_path:
        return False
    if _SANITIZER_RUNTIME_PATH_RE.search(source_path):
        return True
    source_file = source_path.rsplit("/", 1)[-1]
    if _VALGRIND_RUNTIME_FILE_RE.match(source_file):
        return True
    return (
        source_file in _PLUMBING_FRAME_FILES
        and func in _ALLOC_WRAPPER_FUNCS
    )


def _extract_root_leak_anchor(lines: list[str]) -> str:
    """Allocation-site chain of a macOS leaks report, or "".

    A leaks blob has no stack frames, so without this anchor every leak in
    one test file normalizes to the same boilerplate and two distinct leaks
    collapse into one issue. Unsymbolicated roots (bare scrubbed addresses)
    yield an empty site and are skipped; sorted so report order does not
    change the identity.
    """
    sites: set[str] = set()
    for line in lines:
        match = _ROOT_LEAK_RE.search(line)
        if not match:
            continue
        site = match.group("site").strip()
        if site:
            sites.add(site)
    if not sites:
        return ""
    return "roots: " + " > ".join(sorted(sites))


def _frame_anchor(func: str, source_path: str, source_line: str = "") -> str:
    """One frame's identity: "func (file.c:120)", or just "func".

    The file is reduced to its basename so the runner's workspace layout
    ("/home/runner/work/..." on Linux, "/Users/runner/..." on macOS, "/__w/..."
    in a container) cannot split one bug into an issue per platform.

    ``source_line`` is supplied only for the frame that names the bug (see
    :func:`_extract_stack_anchor`). Two bugs can share a function, and then the
    line is the only thing telling them apart, but every line below the bug
    belongs to unrelated code that shifts whenever that code is edited, so
    carrying the whole chain's lines would refile one bug on every commit that
    touched anything above it in the call stack.
    """
    if not source_path:
        return func
    source_file = source_path.rstrip(":").rsplit("/", 1)[-1]
    if not source_file:
        return func
    if not source_line:
        return f"{func} ({source_file})"
    return f"{func} ({source_file}:{source_line})"


def _extract_stack_anchor(lines: list[str]) -> str:
    """Frame chain of the first stack block in a valgrind or sanitizer report.

    Distinguishes two different leaks whose report lines are otherwise
    identical after count scrubbing (e.g. same "definitely lost" shape but
    allocated from debugCommand vs clusterCommand). Returns "" when the
    error has no stack frames (assertions, startup failures).

    Only the first frame carries its line number. That frame is the bug; the
    ones below it are its callers, whose lines move whenever unrelated code in
    them is edited. Keeping the whole chain's lines refiled one leak every time
    any caller shifted, which is most commits.
    """
    frames: list[str] = []
    in_stack = False
    for line in lines:
        match = _STACK_FRAME_RE.match(line)
        if match:
            in_stack = True
            func = match.group("func") or match.group("san_func")
            source_path = match.group("file") or match.group("san_file") or ""
            source_line = match.group("line") or match.group("san_line") or ""
            if not is_plumbing_frame(func, source_path):
                frames.append(_frame_anchor(
                    func, source_path, source_line if not frames else "",
                ))
                if len(frames) >= 8:
                    break
        elif in_stack:
            # First stack block ended; a second block would belong to a
            # different loss record and make the identity order-sensitive.
            break
    if not frames:
        return ""
    return "stack: " + " > ".join(frames)

# Test names that carry no real test identity: they're volatile artifacts of
# whatever the runner happened to be doing when it timed out (spawning a
# server, between tests). Using them as identity would mint a fresh
# fingerprint (and a fresh issue) every run.
_VOLATILE_TEST_NAME_RE = re.compile(
    r"^(?:"
    r"pid:\d+"           # server PID annotation: "pid:92663"
    r"|hang"             # generic "hang in <file> (last state: ...)"
    r")$"
)


def scrub_volatile_tokens(text: str) -> str:
    """Remove the run-specific tokens that must not reach a failure's identity.

    Phrase patterns run before bare-number ones: a phrase match spans the
    digits inside it ("in loss record 1001 of 1,110"), so scrubbing bare
    numbers first would strip the digits and strand the phrase, which would
    then drift between runs of one bug.

    Exposed so the recurrence comparison in :mod:`issue_renderer` scrubs at
    least what the identity scrubs. Two traces the fingerprint calls the same
    bug must compare equal there, or every recurrence posts a redundant
    "new error stack trace".
    """
    text = _ACCESS_WIDTH_RE.sub(r"\1", text)
    for pattern in _VOLATILE_COUNT_PATTERNS:
        text = pattern.sub("", text)
    for pattern in _VOLATILE_PATTERNS:
        text = pattern.sub("", text)
    return text


def normalize_error_identity(error: str) -> str:
    """Extract a stable identity from an error message for fingerprinting.

    Strips run-specific volatile tokens (PIDs, hex addresses, temp paths,
    timestamps) and extracts the first few meaningful lines that characterize
    the error type and location.

    Two runs that hit the same bug with different PIDs/addresses will produce
    the same normalized identity.
    """
    text = _STARTUP_CONFIG_SECTION_RE.sub("\nERROR:\n", error)
    text = _STARTUP_EXE_PATH_RE.sub(r"\1\2", text)
    text = scrub_volatile_tokens(text)

    lines = [line.strip() for line in text.split("\n") if line.strip()]

    # Extract up to 3 significant lines for the identity, skipping tool
    # boilerplate that is identical in every run of every bug.
    significant: list[str] = []
    for index, line in enumerate(lines[:30]):
        if any(bp in line for bp in _BOILERPLATE_SUBSTRINGS):
            continue
        if any(kw in line for kw in _SIGNIFICANT_KEYWORDS):
            if line == "ERROR:" and index + 1 < len(lines):
                # A bare "ERROR:" header (startup blob's stderr separator)
                # matches the keyword but names no bug; the fatal reason
                # follows it, behind progress and banner lines that are
                # identical across causes. Take the reason, not the blob's
                # last line: a trailing server-log tail would bind the
                # identity to volatile text and mint a fresh issue per run.
                reason = startup_reason_from_lines(lines[index + 1 :])
                if reason:
                    line = f"ERROR: {reason}"
            significant.append(line)
            if len(significant) >= 3:
                break

    # The stack frames (or a macOS leaks report's root-leak sites) pin the
    # identity to the code path, so two leaks with identical report lines but
    # different allocation sites stay distinct.
    anchor = _extract_stack_anchor(lines) or _extract_root_leak_anchor(lines)
    if significant:
        return "\n".join([*significant, anchor] if anchor else significant)
    if anchor:
        return anchor
    # Fall back to first 3 non-empty lines
    return "\n".join(lines[:3])


@dataclass
class JobReference:
    """A reference to a specific CI job where a test failed."""

    job: str
    suite: str
    url: str = ""


@dataclass
class UniqueFailure:
    """A deduplicated test failure that may appear across multiple jobs."""

    test_name: str
    test_file: str
    failure_type: FailureType = FailureType.ASSERTION
    error: str = ""
    jobs: list[JobReference] = field(default_factory=list)
    # Traces from other tools that reported this same bug, as (label, text).
    # Valgrind and the sanitizers describe one bug in different vocabularies,
    # so when both fire in a run their reports are kept side by side instead of
    # discarding one: each names details the other omits. Populated when two
    # failures merge under one fingerprint; empty for a single-tool failure.
    extra_traces: list[tuple[str, str]] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        if self.test_name:
            return f"{self.test_name} in {self.test_file}"
        return f"[{self.failure_type.value}] in {self.test_file or 'unknown'}"

    @property
    def has_test_identity(self) -> bool:
        """Whether this failure has a meaningful test_name for fingerprinting."""
        return bool(self.test_name)


def _coerce_str(value: Any) -> str:
    """Return *value* if it is a string, else "".

    Non-string artifact values (an int PID, null) carry no usable test
    identity or error text, so they are treated as absent rather than
    stringified into a bogus identity.
    """
    return value if isinstance(value, str) else ""


def parse_and_deduplicate(
    all_failures: dict[str, Any],
    job_urls: dict[str, str],
) -> list[UniqueFailure]:
    """Parse the all-test-failures JSON and deduplicate.

    Args:
        all_failures: The parsed all-test-failures.json content.
            Structure: {job_name: {suite_name: [{test_name, test_file, type?, error}]}}
        job_urls: Mapping of job name -> HTML URL for CI links.

    Returns:
        List of UniqueFailure objects, deduplicated across jobs.

    Grouping keys, in the order they are tried:
        - a failure naming a test: (failure_type, test_name, test_file)
        - a nameless timeout with a file: (failure_type, test_file), since every
          timeout carries the same generic error text
        - any other nameless failure: (failure_type, normalized_error_identity).
          The file is left out so one memory error reported after different test
          files stays one issue rather than one per file.
    """
    grouped: dict[tuple, UniqueFailure] = {}

    if not isinstance(all_failures, dict):
        logger.warning(
            "Unexpected top-level format: expected dict, got %s",
            type(all_failures).__name__,
        )
        return []

    for job_name, suites in all_failures.items():
        if not isinstance(suites, dict):
            logger.warning(
                "Unexpected format for job %r: expected dict, got %s",
                job_name, type(suites).__name__,
            )
            continue

        for suite_name, entries in suites.items():
            if not isinstance(entries, list):
                logger.warning(
                    "Unexpected format for %s/%s: expected list, got %s",
                    job_name, suite_name, type(entries).__name__,
                )
                continue

            for entry in entries:
                if not isinstance(entry, dict):
                    continue

                # Field values are producer-controlled; a non-string (int PID,
                # null) must degrade to one bad entry, not a TypeError that
                # aborts the whole batch in the regex calls below.
                test_name = _coerce_str(entry.get("test_name", ""))
                test_file = _coerce_str(entry.get("test_file", ""))
                error = _coerce_str(entry.get("error", ""))
                raw_type = entry.get("type", "assertion")

                try:
                    failure_type = FailureType(raw_type)
                except ValueError:
                    # The producer emits a type this enum doesn't know yet.
                    # Exception is the catch-all for non-assertion errors;
                    # warn so producer/consumer drift is visible in run logs.
                    logger.warning(
                        "Unknown failure type %r, classifying as exception", raw_type
                    )
                    failure_type = FailureType.EXCEPTION

                # Volatile test names (bare PIDs, "hang") are transient
                # runner state, not real test identity. Demote them so the
                # grouping key is stable across runs.
                if test_name and _VOLATILE_TEST_NAME_RE.fullmatch(test_name):
                    logger.info(
                        "Demoted volatile test name %r to nameless "
                        "(type=%s, file=%s, job=%s)",
                        test_name, failure_type.value, test_file, job_name,
                    )
                    test_name = ""

                # Determine grouping key
                if test_name:
                    key: tuple = (failure_type, test_name, test_file)
                elif failure_type == FailureType.TIMEOUT and test_file:
                    # Nameless timeouts (volatile PID/hang demoted above, or
                    # captured without a test body running) group by file:
                    # the error text is generic ("Test timed out") across all
                    # timeouts, so without the file every timeout in the run
                    # would collapse into one issue.
                    key = (failure_type, test_file)
                elif error:
                    identity = normalize_error_identity(error)
                    key = (failure_type, identity)
                else:
                    logger.debug(
                        "Skipping entry with no test_name and no error: %s", entry
                    )
                    continue

                if key not in grouped:
                    grouped[key] = UniqueFailure(
                        test_name=test_name,
                        test_file=test_file,
                        failure_type=failure_type,
                        error=error,
                    )

                failure = grouped[key]
                if not any(j.job == job_name for j in failure.jobs):
                    failure.jobs.append(
                        JobReference(
                            job=job_name,
                            suite=suite_name,
                            url=job_urls.get(job_name, ""),
                        )
                    )
                    logger.debug("%s in %s/%s", failure.display_name, job_name, suite_name)

    unique_failures = list(grouped.values())
    if unique_failures:
        type_counts: dict[str, int] = {}
        for f in unique_failures:
            type_counts[f.failure_type.value] = type_counts.get(f.failure_type.value, 0) + 1
        logger.info("Total unique failures: %d (by type: %s)", len(unique_failures), type_counts)
    else:
        logger.info("Total unique failures: 0")
    return unique_failures
