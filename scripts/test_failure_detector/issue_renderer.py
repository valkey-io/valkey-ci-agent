"""Render detected test failures into GitHub issue title, body, and comment text.

Supports multiple failure types (assertion, sanitizer, valgrind, timeout,
exception, startup, memory-leak, unittest) with type-specific titles, labels,
and fingerprint namespaces. The create-or-update machinery lives in
:mod:`scripts.common.issue_dedup`.

For failures WITH a test_name (assertions, timeouts, gtest), the identity is
the (type, test_name, test_file) triple. For failures WITHOUT a test_name
(sanitizer/valgrind/startup), the identity is (type, normalized_error), so the
same underlying bug produces one issue regardless of which test file triggered
the detection. Titles follow the identity: the test file appears only for types
whose fingerprint keys on it, so a title is not rewritten when the same bug is
detected under a different file.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from scripts.common.incidents import compute_fingerprint
from scripts.common.issue_dedup import IssueContent
from scripts.test_failure_detector.parse_failures import (
    TIMESTAMP_PATTERNS,
    FailureType,
    UniqueFailure,
    is_plumbing_frame,
    normalize_error_identity,
    scrub_volatile_tokens,
)

MARKER_NAMESPACE = "valkey-ci-agent:test-failure"

LABEL_NAME = "test-failure"

# Type-specific marker namespaces for fingerprinting and issue search.
_TYPE_NAMESPACE: dict[FailureType, str] = {
    FailureType.ASSERTION: "valkey-ci-agent:test-failure",
    FailureType.SANITIZER: "valkey-ci-agent:sanitizer-error",
    FailureType.VALGRIND: "valkey-ci-agent:valgrind-error",
    FailureType.TIMEOUT: "valkey-ci-agent:test-timeout",
    FailureType.STARTUP: "valkey-ci-agent:startup-failure",
    FailureType.EXCEPTION: "valkey-ci-agent:test-exception",
    FailureType.MEMORY_LEAK: "valkey-ci-agent:memory-leak",
    FailureType.UNITTEST: "valkey-ci-agent:unittest-failure",
}

# Every type shares one title prefix. The failure type is named in the body
# instead, so an issue is not retitled when the same bug is later attributed to
# a different type (a valgrind report and a sanitizer report of one bug).
TITLE_PREFIX = "[TEST-FAILURE]"


def marker_namespace_for(failure: UniqueFailure) -> str:
    """Return the marker namespace for a failure's type."""
    return _TYPE_NAMESPACE.get(failure.failure_type, MARKER_NAMESPACE)


def label_for(failure: UniqueFailure) -> str:
    """Return the issue label. All failure types use the same label.

    One label keeps the tracker filter simple and needs no new labels created on
    the target repo. The type is named in the body instead.
    """
    return LABEL_NAME


def fingerprint_for(failure: UniqueFailure) -> str:
    """Stable dedup key for a failure.

    Keyed on (test_name, test_file) when the failure names a test, on test_file
    for a nameless timeout (every timeout shares one generic error text), and on
    the normalized error otherwise.

    Identity components go in ``namespace``, never ``shapes``: shapes collapses
    every run of digits, which would merge PSYNC2 with PSYNC3 and a
    use-after-free at cluster_legacy.c:3421 with one at :5109.

    """
    ns = marker_namespace_for(failure)

    if failure.has_test_identity:
        return compute_fingerprint(
            namespace=(ns, failure.test_name, failure.test_file),
            shapes=(),
        )
    elif failure.failure_type == FailureType.TIMEOUT and failure.test_file:
        return compute_fingerprint(
            namespace=(ns, failure.test_file),
            shapes=(),
        )
    else:
        error_identity = normalize_error_identity(failure.error)
        return compute_fingerprint(
            namespace=(ns, error_identity),
            shapes=(),
        )


def title_for(failure: UniqueFailure) -> str:
    """Issue title for a failure."""
    return _build_title(failure)


def renderer_for(failure: UniqueFailure) -> _FailureRenderer:
    """Return a renderer supplying the ``render`` and ``body_transform`` hooks
    that :class:`IssueDedupPublisher.upsert` expects for one failure.
    """
    return _FailureRenderer(failure)


