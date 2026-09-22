"""Turn generated bullets into canonical release-notes lines and grouping.

Formats each CategorizedBullet as ``* <text> by @<handle> (#<N>)`` and groups
them by category into the ``{category: [line, ...]}`` map that
release_format.render_release_notes renders into a dated section.
"""

from __future__ import annotations

import logging
import re
from typing import Sequence

from scripts.release_notes import release_format as _release_format
from scripts.release_notes.models import CategorizedBullet

logger = logging.getLogger(__name__)

# GitHub login charset: [A-Za-z0-9-].
_HANDLE_SAFE_RE = re.compile(r"[^\w-]")


def _one_line(text: str) -> str:
    """Collapse *text* to a single physical line."""
    return " ".join(text.splitlines()).strip()


def safe_handle(author: str) -> str:
    """The handle :func:`format_bullet` would credit for *author* ("" for none)."""
    return _HANDLE_SAFE_RE.sub("", author)


def format_bullet(bullet: CategorizedBullet) -> str:
    """Render one canonical bullet line: ``* <text> by @<handle> (#<N>)``.

    Strips any trailing ``(#...)`` the model left in the text, collapses to one
    line, and omits the ``by @`` segment when the author is unknown.
    """
    text = _one_line(bullet.text)
    text = re.sub(r"^\s*[*-]\s+", "", text)
    text = re.sub(r"\s*\(#[^)]*\)[\s.,;:!?]*$", "", text).strip()
    text = re.sub(
        r"\s+by\s+@[A-Za-z0-9-]+[\s.,;:!?]*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    # Handle the inverse accidental order too: "(#N) by @user".
    text = re.sub(r"\s*\(#[^)]*\)[\s.,;:!?]*$", "", text).strip()
    # Valkey's canonical form ends at "(#N)", so sentence punctuation before the
    # generated attribution reads awkwardly ("Fixed X. by @user"). Drop it here
    # even when the model ignored the prompt's no-final-punctuation rule.
    without_terminal_punctuation = text.rstrip(" .,:;!?")
    if without_terminal_punctuation:
        text = without_terminal_punctuation
    parts = [f"* {text}"]
    handle = safe_handle(bullet.author)
    if handle:
        parts.append(f"by @{handle}")
    parts.append(f"(#{bullet.pr_number})")
    return " ".join(parts)


def _reserved_sections() -> set[str]:
    """Case-folded reserved section names that group_bullets refuses to render."""
    return {r.casefold() for r in _release_format.RESERVED_SECTIONS}


def is_reserved_category(category: str) -> bool:
    """Whether *category* names a reserved section that group_bullets will drop."""
    return _one_line(category).casefold() in _reserved_sections()


def group_bullets(
    bullets: Sequence[CategorizedBullet],
    categories: Sequence[str],
) -> dict[str, list[str]]:
    """Group bullets into ``{category: [rendered line, ...]}``.

    *categories* is the profile's canonical list. Non-canonical categories are
    coerced into the catch-all. Bullets under reserved sections (Security
    Fixes, Contributors) are refused and logged. Output keys are in
    *categories* order.
    """
    ordered_categories = categories
    reserved = _reserved_sections()
    canonical = set(ordered_categories)
    # Fallback if CATCH_ALL_CATEGORY were edited off-list.
    catch_all = _release_format.CATCH_ALL_CATEGORY
    if catch_all not in canonical:
        catch_all = ordered_categories[-1]
    grouped: dict[str, list[str]] = {}
    for bullet in bullets:
        category = _one_line(bullet.category)
        if category.casefold() in reserved:
            logger.warning(
                "Refusing PR #%s under reserved section %r (auto-generated at release)",
                bullet.pr_number, category,
            )
            continue
        if category not in canonical:
            logger.warning(
                "PR #%s assigned non-canonical category %r; placing under %r",
                bullet.pr_number, category, catch_all,
            )
            category = catch_all
        grouped.setdefault(category, []).append(format_bullet(bullet))

    # Emit in canonical order.
    ordered: dict[str, list[str]] = {}
    for name in ordered_categories:
        if grouped.get(name):
            ordered[name] = grouped[name]
    return ordered
