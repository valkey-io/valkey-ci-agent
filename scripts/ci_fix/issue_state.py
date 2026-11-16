"""Shared state readers for Test Failure Detector issues."""

from __future__ import annotations

import re
from typing import Any

from scripts.common.github_client import retry_github_call
from scripts.test_failure_detector.issue_renderer import MARKER_NAMESPACE

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
