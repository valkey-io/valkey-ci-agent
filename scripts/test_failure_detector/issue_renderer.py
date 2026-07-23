"""Render detected test failures into GitHub issue title, body, and comment text.

The rendering is test-failure-specific (test name/file, error trace, the list
of CI jobs the failure appeared in); the create-or-update machinery lives in
:mod:`scripts.common.issue_dedup`.

A test failure's identity is the ``test_name`` + ``test_file`` pair, which is
the dedup fingerprint. Across recurrences we accumulate the set of failing
environments (CI jobs) into the issue body; because the dedup publisher's
``render`` callback can't see the previously published body, that merge is done
via the publisher's ``body_transform`` hook (see
:meth:`_FailureRenderer.merge_environments`).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from scripts.common.incidents import compute_fingerprint
from scripts.common.issue_dedup import IssueContent
from scripts.test_failure_detector.parse_failures import UniqueFailure

MARKER_NAMESPACE = "valkey-ci-agent:test-failure"

LABEL_NAME = "test-failure"


def fingerprint_for(failure: UniqueFailure) -> str:
    """Stable dedup key for a failure: a hash of test name + file.

    The identity is ``test_name`` + ``test_file``, hashed via
    :func:`scripts.common.incidents.compute_fingerprint` like the fuzzer
    pipeline, into a fixed-shape hex token safe for the HTML-comment marker.

    The pair is the identity, so it goes in ``namespace`` (joined in order,
    never normalized) rather than ``shapes``. That keeps digits significant so
    PSYNC2 and PSYNC3 stay distinct, and preserves order so a name/file swap
    cannot collide.
    """
    return compute_fingerprint(
        namespace=(MARKER_NAMESPACE, failure.test_name, failure.test_file),
        shapes=(),
    )


def title_for(failure: UniqueFailure) -> str:
    """Issue title for a failure.

    Exposed rather than inlined in the renderer so callers can pass the same
    title to ``IssueDedupPublisher.upsert`` as ``title_fallback`` when
    migrating issues off the old raw-fingerprint marker.
    """
    return _build_title(failure)


def renderer_for(failure: UniqueFailure) -> _FailureRenderer:
    """Return a renderer supplying the ``render`` and ``body_transform`` hooks
    that :class:`IssueDedupPublisher.upsert` expects for one failure.

    The two hooks are coupled so the recurrence comment can name the *newly*
    failing environments. ``upsert`` runs ``body_transform`` (which diffs the
    failure's environments against the previously published body) before
    ``render`` (which builds the comment), so by the time the comment is
    rendered the renderer already knows which environments were not recorded
    before. See :meth:`_FailureRenderer.merge_environments`.
    """
    return _FailureRenderer(failure)


class _FailureRenderer:
    """Per-failure ``render``/``body_transform`` pair sharing the set of newly
    failing environments. Created via :func:`renderer_for`."""

    def __init__(self, failure: UniqueFailure) -> None:
        self._failure = failure
        # Environments failing for the first time on this run, populated by
        # ``merge_environments`` on the update path. Empty on the create path
        # (no prior body to diff, and no comment is posted there anyway).
        self._newly_failing: list[str] = []
        # The latest error trace when it differs (ignoring run-specific noise)
        # from the one recorded on the issue, populated by ``merge_environments``
        # on the update path. ``None`` when the trace is unchanged or absent, so
        # :meth:`render` only calls it out when there is something new to show.
        self._new_error: str | None = None

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
            labels=(LABEL_NAME,),
        )

    def merge_environments(self, existing_body: str) -> str:
        """The ``body_transform`` callback: fold this failure's environments
        into the existing issue body, preserving environments recorded by
        earlier runs and recording which ones are newly failing so
        :meth:`render` can call them out in the recurrence comment.

        Also diffs the failure's error trace against the one recorded on the
        issue and, when it has meaningfully changed, records it so
        :meth:`render` can surface the new trace in the comment. The body's
        original trace is left intact — the body is the first-seen record, the
        comment timeline carries each subsequent change.
        """
        self._new_error = self._detect_new_error(existing_body)
        existing_envs = _extract_environments_from_body(existing_body)
        self._newly_failing = [
            j.job for j in self._failure.jobs if j.job not in existing_envs
        ]
        if not self._newly_failing:
            return existing_body
        return _update_environments_in_body(
            existing_body, existing_envs + self._newly_failing,
        )

    def _detect_new_error(self, existing_body: str) -> str | None:
        """Return the failure's error trace when it differs from the one stored
        on the issue, else ``None``.

        The comparison is normalized (see :func:`_normalize_trace`) so that
        run-specific noise — timestamps, ports/PIDs, hex addresses, temp paths —
        does not flag an unchanged failure as new on every recurrence. An empty
        new error is never called out.

        Legacy issues predating the Error stack trace section have no stored
        trace (``_extract_error_from_body`` returns ``""``); with no baseline to
        diff against, the trace is not called out. Otherwise the body never
        backfilled with the section would diff against "" and re-post the same
        "new" trace on every recurrence.
        """
        new_error = self._failure.error
        if not new_error.strip():
            return None
        stored = _extract_error_from_body(existing_body)
        if not stored.strip():
            return None
        if _normalize_trace(stored) == _normalize_trace(new_error):
            return None
        return new_error


def _build_title(failure: UniqueFailure) -> str:
    return f"[TEST-FAILURE] {failure.test_name} in {failure.test_file}"


def _build_body(failure: UniqueFailure, marker: str, *, occurrences: int) -> str:
    """Build the issue body for a test failure."""
    ci_links = "\n".join(
        f"- `{j.job}`: [CI link]({j.url})" for j in failure.jobs
    )
    env_list = ", ".join(f"`{j.job}`" for j in failure.jobs)

    return "\n".join([
        marker,
        f"<!-- {MARKER_NAMESPACE}:occurrences:{occurrences} -->",
        "",
        "**Summary**",
        "",
        f"`{failure.test_name}` in `{failure.test_file}` is failing in CI.",
        "",
        "**Failing test(s)**",
        "",
        f"- Test name: `{failure.test_name}`",
        f"- Test file: `{failure.test_file}`",
        "- CI link(s):",
        ci_links,
        "",
        "**Error stack trace**",
        "",
        "```",
        failure.error or "N/A",
        "```",
        "",
        f"**Environments:** {env_list}",
        "",
        "---",
        "*Auto-created by Test Failure Detector*",
    ])


def _build_comment(
    failure: UniqueFailure,
    *,
    newly_failing: list[str],
    new_error: str | None = None,
) -> str:
    """Build a comment for an existing issue that failed again.

    When ``newly_failing`` names environments not recorded on the issue before,
    the comment calls them out so a triager can spot a regression spreading to
    new platforms without diffing the body's Environments line.

    When ``new_error`` is set, the failure recurred with a different error trace
    than the one recorded on the issue; the comment shows the new trace so a
    triager can notice the failure mode changed without diffing the body.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ci_links = "\n".join(
        f"- `{j.job}`: [CI link]({j.url})" for j in failure.jobs
    )
    lines = [f"Test failed again on {today}."]
    if newly_failing:
        new_envs = ", ".join(f"`{e}`" for e in newly_failing)
        lines.append(f"\n**Newly failing in:** {new_envs}")
    if new_error:
        lines.append(f"\n**New error stack trace**\n\n```\n{new_error}\n```")
    lines.append(f"\n**Failed in:**\n{ci_links}")
    return "\n".join(lines)


def _extract_environments_from_body(body: str) -> list[str]:
    """Extract existing environment names from an issue body."""
    env_match = re.search(r"\*\*Environments:\*\*\s*(.+)", body)
    if not env_match:
        return []
    return re.findall(r"`([^`]+)`", env_match.group(1))


# The fenced code block holding the trace under the "Error stack trace" header
# in a body built by :func:`_build_body`. Non-greedy so it stops at the closing
# fence rather than swallowing later fenced blocks.
_ERROR_BLOCK_RE = re.compile(
    r"\*\*Error stack trace\*\*\s*```\n(.*?)\n```",
    re.DOTALL,
)


def _extract_error_from_body(body: str) -> str:
    """Extract the error trace recorded under the Error stack trace header.

    Returns ``""`` when the body has no such section (e.g. issues created
    before this section existed), which the caller treats as "unknown" rather
    than "unchanged".
    """
    match = _ERROR_BLOCK_RE.search(body)
    if not match:
        return ""
    return match.group(1).strip()


# Run-specific tokens scrubbed before comparing two traces, so an unchanged
# failure is not reported as new every recurrence. Each match is stripped out,
# then whitespace is collapsed (see _normalize_trace).
_TRACE_NOISE_RES = (
    # ISO-ish timestamps: 2026-06-27 12:34:56 / 2026-06-27T12:34:56
    re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?"),
    # Bare clock times: 12:34:56
    re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"),
    # Hex addresses: 0x7fff1234
    re.compile(r"0x[0-9a-fA-F]+"),
    # Temp paths: /tmp/foo, /tmp/abc.123
    re.compile(r"/tmp/[^\s:]+"),
    # PID/port-style annotations: pid 12345, port=6379, port 6379
    re.compile(r"\b(pid|port)[=\s]+\d+", re.IGNORECASE),
)


def _normalize_trace(text: str) -> str:
    """Normalize a trace for comparison by scrubbing run-specific noise.

    Two traces that differ only in timestamps, ports/PIDs, hex addresses, or
    temp paths normalize to the same string, so a genuinely unchanged failure
    is not flagged as a new error trace on every run.
    """
    for noise in _TRACE_NOISE_RES:
        text = noise.sub("", text)
    # Collapse all remaining whitespace so indentation/line-wrap changes alone
    # do not count as a difference.
    return " ".join(text.split())


def _update_environments_in_body(body: str, all_envs: list[str]) -> str:
    """Replace the Environments line in the issue body with an updated list."""
    new_env_line = f"**Environments:** {', '.join(f'`{e}`' for e in all_envs)}"
    return re.sub(r"\*\*Environments:\*\*\s*.+", new_env_line, body)
