"""Shared AI-notes pipeline: discover -> classify -> generate -> render.

Takes a release line's clone, finds labelled PRs merged since its last tag, and
produces categorized bullets as a {category: [line, ...]} map. The release_format
module renders that map into a dated section at cut time.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from scripts.release_notes import discover as discover_mod
from scripts.release_notes import generate as generate_mod
from scripts.release_notes import release_format as release_format_mod
from scripts.release_notes import render as render_mod
from scripts.release_notes.classify import classify
from scripts.release_notes.models import (
    CollidedCommit,
    MergedPR,
    UncertainNote,
    UnresolvedBackport,
    UnresolvedCherryPick,
    UnresolvedCommit,
    UnresolvedPR,
)

logger = logging.getLogger(__name__)

# Matches trailing "(#N)" to recover PR number from a rendered bullet line.
_TRAILING_PR_RE = re.compile(r"\(#(\d+)\)\s*$")


@dataclass(frozen=True)
class RegenResult:
    """Categorized bullets for a release line's range, plus range metadata."""

    base_tag: str
    grouped: dict[str, list[str]]  # {category: [rendered bullet line, ...]} for this cut
    included: int               # PRs included (labelled release-notes)
    bullet_count: int           # bullets actually rendered (post group_bullets: after dup-PR dedup and reserved-category drops)
    skipped: tuple[int, ...]    # PR numbers with no rendered note: model-declined, parse-failure batches, or reserved-category drops (see regenerate_unreleased)
    triage: tuple[MergedPR, ...]  # untagged / double-labelled PRs
    had_prs: bool               # whether the range contained any PR at all
    duplicate_prs: tuple[int, ...] = ()  # PR numbers the model emitted more than once (extra bullets dropped)
    uncertain: tuple[UncertainNote, ...] = ()  # low-confidence notes the model flagged, for the PR body
    unresolved: tuple[UnresolvedCommit, ...] = ()  # range commits that resolved to no PR (shipped un-noted)
    unresolved_backports: tuple[UnresolvedBackport, ...] = ()  # credited backports whose original source was unreachable
    unresolved_prs: tuple[UnresolvedPR, ...] = ()  # range commits whose resolved PR number could not be fetched (shipped un-noted)
    unresolved_cherry_picks: tuple[UnresolvedCherryPick, ...] = ()  # notes credited past an unresolvable -x trailer (origin unconfirmed)
    collided: tuple[CollidedCommit, ...] = ()  # distinct commits dropped by a reused subject (#N) (shipped un-noted)


def regenerate_unreleased(
    repo: Any, clone_dir: str, *, head_ref: str, tag_glob: str | None,
    base_ref: str | None = None,
) -> RegenResult:
    """Discover the range and generate categorized bullets for it.

    ``base_ref`` overrides tag-based baseline resolution. Returns a RegenResult
    whose ``grouped`` map the cut caller renders into a dated section.
    """
    discovery = discover_mod.discover(
        repo, clone_dir, head_ref, tag_glob=tag_glob, base_ref=base_ref
    )
    if not discovery.prs:
        return RegenResult(
            base_tag=discovery.base_tag, grouped={},
            included=0, bullet_count=0, skipped=(), triage=(), had_prs=False,
            unresolved=discovery.unresolved,
            unresolved_backports=discovery.unresolved_backports,
            unresolved_prs=discovery.unresolved_prs,
            unresolved_cherry_picks=discovery.unresolved_cherry_picks,
            collided=discovery.collided,
        )

    include, _exclude, triage = classify(discovery.prs)
    logger.info(
        "%d included, %d excluded, %d triage", len(include),
        len(discovery.prs) - len(include) - len(triage), len(triage),
    )

    gen = generate_mod.generate(
        include, repo_dir=clone_dir, categories=release_format_mod.CATEGORIES
    )
    # Keep one bullet per PR; prefer a renderable bullet over a reserved-category one.
    bullets, duplicate_prs = _dedup_bullets_by_pr(gen.bullets)
    grouped = render_mod.group_bullets(bullets)

    # Only report uncertainty for bullets that survive into grouped.
    rendered_prs = {
        int(m.group(1))
        for lines in grouped.values()
        for line in lines
        if (m := _TRAILING_PR_RE.search(line))
    }
    uncertain = tuple(
        UncertainNote(pr_number=b.pr_number, category=b.category, reason=b.uncertain_reason)
        for b in bullets
        if b.uncertain and b.pr_number in rendered_prs
    )

    # Count rendered bullets (group_bullets drops reserved-category ones).
    promoted_count = sum(len(lines) for lines in grouped.values())

    # PRs whose only bullet was dropped (reserved category) go into skipped.
    dropped_prs = {b.pr_number for b in bullets} - rendered_prs
    skipped = tuple(sorted((set(gen.skipped) | dropped_prs) - rendered_prs))

    # Only flag duplicates for PRs that actually rendered.
    duplicate_prs = tuple(pr for pr in duplicate_prs if pr in rendered_prs)

    return RegenResult(
        base_tag=discovery.base_tag, grouped=grouped,
        included=len(include), bullet_count=promoted_count, skipped=skipped,
        triage=tuple(triage), had_prs=True,
        duplicate_prs=duplicate_prs, uncertain=uncertain,
        unresolved=discovery.unresolved,
        unresolved_backports=discovery.unresolved_backports,
        unresolved_prs=discovery.unresolved_prs,
        unresolved_cherry_picks=discovery.unresolved_cherry_picks,
        collided=discovery.collided,
    )


def _dedup_bullets_by_pr(bullets):
    """Keep one bullet per PR number; return (kept, duplicate_pr_numbers).

    Prefers a renderable bullet over a reserved-category one so a stray
    reserved bullet cannot shadow the PR's real note.
    """
    order: list[int] = []
    by_pr: dict[int, list] = {}
    for b in bullets:
        if b.pr_number not in by_pr:
            by_pr[b.pr_number] = []
            order.append(b.pr_number)
        by_pr[b.pr_number].append(b)

    kept = []
    dups: list[int] = []
    for pr in order:
        group = by_pr[pr]
        # First renderable bullet wins; fall back to first if all are reserved.
        chosen = next(
            (b for b in group if not render_mod.is_reserved_category(b.category)),
            group[0],
        )
        kept.append(chosen)
        if len(group) > 1:
            dups.append(pr)
            logger.warning("PR #%s has more than one bullet; keeping one renderable bullet", pr)
    return tuple(kept), tuple(dups)
