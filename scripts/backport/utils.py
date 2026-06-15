"""Naming conventions and conflict detection helpers."""

from __future__ import annotations

import re

_CONFLICT_MARKERS = re.compile(
    r"^(<{7} \S|={7}$|>{7} \S|<{7}$|>{7}$)",
    re.MULTILINE,
)


def build_branch_name(source_pr_number: int, target_branch: str) -> str:
    return f"backport/{source_pr_number}-to-{target_branch}"


def pr_numbers_from_commit_subjects(subjects: list[str]) -> set[int]:
    """Source PR numbers referenced by a trailing ``(#N)`` in commit subjects.

    Single source of truth for "which PRs does this commit history contain",
    shared by the sweep (to skip already-applied PRs) and mark-done (to verify
    a board item actually landed). Only subjects are considered — a ``(#N)`` in
    a commit body is a reference, not an application.
    """
    numbers: set[int] = set()
    for line in subjects:
        m = re.search(r"\(#(\d+)\)", line)
        if m:
            numbers.add(int(m.group(1)))
    return numbers


def build_pr_title(source_pr_title: str, target_branch: str) -> str:
    return f"[Backport {target_branch}] {source_pr_title}"


def has_conflict_markers(content: str) -> bool:
    """Return ``True`` if *content* contains git conflict markers."""
    return bool(_CONFLICT_MARKERS.search(content))


def is_whitespace_only_conflict(target_content: str, source_content: str) -> bool:
    """Return ``True`` when the two contents differ only in whitespace."""
    return _strip_all_whitespace(target_content) == _strip_all_whitespace(source_content)


def _strip_all_whitespace(s: str) -> str:
    return re.sub(r"\s+", "", s)
