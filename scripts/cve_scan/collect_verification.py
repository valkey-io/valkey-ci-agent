"""Reconcile per-entry CVE verification markers against the expected matrix.

The verify job runs one build-and-scan per (line, variant, platform) matrix
entry and, WITHOUT failing the entry on a surviving CVE, writes a marker
recording that entry's outcome (verified / survivors / error). The collect job
downloads every marker and invokes this CLI to decide which version lines
earned a rebuild.

Reconciliation, not "count what showed up": the scan job's ``matrix`` output is
the AUTHORITATIVE set of legs that were supposed to run (one entry per affected
architecture). This CLI takes that expected set and reconciles a marker against
EVERY expected (line, variant, platform). A leg that dies before uploading its
marker (transient fetch failure, runner crash, timeout, cancellation) shows up
as a ``missing`` leg rather than silently vanishing, so it is reported, not
dropped.

Any-architecture gate: a version line is dispatchable when AT LEAST ONE expected
leg for it reports ``verified``. valkey-container's ci.yml takes a single
``version`` input and rebuilds ALL platforms in one multi-platform push; there
is no way to ask it to rebuild a single architecture. So withholding a dispatch
because one architecture could not be proven fixed is strictly harmful:
dispatching fixes the architectures whose fix IS live, while a lagging
architecture is rebuilt and stays exactly as vulnerable as it already was.
Nobody is worse off. A line with zero verified legs is not dispatched.

Because a dispatched line may still be vulnerable on an architecture whose fix
has not landed, REPORTING is the safety property: the summary and the machine
readable ``arch_report`` state, per line, the status of every affected
architecture, and never imply a line is fully fixed when only some architectures
were proven.

Exit codes:
  0  success. Either at least one line is dispatchable (dispatch is justified by
     positive evidence, even if some other leg errored or is missing), or a
     legitimate no-dispatch where every leg reported and none errored/missing
     (all survivors): survivors are "not fixable this week", not a failure.
  1  no line is dispatchable AND at least one leg errored or is missing. We
     learned nothing usable: we cannot tell "not fixable" from "we failed to
     look", so nothing is dispatched. Errored and missing legs on a run that
     still dispatched something do NOT trigger this (they are surfaced loudly
     instead).
  2  malformed input (bad marker, marker for a leg not in the expected matrix,
     empty marker set, or a malformed/empty expected matrix). Fail closed: this
     CLI only runs after the scan found candidates, so bad input must never
     yield a silently empty green pass.

Deterministic, stdlib only. Mirrors targets.py: strict decode, loud on bad
input, never a silently empty result.

Usage:
    python -m scripts.cve_scan.collect_verification \\
        --markers-dir <dir> --expected-matrix <json>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: The exact keys every marker carries.
_MARKER_KEYS = frozenset({"line", "variant", "platform", "outcome"})
#: The exact keys every expected-matrix entry carries (matches targets.verify_matrix).
_MATRIX_KEYS = frozenset({"line", "variant", "platform", "image"})

_VERIFIED = "verified"
_SURVIVORS = "survivors"
_ERROR = "error"
#: Synthetic status for an expected leg that never uploaded a marker.
_MISSING = "missing"
#: Valid marker outcomes an entry may actually record.
_VALID_OUTCOMES = frozenset({_VERIFIED, _SURVIVORS, _ERROR})

#: One expected verification leg, keyed by (line, variant, platform).
_LegKey = tuple[str, str, str]


class CollectError(Exception):
    """Base for fail-closed aggregation errors (exit 2)."""


class MarkerError(CollectError):
    """Raised when a marker is malformed, unexpected, or the set is empty."""


class ExpectedMatrixError(CollectError):
    """Raised when the expected verify matrix is malformed or empty."""


@dataclass(frozen=True)
class Marker:
    """One (line, variant, platform) verification outcome."""

    line: str
    variant: str
    platform: str
    outcome: str


@dataclass(frozen=True)
class EntryStatus:
    """Reconciled status of one expected leg (marker outcome, or ``missing``)."""

    line: str
    variant: str
    platform: str
    status: str


@dataclass(frozen=True)
class LineReport:
    """One version line's reconciled per-architecture legs and dispatch verdict.

    ``legs`` is every expected leg for the line (sorted), each carrying its
    reconciled status. A line is dispatched when AT LEAST ONE leg verified.
    """

    line: str
    legs: list[EntryStatus]

    @property
    def dispatched(self) -> bool:
        """True when at least one affected architecture was proven fixed."""
        return any(leg.status == _VERIFIED for leg in self.legs)

    def legs_with(self, status: str) -> list[EntryStatus]:
        """Return the legs whose reconciled status equals ``status``."""
        return [leg for leg in self.legs if leg.status == status]

    @property
    def unresolved_legs(self) -> list[EntryStatus]:
        """Legs that errored or went missing (surfaced, never silently dropped)."""
        return [leg for leg in self.legs if leg.status in (_ERROR, _MISSING)]


@dataclass(frozen=True)
class Aggregate:
    """Per-line dispatch verdicts plus the per-leg statuses behind them.

    ``entries`` is every expected leg (sorted); ``lines`` groups them per version
    line with a dispatch verdict. A line is dispatchable on the any-architecture
    gate (at least one verified leg).
    """

    lines: list[LineReport]
    entries: list[EntryStatus]

    @property
    def dispatched_lines(self) -> list[str]:
        """Lines with at least one verified leg (the dispatch set), sorted."""
        return [lr.line for lr in self.lines if lr.dispatched]

    @property
    def skipped_lines(self) -> list[str]:
        """Lines with zero verified legs (not dispatched), sorted."""
        return [lr.line for lr in self.lines if not lr.dispatched]

    @property
    def unresolved_legs(self) -> list[EntryStatus]:
        """Every errored or missing leg across all lines (always surfaced)."""
        return [e for e in self.entries if e.status in (_ERROR, _MISSING)]

    @property
    def fixable(self) -> bool:
        """True when at least one line is dispatchable (any architecture verified)."""
        return bool(self.dispatched_lines)

    @property
    def learned_nothing(self) -> bool:
        """True when no line is dispatchable AND some leg errored or is missing.

        This is the only nonzero (exit 1) path: with no positive evidence and a
        leg that failed to report, we cannot tell "not fixable" from "we failed
        to look", so we fail closed. When some line IS dispatchable, errored or
        missing legs are surfaced loudly but do not block the justified dispatch.
        """
        return not self.dispatched_lines and bool(self.unresolved_legs)


def _parse_marker(path: Path) -> Marker:
    """Parse and strictly validate one marker JSON file. Raises MarkerError."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MarkerError(f"cannot read marker {path}: {exc}") from exc

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MarkerError(f"marker {path} is not valid JSON: {exc}") from exc

    if not isinstance(obj, dict):
        raise MarkerError(
            f"marker {path} must be a JSON object, got {type(obj).__name__}"
        )

    keys = set(obj)
    if keys != set(_MARKER_KEYS):
        missing = sorted(_MARKER_KEYS - keys)
        extra = sorted(keys - _MARKER_KEYS)
        raise MarkerError(
            f"marker {path} key mismatch: missing={missing}, extra={extra}"
        )

    for key in _MARKER_KEYS:
        if not isinstance(obj[key], str):
            raise MarkerError(
                f"marker {path} field {key!r} must be a string, got "
                f"{type(obj[key]).__name__}"
            )

    outcome = obj["outcome"]
    if outcome not in _VALID_OUTCOMES:
        raise MarkerError(
            f"marker {path} has invalid outcome {outcome!r}; must be one of "
            f"{sorted(_VALID_OUTCOMES)}"
        )

    return Marker(
        line=obj["line"],
        variant=obj["variant"],
        platform=obj["platform"],
        outcome=outcome,
    )


