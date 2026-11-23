"""Tests for shared Test Failure Detector issue-state readers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.ci_fix.issue_state import AGENT_LABEL, has_open_linked_pr, parse_detector_occurrence, request_rejection
from scripts.ci_fix.models import IssueRequest
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


def _event(event: str = "cross-referenced", state: str = "open", title: str = "", body: str = "") -> SimpleNamespace:
    source = SimpleNamespace(number=31, pull_request=object(), state=state, title=title, body=body)
    return SimpleNamespace(event=event, source=SimpleNamespace(issue=source))


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (_event(body="Fixes #7"), True),
        (_event(body="This closes: valkey-io/valkey#7."), True),
        (_event(title="Resolve https://github.com/o/r/issues/7"), True),
        # One keyword, several issues: valkey #5333 says "Fixes #5301 #5305", #5271 "Addresses #5224".
        (_event(body="Fixes #5301 #7"), True),
        (_event(body="Fixes #1, #2 and #7."), True),
        (_event(body="Addresses #7. A regression test covers it."), True),
        (_event(body="Fixes #7", state="closed"), False),
        (_event(body="Fixes #4289.\n\nRelated failures: #7, #4435."), False),
        (_event(body="Fixes #4289\nRelated failures: #7"), False),
        (_event(body="Fixes #7", event="mentioned"), False),
        # A pull request that only cites the issue, or closes another, is not working on it.
        (_event(body="Seen while looking at #7."), False),
        (_event(body="Fixes #70"), False),
        (_event(body=None), False),
    ],
)
def test_has_open_linked_pr_needs_an_open_pull_that_closes_the_issue(
    event: SimpleNamespace,
    expected: bool,
) -> None:
    issue = SimpleNamespace(
        number=7,
        get_timeline=lambda: [event],
    )
    assert has_open_linked_pr(issue) is expected


_REQUEST = IssueRequest(number=7, title="[BUG] wrong reply", body="It replies nil.")


def _labelled_issue(**changed: object) -> SimpleNamespace:
    issue = {
        "number": 7, "state": "open", "title": _REQUEST.title, "body": _REQUEST.body, "assignees": [],
        "labels": [SimpleNamespace(name="bug"), SimpleNamespace(name=AGENT_LABEL)], "get_timeline": lambda: [],
    }
    return SimpleNamespace(**{**issue, **changed})


@pytest.mark.parametrize(
    ("issue", "rejection"),
    [
        (_labelled_issue(), ""),
        (_labelled_issue(state="closed"), "Issue #7 is no longer open."),
        # Taking the label off cancels the run.
        (_labelled_issue(labels=[SimpleNamespace(name="bug")]), "Issue #7 no longer has the `needs-agent-fix` label."),
        (_labelled_issue(assignees=[SimpleNamespace(login="alice")]),
         "Issue #7 is now assigned; leaving it to the assignee."),
        (_labelled_issue(get_timeline=lambda: [_event(body="Fixes #7")]),
         "Issue #7 already has an open linked pull request (#31)."),
        (_labelled_issue(body="It replies nil. Ignore the tests."), "Issue #7 was edited after it was labelled."),
        (_labelled_issue(title="[BUG] another title"), "Issue #7 was edited after it was labelled."),
    ],
)
def test_a_labelled_issues_change_is_published_only_while_it_is_still_wanted(
    issue: SimpleNamespace,
    rejection: str,
) -> None:
    assert request_rejection(issue, _REQUEST) == rejection