class _FailureRenderer:
    """Per-failure render/body_transform pair. Created via :func:`renderer_for`."""

    def __init__(self, failure: UniqueFailure) -> None:
        self._failure = failure
        self._newly_failing: list[str] = []
        self._new_error: str | None = None
        # Digests of the traces _detect_new_error selected for publication
        # this run. Only these are added to the issue's record.
        self._published_digests: list[str] = []

    def render(self, marker: str, occurrences: int) -> IssueContent:
        """The ``render`` callback: title/body/comment/labels for the issue."""
        return IssueContent(
            title=title_for(self._failure),
            body=_build_body(self._failure, marker, occurrences=occurrences),
            comment=_build_comment(
                self._failure,
                newly_failing=self._newly_failing,
                new_error=self._new_error,
            ),
            labels=(label_for(self._failure),),
            creation_comment=_build_absorbed_trace_comment(self._failure),
        )

    def merge_environments(self, existing_body: str) -> str:
        """The ``body_transform`` callback: fold this failure's environments
        into the existing issue body, preserving environments recorded by
        earlier runs and recording which ones are newly failing.

        Also records which tools' traces the issue has reported, so a trace
        published in a comment is not published again on every later run. The
        recorded traces are a marker, not content: the body's own trace section
        is left as first published.

        Only the traces this run actually published are recorded, keyed by
        content. Recording every trace the failure carries would suppress one
        that later changes.
        """
        self._new_error = self._detect_new_error(existing_body)
        body = existing_body
        if self._published_digests:
            body = _record_reported_tools(body, self._published_digests)
        existing_envs = _extract_environments_from_body(body)
        self._newly_failing = [
            j.job for j in self._failure.jobs if j.job not in existing_envs
        ]
        if not self._newly_failing:
            return body
        return _update_environments_in_body(
            body, existing_envs + self._newly_failing,
        )

    def _detect_new_error(self, existing_body: str) -> str | None:
        """Return this run's traces that are not already recorded on the issue,
        or None when it carries nothing new.

        Every trace the failure holds is considered, not just its own: a failure
        that absorbed another tool's report carries both, and the absorbed one is
        the whole point of the merge. The update path publishes only the body it
        was given plus this comment, so a trace the issue lacks reaches a reader
        here or not at all.

        A trace matching any stored one says nothing new. One matching none is
        new, whether because it changed or because that tool's report has not
        appeared on this issue before.
        """
        stored = [
            _normalize_trace(t)
            for t in _extract_errors_from_body(existing_body)
            if t.strip()
        ]
        if not stored:
            return None

        # A tool whose trace was already published in an earlier comment is not
        # published again: the body keeps only its first-seen trace, so without
        # this the same comment would repeat on every later run.
        reported = _reported_tools_in_body(existing_body)
        traces = [
            (trace_label_for(self._failure), self._failure.error),
            *self._failure.extra_traces,
        ]
        fresh: list[str] = []
        published: list[str] = []
        for _label, trace in traces:
            # The stored traces were truncated when published, so a fresh trace
            # must be compared in its truncated form too, or every recurrence of
            # an oversized trace would register as new. The budget must be the
            # one the body used, not a share of it: comparing a half-budget
            # candidate against a full-budget stored trace never matches. The
            # backtick bounding the renderers apply has to be matched here too,
            # for the same reason: the body holds the bounded form.
            candidate = _bound_backtick_runs(
                _truncate_trace(trace, _MAX_TRACE_CHARS)
            )
            if not candidate.strip():
                continue
            digest = trace_digest(candidate)
            if digest in reported:
                continue
            if _normalize_trace(candidate) in stored:
                continue
            fresh.append(candidate)
            if digest not in published:
                published.append(digest)
        self._published_digests = published
        if not fresh:
            return None
        # Joined as one trace: the comment renders it under the template's
        # heading, which carries no per-tool label.
        return "\n\n".join(fresh)


# Keywords that mark a line as carrying diagnostic content rather than
# boilerplate. Used by _error_summary_line to prefer the real payload over
# the generic runner prefix/banner.
_TITLE_KEYWORDS = (
    "Invalid", "definitely lost", "indirectly lost",
    "heap-buffer-overflow", "heap-use-after-free",
    "stack-buffer-overflow", "use-after-poison",
    # Valgrind spells it the British way; the sanitizers do not.
    "uninitialized", "uninitialised", "runtime error",
    "detected memory leaks", "LEAK SUMMARY",
    "fishy", "overlap", "Mismatched",
    "possibly lost", "Jump to the invalid address",
    "Process terminating with default action of signal",
)


# Heap-layout coordinates in a diagnostic line ("in loss record 900 of
# 1,109") shift between runs of the same bug. The fingerprint already
# scrubs them; the title must too, or each recurrence rewrites the title
# of the same issue.
_LOSS_RECORD_RE = re.compile(r"\s*\bin loss record \d[\d,]* of \d[\d,]*")

# Allocation sizes drift run to run for the same leak ("49 bytes" vs
# "52 bytes"), so titles show them as N: "N bytes in N blocks are
# definitely lost".
_COUNT_RE = re.compile(r"\b\d[\d,]*(\s+(?:bytes?|blocks?|byte\(s\)|object\(s\)))\b")

# An AddressSanitizer diagnostic line ends in a volatile address dump
# ("heap-use-after-free on address 0x60... at pc 0x... bp 0x... sp 0x..."). The
# address and registers change every run of the same bug; the fingerprint
# scrubs them, so the title must too or it is rewritten on each recurrence.
_ASAN_ADDR_NOISE_RE = re.compile(r"\s+on address 0x[0-9a-fA-F]+.*$")

