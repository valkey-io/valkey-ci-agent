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


def has_open_linked_pr(issue: Any) -> bool:
    """Return whether an issue timeline links an open pull request."""
    timeline = retry_github_call(
        lambda: list(issue.get_timeline()),
        retries=2,
        description=f"read issue #{issue.number} timeline",
    )
    for event in timeline:
        if str(getattr(event, "event", "") or "") != "cross-referenced":
            continue
        source = getattr(getattr(event, "source", None), "issue", None)
        if (
            source is not None
            and getattr(source, "pull_request", None) is not None
            and str(getattr(source, "state", "") or "") == "open"
        ):
            return True
    return False
