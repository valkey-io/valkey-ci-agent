"""Render a ``FixOutcome`` into a PR comment.

The comment is the agent's accountability surface: for a push it shows exactly
what was changed, the command that was run, its captured output, and the
review rationale - the evidence a maintainer needs to trust (or reject) the
fix. For a refusal it explains why, so the maintainer can take over.
"""

from __future__ import annotations

import re

from scripts.ci_fix.models import FixOutcome, OutcomeKind

_OUTPUT_TAIL_IN_COMMENT = 3000


def _fenced(body: str) -> str:
    """Wrap untrusted text in a code fence it cannot break out of.

    Command output may itself contain ``` runs; per CommonMark, the fence must
    be longer than the longest backtick run inside, so we size it accordingly.
    """
    longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{body}\n{fence}"


def render_comment(outcome: FixOutcome) -> str:
    if outcome.kind is OutcomeKind.PUSHED:
        return _render_pushed(outcome)
    if outcome.kind is OutcomeKind.REFUSED:
        return _render_refused(outcome)
    return _render_failed(outcome)


def _render_pushed(outcome: FixOutcome) -> str:
    proposal = outcome.proposal
    run = outcome.run_result
    review = outcome.review
    lines = [
        f"Fixed **{proposal.failing_check if proposal else 'the failing check'}** "
        f"and pushed `{outcome.commit_sha[:12]}` to this PR's branch.",
        "",
    ]
    if outcome.failing_run_url:
        lines += [f"Fixing the failure from [this run]({outcome.failing_run_url}).", ""]
    lines += [
        f"**Root cause:** {proposal.root_cause if proposal else ''}",
        "",
    ]
    if run is not None:
        check_name = proposal.failing_check if proposal else ""
        highlight = _result_lines_for(run.output_tail, check_name)
        if highlight:
            lines += [
                "The previously-failing check now passes:",
                "",
                _fenced(highlight),
                "",
            ]
        block = f"$ {run.command}\nexit {run.exit_code}\n{run.output_tail[-_OUTPUT_TAIL_IN_COMMENT:]}"
        lines += [
            "<details><summary>Full verification output</summary>",
            "",
            _fenced(block),
            "</details>",
            "",
        ]
    if outcome.verify_backend:
        where = _backend_label(outcome)
        lines += [f"**Verified by:** {where}", ""]
    if review is not None and review.reasoning:
        lines += [f"**Review:** {review.reasoning}", ""]
    lines += _remaining_checks(outcome)
    if outcome.verify_backend == "upstream-port":
        lines.append(
            "_This is a port of an upstream fix; this PR's normal CI is the "
            "verification authority. I do not merge._"
        )
    else:
        lines.append(
            "_The fix passed targeted verification of the failing check; this PR's "
            "full CI will confirm. I do not merge._"
        )
    return "\n".join(lines)


def _result_lines_for(output: str, check_name: str) -> str:
    """Pull the lines that show the target check's result out of the output.

    A verification run can emit hundreds of lines for other passing tests; a
    maintainer wants the one line proving the previously-failing check now
    passes. Prefer lines mentioning the check name; otherwise fall back to the
    last few result-marker lines. Returns an empty string if nothing matches,
    in which case the caller just shows the full output.
    """
    lines = output.splitlines()
    if check_name:
        # Match on a distinctive slice of the check name (the AI's name and the
        # log's wording can differ slightly), longest word first.
        words = sorted((w for w in re.split(r"\W+", check_name) if len(w) > 3), key=len, reverse=True)
        for w in words:
            hits = [ln for ln in lines if w in ln and _RESULT_MARKER.search(ln)]
            if hits:
                return "\n".join(hits[-5:])
    markers = [ln for ln in lines if _RESULT_MARKER.search(ln)]
    return "\n".join(markers[-3:]) if markers else ""


_RESULT_MARKER = re.compile(r"\[ok\]|\[err\]|\[exception\]|\bPASS\b|\bFAIL\b", re.IGNORECASE)


def _backend_label(outcome: FixOutcome) -> str:
    backend = outcome.verify_backend
    if backend == "local":
        return "targeted verification on a Linux runner"
    if backend.startswith("docker:"):
        return f"targeted verification in the `{backend[len('docker:'):]}` container"
    if backend == "macos":
        run = f" ([run]({outcome.macos_run_url}))" if outcome.macos_run_url else ""
        return f"targeted verification on a macOS runner{run}"
    if backend == "upstream-port":
        return "ported upstream fix; awaiting this PR's normal CI"
    return backend


def _render_refused(outcome: FixOutcome) -> str:
    lines = [f"I did not push a fix: {outcome.summary}", ""]
    if outcome.failing_run_url:
        lines += [f"Looked at the failure from [this run]({outcome.failing_run_url}).", ""]
    if outcome.run_result is not None and outcome.run_result.output_tail:
        lines += [
            "<details><summary>Evidence</summary>",
            "",
            _fenced(outcome.run_result.output_tail[-_OUTPUT_TAIL_IN_COMMENT:]),
            "</details>",
            "",
        ]
    lines += _remaining_checks(outcome)
    return "\n".join(lines)


def _render_failed(outcome: FixOutcome) -> str:
    return f"I hit an error and could not complete the fix: {outcome.summary}"


def _remaining_checks(outcome: FixOutcome) -> list[str]:
    if not outcome.other_failing_checks:
        return []
    listed = "\n".join(f"- `{name}`" for name in outcome.other_failing_checks)
    return [
        "Other checks also failed in that run; re-invoke with the same command to "
        "address the next one:",
        listed,
        "",
    ]