# Volatile run-specific tokens in generic title candidates: ports, PIDs, hex
# addresses, temp paths, timestamps, and long bare numbers. The publisher
# re-titles on every update, so a token the fingerprint scrubs but the title
# keeps rewrites one issue's title on each recurrence. Anything added to
# scrub_volatile_tokens for identity needs a counterpart here.
_TITLE_VOLATILE_SUBS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"0x[0-9a-fA-F]+"), "0xN"),
    (re.compile(r"/tmp/[^\s:]+"), "/tmp/..."),
    (re.compile(r"\b(pid|port)([=\s]+)\d+", re.IGNORECASE), r"\1\2N"),
    # A hung client's last-known runner state ("last state: (SPAWNING SERVER)
    # pid:N fd 12"). It names what the runner was doing, not the failure, and
    # every token in it drifts. The body keeps it; the title must not.
    (re.compile(r"[,;]?\s*last state:.*$", re.IGNORECASE | re.DOTALL), ""),
    # The timestamps the identity drops, shared from there so the two cannot
    # drift apart: a datestamp the title keeps but the identity scrubs retitles
    # one issue on every recurrence.
    *((pattern, "<time>") for pattern in TIMESTAMP_PATTERNS),
    # The access width of a valgrind invalid read/write. The same bug reads as
    # size 4 in one build and size 8 in another, and the identity scrubs it.
    (re.compile(r"\b(Invalid (?:read|write) of size )\d+"), r"\1N"),
    # The runner's absolute path to the server binary. It differs per platform
    # ("/home/runner/..." on Linux, "/Users/runner/..." on macOS) while naming
    # the same executable, and the identity reduces it to the basename.
    (re.compile(r"(?:/[^\s/]+)*/(valkey-server|redis-server)\b"), r"\1"),
    (re.compile(r"\b\d{4,}\b"), "N"),
)


def _scrub_volatile_title_tokens(line: str) -> str:
    for pattern, repl in _TITLE_VOLATILE_SUBS:
        line = pattern.sub(repl, line)
    return line

# Valgrind stack frame naming a source location: "by 0x1E8076: debugCommand
# (debug.c:569)". Frames in tool preload libraries (malloc interceptors)
# name no source file:line, so they never match.
_SOURCE_FRAME_RE = re.compile(
    r"^\s*(?:at|by)\s+0x[0-9a-fA-F]+:\s*(?P<func>\S+)\s+\((?P<file>[^():]+):(?P<line>\d+)\)"
)

# Sanitizer stack frame naming a source location: "#4 0x55ba... in debugCommand
# /home/runner/.../src/debug.c:569:9". The malloc interceptor frame carries a
# parenthesized binary offset ("in malloc (.../valkey-server+0x20de33)") rather
# than a file:line, so it never matches.
_SAN_SOURCE_FRAME_RE = re.compile(
    r"^#\d+\s+0x[0-9a-fA-F]+\s+in\s+(?P<func>\S+)\s+(?P<file>\S+?):(?P<line>\d+)(?::\d+)?\b"
)

# A valgrind leak record: "49 bytes in 1 blocks are definitely lost ...".
# The title reformats it as "Definitely lost: 49 bytes in <site>". Leak titles
# keep their real byte counts even though the fingerprint scrubs them as
# volatile: maintainers triage leaks by magnitude, and the publisher refreshes
# the title on each recurrence, so drift keeps it current.
_LEAK_RECORD_RE = re.compile(
    r"(?P<size>\d[\d,]*\s+bytes?)\s+in\s+\d[\d,]*\s+blocks?\s+are\s+"
    r"(?P<kind>definitely|indirectly|possibly)\s+lost"
)

# The AddressSanitizer/LeakSanitizer summary line: "SUMMARY: AddressSanitizer:
# 41 byte(s) leaked in 1 allocation(s)." The banner ("detected memory leaks")
# names no magnitude; this line does.
_SANITIZER_LEAK_RE = re.compile(
    r"(?P<size>\d[\d,]*\s+byte\(s\))\s+leaked\s+in\s+\d[\d,]*\s+allocation\(s\)"
)

# The totals line of a macOS /usr/bin/leaks report: "Process 9443: 1 leak for
# 48 total leaked bytes." This is the memory-leak type's payload, and the type
# does not overlap valgrind/sanitizer: it is the only leak detector on the
# macos jobs (valgrind has no Apple Silicon port; the CI matrix builds ASan
# only on Linux), and it inspects the live server after each test file rather
# than at exit, so it catches leaks the Linux leak jobs miss.
_LEAKS_TOTAL_RE = re.compile(
    r"Process\s+\d+:\s*"
    r"(?P<phrase>\d[\d,]*\s+leaks?\s+for\s+\d[\d,]*\s+total\s+leaked\s+bytes)"
)

# The allocation site named by a macOS leaks report's ROOT LEAK line ("<malloc
# in sdsnewlen 0x600001d1c100>"). A leaks blob has no stack frames, so this is
# the only thing in it that says which code path leaked; the identity keys on it
# for the same reason.
#
# Anchored on the leading count: with stack logging the report also prints a
# heading quoting the same text ("STACK OF 1 INSTANCE OF 'ROOT LEAK: <malloc in
# _sdsnewlen>':"), and an unanchored match took the function from there,
# carrying the heading's trailing quote into the title.
_LEAKS_ROOT_SITE_RE = re.compile(
    r"^\s*\d+\s+\([^)]*\)\s+ROOT LEAK:\s*<[^>]*?\bin\s+(?P<func>[^\s>]+)",
    re.MULTILINE,
)


# Root sites named in a title before it is cut short. A report with more sites
# than this is summarized, since a title listing a dozen functions is unreadable
# and the fingerprint keys on the full set anyway.
_MAX_TITLE_ROOT_SITES = 2


