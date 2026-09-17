"""Carry a note's already-published wording across a repository's release lines.

A backported change is cut once per release line, and each cut is an independent
AI run that never sees what the other lines published. The same source PR
therefore gets freshly-invented prose on every line, so ``9.0``, ``8.1``, ``8.0``
and ``7.2`` can describe one commit four different ways.

This module removes the invention. Every rendered bullet ends with the trailing
``(#N)`` of the PR it credits, and a backport is resolved back to its *original*
source PR before that reference is written (see
:mod:`scripts.release_notes.discover`), so ``(#N)`` is a branch-invariant,
code-produced join key. Reading the sibling release lines' changelogs out of the
clone gives ``{PR number -> already-published wording}``; a cut that generates a
bullet for a PR another line already published substitutes that published text
verbatim, so the wording is identical by construction rather than by luck.

Only the *text* is carried. The category stays this cut's own decision (a change
can legitimately be a "Bug Fix" on a patch line and a "Behavior Change" on the
line that introduced it), and the ``by @handle`` attribution and ``(#N)`` are
re-rendered from this cut's factual fields, never copied.

Everything here is local and read-only: the cut already clones the repository and
fetches every branch, so no additional network call is made. A line that cannot
be read degrades to "no prior wording for that line" and is reported.

Non-goals, deliberately:

* Unmerged sibling prep branches are not read. Only what a line has actually
  published is authoritative; a concurrent cut's draft is not.
* Backport *adaptation* is not fully detected. When a backport changes what the
  change does on this line, the published sibling wording can be wrong, so the
  reviewer is shown every carry and can reject it. Two deterministic signals do
  refuse the carry outright and hold the release PR: the lines crediting
  different handles, and the two wordings naming different environment
  boundaries (see :func:`align_wording`).

This is not :func:`scripts.release_notes.triage.triage`'s dead ``already_noted``
parameter, which would have *excluded* a PR another line noted. Excluding is
wrong: a backported change genuinely belongs in each line's notes. The fix is to
make the entries agree, not to drop them.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, replace
from typing import Callable, Collection, Dict, List, Mapping, Optional, Sequence, Tuple

from scripts.common.proc import git_output
from scripts.release_notes import release_format as rn
from scripts.release_notes import render as render_mod
from scripts.release_notes.models import (
    AlignedNote,
    CategorizedBullet,
    DeclinedAlignment,
    MergedPR,
)

logger = logging.getLogger(__name__)

# A release line is named exactly "M.m"; the remote refs of a full clone carry one
# such branch per minor version.
_RELEASE_LINE_RE = re.compile(r"^(\d+)\.(\d+)$")

# Canonical category heading render_version_section emits: exactly h3.
_CATEGORY_HEADING_RE = re.compile(r"^###\s+(\S.*?)\s*$")

# Any other markdown heading, or a setext underline (a run of "=" or "-" under a
# heading line). Both end the canonical "### <category>" scope, which is what
# keeps pre-canonical history out of the index (see parse_published_notes).
_OTHER_HEADING_RE = re.compile(r"^#{1,6}(?:\s|$)")
_SETEXT_UNDERLINE_RE = re.compile(r"^\s*(?:={3,}|-{3,})\s*$")

# The mechanical suffix render.format_bullet appends after the note prose. The
# handle charset matches what format_bullet can emit (it strips everything
# outside ``[\w-]``), so a published attribution round-trips exactly.
_TRAILING_AUTHOR_RE = re.compile(r"\s+by\s+@([\w-]+)\s*$", re.IGNORECASE)

# Reused prose must read as a note, not as a fragment or a mangled paragraph.
# Anything about its *shape* is settled by the render round-trip in
# :func:`renders_back`, not by pattern-matching the prose.
_MIN_TEXT_WORDS = 2
_MAX_TEXT_CHARS = 500

# Terminal punctuation render.format_bullet drops before appending the credit.
# Recovered text is normalized the same way so it is already at the formatter's
# fixed point (see renders_back).
_TERMINAL_PUNCTUATION = " .,:;!?"

# Polarity check: a published note that asserts a revert while this cut's note
# does not (or the reverse) describes a different change, not different wording.
_REVERT_RE = re.compile(r"\brevert(?:s|ed|ing)?\b", re.IGNORECASE)

# Never read an unbounded blob into memory. A changelog an order of magnitude
# past valkey's largest is a sign the ref is not what we think it is.
MAX_NOTES_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class PriorNote:
    """One note a sibling release line has already published.

    ``text`` is the note prose with the bullet marker, the ``by @handle``
    attribution, and the trailing ``(#N)`` removed, i.e. exactly the value
    :func:`scripts.release_notes.render.format_bullet` would take as input to
    reproduce the published line. ``author`` is the handle that line credited
    ("" when it published none), kept only to detect an attribution
    disagreement. ``order`` is the deterministic precedence key (see
    :func:`build_index`).
    """

    pr_number: int
    text: str
    author: str
    release_line: str        # the sibling line's "M.m"
    release_heading: str     # the dated heading it was published under
    category: str            # the "### <category>" it was published under
    order: Tuple[int, ...]


@dataclass(frozen=True)
class PriorNoteIndex:
    """Published wording from every sibling release line that could be read.

    ``notes`` maps a source PR number to its candidate published notes, best
    precedence first. ``lines_read`` and ``lines_failed`` are reported in the
    release PR body so a reviewer can tell "no sibling published this note" apart
    from "the sibling line could not be read".
    """

    notes: Dict[int, Tuple[PriorNote, ...]]
    lines_read: Tuple[str, ...] = ()
    lines_failed: Tuple[Tuple[str, str], ...] = ()   # (release line, reason)

    def candidates(self, pr_number: int) -> Tuple[PriorNote, ...]:
        """Published notes for *pr_number*, best precedence first."""
        return self.notes.get(pr_number, ())


@dataclass(frozen=True)
class AlignResult:
    """Bullets after alignment, plus every carry and decline for the PR body."""

    bullets: Tuple[CategorizedBullet, ...] = ()
    aligned: Tuple[AlignedNote, ...] = ()
    declined: Tuple[DeclinedAlignment, ...] = ()
    already_consistent: Tuple[int, ...] = ()


def release_line_key(name: str) -> "Optional[Tuple[int, int]]":
    """Parse a release-line branch name ``"M.m"`` into ``(major, minor)``."""
    match = _RELEASE_LINE_RE.match(name.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def is_release_line(name: str) -> bool:
    """Whether *name* is a release-line branch name (exactly ``M.m``)."""
    return release_line_key(name) is not None


def release_line_refs(
    repo_dir: str, *, exclude: Optional[str] = None
) -> "Tuple[Tuple[str, str], ...]":
    """Return ``(ref, line)`` for each sibling release line in the clone.

    Reads the remote-tracking refs of the cut's clone (``git clone`` without
    ``--single-branch`` fetches every branch), keeps the ones named exactly
    ``M.m``, drops *exclude* (the line being cut, whose own notes the cut already
    dedups against), and orders them by descending version.

    Highest line first is a deterministic *canonical* choice, not a claim about
    publication order: valkey develops on the newest line and backports downward,
    so its wording is the one the others are copies of. Ordering does not depend
    on which line happens to be cut first, so two cuts of different lines resolve
    to the same wording. Returns ``()`` when the refs cannot be listed.
    """
    try:
        out = git_output(repo_dir, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Could not list release-line refs: %s", exc)
        return ()
    found: List[Tuple[Tuple[int, int], str, str]] = []
    for raw in out.splitlines():
        ref = raw.strip()
        if not ref.startswith("origin/"):
            continue
        line = ref[len("origin/"):]
        if exclude is not None and line == exclude:
            continue
        key = release_line_key(line)
        if key is None:
            continue
        found.append((key, ref, line))
    # Descending version: the newest line's wording wins (see docstring).
    found.sort(key=lambda item: item[0], reverse=True)
    return tuple((ref, line) for _key, ref, line in found)


def read_line_notes(
    repo_dir: str, ref: str, notes_file: str
) -> "Tuple[Optional[str], str]":
    """Read *notes_file* at *ref*, returning ``(text, reason)``.

    ``text`` is ``None`` when the file could not be read, with *reason* naming
    why (the ref predates the changelog, the blob is implausibly large, git
    failed). Never raises: an unreadable sibling line must degrade to "no prior
    wording", not fail the release cut.
    """
    spec = f"{ref}:{notes_file}"
    try:
        size_out = git_output(repo_dir, "cat-file", "-s", spec)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        return None, _git_reason(exc, f"{notes_file} not readable at {ref}")
    try:
        size = int(size_out.strip())
    except ValueError:
        size = -1
    if size > MAX_NOTES_BYTES:
        return None, f"{notes_file} at {ref} is {size} bytes (over the {MAX_NOTES_BYTES}-byte cap)"
    try:
        return git_output(repo_dir, "show", spec), ""
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        return None, _git_reason(exc, f"{notes_file} not readable at {ref}")


def _git_reason(exc: BaseException, fallback: str) -> str:
    """One short line describing a failed git read, for the PR body."""
    stderr = getattr(exc, "stderr", "") or ""
    first = next((line.strip() for line in str(stderr).splitlines() if line.strip()), "")
    return first or fallback


def parse_published_notes(
    text: str, *, display_name: str, release_line: str
) -> "Tuple[PriorNote, ...]":
    """Extract the published notes from one release line's changelog.

    A bullet is indexed only when it is inside a recognized
    ``<display_name> M.m.p`` dated section **and** under a ``### <category>``
    heading that section emitted. Requiring both is what keeps pre-canonical
    history out of the index without a fragile foreign-format detector.
    :func:`scripts.release_notes.release_format.render_version_section` is the
    only thing that writes ``### <category>``; every hand-authored section in
    valkey's changelogs — including the Redis-derived tail of ``7.2`` — writes its
    categories as setext headings instead (``Bug fixes`` over ``=========``), and
    those bullets carry no ``by @handle`` and sometimes credit several PRs at once
    (``(#47, #232)``). They sit under no ``###`` heading, so they are never
    reached; any other heading, and any setext underline, closes the current
    category scope.

    The effect is that only wording an agent cut published is ever reused, which
    is exactly the wording that diverges between lines. Hand-authored notes stay
    each line's own.

    Reserved sections are skipped: **Security Fixes** entries are hand-authored
    per line and **Contributors** is a generated footer, so neither is note prose
    to carry. A bullet crediting more than one PR is skipped too, because its
    prose describes the union of those PRs and cannot be attributed to one.
    """
    reserved = {name.casefold() for name in rn.RESERVED_SECTIONS}
    notes: List[PriorNote] = []
    line_key = release_line_key(release_line) or (0, 0)
    heading = ""
    heading_key: Tuple[int, ...] = ()
    category = ""
    for offset, raw in enumerate(text.splitlines()):
        dated = rn.dated_release_key(raw, display_name)
        if dated is not None:
            heading = _heading_label(raw)
            heading_key = tuple(dated)
            category = ""
            continue
        match = _CATEGORY_HEADING_RE.match(raw)
        if match is not None:
            # Only inside a recognized dated section does a category open a scope.
            category = match.group(1) if heading else ""
            continue
        if _OTHER_HEADING_RE.match(raw) or _SETEXT_UNDERLINE_RE.match(raw):
            category = ""
            continue
        if not heading or not category or category.casefold() in reserved:
            continue
        if not rn.BULLET_LINE_RE.match(raw):
            continue
        note = _parse_bullet(
            raw,
            release_line=release_line,
            release_heading=heading,
            category=category,
            order=(-line_key[0], -line_key[1], *heading_key, offset),
        )
        if note is not None:
            notes.append(note)
    return tuple(notes)


_RELEASED_TAIL_RE = re.compile(r"\s+-\s+Released\b.*$", re.IGNORECASE)


def _heading_label(raw: str) -> str:
    """The version part of a dated heading, without the ``- Released <date>`` tail."""
    return _RELEASED_TAIL_RE.sub("", raw.strip()).strip()


def split_bullet(raw: str) -> "Optional[Tuple[str, str, int]]":
    """Reverse :func:`scripts.release_notes.render.format_bullet` on one line.

    Returns ``(text, handle, pr_number)`` — the note prose with the marker, the
    ``by @handle`` credit and the trailing ``(#N)`` removed — or ``None`` when the
    line credits no PR or credits more than one (a multi-PR bullet's prose
    describes the union of those PRs and belongs to no single one).

    Terminal punctuation is trimmed exactly as ``format_bullet`` trims it, so the
    recovered text is already what the formatter would settle on.
    """
    refs = rn.trailing_pr_numbers(raw)
    if len(refs) != 1:
        return None
    pr_number = next(iter(refs))
    body = rn.TRAILING_PR_GROUP_RE.sub("", raw.strip()).rstrip()
    body = re.sub(r"^\s*[*-]\s+", "", body)
    author_match = _TRAILING_AUTHOR_RE.search(body)
    handle = ""
    if author_match is not None:
        handle = author_match.group(1)
        body = body[: author_match.start()].rstrip()
    text = " ".join(body.split())
    trimmed = text.rstrip(_TERMINAL_PUNCTUATION)
    return (trimmed or text), handle, pr_number


def _parse_bullet(
    raw: str, *, release_line: str, release_heading: str, category: str,
    order: Tuple[int, ...],
) -> "Optional[PriorNote]":
    """Build the :class:`PriorNote` one published bullet line represents.

    Returns ``None`` when the line does not credit exactly one PR, or when the
    prose it leaves behind cannot be reused as a note (see :func:`usable_text`).
    """
    parsed = split_bullet(raw)
    if parsed is None:
        return None
    text, author, pr_number = parsed
    if not usable_text(text, author=author, pr_number=pr_number):
        return None
    return PriorNote(
        pr_number=pr_number, text=text, author=author,
        release_line=release_line, release_heading=release_heading,
        category=category, order=order,
    )


def renders_back(text: str, *, author: str, pr_number: int) -> bool:
    """Whether rendering *text* and reading it back returns *text* unchanged.

    The one check that matters, and the reason no pattern-matching for "unclean"
    prose is needed: run the real
    :func:`scripts.release_notes.render.format_bullet` and reverse-parse its
    output. If the recovered text, handle, and PR number all come back
    unchanged, substituting *text* renders a well-formed bullet with exactly one
    credit and exactly one ``(#N)``.

    This distinguishes prose that merely *mentions* a reference — "Revert the
    change from (#1200) that broke replicas", "keys deleted by @-prefixed
    clients", both legitimate notes a regex guard would refuse — from prose whose
    trailing reference the formatter would strip or double. Only the latter
    breaks the round trip.
    """
    rendered = render_mod.format_bullet(CategorizedBullet(
        pr_number=pr_number, author=author, category="", text=text,
    ))
    parsed = split_bullet(rendered)
    if parsed is None:
        return False
    return parsed == (text, render_mod.safe_handle(author), pr_number)


def usable_text(text: str, *, author: str = "", pr_number: int = 0) -> bool:
    """Whether *text* can be substituted as the wording of note *pr_number*.

    Rejects prose with no word character, prose too short to be a note, and prose
    long enough to be a mangled paragraph rather than a bullet. Everything about
    the prose's *shape* is decided by the render round trip (see
    :func:`renders_back`).
    """
    if not text or len(text) > _MAX_TEXT_CHARS:
        return False
    if not re.search(r"\w", text):
        return False
    if len(text.split()) < _MIN_TEXT_WORDS:
        return False
    return renders_back(text, author=author, pr_number=pr_number or 1)


def build_index(
    repo_dir: str,
    *,
    notes_file: str,
    display_name: str,
    current_line: Optional[str] = None,
    wanted: Optional[Collection[int]] = None,
) -> PriorNoteIndex:
    """Index the published wording of every sibling release line in the clone.

    *current_line* (the ``M.m`` being cut) is excluded: its own credited PRs are
    already handled by the cut's already-credited dedup. *wanted* bounds the
    index to the PR numbers this cut actually needs.

    Candidates for one PR are ordered by ``(newest line, oldest section in that
    line, rc before GA, file order)``. Newest line first is the canonical-origin
    choice (see :func:`release_line_refs`); within a line the earliest section to
    publish the note is its original wording there. The order is a total order
    over the candidates, so the chosen wording never depends on scan order.
    """
    notes: Dict[int, List[PriorNote]] = {}
    read: List[str] = []
    failed: List[Tuple[str, str]] = []
    for ref, line in release_line_refs(repo_dir, exclude=current_line):
        text, reason = read_line_notes(repo_dir, ref, notes_file)
        if text is None:
            logger.info("No prior release notes from %s: %s", line, reason)
            failed.append((line, reason))
            continue
        read.append(line)
        for note in parse_published_notes(
            text, display_name=display_name, release_line=line
        ):
            if wanted is not None and note.pr_number not in wanted:
                continue
            notes.setdefault(note.pr_number, []).append(note)
    ordered = {
        pr_number: tuple(sorted(items, key=lambda note: note.order))
        for pr_number, items in notes.items()
    }
    logger.info(
        "Prior-wording index: %d PR(s) from %d release line(s) (%d unreadable)",
        len(ordered), len(read), len(failed),
    )
    return PriorNoteIndex(
        notes=ordered, lines_read=tuple(read), lines_failed=tuple(failed),
    )


def align_wording(
    bullets: Sequence[CategorizedBullet],
    index: PriorNoteIndex,
    *,
    prs_by_number: Mapping[int, MergedPR],
    scope_tokens: Optional[Callable[[str], "frozenset[str]"]] = None,
    unconfirmed_credit_prs: Collection[int] = (),
    cve_prs: Collection[int] = (),
) -> AlignResult:
    """Substitute each bullet's text with the wording a sibling line published.

    For every bullet whose PR another line already noted, the first *usable*
    candidate in precedence order supplies the text. Only the text moves: the
    category, the credit, the ``(#N)`` and the model's own ``uncertain`` flag all
    stay this cut's. Keeping the flag is deliberate — it records the model's
    judgement about the *change* ("unclear whether this is user-facing"), which
    the wording substitution does not answer and must not erase.

    Declines are deliberate and reported rather than silently ignored:

    * **Attribution disagreement** — the publishing line credited a different
      handle than this cut resolved. The two lines disagree about whose change
      this is, so they may not be describing the same change at all.
    * **Material scope disagreement** — the published wording and this cut's
      wording name different environment boundaries (*scope_tokens*), which is
      how an adapted backport shows up. See
      :func:`scripts.release_notes.generate.material_scope_tokens` for why no
      other check can catch it.
    * **Named CVE** — a security-relevant note is worded by a human per line;
      code does not copy that wording between lines.
    * **Unconfirmed credit** — this cut could not confirm the PR its note
      credits, and that credit is the only thing joining the two notes.
    * **Revert polarity** — one text asserts a revert and the other does not.
    * **Unusable candidate** — nothing published for that PR survived
      :func:`usable_text` (a hand-edited or pre-canonical bullet).

    The first two hold the release PR (``needs_review``); the rest are reported
    for the record. The line was going to publish its own generated wording
    anyway, and a decline that merely leaves that wording in place is not a new
    problem for a reviewer to resolve.
    """
    unconfirmed = set(unconfirmed_credit_prs)
    cves = set(cve_prs)
    out: List[CategorizedBullet] = []
    aligned: List[AlignedNote] = []
    declined: List[DeclinedAlignment] = []
    consistent: List[int] = []
    for bullet in bullets:
        candidates = index.candidates(bullet.pr_number)
        if not candidates:
            out.append(bullet)
            continue
        # Stage 1: the published line must round-trip through the formatter with
        # the handle *it* credited, i.e. the parse recovered the whole note.
        chosen = next(
            (
                note for note in candidates
                if usable_text(note.text, author=note.author, pr_number=note.pr_number)
            ),
            None,
        )
        if chosen is None:
            first = candidates[0]
            declined.append(DeclinedAlignment(
                pr_number=bullet.pr_number, release_line=first.release_line,
                release_heading=first.release_heading,
                reason="the published wording is not in the canonical bullet form",
            ))
            out.append(bullet)
            continue
        reason, needs_review = _decline_reason(
            bullet, chosen, prs_by_number.get(bullet.pr_number), unconfirmed, cves,
            scope_tokens,
        )
        if reason is not None:
            declined.append(DeclinedAlignment(
                pr_number=bullet.pr_number, release_line=chosen.release_line,
                release_heading=chosen.release_heading, reason=reason,
                needs_review=needs_review,
            ))
            out.append(bullet)
            continue
        if chosen.text == bullet.text:
            consistent.append(bullet.pr_number)
            out.append(bullet)
            continue
        # Stage 2: the same text must also round-trip under *this* cut's credit,
        # which is the credit it will actually render with.
        if not renders_back(
            chosen.text, author=bullet.author, pr_number=bullet.pr_number
        ):
            declined.append(DeclinedAlignment(
                pr_number=bullet.pr_number, release_line=chosen.release_line,
                release_heading=chosen.release_heading,
                reason="carrying the wording would not render as a canonical bullet "
                       "under this cut's credit",
            ))
            out.append(bullet)
            continue
        out.append(replace(bullet, text=chosen.text))
        aligned.append(AlignedNote(
            pr_number=bullet.pr_number, release_line=chosen.release_line,
            release_heading=chosen.release_heading, text=chosen.text,
            generated_text=bullet.text,
        ))
    if aligned:
        logger.info(
            "Carried published wording for %d note(s) from prior release lines",
            len(aligned),
        )
    return AlignResult(
        bullets=tuple(out), aligned=tuple(aligned), declined=tuple(declined),
        already_consistent=tuple(consistent),
    )


def _scope_label(tokens: "Collection[str]") -> str:
    """Render a material-scope token set for the PR body."""
    return ", ".join(sorted(tokens)) or "no environment limit"


def _decline_reason(
    bullet: CategorizedBullet,
    chosen: PriorNote,
    pr: Optional[MergedPR],
    unconfirmed: Collection[int],
    cves: Collection[int],
    scope_tokens: Optional[Callable[[str], "frozenset[str]"]],
) -> "Tuple[Optional[str], bool]":
    """Why *chosen* must not be carried, and whether that must hold the PR.

    Returns ``(None, False)`` when the wording is safe to carry. The two holding
    reasons come first so the strongest one is the one reported.
    """
    # Compare the handles that will actually be *printed*, not the raw logins.
    # chosen.author was recovered from a rendered line, so it is already
    # post-safe_handle; a raw login carrying characters the renderer strips (a
    # bot's "dependabot[bot]" prints as "@dependabotbot") would otherwise never
    # compare equal to its own published credit and would be reported as the two
    # lines disagreeing about the author.
    published = render_mod.safe_handle(chosen.author).casefold()
    local = render_mod.safe_handle(bullet.author or "").casefold()
    if published and local and published != local:
        return (
            f"`{chosen.release_line}` credits @{chosen.author} but this cut "
            f"resolved @{bullet.author}, so the two lines disagree about whose "
            "change this is",
            True,
        )
    if scope_tokens is not None:
        published_scope = scope_tokens(chosen.text)
        local_scope = scope_tokens(bullet.text)
        if published_scope != local_scope:
            return (
                "`{}` limits the change to {} but this cut's note says {}, so the "
                "lines disagree about how far the change reaches".format(
                    chosen.release_line,
                    _scope_label(published_scope),
                    _scope_label(local_scope),
                ),
                True,
            )
    if bullet.pr_number in cves:
        return (
            "the PR names a CVE, so its wording is authored per release line by a human",
            False,
        )
    if bullet.pr_number in unconfirmed:
        return (
            "this cut could not confirm the PR the note credits (unresolved "
            "backport, cherry-pick, or reused PR number), and that credit is the "
            "only thing joining the two notes",
            False,
        )
    # Only the two *wordings* can disagree about polarity. The PR title cannot:
    # it is the original source PR's, so it is byte-identical on every line and
    # says nothing about how this line described the change. It serves only to
    # excuse a disagreement -- when the title itself says the change is a revert,
    # one line spelling that out and the other not is a wording choice, not a
    # claim that the two lines shipped opposite changes.
    if bool(_REVERT_RE.search(chosen.text)) != bool(
        _REVERT_RE.search(bullet.text)
    ) and not _REVERT_RE.search((pr.title if pr is not None else "") or ""):
        return (
            f"`{chosen.release_line}` describes a revert and this cut does not "
            "(or the reverse), so the two notes are not the same change",
            False,
        )
    return None, False
