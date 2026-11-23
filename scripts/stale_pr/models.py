"""Data shapes shared by the stale pull request triage steps."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PRActivity:
    """When a pull request last saw real work, by kind.

    These dates count only the events that mean someone worked on the PR.
     ``None`` means the PR has no event of that kind.
    """

    last_code_at: datetime | None
    last_author_at: datetime | None
    last_maintainer_at: datetime | None


@dataclass(frozen=True)
class CandidatePolicy:
    """Per-repository settings for picking candidates."""

    days_without_code: int = 365
    days_without_comments: int = 180
    # Labels that tell the job to leave a PR alone, compared case-insensitively.
    skip_labels: frozenset[str] = frozenset({"do-not-close", "to-be-merged"})
    max_per_run: int = 10


@dataclass(frozen=True)
class CandidateDecision:
    """Whether one PR is due for review, and why."""

    number: int
    due: bool
    reason: str
    # The newer of the PR's last code and last comment; candidates are reviewed oldest first.
    last_activity_at: datetime