def _leaks_root_site(error: str) -> str:
    """The functions a macOS leaks report blames, or "".

    Every distinct site is named, sorted, matching what the identity keys on: a
    report can blame several, and naming only the first gave two reports that
    happened to share it one title while they remained separate issues.

    Returns "" for an unsymbolicated report, whose root lines carry a bare
    address and name no function.
    """
    sites = sorted({m.group("func") for m in _LEAKS_ROOT_SITE_RE.finditer(error)})
    if not sites:
        return ""
    if len(sites) > _MAX_TITLE_ROOT_SITES:
        shown = ", ".join(sites[:_MAX_TITLE_ROOT_SITES])
        return f"{shown} and {len(sites) - _MAX_TITLE_ROOT_SITES} more"
    return ", ".join(sites)


def _leak_site(error: str) -> str:
    """Distinctive "func (file:line)" in the report's first stack, or "".

    Skips shared allocation and sanitizer-runtime plumbing via the same
    predicate the identity uses, so the site names the code path that leaked
    (debugCommand (debug.c:569)) rather than the allocator every leak passes
    through. Falls back to the first source frame when the whole stack is
    plumbing, so a title is never left empty.

    Only the first stack is considered. A later stack belongs to a different
    record, and taking a site from it made the title of one issue depend on what
    else the tool happened to report in the same buffer.
    """
    first_source_site = ""
    in_stack = False
    for line in error.split("\n"):
        line = re.sub(r"==\d+==\s*", "", line).strip()
        match = _SOURCE_FRAME_RE.match(line) or _SAN_SOURCE_FRAME_RE.match(line)
        if not match:
            # Frames run consecutively, so the first non-blank non-frame line
            # after one ends the block.
            if in_stack and line:
                break
            continue
        in_stack = True
        source_path = match.group("file")
        source_file = source_path.rsplit("/", 1)[-1]
        func = match.group("func")
        site = f"{func} ({source_file}:{match.group('line')})"
        if not first_source_site:
            first_source_site = site
        if is_plumbing_frame(func, source_path):
            continue
        return site
    return first_source_site


# Longest error summary a title carries. GitHub truncates a title past 256
# characters, which would leave the stored title unequal to the rendered one and
# break the publisher's exact-match title fallback for good. This leaves room for
# the prefix and an appended stack site.
_MAX_SUMMARY_CHARS = 120


def _title_text(summary: str) -> str:
    """Scrub, collapse, and cap a summary for use in a title.

    Newlines are collapsed because GitHub rewrites them in a title, which would
    leave the stored title unequal to the one rendered and permanently break the
    exact-match title fallback in the dedup publisher.
    """
    collapsed = " ".join(_scrub_volatile_title_tokens(summary).split())
    return collapsed[:_MAX_SUMMARY_CHARS]


def _with_site(summary: str, site: str) -> str:
    """Join a summary and its stack site, truncating only the summary.

    The site is the token that tells two bugs with one diagnostic line apart, so
    it is appended whole: capping the joined string instead cut the site off and
    left the two sharing a title.
    """
    text = _title_text(summary)
    if not site:
        return text
    return f"{text} in {site}"


def _error_summary_line(error: str) -> str:
    """Extract a short summary from an error for the title.

    Strips ANSI codes and valgrind PID annotations, then drops the runner's
    wrapper prefix ("Valgrind error: ...", "Sanitizer error: ...") which is
    always the same across issues. Prefers lines containing diagnostic
    keywords over generic banners so different bugs get distinct titles,
    and names the first user-code stack frame so two bugs with the same
    diagnostic line stay tellable apart in an issue list.
    """
    # The runner emits the message behind a "[err]: " status tag whose removal
    # leaves a leading space, so match on the stripped text rather than the raw
    # field; otherwise a startup blob falls through to generic truncation and
    # the title becomes the runner's absolute exe path cut mid-word.
    error = error.strip()

    # A macOS leaks report's first line is the Tcl test name with a volatile
    # PID ("Check for memory leaks (pid 9443) in ..."); the totals line is the
    # payload. Its counts are left out for the same reason the other leak titles
    # omit theirs, so the title does not change as the leak's magnitude does.
    # The allocation site takes their place, since without it every leak on the
    # macos jobs would carry one indistinguishable title.
    if _LEAKS_TOTAL_RE.search(error):
        return _with_site("Leaked memory", _leaks_root_site(error))

    sanitizer_leak = _SANITIZER_LEAK_RE.search(error)
    if sanitizer_leak:
        return _with_site("Leaked memory", _leak_site(error))

    clean = re.sub(r"\033\[[0-9;]*m", "", error)
    clean = re.sub(r"==\d+==\s*", "", clean)
    # The test runner prepends a wrapper on the first line: "Valgrind error: ...",
    # "Sanitizer error: ...", or "Executing test client: <message>" for an
    # uncaught exception. Strip it so the summary comes from the actual message,
    # not the wrapper.
    clean = re.sub(
        r"^\s*(?:Valgrind\s+error:|Sanitizer\s+error:|Executing\s+test\s+client:)\s*",
        "", clean, count=1,
    )

    candidates = []
    for line in clean.split("\n"):
        line = line.strip()
        # Drop a leading "ERROR:" severity tag so the tool name behind it
        # ("LeakSanitizer: ...") leads the title instead of the empty tag.
        line = re.sub(r"^ERROR:\s*", "", line)
        if line and not line.startswith("at ") and len(line) > 5:
            candidates.append(line)

    # Prefer a line carrying a diagnostic keyword over the first non-empty
    # line (which is often a generic banner like "Memcheck, a memory error
    # detector" that every valgrind issue would share).
    for line in candidates:
        if any(kw in line for kw in _TITLE_KEYWORDS):
            leak = _LEAK_RECORD_RE.search(line)
            if leak:
                # "Definitely lost in debugCommand (debug.c:569)". The size is
                # left out: one leak is reported at different sizes by different
                # builds (the NO_MALLOC_USABLE_SIZE jobs account an allocation
                # differently), and the publisher rewrites the title on every
                # update, so a size in the title would flip between runs. The
                # exact figures stay in the trace.
                kind = leak.group("kind").capitalize()
                return _with_site(f"{kind} lost", _leak_site(error))
            summary = _LOSS_RECORD_RE.sub("", line)
            summary = _COUNT_RE.sub(r"N\1", summary)
            summary = _ASAN_ADDR_NOISE_RE.sub("", summary)
            summary = _scrub_volatile_title_tokens(summary)
            site = _leak_site(error)
            func = site.split(" (")[0] if site else ""
            return _with_site(summary, "" if func and func in summary else site)

    if candidates:
        return _title_text(candidates[0])
    stripped = clean.strip()
    return _title_text(stripped) if stripped else "unknown error"


