"""Render a CI-fix result for a pull request or detector issue."""

from __future__ import annotations

import re

from scripts.ci_fix.models import (
    Failure,
    FailureType,
    FixCandidate,
    FixResult,
    ResultKind,
)
from scripts.common.text_utils import compact_text

_MAX_OTHER_JOBS = 10
# A command longer than this goes on its own lines, below the check it backs.
_INLINE_COMMAND_CHARS = 80


def render_result(result: FixResult) -> str:
    """Return the maintainer-facing report for one fix attempt."""
    failure = result.task.failure
    job = _job(failure)

    if result.task.target.fix_branch:
        # A sweep PR is told in a line where its fix went, or why there is
        # none: the fix is explained on the CI-sweep PR, where it is reviewed.
        upstream, base = result.task.target.upstream_branch, failure.base_branch
        if result.open_upstream and result.kind is ResultKind.OPENED:
            return f"Opened #{result.pr_number} into `{upstream}` with a fix for {job}. Backport it to `{base}` once it merges."
        if result.open_upstream:
            return f"#{result.pr_number}, open on `{upstream}`, fixes {job}. Backport it to `{base}` once it merges."
        if result.kind is ResultKind.OPENED:
            return f"Opened #{result.pr_number} with a fix for {job}: {result.commit_sha}."
        if result.kind is ResultKind.PUSHED:
            return f"Pushed a fix for {job} to #{result.pr_number}: {result.commit_sha}."
        if result.kind is ResultKind.REFUSED:
            return f"No fix for {job}. {sentence(result.summary)}"
        if result.kind is ResultKind.SUGGESTED and result.pr_number:
            return f"#{result.pr_number} fixes {job}. Backport it to `{failure.base_branch}`."
        if result.kind is ResultKind.SUGGESTED and result.candidate is not None:
            return "\n".join([
                f"A fix for {job} could not be run here, so it was not pushed.",
                "",
                "<details><summary>Proposed patch</summary>",
                "",
                _fenced(result.candidate.patch, lang="diff"),
                "",
                "</details>",
            ])

    if result.kind is ResultKind.PUSHED:
        body = [f"Pushed {result.commit_sha} to this branch with a fix for {job}.", ""]
        body += _context(result)
        body += [_footer("This PR's own CI decides whether the fix holds, and the bot never merges.")]
        return "\n".join(body)

    if result.kind is ResultKind.SUGGESTED:
        body = [
            f"A suggested fix for {job}, prepared against `{result.task.target.expected_sha[:12]}`. "
            + ("The bot could not run the failing check, so nothing was pushed to this branch."
               if result.candidate is not None and result.candidate.not_run
               else "Nothing was pushed to this branch."),
            "",
        ]
        body += _context(result)
        body += candidate_patch(result.candidate)
        body += [_footer("If it looks right, apply it on this branch and let the PR's CI check it.")]
        return "\n".join(body)

    if result.kind is ResultKind.REFUSED:
        body = [f"No fix for {job}. {sentence(result.summary)}", ""]
        body += _context(result, include_reasoning=False)
        return "\n".join(body).rstrip()

    return f"The fix attempt for {job} stopped with an error: {sentence(result.summary)}"


# A sentence of the agent's summary about whether its fix ran: it writes it
# before code runs the test, and code says what ran instead.
_SELF_CHECK_RE = re.compile(r"\b(?:unverified|nothing was (?:run|built)|not (?:been )?(?:run|built|verified))\b", re.I)
_PASSED_RE = re.compile(r"passed (\d+) loop")


def render_ci_sweep_fix(result: FixResult) -> str:
    """The CI-sweep PR's short report of a fix pushed to it; the diagnosis and checks are folded away."""
    failure = result.task.failure
    # A bare SHA, which GitHub links to the commit.
    body = [f"Pushed {result.commit_sha} with a fix for {_job(failure)} on #{failure.pr_number}.", ""]
    candidate = result.candidate
    if candidate is not None:
        sentences = re.split(r"(?<=[.!?])\s+", candidate.summary.strip())
        said = " ".join(part for part in sentences if part and not _SELF_CHECK_RE.search(part))
        if said:
            body += [prose(said, 600), ""]
        passed = _PASSED_RE.search(candidate.verification)
        body += [
            f"The failing test passed {_plural(passed.group(1) + ' loop(s)')} with the fix, "
            "and the bot's review approved it." if passed else "The bot's review approved it.",
            "",
            "<details><summary>Diagnosis and checks</summary>",
            "",
            *candidate_context(candidate, include_reasoning=True, include_failing_check=True),
            "</details>",
            "",
        ]
    body += [_footer(f"#{failure.pr_number}'s CI decides whether the fix holds once this is merged, "
                     "and the bot never merges.")]
    return "\n".join(body)


def _job(failure: Failure) -> str:
    """The failed job as a comment names it, with its run."""
    job = f"`{prose(failure.job_name, 300)}`" if failure.job_name else "the failed job"
    return f"{job} ([run]({failure.run_url}))" if failure.run_url else job


