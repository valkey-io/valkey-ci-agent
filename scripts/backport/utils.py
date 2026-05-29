"""Naming conventions and conflict detection helpers."""

from __future__ import annotations

import re

_CONFLICT_MARKERS = re.compile(
    r"^(<{7} \S|={7}$|>{7} \S|<{7}$|>{7}$)",
    re.MULTILINE,
)


def build_branch_name(source_pr_number: int, target_branch: str) -> str:
    return f"backport/{source_pr_number}-to-{target_branch}"


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