# Nameless failure types whose fingerprint keys on test_file unconditionally, so
# the file is stable identity and belongs in the title. Only TIMEOUT qualifies:
# it keys on the file directly (see fingerprint_for). A type keying on the
# normalized error must not show the file, or one issue would be retitled
# whenever the same bug surfaced under a different file: a leak in shared code is
# reported after whichever test file happened to expose it.
#
# MEMORY_LEAK is decided per report rather than listed here, since only some of
# its reports key on the file. See _title_shows_file.
_TITLE_SHOWS_FILE = frozenset({FailureType.TIMEOUT})


# GitHub silently truncates an issue title past this, which would leave the
# stored title unequal to the rendered one and break the title fallback.
_MAX_TITLE_CHARS = 256


def _title_shows_file(failure: UniqueFailure) -> bool:
    """Whether the test file is stable identity for *failure*, so it may appear
    in the title without the publisher retitling the issue later.
    """
    if failure.failure_type in _TITLE_SHOWS_FILE:
        return True
    # An unsymbolicated macOS leaks report names no allocation site, only a bare
    # address, so nothing in it survives normalization except the blob's opening
    # line, which quotes the test file. The file is therefore already part of
    # that report's identity, and showing it keeps the title matching the
    # fingerprint. A symbolicated report keys on its allocation site instead, so
    # it falls through and the file stays out.
    #
    # This makes the file identity for reports where it should not be: one leak
    # in shared code becomes an issue per test file that exposes it. The fix is
    # to symbolicate the reports, not to drop the file from the title, which
    # would leave the split in place and merely hide it.
    return (
        failure.failure_type == FailureType.MEMORY_LEAK
        and not _leaks_root_site(failure.error)
    )


def _build_title(failure: UniqueFailure) -> str:
    if failure.has_test_identity:
        title = f"{TITLE_PREFIX} {failure.test_name} in {failure.test_file}"
    else:
        summary = _error_summary_line(failure.error)
        if failure.test_file and _title_shows_file(failure):
            title = f"{TITLE_PREFIX} {summary} in {failure.test_file}"
        else:
            title = f"{TITLE_PREFIX} {summary}"
    return " ".join(title.split())[:_MAX_TITLE_CHARS]


# Stands in for a field the failure has no value for. A failure without a test
# name (a memory error, a startup failure) still fills every row the template
# defines, so one body shape parses for every type.
_MISSING_FIELD = "[no test]"


def _test_name_for(failure: UniqueFailure) -> str:
    """The failing test's name, or the filler when the failure names none."""
    return failure.test_name or _MISSING_FIELD


def _test_file_for(failure: UniqueFailure) -> str:
    """The failing test's file, or the filler when the failure names none."""
    return failure.test_file or _MISSING_FIELD


def _summary_sentence_for(failure: UniqueFailure) -> str:
    """The one-line summary opening the issue body.

    Always "<test> in <file> is failing in CI.", the wording an issue filed from
    the repository's template uses. A failure with no test name puts its error
    summary there instead, since that is what identifies it, and a missing file
    is filled rather than dropped so the sentence keeps its shape.
    """
    subject = failure.test_name or _error_summary_line(failure.error)
    return f"`{subject}` in `{_test_file_for(failure)}` is failing in CI."