def _context(result: FixResult, *, include_reasoning: bool = True) -> list[str]:
    failure = result.task.failure
    lines: list[str] = []
    if result.candidate is not None:
        lines += candidate_context(result.candidate, include_reasoning=include_reasoning, include_failing_check=True)
    if failure.other_failed_jobs:
        visible = failure.other_failed_jobs[:_MAX_OTHER_JOBS]
        lines += [
            "Other jobs also failed in that run:",
            "",
            *[f"- `{prose(name, 300)}`" for name in visible],
        ]
        hidden = len(failure.other_failed_jobs) - len(visible)
        if hidden:
            lines += [f"- and {hidden} more"]
        lines += [""]
    return lines


def candidate_context(
    candidate: FixCandidate, *, include_reasoning: bool, include_failing_check: bool = False,
) -> list[str]:
    """The diagnosis and checks of ``candidate``, as a PR comment or draft PR body shows them."""
    lines: list[str] = []
    if include_failing_check and candidate.failing_check:
        lines += [f"**What failed:** {prose(candidate.failing_check, 300)}", ""]
    if candidate.root_cause:
        cause = prose(candidate.root_cause, 2_000)
        if candidate.failure_type is FailureType.FLAKY:
            cause = f"{sentence(cause)} The failure is flaky."
        lines += [f"**Root cause:** {cause}", ""]
    if include_reasoning and candidate.reasoning:
        lines += [f"**The fix:** {prose(candidate.reasoning, 3_000)}", ""]
    if include_reasoning and candidate.adapts:
        lines += [f"**Adapts upstream:** `{candidate.adapts[:12]}`", ""]
    checks = check_items(candidate.verification)
    if checks:
        lines += ["**What the bot checked**", "", *checks, ""]
    if candidate.validation:
        lines += [
            "<details><summary>The agent's own notes</summary>",
            "",
            prose(candidate.validation, 2_000),
            "",
            "</details>",
            "",
        ]
    return lines


def check_items(verification: str) -> list[str]:
    """``verification``'s lines as a Markdown list, each long command in a block of its own."""
    items: list[str] = []
    for line in verification.splitlines():
        if line.strip():
            items += _check_item(line)
    return items


_CODE_RE = re.compile(r"`([^`\n]+)`")


def _check_item(line: str) -> list[str]:
    text = _plural(prose(line, 5_000))
    command = next((match for match in _CODE_RE.finditer(text)
                    if len(match.group(1)) > _INLINE_COMMAND_CHARS), None)
    if command is None:
        return [f"- {text}"]
    before, after = text[:command.start()], text[command.end():]
    if not before.strip():
        text = "This command" + after
    elif before.rstrip().endswith(","):
        text = before + "this command" + after
    else:
        text = re.sub(r"\s*(?:\bwith|:)\s*$", "", before) + after
    # The command follows the sentence it backs; any later sentence, the block.
    first, _, rest = text.partition(". ")
    block = _fenced(command.group(1), lang="sh", indent="  ").splitlines()
    item = [f"- {first.rstrip().rstrip('.:')}:", "", *block]
    if rest.strip():
        item += ["", f"  {rest.strip()}"]
    return item


# An em dash, or an en dash set off by spaces (not one in a range, ``1–3``).
_DASH = r"(?:\s*\u2014\s*|\s+\u2013\s+)"
# Two in one sentence set off an aside: ``a — or b — c``.
_DASH_PAIR_RE = re.compile(_DASH + r"([^.!?\u2014\u2013]+?)" + _DASH)
_DASH_RE = re.compile(_DASH)
_PLURAL_RE = re.compile(r"\b(\d+) (loop|time|attempt)\(s\)")


def prose(text: str, limit: int) -> str:
    """``text`` as one line of at most ``limit`` characters, without dashes.

    An aside set off by two dashes takes commas; a single dash, which joins
    a clause to what explains it, takes a colon. The agent wrote the text:
    an HTML comment in it must not read as one of the bot's own markers.
    """
    text = _DASH_PAIR_RE.sub(r", \1, ", compact_text(text, limit))
    return _DASH_RE.sub(": ", text).replace("<!--", "<!- -")


def _plural(text: str) -> str:
    return _PLURAL_RE.sub(lambda match: f"{match[1]} {match[2]}{'' if match[1] == '1' else 's'}", text)


def sentence(text: str) -> str:
    """``text`` as one sentence: capitalized, and ended."""
    text = prose(text, 2_000)
    text = text[:1].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _footer(text: str) -> str:
    return f"<sub>{text}</sub>"


def candidate_patch(candidate: FixCandidate | None) -> list[str]:
    """The candidate's patch as a fenced block, when there is one.

    Every suggested patch is already held to a comment's size, by the engine
    or by publication.
    """
    if candidate is None:
        return []
    if not candidate.patch:
        return []
    return [
        "Proposed patch:",
        "",
        _fenced(candidate.patch, lang="diff"),
        "",
    ]


def _fenced(body: str, *, lang: str = "", indent: str = "") -> str:
    """Fence untrusted text with a delimiter longer than any inside it."""
    longest = max((len(match) for match in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest + 1)
    return "\n".join(f"{indent}{line}" for line in (f"{fence}{lang}", *body.splitlines(), fence))
