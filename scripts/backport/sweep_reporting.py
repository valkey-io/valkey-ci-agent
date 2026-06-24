"""Markdown reporting for scheduled backport sweeps."""

from __future__ import annotations

import re
from collections.abc import Iterator

from scripts.backport.sweep_models import (
    DETAIL_ALREADY_ON_SWEEP_BRANCH,
    DETAIL_EMPTY_ON_TARGET,
    DETAIL_RESOLVED_BY_AI,
    BranchSweepResult,
    CandidateResult,
)


def _is_ai_resolved(result: CandidateResult | None) -> bool:
    """Whether *result* records an AI conflict resolution, durably or by detail."""
    if result is None:
        return False
    return result.resolved_by_ai or result.detail == DETAIL_RESOLVED_BY_AI


_SKIP_REASON_FALLBACK = (
    "The cherry-pick produced no net change on this branch, so there is "
    "nothing to backport."
)


def _skip_reason(result: CandidateResult) -> str:
    """A concise, maintainer-readable reason a candidate was skipped.

    Uses the deterministic reason recorded at apply time (derived from whether
    the resolution matched the target branch), falling back to a generic line.
    """
    return result.skip_reason.strip() or _SKIP_REASON_FALLBACK


def result_is_on_backport_branch(result: CandidateResult) -> bool:
    return result.outcome == "applied" or (
        result.outcome == "skipped-existing"
        and result.detail == DETAIL_ALREADY_ON_SWEEP_BRANCH
    )


def repair_diagnosis_from_detail(detail: str) -> str:
    prefix = "Claude repair diagnosis:\n"
    if not detail.startswith(prefix):
        return ""
    body = detail[len(prefix):]
    return body.split("\n\nValidation output:\n", 1)[0].strip()


def validation_failure_detail(output: str) -> str:
    """Preserve AI diagnosis while keeping routine validation logs compact."""
    diagnosis = repair_diagnosis_from_detail(output)
    if not diagnosis:
        return compact_validation_output(output)
    marker = "\n\nValidation output:\n"
    validation_tail = output.split(marker, 1)[1] if marker in output else ""
    return (
        "Claude repair diagnosis:\n"
        f"{diagnosis[:1500]}\n\n"
        "Validation output:\n"
        f"{compact_validation_output(validation_tail)}"
    )


def compact_validation_output(output: str, *, limit: int = 500) -> str:
    text = output.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def build_pr_body(
    result: BranchSweepResult,
    *,
    branch_applied: list[CandidateResult] | None = None,
    previous_body: str | None = None,
    comment_urls: dict[int, str] | None = None,
) -> str:
    comment_urls = comment_urls or {}
    lines = [
        f"# Backport sweep for {result.target_branch}",
        "",
        'Automated cherry-picks from PRs marked "To be backported".',
        "",
    ]

    applied = merge_applied_results(
        [r for r in result.results if result_is_on_backport_branch(r)],
        branch_applied=branch_applied,
        previous_body=previous_body,
    )
    resolved = {r.source_pr_number for r in applied}
    failed_now = []
    skipped_empty = []
    for r in result.results:
        if r.outcome in {"applied", "skipped-existing"}:
            resolved.add(r.source_pr_number)
            if (
                r.outcome == "skipped-existing"
                and r.detail == DETAIL_EMPTY_ON_TARGET
                and r.source_pr_number not in {a.source_pr_number for a in applied}
            ):
                skipped_empty.append(r)
        else:
            failed_now.append(r)
    failed = merge_failed_results(failed_now, resolved=resolved, previous_body=previous_body)

    if applied:
        lines.extend(["## Applied", "", "| Source PR | Title | Detail |", "|---|---|---|"])
        for r in applied:
            detail = _esc(r.detail)
            url = comment_urls.get(r.source_pr_number)
            if url and _is_ai_resolved(r):
                detail = f"[{detail}]({url})"
            lines.append(
                f"| #{r.source_pr_number} | {_esc(r.source_pr_title)} | {detail} |",
            )
        lines.append("")
        if any(_is_ai_resolved(r) for r in applied):
            lines.extend([
                "AI resolution details are posted as comments on this PR when available.",
                "",
            ])

    if skipped_empty:
        lines.extend([
            "## Skipped",
            "",
            "These candidates were evaluated but contribute no change to this "
            "branch, e.g. the fix targets code that does not exist here. No "
            "backport commit was created for them.",
            "",
            "| Source PR | Title | Reason |",
            "|---|---|---|",
        ])
        for r in skipped_empty:
            lines.append(
                f"| #{r.source_pr_number} | {_esc(r.source_pr_title)} | "
                f"{_esc(_skip_reason(r))} |",
            )
        lines.append("")

    if failed:
        lines.extend([
            "## Needs attention",
            "",
            "These candidates could not be applied automatically and need a maintainer to follow up.",
            "",
            f"<details><summary>{len(failed)} candidate(s)</summary>",
            "",
            "| Source PR | Title | Outcome | Reason |",
            "|---|---|---|---|",
        ])
        for r in failed:
            lines.append(
                f"| #{r.source_pr_number} | {_esc(r.source_pr_title)} | "
                f"{r.outcome} | {_esc(r.detail)} |",
            )
        lines.extend(["", "</details>", ""])

    lines.extend(["---", "*Generated by valkey-ci-agent using Claude Code.*"])
    return "\n".join(lines)


