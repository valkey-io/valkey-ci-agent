"""Decide which open pull requests are due for a stale review.

A pull request is a candidate when it has had no new code for a long time
(one year by default) and no comment or review from its author or a
maintainer for a shorter time (six months by default). A PR that never had
a commit or comment of that kind is measured from when it was opened.

A PR is skipped, whatever its age, when it has a skip label, when someone
asked to keep it open, or when the job is already waiting on it: it asked
the author and the 30-day notice is running, or it asked a maintainer to
decide. Those PRs are followed up separately, not reviewed again.

These are pure rules over facts the caller has already fetched; nothing here
calls GitHub.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from scripts.stale_pr.models import CandidateDecision, CandidatePolicy, PRActivity


def decide(
    number: int,
    *,
    activity: PRActivity,
    opened_at: datetime,
    labels: Iterable[str],
    now: datetime,
    policy: CandidatePolicy,
    keep_requested: bool = False,
    awaiting_reply: bool = False,
) -> CandidateDecision:
    """Return whether PR *number* is due for review under *policy*."""
    last_code = activity.last_code_at or opened_at
    last_comment = _newest(activity.last_author_at, activity.last_maintainer_at) or opened_at
    last_activity = max(last_code, last_comment)

    def result(due: bool, reason: str) -> CandidateDecision:
        return CandidateDecision(number=number, due=due, reason=reason, last_activity_at=last_activity)

    skip = {label.casefold() for label in policy.skip_labels}
    matched = sorted(label for label in labels if label.casefold() in skip)
    if matched:
        return result(False, f"has skip label {', '.join(matched)}")
    if keep_requested:
        return result(False, "someone asked to keep it open")
    if awaiting_reply:
        return result(False, "already waiting for a reply")

    code_days = (now - last_code).days
    comment_days = (now - last_comment).days
    if code_days < policy.days_without_code:
        return result(False, f"code changed {code_days} days ago")
    if comment_days < policy.days_without_comments:
        return result(False, f"author or maintainer commented {comment_days} days ago")
    return result(True, f"no code for {code_days} days and no comments for {comment_days} days")


def pick_candidates(decisions: Iterable[CandidateDecision], policy: CandidatePolicy) -> list[CandidateDecision]:
    """The due PRs, oldest activity first, at most ``policy.max_per_run`` of them."""
    due = sorted((d for d in decisions if d.due), key=lambda d: (d.last_activity_at, d.number))
    return due[: policy.max_per_run]


def _newest(*dates: datetime | None) -> datetime | None:
    present = [d for d in dates if d is not None]
    return max(present) if present else None
