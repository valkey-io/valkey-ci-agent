"""Turn generated bullets into the canonical release-notes lines and grouping.

The format lives in :mod:`scripts.release_notes.release_format` (valkey-io/valkey
ships no release tooling of its own; see that module's docstring). This module
reuses its ``CATEGORIES`` so the agent never re-encodes the category names.

What this module owns is purely mechanical: turning each
:class:`CategorizedBullet` into the canonical bullet line
``* <text> by @<handle> (#<N>)`` (the ``(#N)`` trailing and the ``by @handle``
following valkey's hand-written release-note convention) and grouping those
lines by category into the ``{category: [line, ...]}`` map that
:func:`release_format.render_release_notes` renders into a dated section.
valkey's ``check_release_notes`` gate is label-only and does not parse this
file, so the form here is a convention, not something CI validates.
"""

from __future__ import annotations

import logging
import re
from typing import Sequence

from scripts.release_notes import release_format as _release_format
from scripts.release_notes.models import CategorizedBullet

logger = logging.getLogger(__name__)

# A GitHub login is [A-Za-z0-9-]; the attribution follows ``by @([\w-]+)``.
# Anything outside that set (a space, '.', '@', or parens from a malformed
# author) would truncate or break the captured handle.
_HANDLE_SAFE_RE = re.compile(r"[^\w-]")


def _one_line(text: str) -> str:
    """Collapse *text* to a single physical line.

    A bullet and a category header are line-oriented, so an embedded line break
    would split the bullet or inject a spurious ``"### ..."``/``"## ..."`` line
    into the rendered section. We split on exactly the boundaries
    ``str.splitlines()`` recognizes, then join with single spaces.
    """
    return " ".join(text.splitlines()).strip()


def format_bullet(bullet: CategorizedBullet) -> str:
    """Render one canonical bullet line: ``* <text> by @<handle> (#<N>)``.

    The trailing ``(#N)`` and the ``by @handle`` are appended in this fixed
    order to match valkey's hand-written release-note convention (trailing PR
    ref, then attribution). When the author is unknown (a ghost account), the
    ``by @`` segment is omitted. This is a formatting convention only: valkey's
    ``check_release_notes`` gate is label-only and validates neither the PR ref
    nor the attribution.

    Both the text and the handle are sanitized: the text is collapsed to a
    single line (a newline would split the bullet or inject a ``##``/``###``
    line that terminates the block), a trailing ``(#...)`` the model left inside
    the text is removed so the appended reference is the only trailing one, and
    the handle is reduced to the ``[\\w-]`` login charset so a stray space or
    punctuation can't break the attribution.
    """
    text = _one_line(bullet.text)
    text = re.sub(r"\s*\(#[^)]*\)\s*$", "", text).strip()
    parts = [f"* {text}"]
    handle = _HANDLE_SAFE_RE.sub("", bullet.author)
    if handle:
        parts.append(f"by @{handle}")
    parts.append(f"(#{bullet.pr_number})")
    return " ".join(parts)


def _reserved_sections() -> set[str]:
    """Case-folded reserved section names ``group_bullets`` refuses to render.

    ``Security Fixes`` / ``Contributors`` are populated at release-cut time from a
    factual source, so a model-assigned bullet under either is dropped. Folded to
    a case-insensitive set so a lowercase ``security fixes`` is refused too.
    """
    return {r.casefold() for r in _release_format.RESERVED_SECTIONS}


def is_reserved_category(category: str) -> bool:
    """Whether *category* names a reserved section ``group_bullets`` will drop.

    Mirrors the refusal test in :func:`group_bullets` (single-lined, case-folded)
    so a caller that must know *before* grouping whether a bullet will render
    (the pipeline's per-PR dedup, which must not let a to-be-dropped reserved
    bullet shadow a renderable one) shares one definition with the grouping.
    """
    return _one_line(category).casefold() in _reserved_sections()


def group_bullets(
    bullets: Sequence[CategorizedBullet],
) -> dict[str, list[str]]:
    """Group bullets into ``{category: [rendered line, ...]}``.

    Only canonical categories (``release_format.CATEGORIES``) are ever emitted as
    headers, in their canonical order. The model never creates a new ``### <name>``
    header: a category it returns that is not canonical is a *suggestion*, so the
    bullet is coerced into the catch-all (``CATCH_ALL_CATEGORY``, "Other Changes")
    rather than rendered under an invented header. The suggestion is surfaced
    separately for review (:mod:`generate` flags the bullet uncertain with the
    suggested name; the cut lists it in the PR body). This also closes an injection
    vector: an attacker-controlled category string can no longer emit a raw
    ``### ``/``## `` header line into the changelog.

    Bullets the model placed under the reserved ``Security Fixes`` /
    ``Contributors`` sections are refused and logged; those are generated at
    release-cut time from a factual source.
    """
    # Case-folded so a lowercase "security fixes" is refused too, not coerced into
    # the catch-all and shipped alongside the real auto-generated section.
    reserved = _reserved_sections()
    canonical = set(_release_format.CATEGORIES)
    # The catch-all must be a canonical category; keep a cheap consistency guard so
    # that if release_format's CATCH_ALL_CATEGORY were ever edited off-list, an
    # off-list bullet still lands under a valid canonical header (the last one)
    # rather than resurrecting an invented one.
    catch_all = _release_format.CATCH_ALL_CATEGORY
    if catch_all not in canonical:
        catch_all = _release_format.CATEGORIES[-1]
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
            # Off-list category: the model's choice is only a suggestion, never a
            # new header. Land the note in the catch-all so it still ships.
            logger.warning(
                "PR #%s assigned non-canonical category %r; placing under %r",
                bullet.pr_number, category, catch_all,
            )
            category = catch_all
        grouped.setdefault(category, []).append(format_bullet(bullet))

    # Emit in canonical order; every key is canonical by construction.
    ordered: dict[str, list[str]] = {}
    for name in _release_format.CATEGORIES:
        if grouped.get(name):
            ordered[name] = grouped[name]
    return ordered
