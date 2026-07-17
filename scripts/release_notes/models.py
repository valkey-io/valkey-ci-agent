"""Typed data model for the release-notes generation pipeline.

    discover -> DiscoveryResult     (git/GitHub: which PRs merged since the last tag)
    classify -> MergedPR.disposition (code: include / candidate, from the label)
    triage   -> TriageResult        (AI: include/exclude each label-less candidate)
    generate -> GenerationResult    (AI: one categorized bullet per included PR)
    render   -> updated 00-RELEASENOTES text (code: canonical format, authoritative)
    publish  -> PR url              (code: branch + PR on valkey)

AI populates judgment fields (triage include/exclude, category, text); code
populates factual fields (PR number, author, labels, the trailing ``(#N)``, the
``by @handle`` attribution).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PRDisposition(str, Enum):
    """Label-derived disposition for a discovered PR.

    Only ``release-notes`` hard-includes. Everything else is a CANDIDATE that AI
    triage judges (see :mod:`scripts.release_notes.triage`), so a change whose
    author forgot the label is caught rather than silently dropped.
    """

    INCLUDE = "include"      # has 'release-notes' (other labels ignored)
    CANDIDATE = "candidate"  # no 'release-notes' label -> AI triage decides


@dataclass(frozen=True)
class MergedPR:
    """One PR discovered in the release range (factual fields only).

    For backports resolved to their original, number/title/author/body/labels come
    from the original PR while merge_commit_sha stays the range (backport) commit.
    """

    number: int
    title: str
    author: str
    url: str
    body: str = ""
    labels: tuple[str, ...] = ()
    merge_commit_sha: str = ""
    disposition: PRDisposition = PRDisposition.CANDIDATE


@dataclass(frozen=True)
class CategorizedBullet:
    """One note line: category + description text (no ``(#N)`` or attribution).

    ``uncertain`` flags low-confidence notes for maintainer review in the PR body.
    """

    pr_number: int
    author: str
    category: str
    text: str
    uncertain: bool = False
    uncertain_reason: str = ""


@dataclass(frozen=True)
class UncertainNote:
    """A low-confidence note surfaced in the PR body for maintainer review."""

    pr_number: int
    category: str
    reason: str


@dataclass(frozen=True)
class GenerationResult:
    """AI output for the whole range: categorized bullets and skipped PRs."""

    bullets: tuple[CategorizedBullet, ...] = ()
    skipped: tuple[int, ...] = ()   # PR numbers the model declined to summarize


@dataclass(frozen=True)
class TriageDecision:
    """AI verdict on one label-less candidate PR: keep it in the notes or drop it.

    ``included`` True means the change is user-facing and should be noted despite
    the missing ``release-notes`` label; False means it is internal-only. ``reason``
    is a short human-readable justification surfaced in the release PR body so a
    maintainer can audit and override either call. ``uncertain`` flags a
    low-confidence verdict (either direction) for closer review.
    """

    pr_number: int
    included: bool
    reason: str = ""
    uncertain: bool = False


@dataclass(frozen=True)
class TriageResult:
    """AI triage output for the label-less candidates in a range.

    ``included`` and ``excluded`` partition every candidate the model returned a
    verdict for; ``undecided`` holds candidates the model gave no verdict for (a
    parse failure or a dropped entry), which are surfaced for human triage rather
    than silently included or dropped.
    """

    included: tuple[TriageDecision, ...] = ()
    excluded: tuple[TriageDecision, ...] = ()
    undecided: tuple[int, ...] = ()


@dataclass(frozen=True)
class TriagedPR:
    """A label-less PR paired with its AI triage verdict, for the release PR body.

    Joins the factual fields the body table needs (number/title/author/url) with
    the model's ``included`` call, ``reason``, and ``uncertain`` flag, so a
    maintainer can audit (and override) each AI include/exclude decision.
    """

    number: int
    title: str
    author: str
    url: str
    included: bool
    reason: str = ""
    uncertain: bool = False


@dataclass(frozen=True)
class UnresolvedCommit:
    """A range commit that could not be resolved to any PR number.

    Surfaced for maintainer triage so the change is not shipped un-noted.
    """

    sha: str
    subject: str


@dataclass(frozen=True)
class CollidedCommit:
    """A range commit dropped because another commit already claimed its PR number.

    Only subject-tier collisions between non-matching subjects are recorded.
    Carries the dropped sha/subject and the kept_sha for maintainer comparison.
    """

    number: int
    sha: str
    subject: str
    kept_sha: str = ""


@dataclass(frozen=True)
class UnresolvedBackport:
    """A backport PR credited in place of its unreachable original source.

    The note renders normally but credits the backport PR, not the original
    author. Flagged so a maintainer can verify attribution.
    """

    number: int
    title: str
    url: str = ""


@dataclass(frozen=True)
class UnresolvedPR:
    """A range commit whose resolved PR number could not be fetched from the API.

    Unlike UnresolvedCommit, a number was found but the PR itself is gone
    (deleted, moved, or from a different repo). Surfaced for maintainer triage.
    """

    number: int
    sha: str


@dataclass(frozen=True)
class UnresolvedCherryPick:
    """A note credited past an unresolvable cherry-pick -x trailer.

    The source SHAs did not resolve to a PR, so credit was assigned from a
    lower-confidence signal. Flagged so a maintainer can verify attribution.
    """

    number: int
    sha: str
    source_shas: tuple[str, ...] = ()
    subject: str = ""


@dataclass(frozen=True)
class DiscoveryResult:
    """Factual summary of the release range, from discover.py.

    ``prs`` is deduplicated to one entry per originating PR number. The various
    unresolved/collided tuples carry triage items for maintainer review.
    """

    base_tag: str
    head_ref: str
    prs: tuple[MergedPR, ...] = field(default_factory=tuple)
    unresolved: tuple[UnresolvedCommit, ...] = field(default_factory=tuple)
    unresolved_backports: tuple[UnresolvedBackport, ...] = field(default_factory=tuple)
    unresolved_prs: tuple[UnresolvedPR, ...] = field(default_factory=tuple)
    unresolved_cherry_picks: tuple[UnresolvedCherryPick, ...] = field(default_factory=tuple)
    collided: tuple[CollidedCommit, ...] = field(default_factory=tuple)