def _build_body(failure: UniqueFailure, marker: str, *, occurrences: int) -> str:
    """Build the issue body for a test failure."""
    ns = marker_namespace_for(failure)
    # Indented under the "CI link(s):" row they belong to, so a long list reads
    # as that row's links rather than as more rows in the Failing test(s) list.
    ci_links = "\n".join(
        f"    - `{j.job}`: [CI link]({j.url})" for j in failure.jobs
    )
    env_list = ", ".join(f"`{j.job}`" for j in failure.jobs)

    lines = [
        marker,
        f"<!-- {ns}:occurrences:{occurrences} -->",
        "",
        "**Summary**",
        "",
        _summary_sentence_for(failure),
        "",
        "**Failing test(s)**",
        "",
    ]

    # Every type fills the same rows, so one body shape parses for all of them.
    # A failure that names no test gets the filler rather than a missing row.
    lines.extend([
        f"- Test name: `{_test_name_for(failure)}`",
        f"- Test file: `{_test_file_for(failure)}`",
        "- CI link(s):",
        ci_links,
    ])

    lines.extend([
        "",
        "**Error stack trace**",
        "",
        *_render_traces(failure),
        "",
        f"**Environments:** {env_list}",
        "",
        "---",
        "*Auto-created by Test Failure Detector*",
    ])
    body = "\n".join(lines)
    if failure.extra_traces:
        # An absorbed trace is published at creation time, in the creation
        # comment. The record of published traces has to start here rather than
        # on the update path, or the first recurrence finds the trace absent
        # from the body, reads it as new, and posts it a second time.
        #
        # Only the absorbed traces are recorded, not the survivor's: the body
        # carries that one, so it is compared by content and needs no record.
        # Digested in the form _detect_new_error will compare against, or the
        # digests would never match.
        body = _record_reported_tools(body, [
            trace_digest(
                _bound_backtick_runs(_truncate_trace(trace, _MAX_TRACE_CHARS))
            )
            for _label, trace in failure.extra_traces
        ])
    return body


def trace_label_for(failure: UniqueFailure) -> str:
    """Human label naming which tool produced a failure's own trace."""
    return failure.failure_type.value.replace("-", " ").title()


# An HTML comment opener inside trace text. The dedup machinery finds an issue
# by searching its whole body for a marker, and the body embeds a tool's report
# verbatim, so a report that happens to contain a marker-shaped comment would be
# read as one. Defusing the opener keeps the trace readable while making it inert
# to that search. Applied to the trace only, never to a marker this module
# writes itself.
_HTML_COMMENT_OPEN_RE = re.compile(r"<!--")

# A body row heading at the start of a line inside trace text. The trace is
# embedded above the real rows, and the row readers are anchored to a line start
# but search the whole body, so a report line beginning with one of these
# headings matches before the real row: the environments would be read out of
# the trace and written back into it, corrupting the published trace while the
# real row went stale. Anchoring alone cannot prevent this, since a trace line
# does start at a line start.
_ROW_HEADING_RE = re.compile(r"(?m)^(\*\*(?:Environments|Summary|Failing test\(s\)):\*\*)")


def _defuse_markers(trace: str) -> str:
    """Make marker-shaped comments and row headings in tool output inert.

    Both defusings keep the trace readable while making it invisible to the
    searches that read the body back.
    """
    trace = _HTML_COMMENT_OPEN_RE.sub("<! --", trace)
    return _ROW_HEADING_RE.sub(r"​\1", trace)


def _fenced(text: str) -> list[str]:
    text = _defuse_markers(text)
    text = _bound_backtick_runs(text)
    fence = _fence_for(text)
    return [fence, text, fence]


def _render_traces(failure: UniqueFailure) -> list[str]:
    """Render the failure's own trace as a single fenced block.

    The body carries exactly one trace, in the same shape as an issue filed by
    hand from the repository's template, so the section stays predictable to
    read and to parse. A second tool's report of the same bug goes to a comment
    instead (see :meth:`_FailureRenderer._detect_new_error`).
    """
    return _fenced(_truncate_trace(failure.error) or "N/A")


def _fence_for(text: str) -> str:
    """A code fence longer than any backtick run in *text*.

    A fence closes only on a run at least as long as its opener, so an error
    that itself contains ``` must be wrapped in a longer fence or it would
    close the block early (and the round-trip in _extract_error_from_body
    would return a truncated trace, triggering a spurious new-error comment
    on every recurrence).

    A trace can itself be a long run of backticks, so the length is clamped:
    the fence is written twice per block, and an unbounded one multiplied the
    body past the size GitHub accepts on its own. At the clamp the fence can no
    longer out-run the text, so callers pass text through
    :func:`_bound_backtick_runs` first, which keeps every run short enough for
    the clamped fence to beat.
    """
    longest = max((len(m.group()) for m in re.finditer(r"`+", text)), default=0)
    return "`" * min(max(3, longest + 1), _MAX_FENCE_CHARS)


# GitHub rejects issue bodies and comments over 65536 characters. A full
# valgrind or sanitizer log can be several times that; the create call would
# 422 and the failure would never get an issue. The body carries one trace, so
# this is that trace's budget, leaving room for the surrounding body (markers,
# links, environments). A comment carrying several absorbed traces divides this
# budget among them (see _build_absorbed_trace_comment).
_MAX_TRACE_CHARS = 40_000

# Longest code fence that may be emitted. See _fence_for: the fence grows to
# out-run backtick runs in the text, and is written twice per block.
_MAX_FENCE_CHARS = 64

# A backtick run this long or longer would need a fence past the clamp, so it is
# broken up before rendering. Well above any run real tool output contains: a
# trace quoting markdown carries three or four.
_MAX_TEXT_BACKTICK_RUN = _MAX_FENCE_CHARS - 1

_LONG_BACKTICK_RUN_RE = re.compile(r"`{%d,}" % _MAX_TEXT_BACKTICK_RUN)

# Splits an over-long backtick run without adding a visible character.
_ZERO_WIDTH_SPACE = "​"


