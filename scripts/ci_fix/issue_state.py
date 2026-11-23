"""Shared state readers for the issues the bot works on: the Test Failure Detector's, and the ones a committer labels."""

from __future__ import annotations

import re
from typing import Any

from scripts.ci_fix.models import IssueRequest
from scripts.common.github_client import retry_github_call
from scripts.test_failure_detector.issue_renderer import MARKER_NAMESPACE

# The label a committer puts on an issue to have the bot make its change (scripts/ci_fix/labelled.py).
AGENT_LABEL = "needs-agent-fix"
# How a rejection says an open pull request already links the issue: a
# publication that finds one checks whether it is its own (scripts/ci_fix/publish.py).
LINKED_PR = "already has an open linked pull request"

_OCCURRENCE_RE = re.compile(
    rf"<!-- {re.escape(MARKER_NAMESPACE)}:last-key:(?P<run>\d+) -->"
)


def parse_detector_occurrence(body: str) -> int | None:
    """Return the run id from a detector occurrence marker."""
    marker = _OCCURRENCE_RE.search(body)
    if marker is None:
        return None
    return int(marker.group("run"))


# A closing keyword, then the issue among the references that follow it on
# the line: "Fixes #12", "closes: valkey-io/valkey#12", "Fixes #11 #12",
# "resolves https://github.com/o/r/issues/11, #12". GitHub itself closes only
# the first of a list, but its author is working on all of them; so is one
# who writes "Addresses #12".
_REFERENCE = r"\S*(?:#|/issues/)"
_CLOSING_REFERENCE = (
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|address(?:e[sd])?):?[ \t]+"
    rf"(?:{_REFERENCE}\d+[ \t,]*(?:and[ \t]+)?)*?{_REFERENCE}{{number}}(?!\d)"
)


def has_open_linked_pr(issue: Any) -> bool:
    """Return whether an open pull request says it closes ``issue``.

    A pull request that only mentions the issue ("Related failures: #12") is
    not working on it: one that cites the failure must not keep the issue
    from a fix.
    """
    return bool(open_linked_prs(issue))


def open_linked_prs(issue: Any) -> list[int]:
    """The open pull requests that say they close ``issue`` (:func:`has_open_linked_pr`)."""
    timeline = retry_github_call(
        lambda: list(issue.get_timeline()),
        retries=2,
        description=f"read issue #{issue.number} timeline",
    )
    closes = re.compile(_CLOSING_REFERENCE.format(number=int(issue.number)), re.IGNORECASE)
    linked: list[int] = []
    for event in timeline:
        if str(getattr(event, "event", "") or "") != "cross-referenced":
            continue
        source = getattr(getattr(event, "source", None), "issue", None)
        if (
            source is not None
            and getattr(source, "pull_request", None) is not None
            and str(getattr(source, "state", "") or "") == "open"
            and closes.search(f"{getattr(source, 'title', '') or ''}\n{getattr(source, 'body', '') or ''}")
        ):
            linked.append(int(source.number))
    return linked


def labels_of(issue: Any) -> set[str]:
    """The names of the labels on ``issue``."""
    return {str(getattr(label, "name", "") or "") for label in getattr(issue, "labels", None) or ()}


def request_rejection(issue: Any, request: IssueRequest) -> str:
    """Why the change made for ``request`` is no longer wanted on ``issue``, or "".

    Checked when the issue is picked up and again right before publishing: a
    person who took the issue meanwhile is not raced, taking the label off
    cancels the run, and the change answers the text a committer labelled,
    not one edited since.
    """
    number = int(issue.number)
    if str(getattr(issue, "state", "") or "") != "open":
        return f"Issue #{number} is no longer open."
    if AGENT_LABEL not in labels_of(issue):
        return f"Issue #{number} no longer has the `{AGENT_LABEL}` label."
    if getattr(issue, "assignees", None):
        return f"Issue #{number} is now assigned; leaving it to the assignee."
    linked = open_linked_prs(issue)
    if linked:
        return f"Issue #{number} {LINKED_PR} (#{linked[0]})."
    if (str(getattr(issue, "title", "") or ""), str(getattr(issue, "body", "") or "")) != (request.title, request.body):
        return f"Issue #{number} was edited after it was labelled."
    return ""
