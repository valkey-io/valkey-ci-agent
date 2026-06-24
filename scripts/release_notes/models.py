"""Typed data model for the release-notes generation pipeline.

The pipeline is a chain of small, explicit handoffs:

    discover -> DiscoveryResult     (git/GitHub: which PRs merged since the last tag)
    classify -> MergedPR.disposition (code: include / exclude / triage, from labels)
    generate -> GenerationResult    (AI: one categorized bullet per included PR)
    render   -> updated 00-RELEASENOTES text (code: canonical format, authoritative)
    publish  -> PR url              (code: branch + PR on valkey)

AI populates only the judgment fields (``CategorizedBullet`` category and text);
code populates every factual field (PR number, author, labels, the trailing
``(#N)``, the ``by @handle`` attribution). The split is deliberate: the model
decides what to say and where it goes, never the dedup identity or the canonical
hand-written bullet layout (valkey ships no release tooling; its CI gate is
label-only and never parses this file).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PRDisposition(str, Enum):
    """What the labelling says to do with a discovered PR."""

    INCLUDE = "include"   # has 'release-notes', not 'no-release-notes' (other labels ignored)
    EXCLUDE = "exclude"   # has 'no-release-notes', not 'release-notes' (other labels ignored)
    TRIAGE = "triage"     # neither gate label, or both -> a human must decide


@dataclass(frozen=True)
class MergedPR:
    """One PR discovered in the release range. Entirely factual.

    ``author`` is a bare login (no leading ``@``); it may be ``""`` when GitHub
    returns no user (a deleted/ghost account), which render must tolerate.
    ``body`` is the PR description, cleaned and truncated at discovery time (see
    :func:`discover._clean_pr_body`); it is the model's richest signal for what a
    change means to a user, and is ``""`` when the PR has no description. It is
    untrusted text, so the generate prompt marks it as data, never instructions.
    ``merge_commit_sha`` defaults to ``""`` (used by test constructors); the
    discovery pipeline always fills it with ``pull.merge_commit_sha`` or the
    range commit's SHA (see :func:`discover.hydrate_prs`), so it is non-empty
    for a PR built there.
    """

    number: int
    title: str
    author: str
    url: str
    body: str = ""
    labels: tuple[str, ...] = ()
    merge_commit_sha: str = ""
    disposition: PRDisposition = PRDisposition.TRIAGE


@dataclass(frozen=True)
class CategorizedBullet:
    """One note line's content. ``category`` must be a canonical category.

    ``text`` is the human-readable description ONLY: it must not contain the
    ``(#N)`` reference or the ``by @handle`` attribution, which render appends
    in the fixed positions of valkey's hand-written release-note convention.

    ``uncertain`` is set when the model was not confident about this note (the
    category, or whether the change is user-facing at all). The bullet still
    renders normally; ``uncertain_reason`` is a short human-readable explanation
    surfaced in the PR body so a maintainer can confirm or fix it before merging.
    """

    pr_number: int
    author: str
    category: str
    text: str
    uncertain: bool = False
    uncertain_reason: str = ""


@dataclass(frozen=True)
class UncertainNote:
    """A note the model flagged as low-confidence, for the PR-body warning.

    Carried separately from the rendered bullet so the warning can name the PR,
    the category the model landed on, and why it was unsure, without re-parsing
    the rendered line.
    """

    pr_number: int
    category: str
    reason: str


@dataclass(frozen=True)
class GenerationResult:
    """The AI's output for the whole range. Pure judgment."""

    bullets: tuple[CategorizedBullet, ...] = ()
    skipped: tuple[int, ...] = ()   # PR numbers the model declined to summarize


@dataclass(frozen=True)
class DiscoveryResult:
    """Factual summary of the release range, from discover.py.

    ``prs`` is deduplicated to one entry per originating PR number, so a change
    cherry-picked across the range collapses to a single PR.
    """

    base_tag: str
    head_ref: str
    prs: tuple[MergedPR, ...] = field(default_factory=tuple)