def _bound_backtick_runs(text: str) -> str:
    """Break backtick runs too long for a clamped fence to enclose.

    A fence closes on the first run at least as long as its opener. Since the
    fence is clamped at _MAX_FENCE_CHARS, a run that reaches the clamp would
    close the block early: the body would keep only the text before it, the
    round-trip in _extract_error_from_body would read back a truncated trace,
    and _detect_new_error would call the same trace new on every recurrence,
    commenting forever.

    Truncation cannot be relied on to remove such a run, since it keeps the head
    and tail of the trace. A zero-width space splits the run so it no longer
    closes the fence, while the text still reads as the tool emitted it.
    """
    chunk = _MAX_TEXT_BACKTICK_RUN - 1
    return _LONG_BACKTICK_RUN_RE.sub(
        lambda m: _ZERO_WIDTH_SPACE.join(
            m.group()[i:i + chunk] for i in range(0, len(m.group()), chunk)
        ),
        text,
    )

_TRUNCATION_NOTICE = "\n... [trace truncated by Test Failure Detector] ...\n"


def _truncate_trace(trace: str, budget: int = _MAX_TRACE_CHARS) -> str:
    """Cap a trace to *budget* characters, keeping its head and tail.

    Head and tail are both kept: the head names the error, the tail holds the
    summary totals.
    """
    if len(trace) <= budget:
        return trace
    keep = max(0, (budget - len(_TRUNCATION_NOTICE)) // 2)
    return f"{trace[:keep]}{_TRUNCATION_NOTICE}{trace[-keep:]}"


def _build_absorbed_trace_comment(failure: UniqueFailure) -> str:
    """A comment carrying the reports of other tools that found the same bug.

    The body holds one trace so it keeps the shape of a hand-filed issue, which
    also keeps it parseable. When two tools reported one bug the second report
    goes here: it says things the first does not (each tool names details the
    other omits), and discarding it would lose the reason the two were merged.

    Rendered by :func:`_build_comment`, so it is the same comment a recurrence
    posts, opening line and all. Duplicating the structure here let the two
    drift: this one carried a bare trace under a heading of its own while the
    recurrence comment named the date and the jobs.

    Returns "" for the ordinary single-tool failure, which posts no comment.
    """
    if not failure.extra_traces:
        return ""
    budget = _MAX_TRACE_CHARS // (len(failure.extra_traces) + 1)
    traces = [
        _defuse_markers(_truncate_trace(trace, budget))
        for _label, trace in failure.extra_traces
    ]
    return _build_comment(
        failure, newly_failing=[], new_error="\n\n".join(traces) or None,
    )


def _build_comment(
    failure: UniqueFailure,
    *,
    newly_failing: list[str],
    new_error: str | None = None,
) -> str:
    """Build a comment for an existing issue that failed again."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ci_links = "\n".join(
        f"- `{j.job}`: [CI link]({j.url})" for j in failure.jobs
    )
    lines = [f"Test failed again on {today}."]
    if newly_failing:
        new_envs = ", ".join(f"`{e}`" for e in newly_failing)
        lines.append(f"\n**Newly failing in:** {new_envs}")
    if new_error:
        new_error = _bound_backtick_runs(_truncate_trace(new_error))
        fence = _fence_for(new_error)
        lines.append(f"\n**New error stack trace**\n\n{fence}\n{new_error}\n{fence}")
    if ci_links:
        lines.append(f"\n**Failed in:**\n{ci_links}")
    return "\n".join(lines)


# Tools whose traces this issue has already published, recorded so a trace sent
# in a comment is not sent again on every later run. A marker rather than prose:
# it must survive a maintainer editing the body, and it carries no content.
_REPORTED_TOOLS_MARKER = "valkey-ci-agent:reported-traces"
_REPORTED_TOOLS_RE = re.compile(
    rf"<!-- {re.escape(_REPORTED_TOOLS_MARKER)}:([^>]*) -->"
)


# Prefixes every recorded digest so the record cannot be read as a fingerprint
# claim. issue_dedup's fingerprint-marker pattern matches a namespaced marker
# whose last segment is bare hex, which a lone digest would satisfy: an issue
# carrying a one-entry record would then read as claimed by that digest and
# become unadoptable by title. The "occurrences" and "last-key" markers stay out
# of that pattern the same way, by not ending in bare hex.
_TRACE_DIGEST_PREFIX = "t"


def trace_digest(trace: str) -> str:
    """A short stable digest of a trace, for the reported-traces record.

    Keyed on the trace's normalized content rather than the tool that produced
    it. A tool label would suppress that tool's next trace even after the trace
    changed, since the label is checked before any content comparison; a digest
    only ever suppresses the identical trace. Normalized with the same scrub the
    comparison uses, so a trace that recurs unchanged digests the same.
    """
    digest = compute_fingerprint(
        namespace=(_normalize_trace(trace),), shapes=(),
    )[:16]
    return f"{_TRACE_DIGEST_PREFIX}{digest}"


def _reported_tools_in_body(body: str) -> set[str]:
    """Digests of traces the issue has already reported."""
    match = _REPORTED_TOOLS_RE.search(body)
    if not match:
        return set()
    return {part.strip() for part in match.group(1).split(",") if part.strip()}


def _record_reported_tools(body: str, digests: list[str]) -> str:
    """Add *digests* to the issue's record of reported traces."""
    recorded = _reported_tools_in_body(body) | set(digests)
    marker = (
        f"<!-- {_REPORTED_TOOLS_MARKER}:{','.join(sorted(recorded))} -->"
    )
    if _REPORTED_TOOLS_RE.search(body):
        # Escaped: the record is data, and a backslash in it would read as a
        # group reference. Digests are hex, but the escape is kept so a body
        # carrying a label-era record cannot break the substitution.
        return _REPORTED_TOOLS_RE.sub(marker.replace("\\", r"\\"), body, count=1)
    return f"{body}\n{marker}"


# The body's own Environments line, anchored to the start of a line. A trace is
# embedded verbatim above it and can quote the same text, which an unanchored
# pattern matches first: the environments would then be read from, and written
# back into, the trace, leaving the real line frozen and every job reading as
# newly failing on every run.
_ENVIRONMENTS_LINE_RE = re.compile(r"(?m)^\*\*Environments:\*\*[ \t]*.*$")


def _extract_environments_from_body(body: str) -> list[str]:
    """Extract existing environment names from an issue body."""
    env_match = _ENVIRONMENTS_LINE_RE.search(body)
    if not env_match:
        return []
    return re.findall(r"`([^`]+)`", env_match.group(0))


# The fence length varies (see _fence_for); the backreference requires the
# closer to be the same run that opened the block, so an embedded shorter
# backtick run inside the error does not end the match early.
_ERROR_BLOCK_RE = re.compile(
    r"\*\*Error stack trace\*\*\s*(`{3,})\n(.*?)\n\1",
    re.DOTALL,
)

# A body holding more than one tool's trace wraps each in a labeled <details>
# block, which the single-block pattern above cannot read: it would match across
# the first block's markup and compare garbage. _render_traces no longer writes
# that shape (the body carries one trace and absorbed ones go to a comment), so
# this is only for reading a body already published in it.
_TRACE_DETAILS_RE = re.compile(
    r"<summary>(?P<label>[^<]*?)\s*trace</summary>\s*(?P<fence>`{3,})\n(?P<trace>.*?)\n\2",
    re.DOTALL,
)


def _extract_errors_from_body(body: str) -> list[str]:
    """Every error trace recorded in an issue body.

    Returns the traces from the <details> blocks of a body published in the
    older multi-trace shape, or the single trace a body now carries. Empty when
    the body records none.
    """
    details = [m.group("trace").strip() for m in _TRACE_DETAILS_RE.finditer(body)]
    if details:
        return details
    match = _ERROR_BLOCK_RE.search(body)
    if not match:
        return []
    return [match.group(2).strip()]


def _extract_error_from_body(body: str) -> str:
    """The first error trace recorded in an issue body, or ""."""
    traces = _extract_errors_from_body(body)
    return traces[0] if traces else ""


# Must scrub at least everything the fingerprint scrubs: two traces the
# fingerprint calls the same bug must compare equal here, or every recurrence
# posts a spurious "new error stack trace" comment. The shared scrub covers
# the identity's tokens (PID markers, addresses, loss records, byte/block
# counts); these add the timestamps a trace can carry that an identity's few
# significant lines never reach.
_TRACE_NOISE_RES = (
    re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?"),
    re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"),
    # A sanitizer frame's build hash ("(BuildId: 2bf960fb...)"). It changes
    # whenever the binary is recompiled, so it differs between two runs of one
    # bug. Only the trace carries it; a frame reaches the identity as a
    # function name, without this suffix.
    re.compile(r"\s*\(BuildId:\s*[0-9a-fA-F]+\)"),
    # The macOS leaks report's process footprint ("Physical footprint: 2801K").
    # It measures the live server when the report was taken, not the leak.
    re.compile(r"(Physical footprint(?:\s*\(peak\))?:\s*)\d+K"),
    # gtest-parallel's progress counter and per-test duration ("[6/298] ... (2 ms)").
    # Both move with how the run was sharded and scheduled.
    re.compile(r"^\[\d+/\d+\]\s*", re.MULTILINE),
    re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)"),
    # A Tcl socket handle from the runner's clients-state report.
    re.compile(r"\bsock[0-9a-f]{6,}\b"),
    # The runner's workspace root, which differs per platform. A frame reaches
    # the identity as a basename, so only the trace comparison needs this.
    re.compile(r"/(?:home|Users)/runner/[^\s:]*|/__w/[^\s:]*"),
    # A leaked file descriptor number.
    re.compile(r"\b(fd)\s+\d+", re.IGNORECASE),
)


def _normalize_trace(text: str) -> str:
    """Normalize a trace for comparison by scrubbing run-specific noise.

    Defusing runs here too: the stored trace was defused when published, so a
    fresh trace must be compared in the same form or a report carrying a
    marker-shaped comment would look new on every recurrence.
    """
    text = _defuse_markers(text)
    for noise in _TRACE_NOISE_RES:
        text = noise.sub("", text)
    return " ".join(scrub_volatile_tokens(text).split())


def _update_environments_in_body(body: str, all_envs: list[str]) -> str:
    """Replace the issue body's Environments line with an updated list.

    The replacement is escaped because a job name is data, and a backslash in
    one would read as a group reference to ``re.sub``.
    """
    new_env_line = f"**Environments:** {', '.join(f'`{e}`' for e in all_envs)}"
    return _ENVIRONMENTS_LINE_RE.sub(
        new_env_line.replace("\\", r"\\"), body,
        count=1,
    )