def load_markers(markers_dir: Path) -> list[Marker]:
    """Load every ``*.json`` marker under ``markers_dir`` (recursive), strictly.

    Raises MarkerError on any malformed marker or on an empty set (fail closed).
    """
    paths = sorted(markers_dir.rglob("*.json"))
    if not paths:
        raise MarkerError(
            f"no marker files found under {markers_dir}; expected at least one "
            "(fail closed)"
        )
    return [_parse_marker(p) for p in paths]


def parse_expected_matrix(raw: str) -> set[_LegKey]:
    """Decode the scan job's verify-matrix JSON into the expected leg set.

    ``raw`` is ``targets.verify_matrix``'s output: a JSON list of
    ``{line, variant, platform, image}`` dicts. Returns the set of expected
    (line, variant, platform) legs. Raises ExpectedMatrixError on malformed
    JSON, a non-list payload, a bad entry, or an empty/duplicate leg set (fail
    closed): this CLI runs only when the scan found candidates, so an empty
    expected matrix is a contract violation, not a clean pass.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExpectedMatrixError(
            f"expected matrix is not valid JSON: {exc}"
        ) from exc

    if not isinstance(payload, list):
        raise ExpectedMatrixError(
            f"expected matrix must be a JSON list, got {type(payload).__name__}"
        )
    if not payload:
        raise ExpectedMatrixError(
            "expected matrix is empty; nothing to reconcile against (fail closed)"
        )

    expected: set[_LegKey] = set()
    for idx, entry in enumerate(payload):
        if not isinstance(entry, dict):
            raise ExpectedMatrixError(
                f"expected matrix[{idx}] must be an object, got "
                f"{type(entry).__name__}"
            )
        keys = set(entry)
        if keys != set(_MATRIX_KEYS):
            missing = sorted(_MATRIX_KEYS - keys)
            extra = sorted(keys - _MATRIX_KEYS)
            raise ExpectedMatrixError(
                f"expected matrix[{idx}] key mismatch: missing={missing}, "
                f"extra={extra}"
            )
        for key in _MATRIX_KEYS:
            if not isinstance(entry[key], str):
                raise ExpectedMatrixError(
                    f"expected matrix[{idx}].{key} must be a string, got "
                    f"{type(entry[key]).__name__}"
                )
        leg: _LegKey = (entry["line"], entry["variant"], entry["platform"])
        if leg in expected:
            raise ExpectedMatrixError(
                f"expected matrix has a duplicate leg {leg}; the verify matrix "
                "is deduplicated, so a duplicate is a contract mismatch"
            )
        expected.add(leg)
    return expected


def reconcile(markers: list[Marker], expected: set[_LegKey]) -> Aggregate:
    """Reconcile actual markers against the expected leg set, per line.

    Every expected leg gets exactly one reconciled status. A leg with no marker
    is ``missing`` (a leg died without reporting). A marker for a leg not in
    ``expected`` is a contract mismatch and raises MarkerError (fail closed).
    Any-architecture gate: a line is dispatched when at least one of its legs
    verified; errored and missing legs are recorded and surfaced but never
    block a line that has positive evidence on another architecture.
    """
    actual: dict[_LegKey, str] = {}
    for marker in markers:
        key: _LegKey = (marker.line, marker.variant, marker.platform)
        if key not in expected:
            raise MarkerError(
                f"unexpected marker {key}: leg not in the expected verify matrix "
                "(contract mismatch, fail closed)"
            )
        actual[key] = marker.outcome

    # One reconciled status per expected leg, missing when no marker showed up.
    entries = [
        EntryStatus(line, variant, platform, actual.get((line, variant, platform), _MISSING))
        for line, variant, platform in sorted(expected)
    ]

    # Group legs per line, preserving the sorted (variant, platform) order.
    legs_by_line: dict[str, list[EntryStatus]] = {}
    for entry in entries:
        legs_by_line.setdefault(entry.line, []).append(entry)

    lines = [LineReport(line=line, legs=legs_by_line[line]) for line in sorted(legs_by_line)]
    return Aggregate(lines=lines, entries=entries)


def _leg_label(leg: EntryStatus) -> str:
    """Human label for one leg: ``variant platform`` (e.g. ``alpine linux/amd64``)."""
    return f"{leg.variant} {leg.platform}"


def _render_summary(agg: Aggregate) -> str:
    """Render a markdown block: the any-arch policy, then per-line per-arch status.

    The safety property is honesty: every affected architecture is named with
    its status, and a partially fixed line is never presented as fully fixed.
    """
    lines = [
        "## CVE Verification Aggregation",
        "",
        "Policy: a version line is dispatched when AT LEAST ONE affected "
        "architecture is proven fixed. valkey-container's `ci.yml` rebuilds all "
        "platforms from a single `version` input and cannot target one "
        "architecture, so dispatching on a partial fix is a strict improvement: "
        "the architectures whose fix is live get fixed, and a lagging "
        "architecture is rebuilt no worse off than before. A dispatched line may "
        "therefore still be vulnerable on an architecture whose fix has not "
        "landed, so the per-architecture status below is the safety record. A "
        "`missing` leg is a verify leg that died without reporting.",
        "",
        "| Version line | Variant | Platform | Status |",
        "|---|---|---|---|",
    ]
    for entry in agg.entries:
        lines.append(
            f"| `{entry.line}` | {entry.variant} | {entry.platform} | {entry.status} |"
        )
    lines += [
        "",
        "| Version line | Dispatched | Proven fixed on | Still vulnerable / unknown |",
        "|---|---|---|---|",
    ]
    for lr in agg.lines:
        fixed = ", ".join(_leg_label(leg) for leg in lr.legs_with(_VERIFIED)) or "none"
        unfixed = (
            ", ".join(
                f"{_leg_label(leg)} ({leg.status})"
                for leg in lr.legs
                if leg.status != _VERIFIED
            )
            or "none"
        )
        dispatched = "yes" if lr.dispatched else "no"
        lines.append(f"| `{lr.line}` | {dispatched} | {fixed} | {unfixed} |")
    lines.append("")

    if agg.dispatched_lines:
        lines.append(
            "Dispatching line(s) with at least one proven-fixed architecture: "
            f"`{' '.join(agg.dispatched_lines)}`. Each is fixed only on the "
            "architectures listed as proven fixed; any others remain vulnerable "
            "until their fix lands and the next rebuild picks it up."
        )
        if agg.skipped_lines:
            lines.append(
                "Skipped (no architecture proven fixed): "
                f"`{' '.join(agg.skipped_lines)}`."
            )
        if agg.unresolved_legs:
            labels = ", ".join(
                f"`{leg.line}` {_leg_label(leg)} ({leg.status})"
                for leg in agg.unresolved_legs
            )
            lines.append(
                "Note: some legs errored or went missing but did NOT block "
                f"dispatch, because another architecture verified: {labels}. "
                "These architectures were not proven fixed."
            )
    elif agg.learned_nothing:
        labels = ", ".join(
            f"`{leg.line}` {_leg_label(leg)} ({leg.status})"
            for leg in agg.unresolved_legs
        )
        lines.append(
            "Failing closed: no architecture was proven fixed for any line, and "
            f"at least one leg errored or went missing ({labels}). With no "
            'positive evidence we cannot tell "not fixable" from "we failed to '
            'look", so nothing is dispatched.'
        )
    else:
        lines.append(
            "No architecture was proven fixed for any line (all survivors); "
            "dispatching nothing. Survivors are a legitimate not-fixable-this-week."
        )
    lines.append("")
    return "\n".join(lines)


def _arch_report(agg: Aggregate) -> str:
    """Build the compact machine-readable per-architecture report (single-line JSON).

    Shape: ``{"lines": [{"line", "dispatched", "legs": [{"variant", "platform",
    "status"}]}]}``. The rebuild job renders this into its own summary and Slack
    message so the downstream report is per-architecture too. Compact separators
    keep it a single GITHUB_OUTPUT line.
    """
    payload = {
        "lines": [
            {
                "line": lr.line,
                "dispatched": lr.dispatched,
                "legs": [
                    {
                        "variant": leg.variant,
                        "platform": leg.platform,
                        "status": leg.status,
                    }
                    for leg in lr.legs
                ],
            }
            for lr in agg.lines
        ]
    }
    return json.dumps(payload, separators=(",", ":"))


#: The arch_report emitted on a fail-closed (exit 2) path with no aggregate.
_EMPTY_ARCH_REPORT = '{"lines":[]}'


def _write_summary(text: str) -> None:
    """Append a markdown block to GITHUB_STEP_SUMMARY when it is set."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def _write_outputs(dispatched_lines: list[str], arch_report: str) -> None:
    """Emit ``verified_versions``, ``fixable``, and ``arch_report`` to GITHUB_OUTPUT.

    ``verified_versions`` is the dispatch set (lines with at least one verified
    architecture); the name is preserved so the rebuild job's wiring is
    unchanged. ``arch_report`` is compact single-line JSON. Falls back to stdout
    when GITHUB_OUTPUT is unset.
    """
    verified_str = " ".join(dispatched_lines)
    fixable_str = "true" if dispatched_lines else "false"

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"verified_versions={verified_str}\n")
            handle.write(f"fixable={fixable_str}\n")
            handle.write(f"arch_report={arch_report}\n")
    else:
        print(f"verified_versions={verified_str}")
        print(f"fixable={fixable_str}")
        print(f"arch_report={arch_report}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on success, nonzero on any fail-closed path."""
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile CVE verification markers against the expected verify "
            "matrix and emit the set of version lines with at least one verified "
            "architecture (the any-architecture dispatch gate)."
        ),
    )
    parser.add_argument(
        "--markers-dir",
        required=True,
        help="Directory of downloaded per-entry marker JSON files.",
    )
    parser.add_argument(
        "--expected-matrix",
        required=True,
        help=(
            "The scan job's verify-matrix JSON (list of {line, variant, "
            "platform, image}). Every expected leg is reconciled to a marker."
        ),
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    try:
        expected = parse_expected_matrix(args.expected_matrix)
        markers = load_markers(Path(args.markers_dir))
        agg = reconcile(markers, expected)
    except CollectError as exc:
        # Malformed/empty/unexpected input: fail closed with no dispatch list.
        logger.error("Aggregation failed (fail closed): %s", exc)
        _write_summary(
            f"## CVE Verification Aggregation\n\nFAIL (fail closed): {exc}\n"
        )
        _write_outputs([], _EMPTY_ARCH_REPORT)
        return 2

    _write_summary(_render_summary(agg))
    arch_report = _arch_report(agg)

    if agg.fixable:
        # At least one line has a verified architecture: dispatch is justified by
        # positive evidence. Surface any errored/missing legs loudly, but they do
        # not block the dispatch (ci.yml rebuilds every platform anyway, so a
        # lagging architecture is no worse off).
        _write_outputs(agg.dispatched_lines, arch_report)
        if agg.unresolved_legs:
            logger.warning(
                "Dispatching despite unresolved legs (surfaced, not blocking): %s",
                " ".join(
                    f"{e.line}/{e.variant}/{e.platform}={e.status}"
                    for e in agg.unresolved_legs
                ),
            )
        logger.info(
            "Aggregation complete: dispatched=%s skipped=%s",
            " ".join(agg.dispatched_lines) or "(none)",
            " ".join(agg.skipped_lines) or "(none)",
        )
        return 0

    if agg.learned_nothing:
        # No line dispatchable AND a leg errored or is missing: we cannot tell
        # "not fixable" from "we failed to look". Fail closed, dispatch nothing.
        _write_outputs([], arch_report)
        logger.error(
            "Learned nothing usable (fail closed): no architecture verified for "
            "any line, and legs errored or went missing: %s",
            " ".join(
                f"{e.line}/{e.variant}/{e.platform}={e.status}"
                for e in agg.unresolved_legs
            ),
        )
        return 1

    # No verified architecture anywhere, but every leg reported (all survivors):
    # a legitimate not-fixable-this-week, not a failure.
    _write_outputs([], arch_report)
    logger.info(
        "No architecture proven fixed for any line (all survivors); nothing to "
        "dispatch."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
