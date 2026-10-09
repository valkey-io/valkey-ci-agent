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


# Cross-tool error classes. Valgrind and the sanitizers report the same bug in
# different vocabularies, so an identity that spans both needs a class each can
# be mapped onto. The classes are coarse on purpose: they only have to be equal
# for one bug seen by both tools, and unequal for different bugs at one site.
_CLASS_LEAK = "leak"
_CLASS_USE_AFTER_FREE = "use-after-free"
_CLASS_BUFFER_OVERFLOW = "buffer-overflow"
_CLASS_UNINITIALIZED = "uninitialized"
_CLASS_INVALID_FREE = "invalid-free"
_CLASS_RUNTIME_ERROR = "runtime-error"
# A null or wild pointer: the address was never a heap block. Distinct from a
# use after free, and from an overflow of a real block.
_CLASS_WILD_POINTER = "wild-pointer"

# Sanitizer diagnostics name their class outright, so a substring is enough.
# Ordered: the longer, more specific names come first so "use-after-poison" is
# not shadowed by a bare overflow match.
_SANITIZER_CLASS_TOKENS: tuple[tuple[str, str], ...] = (
    ("heap-use-after-free", _CLASS_USE_AFTER_FREE),
    ("use-after-poison", _CLASS_USE_AFTER_FREE),
    ("heap-buffer-overflow", _CLASS_BUFFER_OVERFLOW),
    ("stack-buffer-overflow", _CLASS_BUFFER_OVERFLOW),
    ("stack-buffer-underflow", _CLASS_BUFFER_OVERFLOW),
    ("dynamic-stack-buffer-overflow", _CLASS_BUFFER_OVERFLOW),
    ("global-buffer-overflow", _CLASS_BUFFER_OVERFLOW),
    ("alloc-dealloc-mismatch", _CLASS_INVALID_FREE),
    ("attempting double-free", _CLASS_INVALID_FREE),
    ("bad-free", _CLASS_INVALID_FREE),
    ("use-of-uninitialized-value", _CLASS_UNINITIALIZED),
    ("LeakSanitizer", _CLASS_LEAK),
    ("detected memory leaks", _CLASS_LEAK),
    ("runtime error", _CLASS_RUNTIME_ERROR),
)

# Valgrind diagnostics that name their class outright. "Invalid read/write" is
# absent: it is shared by use-after-free and overflow and needs the follow-up
# address description to tell them apart (see _valgrind_access_class).
_VALGRIND_CLASS_TOKENS: tuple[tuple[str, str], ...] = (
    ("definitely lost", _CLASS_LEAK),
    ("indirectly lost", _CLASS_LEAK),
    ("possibly lost", _CLASS_LEAK),
    ("Mismatched free", _CLASS_INVALID_FREE),
    ("Invalid free", _CLASS_INVALID_FREE),
    ("uninitialised value", _CLASS_UNINITIALIZED),
    ("uninitialized value", _CLASS_UNINITIALIZED),
    ("Conditional jump", _CLASS_UNINITIALIZED),
)

_VALGRIND_ACCESS_RE = re.compile(r"\bInvalid (?:read|write)\b")

# Valgrind's address descriptions. The unallocated form has to be tested first:
# it contains the word "free'd" while meaning the address was never a heap
# block, so matching on that word alone files a null dereference as a use after
# free and merges it into an unrelated bug's issue.
_VALGRIND_UNALLOCATED_RE = re.compile(
    r"is not stack'd, malloc'd or \(recently\) free'd"
)
_VALGRIND_FREED_RE = re.compile(r"\bfree'd\b")

# Valgrind's address description follows the offending access and its stack,
# and is the only thing distinguishing a use-after-free ("... free'd") from an
# overflow ("... after a block of size N alloc'd"). It appears within a few
# lines of the access, so the lookahead is bounded rather than scanning the
# whole report and picking up an unrelated later error.
_VALGRIND_ADDRESS_LOOKAHEAD = 15


def _valgrind_access_class(lines: list[str], start: int) -> str | None:
    """Class of a valgrind "Invalid read/write" from its address description.

    Returns None when no description is found in the lookahead window, which
    keeps the failure on its per-tool identity rather than guessing a class.
    """
    for line in lines[start + 1 : start + 1 + _VALGRIND_ADDRESS_LOOKAHEAD]:
        # "not stack'd, malloc'd or (recently) free'd" contains "free'd" but
        # means the opposite: the address was never a heap block at all, which
        # is a null or wild pointer rather than a use after free.
        if _VALGRIND_UNALLOCATED_RE.search(line):
            return _CLASS_WILD_POINTER
        if _VALGRIND_FREED_RE.search(line):
            return _CLASS_USE_AFTER_FREE
        if "after a block" in line or "before a block" in line:
            return _CLASS_BUFFER_OVERFLOW
    return None


def _classified_diagnostics(
    error: str, failure_type: FailureType,
) -> list[tuple[str, int]]:
    """Every classifiable diagnostic in *error*, as ``(class, line index)``.

    The index is where the diagnostic was found, so a caller can take the stack
    that belongs to it rather than whichever stack comes first in the buffer.
    """
    if failure_type == FailureType.VALGRIND:
        tokens = _VALGRIND_CLASS_TOKENS
    elif failure_type == FailureType.SANITIZER:
        tokens = _SANITIZER_CLASS_TOKENS
    else:
        return []

    found: list[tuple[str, int]] = []
    lines = [line.strip() for line in scrub_volatile_tokens(error).split("\n")]
    for index, line in enumerate(lines):
        if not line or any(bp in line for bp in _BOILERPLATE_SUBSTRINGS):
            continue
        matched = next((cls for token, cls in tokens if token in line), None)
        if matched is None and failure_type == FailureType.VALGRIND:
            if _VALGRIND_ACCESS_RE.search(line):
                matched = _valgrind_access_class(lines, index)
        if matched is not None:
            found.append((matched, index))
    return found


