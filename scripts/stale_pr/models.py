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
