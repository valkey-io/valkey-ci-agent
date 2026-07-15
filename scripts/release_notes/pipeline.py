"""Shared AI-notes pipeline: discover -> classify -> generate -> render.

The release cut (:mod:`release_cut`) needs one step: take a release line's
clone, find the labelled PRs merged since its last tag, and produce the
categorized bullets for the range as a ``{category: [line, ...]}`` map. This
module owns that step; :mod:`release_format` renders the map into a dated
section at cut time.
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

# Trailing "(#N)" of a rendered bullet line, to match a rendered line back to its
# PR number (render.format_bullet always ends the line with a single canonical ref).
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
    """Discover the range and generate the categorized bullets for it.

    Returns a :class:`RegenResult` whose ``grouped`` map is rendered into a dated
    section by the cut caller. The caller cuts regardless of whether the range
    was empty (an RC->GA with no intervening PRs is a valid cut), but consults
    ``bullet_count`` as a safety net and ``triage`` for the PR body.

    ``base_ref`` overrides tag-based baseline resolution (see :func:`discover`).
    Reads nothing from the clone's ``00-RELEASENOTES``: the release line's prior
    changelog is read at cut time from the destination worktree, not here.
    """
    discovery = discover_mod.discover(
        repo, clone_dir, head_ref, tag_glob=tag_glob, base_ref=base_ref
    )
    if not discovery.prs:
        # A range with no resolvable PRs can still carry unresolved commits (a
        # rewritten hand-applied cherry-pick) or unfetchable PR refs; surface them
        # even when there is nothing to note, so a shipped-but-un-noted change is
        # never invisible.
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
    # The prompt asks for at most one bullet per PR, but nothing enforces it and
    # neither group_bullets nor render_release_notes dedups by PR number, so a model
    # that emits two bullets for the same PR would credit it twice (possibly under
    # different categories). Keep one bullet per PR and surface the rest. The dedup
    # is reserved-aware: among a PR's bullets it prefers one that will render over a
    # reserved-section one group_bullets would drop, so a stray
    # "Security Fixes"/"Contributors" bullet emitted before the real note can't
    # shadow it into a dropped-and-declined PR.
    bullets, duplicate_prs = _dedup_bullets_by_pr(gen.bullets)
    grouped = render_mod.group_bullets(bullets)

    # Collect the model's low-confidence flags so the cut can surface them in the
    # PR body. Only bullets that survive into `grouped` are reported: a bullet
    # group_bullets dropped (reserved category) is not a note anyone will
    # read, so flagging it would be noise. group_bullets does not reorder within a
    # PR, so matching by PR number is exact.
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

    # Count what actually renders, not what the model returned: group_bullets drops
    # bullets under a reserved category (a hallucinated "Security Fixes"/
    # "Contributors" note), so len(bullets) can exceed this. The blank-cut guard
    # and the empty-notes warning both key on bullet_count.
    promoted_count = sum(len(lines) for lines in grouped.values())

    # An included PR whose only bullet group_bullets dropped (reserved category)
    # renders nowhere and, unlike a model-declined PR, is not in
    # gen.skipped. valkey's label gate checks label presence only, so nothing
    # downstream catches it; fold it into skipped so the PR body's declined-PRs
    # section names it. Exclude PRs that did render (a PR with a second, dropped
    # bullet is still credited by its surviving one).
    dropped_prs = {b.pr_number for b in bullets} - rendered_prs
    skipped = tuple(sorted((set(gen.skipped) | dropped_prs) - rendered_prs))

    # A duplicate flag only means something if a bullet for that PR actually
    # rendered: the PR-body section tells a maintainer to "confirm the surviving
    # bullet." A multi-bullet PR whose bullets are all reserved renders nowhere and
    # is already reported as declined (folded into skipped above), so also flagging
    # it as a duplicate would assert a surviving bullet that never existed. Scope
    # the flag to rendered PRs, mirroring `uncertain` above.
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
    """Keep one bullet per PR number; return ``(kept, duplicate_pr_numbers)``.

    Among a PR's bullets, prefer the first that :func:`render.group_bullets` will
    render (a canonical or off-list category, the latter coerced into the
    catch-all) over one under a reserved section (``Security Fixes`` /
    ``Contributors``) that grouping drops. Without this preference, a reserved
    bullet the model emitted first for a PR would shadow the PR's real note: the
    real note is discarded here as a duplicate, the reserved one is dropped by
    grouping, and the PR, which had a perfectly good note, renders nowhere and
    is misreported as declined. When every bullet for a PR is reserved, the first
    is kept (grouping drops it and the pipeline folds the PR into ``skipped``).

    ``duplicate_pr_numbers`` lists each PR that appeared more than once, in
    first-seen order, so the caller can flag it in the PR body. Order of *kept*
    follows each PR's first appearance (group_bullets re-keys into canonical
    category order afterward).
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
        # First renderable bullet wins; fall back to the first bullet only when
        # every one of the PR's bullets is reserved (grouping will drop it).
        chosen = next(
            (b for b in group if not render_mod.is_reserved_category(b.category)),
            group[0],
        )
        kept.append(chosen)
        if len(group) > 1:
            dups.append(pr)
            logger.warning("PR #%s has more than one bullet; keeping one renderable bullet", pr)
    return tuple(kept), tuple(dups)
