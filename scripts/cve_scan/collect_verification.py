"""Reconcile CVE verification markers against the scan's verification plan."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)
_PLAN_KEYS = {"line", "variant", "platform", "cves"}
_MARKER_KEYS = {"line", "variant", "platform", "outcome"}
_OUTCOMES = {"verified", "survivors", "error"}
_LegKey = tuple[str, str, str]


class CollectError(Exception):
    """A plan or marker contract is malformed."""


@dataclass(frozen=True)
class Status:
    line: str
    variant: str
    platform: str
    outcome: str

    @property
    def key(self) -> _LegKey:
        return self.line, self.variant, self.platform


def _object(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise CollectError(f"{label} must be an object")
    if set(value) != keys:
        raise CollectError(
            f"{label} key mismatch: missing={sorted(keys - set(value))}, extra={sorted(set(value) - keys)}"
        )
    return value


def parse_plan(raw: str) -> set[_LegKey]:
    """Validate the grouped plan and return its unique expected legs."""
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CollectError(f"plan is not valid JSON: {exc}") from exc
    if not isinstance(plan, list) or not plan:
        raise CollectError("plan must be a non-empty JSON list")

    expected: set[_LegKey] = set()
    for index, value in enumerate(plan):
        leg = _object(value, _PLAN_KEYS, f"plan[{index}]")
        for key in ("line", "variant", "platform"):
            if not isinstance(leg[key], str) or not leg[key]:
                raise CollectError(f"plan[{index}].{key} must be a non-empty string")
        cves = leg["cves"]
        if (
            not isinstance(cves, list)
            or not cves
            or any(not isinstance(cve, str) or not cve for cve in cves)
            or len(cves) != len(set(cves))
        ):
            raise CollectError(f"plan[{index}].cves must contain unique CVE strings")
        leg_key: _LegKey = leg["line"], leg["variant"], leg["platform"]
        if leg_key in expected:
            raise CollectError(f"plan contains duplicate leg {leg_key}")
        expected.add(leg_key)
    return expected


def load_markers(directory: Path) -> list[Status]:
    """Load unique, strictly shaped result files."""
    paths = sorted(directory.rglob("*.json"))
    if not paths:
        raise CollectError(f"no marker files found under {directory}")
    markers: list[Status] = []
    seen: set[_LegKey] = set()
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CollectError(f"cannot parse marker {path}: {exc}") from exc
        marker = _object(value, _MARKER_KEYS, f"marker {path}")
        if any(not isinstance(marker[key], str) or not marker[key] for key in _MARKER_KEYS):
            raise CollectError(f"marker {path} fields must be non-empty strings")
        if marker["outcome"] not in _OUTCOMES:
            raise CollectError(f"marker {path} has invalid outcome {marker['outcome']!r}")
        status = Status(**marker)
        if status.key in seen:
            raise CollectError(f"duplicate marker for leg {status.key}")
        seen.add(status.key)
        markers.append(status)
    return markers


def reconcile(markers: list[Status], expected: set[_LegKey]) -> list[Status]:
    actual = {marker.key: marker.outcome for marker in markers}
    unexpected = set(actual) - expected
    if unexpected:
        raise CollectError(f"unexpected marker leg(s): {sorted(unexpected)}")
    return [Status(*key, actual.get(key, "missing")) for key in sorted(expected)]


def _group(statuses: list[Status]) -> dict[str, list[Status]]:
    lines: dict[str, list[Status]] = {}
    for status in statuses:
        lines.setdefault(status.line, []).append(status)
    return lines


def _arch_report(statuses: list[Status]) -> str:
    lines = _group(statuses)
    payload = {
        "lines": [
            {
                "line": line,
                "dispatched": any(item.outcome == "verified" for item in items),
                "legs": [
                    {
                        "variant": item.variant,
                        "platform": item.platform,
                        "status": item.outcome,
                    }
                    for item in items
                ],
            }
            for line, items in lines.items()
        ]
    }
    return json.dumps(payload, separators=(",", ":"))


def _summary(statuses: list[Status]) -> str:
    lines = [
        "## CVE Verification Aggregation",
        "",
        "A line dispatches when at least one affected architecture is verified.",
        "",
        "| Line | Variant | Platform | Status |",
        "|---|---|---|---|",
    ]
    lines.extend(f"| `{item.line}` | {item.variant} | {item.platform} | {item.outcome} |" for item in statuses)
    return "\n".join(lines) + "\n"


def _write_outputs(versions: list[str], report: str) -> None:
    values = {
        "verified_versions": " ".join(versions),
        "arch_report": report,
    }
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
    else:
        for key, value in values.items():
            print(f"{key}={value}")


def _write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markers-dir", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    try:
        expected = parse_plan(args.plan)
        statuses = reconcile(load_markers(Path(args.markers_dir)), expected)
    except CollectError as exc:
        logger.error("Aggregation failed: %s", exc)
        _write_outputs([], '{"lines":[]}')
        _write_summary(f"## CVE Verification Aggregation\n\nFAIL: {exc}\n")
        return 2

    lines = _group(statuses)
    versions = sorted(line for line, items in lines.items() if any(item.outcome == "verified" for item in items))
    _write_outputs(versions, _arch_report(statuses))
    _write_summary(_summary(statuses))

    unresolved = [item for item in statuses if item.outcome in {"error", "missing"}]
    if not versions and unresolved:
        logger.error("No verified architecture and unresolved legs remain")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
