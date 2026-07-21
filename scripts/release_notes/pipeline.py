"""Shared AI-notes pipeline: discover -> classify -> triage -> generate -> render.

Takes a release line's clone, finds PRs merged since its last tag, and produces
categorized bullets as a {category: [line, ...]} map. PRs labelled ``release-notes``
are included directly and PRs labelled ``no-release-notes`` are hard-excluded; the
rest go through an AI triage pass that decides which are user-facing enough to note. The release_format module renders the resulting map
into a dated section at cut time.
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
from scripts.release_notes import triage as triage_mod
from scripts.release_notes.ai_inputs import PRDiffCollector
from scripts.release_notes.classify import classify
from scripts.release_notes.models import (
    CollidedCommit,
    MergedPR,
    ReleaseImpact,
    RevertedSourcePR,
    TriagedPR,
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
    included: int               # PRs fed to generate (labelled release-notes + AI-triaged in)
    bullet_count: int           # bullets actually rendered (post group_bullets: after dup-PR dedup and reserved-category drops)
    skipped: tuple[int, ...]    # PR numbers with no rendered note: model-declined, parse-failure batches, or reserved-category drops (see regenerate_unreleased)
    triage: tuple[MergedPR, ...]  # non-release-notes PRs AI could not decide -> human triage
    had_prs: bool               # whether the range contained any PR at all
    ai_included: tuple[TriagedPR, ...] = ()  # non-release-notes PRs AI judged user-facing
    guardrail_included: tuple[TriagedPR, ...] = ()  # AI-excluded/missing risky PRs code forced into notes
    ai_excluded: tuple[TriagedPR, ...] = ()  # non-release-notes PRs AI judged internal-only
    label_excluded: tuple[TriagedPR, ...] = ()  # PRs hard-excluded by the no-release-notes label
    impact_review: tuple[ReleaseImpact, ...] = ()  # deterministic impact signals for urgency/security review
    duplicate_prs: tuple[int, ...] = ()  # PR numbers the model emitted more than once (extra bullets dropped)
    uncertain: tuple[UncertainNote, ...] = ()  # low-confidence notes the model flagged, for the PR body
    unresolved: tuple[UnresolvedCommit, ...] = ()  # range commits that resolved to no PR (shipped un-noted)
    unresolved_backports: tuple[UnresolvedBackport, ...] = ()  # credited backports whose original source was unreachable
    unresolved_prs: tuple[UnresolvedPR, ...] = ()  # range commits whose resolved PR number could not be fetched (shipped un-noted)
    unresolved_cherry_picks: tuple[UnresolvedCherryPick, ...] = ()  # notes credited past an unresolvable -x trailer (origin unconfirmed)
    collided: tuple[CollidedCommit, ...] = ()  # distinct commits dropped by a reused subject (#N) (shipped un-noted)
    reverted: tuple[RevertedSourcePR, ...] = ()  # Revert-titled sweep manifest rows (the range ships the revert, not the change)
    pr_authors: tuple[str, ...] = ()  # GitHub logins of all resolved PR authors in the range (for contributor list)


def regenerate_unreleased(
    repo: Any, clone_dir: str, *, head_ref: str, tag_glob: str | None,
    base_ref: str | None = None, release_branch: str | None = None,
) -> RegenResult:
    """Discover the range, triage PRs without ``release-notes``, and generate bullets.

    PRs labelled ``release-notes`` are included directly and PRs labelled
    ``no-release-notes`` are hard-excluded (never triaged, never noted); the rest are
    run through AI triage (see :mod:`scripts.release_notes.triage`) and the ones
    judged user-facing join generation. ``base_ref`` overrides tag-based baseline
    resolution. ``release_branch`` binds trusted sweep manifests to the active M.m
    line. Returns a RegenResult whose ``grouped`` map the cut caller renders into a
    dated section, plus the AI include/exclude decisions for the PR body.
    """
    discovery = discover_mod.discover(
        repo, clone_dir, head_ref, tag_glob=tag_glob, base_ref=base_ref,
        release_branch=release_branch,
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
            reverted=discovery.reverted,
            pr_authors=(),
        )

    # Labelled PRs are included directly; no-release-notes PRs are hard-excluded
    # before triage; the rest are candidates AI triage judges.
    labelled, candidates, hard_excluded = classify(discovery.prs)
    impact_review = tuple(
        ReleaseImpact(
            number=pr.number,
            title=pr.title,
            url=pr.url,
            reason=reason,
        )
        for pr in discovery.prs
        if (reason := triage_mod.release_impact_reason(pr)) is not None
    )
    # Sweep-expanded source PRs can share one combined range commit, while
    # triaged-in PRs appear in both AI stages. One collector omits ambiguous
    # shared patches and caches attributable commits across both stages.
    diff_collector = PRDiffCollector(clone_dir, discovery.prs)
    triage_result = triage_mod.triage(
        candidates,
        repo_dir=clone_dir,
        base_ref=discovery.base_tag,
        diff_collector=diff_collector,
    )

    # Join each verdict back to its PR facts for the body, and collect the PRs the
    # model judged user-facing so they flow into generation with the labelled ones.
    by_number = {pr.number: pr for pr in candidates}
    included_decisions = _triaged_prs(triage_result.included, by_number)
    ai_included = tuple(pr for pr in included_decisions if not pr.guardrail)
    guardrail_included = tuple(pr for pr in included_decisions if pr.guardrail)
    ai_excluded = _triaged_prs(triage_result.excluded, by_number)
    # PRs the author opted out of via no-release-notes, surfaced (not silently
    # dropped) so a maintainer can catch a mislabelled user-facing change.
    label_excluded = tuple(
        TriagedPR(
            number=pr.number, title=pr.title, author=pr.author, url=pr.url,
            included=False, reason="labelled `no-release-notes`",
        )
        for pr in hard_excluded
    )
    triaged_in = [
        by_number[d.pr_number] for d in triage_result.included if d.pr_number in by_number
    ]
    # Candidates AI triage could not decide fall back to human triage.
    human_triage = tuple(
        by_number[n] for n in triage_result.undecided if n in by_number
    )

    include = labelled + triaged_in
    logger.info(
        "%d labelled, %d candidates, %d no-release-notes excluded -> "
        "%d AI-included, %d AI-excluded, %d undecided",
        len(labelled), len(candidates), len(label_excluded), len(ai_included),
        len(ai_excluded), len(human_triage),
    )

    gen = generate_mod.generate(
        include,
        repo_dir=clone_dir,
        categories=release_format_mod.CATEGORIES,
        diff_collector=diff_collector,
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

    all_pr_authors = tuple(dict.fromkeys(
        pr.author for pr in discovery.prs
        if pr.author and not pr.author.endswith("[bot]")
    ))

    return RegenResult(
        base_tag=discovery.base_tag, grouped=grouped,
        included=len(include), bullet_count=promoted_count, skipped=skipped,
        triage=human_triage, had_prs=True,
        ai_included=ai_included, guardrail_included=guardrail_included,
        ai_excluded=ai_excluded, label_excluded=label_excluded,
        impact_review=impact_review,
        duplicate_prs=duplicate_prs, uncertain=uncertain,
        unresolved=discovery.unresolved,
        unresolved_backports=discovery.unresolved_backports,
        unresolved_prs=discovery.unresolved_prs,
        unresolved_cherry_picks=discovery.unresolved_cherry_picks,
        collided=discovery.collided,
        reverted=discovery.reverted,
        pr_authors=all_pr_authors,
    )


def _triaged_prs(decisions, by_number):
    """Join AI triage decisions back to their PR facts, dropping unknown numbers.

    Preserves decision order and skips any verdict whose PR is not in *by_number*
    (an unknown number the triage parser should already have filtered out).
    """
    out = []
    for d in decisions:
        pr = by_number.get(d.pr_number)
        if pr is None:
            continue
        out.append(TriagedPR(
            number=pr.number, title=pr.title, author=pr.author, url=pr.url,
            included=d.included, reason=d.reason, uncertain=d.uncertain,
            guardrail=d.guardrail,
        ))
    return tuple(out)


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
