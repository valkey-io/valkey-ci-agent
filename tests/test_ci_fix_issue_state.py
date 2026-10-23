"""Tests for shared Test Failure Detector issue-state readers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.ci_fix.issue_state import has_open_linked_pr, parse_detector_occurrence
from scripts.test_failure_detector.issue_renderer import MARKER_NAMESPACE


def test_parse_detector_occurrence_reads_the_run() -> None:
    assert parse_detector_occurrence(f"<!-- {MARKER_NAMESPACE}:last-key:100 -->") == 100


def test_parse_detector_occurrence_rejects_missing_or_malformed_markers() -> None:
    assert parse_detector_occurrence("no detector marker") is None
    assert (
        parse_detector_occurrence(
            f"<!-- {MARKER_NAMESPACE}:last-key:not-a-run -->"
        )
        is None
    )


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            SimpleNamespace(
                event="cross-referenced",
                source=SimpleNamespace(
                    issue=SimpleNamespace(
                        pull_request=object(),
                        state="open",
                    )
                ),
            ),
            True,
        ),
        (
            SimpleNamespace(
                event="cross-referenced",
                source=SimpleNamespace(
                    issue=SimpleNamespace(
                        pull_request=object(),
                        state="closed",
                    )
                ),
            ),
            False,
        ),
        (
            SimpleNamespace(
                event="mentioned",
                source=SimpleNamespace(
                    issue=SimpleNamespace(
                        pull_request=object(),
                        state="open",
                    )
                ),
            ),
            False,
        ),
    ],
)
def test_has_open_linked_pr_checks_cross_referenced_open_pulls(
    event: SimpleNamespace,
    expected: bool,
) -> None:
    issue = SimpleNamespace(
        number=7,
        get_timeline=lambda: [event],
    )
    assert has_open_linked_pr(issue) is expected