def build_summary(results: list[BranchSweepResult]) -> str:
    lines = ["## Backport Sweep", ""]
    for r in results:
        applied = sum(1 for c in r.results if result_is_on_backport_branch(c))
        suffix = f" -- [PR]({r.pr_url})" if r.pr_url else ""
        if r.error:
            suffix += f" -- error: {r.error}"
        lines.append(
            f"- `{r.target_branch}`: {applied}/{r.candidates_found} applied" + suffix
        )
    return "\n".join(lines)


def merge_applied_results(
    current: list[CandidateResult],
    *,
    branch_applied: list[CandidateResult] | None = None,
    previous_body: str | None = None,
) -> list[CandidateResult]:
    previous_by_pr = {r.source_pr_number: r for r in parse_previous_applied(previous_body or "")}
    current_by_pr = {r.source_pr_number: r for r in current}
    membership = branch_applied if branch_applied is not None else current

    merged: list[CandidateResult] = []
    seen: set[int] = set()
    for base in membership:
        if base.source_pr_number in seen:
            continue
        seen.add(base.source_pr_number)
        merged.append(
            _merge_applied_result(
                base,
                current_result=current_by_pr.get(base.source_pr_number),
                previous_result=previous_by_pr.get(base.source_pr_number),
            )
        )
    return merged


def merge_failed_results(
    current: list[CandidateResult],
    *,
    resolved: set[int],
    previous_body: str | None = None,
) -> list[CandidateResult]:
    current_by_pr = {r.source_pr_number: r for r in current}

    merged: list[CandidateResult] = []
    seen: set[int] = set()
    for base in [*current, *parse_previous_failed(previous_body or "")]:
        pr_number = base.source_pr_number
        if pr_number in seen or pr_number in resolved:
            continue
        seen.add(pr_number)
        merged.append(current_by_pr.get(pr_number, base))
    return merged


def parse_previous_applied(body: str) -> list[CandidateResult]:
    results: list[CandidateResult] = []
    for pr_number, cells in _parse_section_rows(body, "## Applied", min_cells=3):
        detail = _markdown_link_label(cells[2])
        results.append(
            CandidateResult(
                pr_number, cells[1], "applied", detail,
                resolved_by_ai=detail == DETAIL_RESOLVED_BY_AI,
            )
        )
    return results


def _markdown_link_label(value: str) -> str:
    text = value.strip()
    match = re.fullmatch(r"\[([^\]]+)\]\([^)]+\)", text)
    return match.group(1) if match else text


def parse_previous_failed(body: str) -> list[CandidateResult]:
    return [
        CandidateResult(pr_number, cells[1], cells[2] or "error", cells[3])
        for pr_number, cells in _parse_section_rows(body, "## Needs attention", min_cells=4)
    ]


def _parse_section_rows(body: str, heading: str, *, min_cells: int) -> Iterator[tuple[int, list[str]]]:
    in_section = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped == heading:
            in_section = True
            continue
        if in_section and stripped.startswith("## "):
            break
        if not in_section or not stripped.startswith("|"):
            continue

        cells = _split_markdown_table_row(stripped)
        if len(cells) < min_cells:
            continue
        source = cells[0]
        if source.lower() == "source pr" or set(source) <= {"-"}:
            continue
        match = re.search(r"#(\d+)", source)
        if not match:
            continue
        yield int(match.group(1)), cells


def _merge_applied_result(
    base: CandidateResult,
    *,
    current_result: CandidateResult | None,
    previous_result: CandidateResult | None,
) -> CandidateResult:
    # Whether this candidate's conflicts were resolved by the AI, from any
    # source that survives across runs: the current run's durable flag/detail,
    # the membership base, or the detail parsed from the previous PR body.
    resolved_by_ai = (
        _is_ai_resolved(current_result)
        or _is_ai_resolved(base)
        or _is_ai_resolved(previous_result)
    )

    if current_result is not None:
        title = current_result.source_pr_title or base.source_pr_title
        detail = current_result.detail
        if detail == DETAIL_ALREADY_ON_SWEEP_BRANCH and previous_result is not None:
            detail = previous_result.detail
    else:
        title = base.source_pr_title
        detail = base.detail
        if previous_result is not None:
            detail = previous_result.detail
            title = title or previous_result.source_pr_title

    if resolved_by_ai:
        # The fact that the AI resolved this candidate is durable; never let it
        # collapse into the generic prior-sweep string on later runs.
        detail = DETAIL_RESOLVED_BY_AI
    elif detail == DETAIL_ALREADY_ON_SWEEP_BRANCH:
        detail = "cherry-picked in a prior sweep"

    return CandidateResult(
        source_pr_number=base.source_pr_number,
        source_pr_title=title,
        outcome="applied",
        detail=detail,
        resolved_by_ai=resolved_by_ai,
    )


def _split_markdown_table_row(row: str) -> list[str]:
    text = row.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in text:
        if char == "\\" and not escaped:
            escaped = True
            current.append(char)
            continue
        if char == "|" and not escaped:
            cells.append(_unesc("".join(current).strip()))
            current = []
            continue
        current.append(char)
        escaped = False
    cells.append(_unesc("".join(current).strip()))
    return cells


def _unesc(value: str) -> str:
    return value.replace("\\|", "|")


def _esc(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