def _classified_diagnostic(
    error: str, failure_type: FailureType,
) -> tuple[str, int] | None:
    """The first classifiable diagnostic, or None when there is none."""
    found = _classified_diagnostics(error, failure_type)
    return found[0] if found else None


def error_class(error: str, failure_type: FailureType) -> str | None:
    """Cross-tool class of *error*, or None when it cannot be determined.

    Only valgrind and sanitizer errors are classified; every other type keeps
    its own identity and never participates in a cross-tool merge.

    None is returned rather than a catch-all class whenever the vocabulary is
    unrecognized, so an unclassifiable report stays separate instead of
    merging on a guess.
    """
    classified = _classified_diagnostic(error, failure_type)
    return None if classified is None else classified[0]


def stack_anchor(error: str) -> str:
    """Frame chain of *error*'s first stack block, or "" when it has none.

    The same anchor both tools reduce to (see :func:`_extract_stack_anchor`),
    exposed so the renderer can key a cross-tool identity on it.
    """
    text = scrub_volatile_tokens(error)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return _extract_stack_anchor(lines)


# Frames nearest the bug used to match one bug across tools.
#
# Two, and the width is a genuine tradeoff rather than a setting that satisfies
# everything. Valgrind and the sanitizers stop at different depths on one stack,
# so a wider chain can disagree between them and leave one bug with an issue per
# tool. One frame is narrower than a bug: two distinct allocations reached
# through a shared constructor collapse into one issue, which loses a report
# outright. Two prefers the recoverable failure: a duplicate issue a maintainer
# can close, over a bug that is never filed.
_CROSS_TOOL_ANCHOR_FRAMES = 2


def cross_tool_identity(
    error: str, failure_type: FailureType,
) -> tuple[str, str] | None:
    """A valgrind/sanitizer report's ``(class, anchor)`` identity, or None.

    Both halves are read from the same diagnostic, and a report whose
    diagnostics disagree on the class is refused. A tool hands over its whole
    stderr buffer, so a report can describe several errors, and there is then no
    single bug for one identity to name: two distinct use-after-frees whose
    buffers both opened with a shared uninitialised-value warning reduced to the
    same identity, which collapsed them into one issue and discarded a report
    entirely. Such a report keeps its per-tool identity instead.

    Agreeing diagnostics are not refused. A leak report names its leak once per
    loss record and again in the summary breakdown, so a single bug routinely
    classifies several times; requiring exactly one match rejected every real
    valgrind leak.

    Returns None when no diagnostic classifies, when they disagree, or when the
    first one has no stack.

    Refusing a mixed buffer only keeps it out of the cross-tool pool. The
    per-tool identity it falls back to anchors on the first stack block too, so
    two such reports whose leading error is the same still group together; that
    is the pre-existing granularity of normalize_error_identity, not something
    the merge introduces.
    """
    diagnostics = _classified_diagnostics(error, failure_type)
    if not diagnostics:
        return None
    if len({cls for cls, _ in diagnostics}) > 1:
        return None
    cls, index = diagnostics[0]

    lines = [line.strip() for line in scrub_volatile_tokens(error).split("\n")]
    # Take the stack that follows the classified diagnostic. Valgrind prints the
    # frames under the diagnostic line; the sanitizers print a line or two of
    # detail first, which _extract_stack_anchor skips over.
    anchor = _extract_stack_anchor([line for line in lines[index:] if line])
    if not anchor:
        return None
    frames = _leading_frames(anchor)
    # A stripped build names no function, so every such report reduces to the
    # same "??? > ???" chain and unrelated bugs would share one identity.
    if not _names_any_symbol(frames):
        return None
    return cls, frames


# A frame valgrind could not symbolicate. The chain is built from these when the
# build carries no symbols, and they identify nothing.
_UNSYMBOLICATED_FRAME = "???"


def _names_any_symbol(anchor: str) -> bool:
    """Whether *anchor* names at least one real function."""
    _, _, chain = anchor.partition(": ")
    return any(
        frame.split(" (")[0] != _UNSYMBOLICATED_FRAME
        for frame in chain.split(" > ")
    )


def _leading_frames(anchor: str) -> str:
    """The leading :data:`_CROSS_TOOL_ANCHOR_FRAMES` frames of *anchor*."""
    prefix, _, chain = anchor.partition(": ")
    frames = chain.split(" > ")[:_CROSS_TOOL_ANCHOR_FRAMES]
    return f"{prefix}: " + " > ".join(frames)


def cross_tool_anchor(error: str) -> str:
    """The leading :data:`_CROSS_TOOL_ANCHOR_FRAMES` frames of *error*'s stack.

    Returns "" when the error has no stack frames. Shorter chains are returned
    whole rather than rejected: a stack that ends before the cap is the entire
    call path the tool reported, so it is the strongest identity available.
    """
    anchor = stack_anchor(error)
    if not anchor:
        return ""
    prefix, _, chain = anchor.partition(": ")
    frames = chain.split(" > ")[:_CROSS_TOOL_ANCHOR_FRAMES]
    return f"{prefix}: " + " > ".join(frames)


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
    text = scrub_volatile_tokens(error)

    lines = [line.strip() for line in text.split("\n") if line.strip()]

    # Extract up to 3 significant lines for the identity, skipping tool
    # boilerplate that is identical in every run of every bug.
    significant: list[str] = []
    for index, line in enumerate(lines[:30]):
        if any(bp in line for bp in _BOILERPLATE_SUBSTRINGS):
            continue
        if any(kw in line for kw in _SIGNIFICANT_KEYWORDS):
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
