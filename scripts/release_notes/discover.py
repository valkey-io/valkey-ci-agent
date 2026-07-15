"""Discover the PRs a release line has accrued since its last tag.

Selection is by graph reachability, never by date: we resolve the highest-version
tag reachable from the release-line tip and walk ``tag..head``. A backport
cherry-picked onto the line is a distinct commit with its own date, so a date
window would either miss it or double-count it; a graph range counts it exactly
when it is part of this line's history.

The walk is first-parent (:func:`list_range_commits`): it enumerates each PR's
merge or squash commit, not the work-in-progress commits on a merge-merged PR's
branch. Enumerating those intermediates would mint a phantom note whenever one's
subject ends in an unrelated ``(#N)`` (the subject tier would credit it as a PR in
the range). The exception is a backport sweep's per-source cherry-picks, which
carry each source ``(#N)`` only on the second-parent side; those are spliced back
for sweep merges alone (see :func:`list_range_commits`), so sweep attribution is
preserved without reopening the phantom.

Each commit is resolved to the original PR that introduced the change (the PR
whose author the note should credit, not the backport PR that carried it onto
this line), and the set is deduplicated by that PR number rather than by commit
SHA. Resolution tries the markers that recover the original first (see
:func:`resolve_commit_prs`): the ``## Applied`` table a squash-merged sweep
writes into the commit body, then a ``-x`` cherry-pick trailer, then the
subject's trailing ``(#N)``, and last the GitHub "PRs associated with a commit"
API on the commit's own SHA. The ``-x`` trailer tier is not written by this
repo's *backport* tooling (:mod:`scripts.backport` picks without ``-x``, so the
source subject's ``(#N)`` survives for the subject tier); it recovers a rewritten
``-x`` pick made elsewhere, e.g. a maintainer's hand-applied pick or the ci-fix
port path (:mod:`scripts.ci_fix.push`). When only the backport PR resolves, the
change is still noted and a warning fires so a maintainer can check the original.

A commit that resolves to no PR at all (a hand-applied cherry-pick whose message
was rewritten, an unusual merge) is not dropped: it is returned in
``DiscoveryResult.unresolved`` and surfaced in the cut's PR body, so a shipped
change can never vanish past valkey's label-only gate un-noted.

Known limitation (squash-merged sweeps). A backport *sweep*
(:mod:`scripts.backport.sweep`) batches many source PRs onto one branch as
separate cherry-picks and opens one PR. It records the source PRs it carried in
an ``## Applied`` table written only to the sweep PR body, never into a
commit message. This recovery reads that table from the *commit* body
(:func:`resolve_commit_prs` tier 1), so it recovers the sources only when the
sweep is squash-merged and the merger carried the PR description into the
squash commit body (e.g. GitHub's "default to PR title and description for squash
merges" setting). Merge-commit sweeps are unaffected: each cherry-pick keeps its
source ``(#N)`` in its own commit subject, which tier 3 resolves directly without
the table, and :func:`list_range_commits` puts those second-parent commits in
place of the sweep merge (dropping the container commit, which is never a note; a
non-sweep merge's intermediates stay excluded). But a squash whose commit body
lacks the table degrades safely, not silently: the squash resolves to the *sweep*
PR, whose
own ``## Applied``-less body, multi-``(#N)`` commits, and ``agent/backport/sweep``
branch yield no per-PR source, so the whole sweep collapses to one
``UnresolvedBackport`` flagged in the cut's PR body for a maintainer, rather than
one note per source. Reading the sweep PR body's ``## Applied`` table as a
fallback is deferred until the backport infrastructure settles; discovery does
not read that PR body today.
"""

from __future__ import annotations

import difflib
import fnmatch
import logging
import re
import subprocess
from typing import Any

from github.GithubException import GithubException, UnknownObjectException

from scripts.common.github_client import retry_github_call
from scripts.common.proc import git_output
from scripts.release_notes.backport_refs import (
    applied_source_prs_from_body,
    cherry_pick_source_shas,
    is_backport_title,
    source_pr_from_branch,
    source_title_from_backport_title,
    summary_source_pr_from_body,
    summary_source_title_from_body,
)
from scripts.release_notes.models import (
    CollidedCommit,
    DiscoveryResult,
    MergedPR,
    UnresolvedBackport,
    UnresolvedCherryPick,
    UnresolvedCommit,
    UnresolvedPR,
)

logger = logging.getLogger(__name__)

# One commit record per ``\x1e`` (ASCII record separator), three ``\x1f``-separated
# (unit separator) fields: full SHA, subject, and full body. Both control bytes are
# illegal in a git ref and effectively never appear in a commit message, so they
# survive subjects/bodies that contain tabs, pipes, or NUL-adjacent text. The body
# is needed to recover the *original* PR of a backport: a squash-merged sweep lists
# its source PRs only in an ``## Applied`` table in the body, and a ``-x``
# cherry-pick names its source commit in a body trailer.
_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"
_LOG_FORMAT = f"%H{_FIELD_SEP}%s{_FIELD_SEP}%b{_RECORD_SEP}"

# A merge commit that merged a backport sweep's branch (scripts.backport.sweep
# pushes to ``agent/backport/sweep/<target>``, and GitHub's default merge-commit
# subject names the head branch: ``Merge pull request #N from <owner>/agent/
# backport/sweep/<target>``). Its per-source ``(#N)`` cherry-picks live only on the
# second-parent side, so :func:`list_range_commits` splices them back after
# excluding second parents in general (see there). Kept in step with
# ``scripts.backport.sweep_git.BRANCH_PREFIX``.
_SWEEP_MERGE_RE = re.compile(r"agent/backport/sweep")

# A squash/normal merge names its PR in the *trailing* ``(#N)``. We tolerate a
# run of trailing ``)``, ``.`` or whitespace after the ref (``Fix (#12).`` or a
# stray ``Fix (#12))``) but not a following ``(``, which would be a later
# parenthetical, and un-anchoring past it risks crediting a quoted/reverted ref
# instead of the commit's own PR (``Revert "... (#3544)" (#3756)`` is 3756).
_TRAILING_REF_RE = re.compile(r"\(#(\d+)\)[)\s.]*$")
# GitHub's default merge-commit subject carries the PR number bare, at the
# start: ``Merge pull request #123 from foo/bar``. Anchored to that exact prefix
# so a bare ``#N`` is trusted only in the one context GitHub reserves for it; a
# bare ``#N`` anywhere else stays ambiguous (issue vs. PR) and is left for the
# API fallback rather than guessed at.
_MERGE_COMMIT_RE = re.compile(r"^Merge pull request #(\d+)\b")


def _pr_numbers_from_subjects(subjects: list[str]) -> set[int]:
    """PR numbers from each commit subject (trailing ``(#N)`` or merge-commit prefix).

    Recognizes the two subject shapes GitHub emits: a squash/normal merge whose
    PR is the *trailing* ``(#N)``, and a default merge commit
    (``Merge pull request #N from ...``). The trailing ref is checked first, so
    an earlier ``(#N)`` in the subject stays a reference, not the commit's own PR.
    """
    numbers: set[int] = set()
    for line in subjects:
        m = _TRAILING_REF_RE.search(line)
        if m is None:
            m = _MERGE_COMMIT_RE.search(line)
        if m:
            numbers.add(int(m.group(1)))
    return numbers


# PR bodies feed the generation prompt as extra context. Cap the length so a
# handful of long descriptions can't blow the batch prompt, and strip the noise
# a human reviewer skips anyway: HTML comments (PR-template guidance, checklists
# rendered as comments) and DCO/attribution trailers that render.py re-derives
# from factual fields. The model still gets the substance ("what changed, why").
_MAX_PR_BODY_CHARS = 2000
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# Sign-off / co-author trailers: whole line, case-insensitive, to end of line.
_TRAILER_RE = re.compile(r"(?im)^[ \t]*(?:signed-off-by|co-authored-by):.*$")


def _clean_pr_body(body: Any) -> str:
    """Strip HTML comments and DCO trailers from a PR body, then truncate.

    Returns ``""`` for a missing/empty body, or any non-string value: PyGithub
    types ``body`` as ``str`` but the payload is not guaranteed to match, and a
    mis-parsed attribute must degrade to "no body", never crash the cut (same
    stance as the other ``pull`` fields, coerced with ``or ""``). HTML comments
    (PR-template prose, hidden checklists) and ``Signed-off-by``/
    ``Co-authored-by`` trailers carry no release-note signal; dropping them keeps
    the prompt focused and shorter. Collapses the runs of blank lines the removals
    leave behind, then clips to :data:`_MAX_PR_BODY_CHARS` on a word boundary
    where one is near the cut so a token is not split mid-word.
    """
    if not isinstance(body, str) or not body:
        return ""
    text = _HTML_COMMENT_RE.sub("", body)
    text = _TRAILER_RE.sub("", text)
    # Normalize CRLF and collapse 3+ newlines (left by the removals) to a blank line.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= _MAX_PR_BODY_CHARS:
        return text
    clipped = text[:_MAX_PR_BODY_CHARS]
    # Prefer cutting at the last whitespace in the tail so we don't split a word;
    # only do so if that boundary is reasonably close to the cap (else a body with
    # no late whitespace, e.g. one long token, would be truncated far too short).
    cut = clipped.rfind(" ")
    if cut >= _MAX_PR_BODY_CHARS - 200:
        clipped = clipped[:cut]
    return clipped.rstrip() + "…"


# Matches a release tag, tolerating a leading ``v`` and an ``-rcN`` / ``-ga``
# suffix, so ``9.1.0``, ``v9.1.0``, and ``9.1.0-rc2`` all parse. A dedicated
# regex (rather than release_format.parse_version) is used because a tag that is
# out of range or oddly shaped must be *skipped*, not raise: an unrelated tag in
# the repo can never abort baseline resolution.
_TAG_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:-rc([1-9]\d*)|-ga)?$", re.IGNORECASE)


def _tag_sort_key(tag: str) -> tuple[int, int, int, int, int] | None:
    """Return a version sort key for a release *tag*, or None if it is not one.

    The key orders by ``(major, minor, patch)`` then places a GA above every rc
    of the same ``M.m.p`` (``9.1.0-rc1 < 9.1.0-rc2 < 9.1.0``): a pre-release
    ranks below its release, and higher rc numbers rank above lower ones.
    """
    m = _TAG_RE.fullmatch(tag.strip())
    if not m:
        return None
    major, minor, patch = (int(g) for g in m.group(1, 2, 3))
    rc = m.group(4)
    # rc -> (…, 0, N); GA (bare M.m.p or -ga) -> (…, 1, 0), which sorts above any rc.
    return (major, minor, patch, 0, int(rc)) if rc is not None else (major, minor, patch, 1, 0)


def _tag_matches_glob(tag: str, glob: str) -> bool:
    """Whether *tag* matches *glob*, tolerating a leading ``v`` on the tag.

    The line-scoping globs (e.g. ``8.1.*``, ``9.1.0-rc*``) are bare, but
    :data:`_TAG_RE` accepts a ``v`` prefix, so a ``v8.1.8`` tag must still count
    as a member of the ``8.1.*`` line. Filtering in Python (rather than via
    ``git tag --list <glob>``, which matches the literal name and would silently
    drop ``v``-prefixed tags) keeps the glob consistent with the parser. The tag
    is tested both as given and with a single leading ``v`` stripped, so a glob
    that itself starts with ``v`` still works.
    """
    return fnmatch.fnmatch(tag, glob) or (
        tag[:1] in ("v", "V") and fnmatch.fnmatch(tag[1:], glob)
    )


def resolve_last_tag(repo_dir: str, head_ref: str, *, tag_glob: str | None = None) -> tuple[str, str]:
    """Return ``(tag_name, tag_sha)`` for the highest-version tag reachable from *head_ref*.

    Selection is by version, not graph distance. ``git describe --abbrev=0``
    reports only the single graph-nearest ancestor tag, which after a
    cross-line merge can be a tag from a different release line, silently
    widening or narrowing the range. Instead we list every reachable tag
    (``git tag --merged`` guarantees ancestry), parse each to a version, and pick
    the maximum. ``tag_glob`` (e.g. ``"9.1.*"``) restricts candidates to one line;
    it is applied in Python (via :func:`_tag_matches_glob`), not passed to ``git
    tag --list``, because git matches the literal name and would silently drop a
    ``v``-prefixed tag that :data:`_TAG_RE` otherwise accepts (a fork or a retag).
    A tag that does not parse as ``[v]M.m.p[-rcN|-ga]`` is skipped rather than
    mis-ordered by string comparison. Raises :class:`ValueError` when no parseable
    tag matching the glob is reachable.
    """
    try:
        # Unlike `describe`, `git tag` reports "no match" as empty output (exit 0),
        # not a non-zero exit; a non-zero exit here means head_ref is unresolvable.
        # A TimeoutExpired from a hung git must propagate, not be disguised as a
        # missing baseline that would send the caller to the wrong range.
        out = git_output(repo_dir, "tag", "--merged", head_ref)
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            f"no tag reachable from {head_ref!r}"
            + (f" matching {tag_glob!r}" if tag_glob else "")
        ) from exc
    candidates = [
        (key, name)
        for name in out.split()
        if (tag_glob is None or _tag_matches_glob(name, tag_glob))
        and (key := _tag_sort_key(name)) is not None
    ]
    if not candidates:
        raise ValueError(
            f"no tag reachable from {head_ref!r}"
            + (f" matching {tag_glob!r}" if tag_glob else "")
        )
    _key, tag = max(candidates)
    # Dereference the (possibly annotated) tag to the commit it points at.
    tag_sha = git_output(repo_dir, "rev-list", "-n", "1", tag).strip()
    logger.info("Highest-version tag reachable from %s: %s (%s)", head_ref, tag, tag_sha[:12])
    return tag, tag_sha


def resolve_previous_release_tag(
    repo_dir: str, target_version: str
) -> tuple[str, str] | None:
    """Return ``(tag, sha)`` for the highest release tag strictly below *target_version*.

    This is the baseline resolver for an **rc1** cut, whose true baseline is the
    previous release (there is no rc0 to walk back to). It considers every tag
    in the repo, not only those reachable from the source branch, and returns
    ``None`` when the repo carries no release tag below the target (the very first
    release ever). It differs from :func:`resolve_last_tag` (rc2+/ga) in two ways
    that matter under valkey's fork-at-freeze model:

    * Reachability is not required. valkey tags a release on its own
      ``pre-release-M.m.p`` / ``M.m`` branch, never on ``unstable``, so
      ``git tag --merged unstable`` is empty and a reachable-only resolver would
      raise. The previous release tag is still the correct range base: for tags
      ``prev`` and head ``H``, ``prev..H`` excludes only the commits reachable from
      ``prev`` (their shared freeze point and its ancestors), so branch-only
      release commits on ``prev`` (which ``H`` never merged) drop out and the range
      is exactly this line's new history. ``git tag`` (no ``--merged``) lists all
      tags.
    * Selection is *strictly below the target's ``M.m.p``*, so it finds the real
      previous release even across a skipped minor (target ``9.1.0`` with no ``9.0``
      line resolves to the ``8.2`` line's last tag) and never picks the target's own
      pre-release tags (``9.1.0-rc*`` share the target's ``M.m.p`` and are excluded).

    Selection is by parsed version, never string order or graph distance; a tag
    that is not ``[v]M.m.p[-rcN|-ga]`` is skipped. The chosen tag is dereferenced to
    the commit it points at (annotated tags included).
    """
    target_key = _tag_sort_key(target_version)
    if target_key is None:
        return None
    target_mmp = target_key[:3]
    out = git_output(repo_dir, "tag")
    candidates = [
        (key, name)
        for name in out.split()
        if (key := _tag_sort_key(name)) is not None and key[:3] < target_mmp
    ]
    if not candidates:
        return None
    _key, tag = max(candidates)
    # Dereference the (possibly annotated) tag to the commit it points at.
    tag_sha = git_output(repo_dir, "rev-list", "-n", "1", tag).strip()
    logger.info(
        "Previous-release baseline for %s: %s (%s) [highest release tag below %s]",
        target_version, tag, tag_sha[:12], target_version,
    )
    return tag, tag_sha


def _parse_log_records(out: str) -> list[tuple[str, str, str]]:
    """Parse ``git log --format=_LOG_FORMAT`` output into ``[(sha, subject, body)]``."""
    records: list[tuple[str, str, str]] = []
    # Split on the record separator we appended, not str.splitlines(): a body
    # spans many lines and legitimately contains \n, \v, \f, etc., so a
    # line-based split would tear one commit into bogus records.
    for record in out.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, rest = record.partition(_FIELD_SEP)
        subject, _, body = rest.partition(_FIELD_SEP)
        records.append((sha, subject, body))
    return records


def list_range_commits(repo_dir: str, base: str, head_ref: str) -> list[tuple[str, str, str]]:
    """Return ``[(sha, subject, body), ...]`` for commits in ``base..head_ref``, oldest first.

    ``base`` is the prior tag (or its SHA); the range excludes it and includes the
    line's new history. The body is carried alongside the subject so
    :func:`resolve_commit_prs` can recover the original PR of a backport from an
    ``## Applied`` table or a ``-x`` cherry-pick trailer.

    The walk is ``--first-parent``: it enumerates the mainline (each PR's merge or
    squash commit), not the individual work-in-progress commits on a merge-merged
    PR's branch. A plain ``base..head`` walk surfaces those intermediates, and an
    intermediate whose subject happens to end in an unrelated ``(#N)`` (e.g.
    ``Revert accidental change from (#111)``) is then mis-credited as a PR in the
    range by :func:`resolve_commit_prs`' subject tier, a phantom note beside the
    real PR the merge commit already credits. First-parent drops the intermediate
    so no phantom is minted.

    The one kind of second-parent commit that must survive is a backport sweep's
    per-source cherry-picks: a merge-merged sweep carries each source PR's ``(#N)``
    only on its branch (second-parent) side, and dropping them would collapse the
    whole sweep to its un-attributable merge commit. So for a merge whose subject
    marks it as a sweep (:data:`_SWEEP_MERGE_RE`), the second-parent-only commits
    (``merge^1..merge^2``) take the merge's place, preserving oldest-first order and
    per-source attribution. The sweep merge commit itself is dropped: its sources
    are now enumerated and the sweep container PR is never a note, so keeping it
    would mint a phantom note plus a false-positive unresolved-backport flag (its
    subject resolves to the backport-labeled container, which has no single
    recoverable source). Non-sweep merges keep only their mainline merge commit.
    """
    out = git_output(
        repo_dir, "log", "--reverse", "--first-parent",
        f"--format={_LOG_FORMAT}%P{_RECORD_SEP}", f"{base}..{head_ref}",
    )
    commits: list[tuple[str, str, str]] = []
    # Records alternate: the commit fields, then its parent SHAs on the next
    # record (%P, appended after our record separator). A merge has 2+ parents.
    fields = out.split(_RECORD_SEP)
    i = 0
    while i < len(fields):
        record = fields[i].strip("\n")
        if not record:
            i += 1
            continue
        sha, _, rest = record.partition(_FIELD_SEP)
        subject, _, body = rest.partition(_FIELD_SEP)
        parents = fields[i + 1].strip().split() if i + 1 < len(fields) else []
        i += 2
        if len(parents) >= 2 and _SWEEP_MERGE_RE.search(subject):
            # A sweep merge: splice its per-source cherry-picks (second-parent only)
            # in place of the merge so each source (#N) is discovered. `p1..p2` is
            # the set reachable from the merged branch but not the mainline. The
            # merge commit itself is dropped (`continue`): its sources are now
            # enumerated and the sweep container PR is never a note, so keeping it
            # would mint a phantom note and, since its subject carries the backport
            # label with no single recoverable source, a false-positive
            # unresolved-backport flag for a sweep that resolved cleanly.
            sub = git_output(
                repo_dir, "log", "--reverse", f"--format={_LOG_FORMAT}",
                f"{parents[0]}..{parents[1]}",
            )
            commits.extend(_parse_log_records(sub))
            continue
        commits.append((sha, subject, body))
    logger.info("%d commit(s) in %s..%s (first-parent + sweep sources)", len(commits), base, head_ref)
    return commits


def resolve_commit_prs(
    repo: Any, commits: list[tuple[str, str, str]]
) -> tuple[
    dict[int, str],
    list[UnresolvedCommit],
    dict[int, UnresolvedCherryPick],
    list[CollidedCommit],
]:
    """Map *original* PR number -> commit SHA, plus unresolved/collided commits and cherry-pick suspects.

    The PR of interest is the one that originally introduced a change, not the
    backport PR that carried it onto this line. Resolution tries the tiers in
    the order that recovers the original first and falls back to the on-line PR
    only when no origin marker exists:

    1. ``## Applied`` table (offline): a squash-merged backport *sweep* records
       the original source PRs it batched only in an ``## Applied`` table in the
       commit body; the squash *subject* is the backport PR, so this table is
       the sole path to the originals. Reused from :mod:`scripts.backport`.
    2. ``-x`` cherry-pick trailer (one API call): a ``(cherry picked from commit
       <sha>)`` trailer names the source commit even when the subject was
       rewritten. We resolve that source SHA's PR via the API, preferring the
       oldest hop (the original). Only ``git cherry-pick -x`` writes this trailer,
       and this repo's *backport* tooling (:mod:`scripts.backport`) does not pass
       ``-x`` (its picks preserve the source subject, resolved by tier 3), so a
       tool-made backport does not reach this tier. It fires for a ``-x`` pick made
       elsewhere: a maintainer's hand-applied pick, or the ci-fix port path
       (:mod:`scripts.ci_fix.push`, the one place in this repo that passes ``-x``),
       whose subject may have been rewritten.
    3. Subject ``(#N)`` (offline): the trailing ``(#N)`` of a direct merge, or
       a cherry-pick that preserved the original commit message, is already the
       original PR. This is the common case for non-backport commits.
    4. API fallback: a commit with none of the above (a merge commit, very old
       history) -> the first PR the API associates with its own SHA.

    The first commit seen per PR number wins; later occurrences collapse onto
    it, so a change cherry-picked several times across the range dedups to one
    entry. A commit that resolves to no PR is returned in *unresolved* (not
    silently dropped): it shipped a change with no recoverable PR reference, so
    the cut surfaces it for a maintainer instead of letting it vanish.

    A later commit that collides with an already-claimed number is normally a
    correct collapse (a cherry-pick of the same change, or a multi-commit PR the
    API maps to one number). The exception is the ambiguous subject ``(#N)`` tier:
    a backport can reuse a source PR's ``(#N)`` on an unrelated follow-up commit
    (a feature commit and a later comment-wording fixup both ending ``(#3380)``),
    so two *different* changes claim one number. When the *loser* resolved via the
    subject tier and its subject differs from the winner's
    (:func:`_same_change_subject`), the dropped commit is returned in *collided* so
    it cannot vanish, regardless of which tier the winner used. Only ``## Applied``
    collisions on *both* sides are trusted to collapse correctly and stay silent
    (the sweep commit's own subject is unrelated to the individual source PRs it
    claims, so subject comparison would be meaningless).

    The third return value maps a credited PR number to an
    :class:`UnresolvedCherryPick` when tier 2 had trailer SHAs to try but *none*
    resolved (the source commit is not in this repo) and the credit therefore fell
    through to the subject or the commit->PR API. When the source is unreachable we
    cannot tell a preserved-message pick (subject already names the original) from a
    rewritten one (subject names the backport), so the credit is recorded as
    *unconfirmed* for :func:`discover` to reconcile against the final hydrated PRs;
    keyed by number, first-seen-aligned with ``pr_to_sha``.

    The fourth return value lists distinct commits dropped by a reused-``(#N)``
    subject-tier collision (see the dedup note above), so a reused ``(#N)`` cannot
    silently swallow a second change.
    """
    pr_to_sha: dict[int, str] = {}
    # Subject of the commit that won each number, stored for any first claim NOT
    # from the Applied-table tier (whose sweep subject is unrelated to the individual
    # source PRs it claims). A later subject-tier commit reusing the same number is
    # compared against this to detect a reused (#N) on a distinct change. Applied-
    # table collisions remain trusted (the subject comparison would be meaningless
    # since the sweep commit's subject is its own backport PR, not the source).
    winner_subject: dict[int, str] = {}
    unresolved: list[UnresolvedCommit] = []
    collided: list[CollidedCommit] = []
    cherry_pick_suspects: dict[int, UnresolvedCherryPick] = {}
    for sha, subject, body in commits:
        unconfirmed_source_shas: tuple[str, ...] = ()
        via_subject = False
        numbers = applied_source_prs_from_body(body)
        via_applied = bool(numbers)
        if not numbers:
            number = _pr_from_cherry_pick_trailer(repo, body)
            if number is not None:
                numbers = {number}
            else:
                # Tier 2 miss. If the body carried trailer SHAs but none resolved,
                # the credit below (subject or API) is a lower-confidence fallback
                # past an unreachable source; remember the SHAs so it can be flagged.
                unconfirmed_source_shas = tuple(cherry_pick_source_shas(body))
        if not numbers:
            numbers = _pr_numbers_from_subjects([subject])
            via_subject = bool(numbers)
        if not numbers:
            number = _pr_from_commit_api(repo, sha)
            numbers = {number} if number is not None else set()
        if not numbers:
            logger.warning("Commit %s has no resolvable PR (subject: %s)", sha[:12], subject[:80])
            unresolved.append(UnresolvedCommit(sha=sha, subject=subject))
            continue
        for number in numbers:
            if number in pr_to_sha:
                # First commit seen for this number wins (dedup). Usually correct (a
                # re-picked change, or a multi-commit PR). Dangerous when the loser
                # resolved via the ambiguous subject tier and its subject describes a
                # different change from the winner's. Applied-table collisions on
                # both sides are the real multi-commit case and stay silent (both the
                # winner and the loser are sweep commits whose subjects are unrelated
                # to the source PRs claimed). For all other winner tiers (subject,
                # -x trailer, API), the winner's commit subject IS the change it
                # introduced, so a distinct-subject loser indicates a reused (#N)
                # that would otherwise vanish.
                won = winner_subject.get(number)
                if via_subject and won is not None and not _same_change_subject(won, subject):
                    logger.warning(
                        "Commit %s reuses #%s already claimed by %s; surfacing the dropped change",
                        sha[:12], number, pr_to_sha[number][:12],
                    )
                    collided.append(CollidedCommit(
                        number=number, sha=sha, subject=subject, kept_sha=pr_to_sha[number],
                    ))
                continue
            pr_to_sha[number] = sha
            if not via_applied:
                winner_subject[number] = subject
            if unconfirmed_source_shas:
                cherry_pick_suspects[number] = UnresolvedCherryPick(
                    number=number, sha=sha,
                    source_shas=unconfirmed_source_shas, subject=subject,
                )
    logger.info(
        "Resolved %d unique PR(s) from %d commit(s); %d unresolved, %d collided, "
        "%d unconfirmed cherry-pick(s)",
        len(pr_to_sha), len(commits), len(unresolved), len(collided), len(cherry_pick_suspects),
    )
    return pr_to_sha, unresolved, cherry_pick_suspects, collided


# A backport that reuses a source PR's ``(#N)`` on the *same* change carries the same
# subject (a plain re-pick) or the source subject behind a ``[Backport ...]`` prefix
# (``_title_core`` strips it); either collapses correctly and must not be flagged. A
# reused ref on an *unrelated* follow-up commit has a different subject and is the
# dangerous case. Reuse the title normalization/threshold the backport walk already uses.
def _same_change_subject(a: str, b: str) -> bool:
    """True if two commit subjects describe the same change (dedup is a correct collapse)."""
    na, nb = _norm_title(_title_core(a)), _norm_title(_title_core(b))
    if not na or not nb:
        return False
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= _TITLE_SIMILARITY_MIN


def _pr_from_cherry_pick_trailer(repo: Any, body: str) -> int | None:
    """Return the original PR named by a ``-x`` cherry-pick trailer, or None.

    A commit picked through several branches accumulates one trailer per hop.
    ``git cherry-pick -x`` *appends* its trailer, so an inherited trailer from an
    earlier hop stays above the one the latest hop adds: file order is
    oldest-hop-first, most-recent-hop-last (see
    :func:`scripts.backport.utils.cherry_pick_source_shas`). We try the source
    SHAs in that file order and take the first whose commit resolves to a PR: the
    oldest hop is the original commit on the source line, so its PR is the
    original PR, not an intermediate backport. Returns None when there is no
    trailer or none of the source commits resolves (e.g. the source SHA is not in
    this repo, or predates the API's PR association).
    """
    source_shas = cherry_pick_source_shas(body)
    for source_sha in source_shas:  # oldest hop (the original) first
        number = _pr_from_commit_api(repo, source_sha)
        if number is not None:
            return number
    return None


def _pr_from_commit_api(repo: Any, sha: str) -> int | None:
    """Return the first PR number associated with *sha* via the GitHub API, or None.

    Uses ``GET /repos/{owner}/{repo}/commits/{sha}/pulls`` (PyGithub
    ``Commit.get_pulls()``). Only the first page's first item is consulted; a
    commit belongs to at most one merge in practice.
    """
    def _lookup() -> int | None:
        commit = repo.get_commit(sha)
        for pull in commit.get_pulls():
            return int(pull.number)
        return None

    try:
        return retry_github_call(_lookup, retries=3, description=f"PRs for commit {sha[:12]}")
    except Exception as exc:  # noqa: BLE001 - a lookup miss must not abort discovery
        logger.warning("Could not resolve PR for commit %s: %s", sha[:12], exc)
        return None


# A per-PR backport whose source is itself a backport is not expected within one
# release line's range, but a bad head-branch name or a cyclic body table could
# send recovery into a loop; cap the walk defensively.
_MAX_BACKPORT_DEPTH = 2


def _fetch_pull(repo: Any, number: int, cache: dict[int, Any]) -> Any:
    """Fetch and memoize PR *number*, returning ``None`` when it does not exist.

    A number that 404s (an issue reference, a ``(#N)`` from a different repo, or a
    recovered source PR that no longer resolves) yields ``None`` so the caller can
    skip or fall back. Any other failure (a 5xx outlasting retries, an auth error)
    propagates: silently dropping a real release-noted PR would ship it un-noted,
    and valkey's label gate would not catch it. ``None`` results are cached too, so
    a missing PR reached twice is fetched once.
    """
    if number in cache:
        return cache[number]
    try:
        pull = retry_github_call(
            lambda: repo.get_pull(number), retries=3, description=f"get PR #{number}"
        )
    except UnknownObjectException:
        logger.warning("Skipping PR #%s (not found; likely an issue or cross-repo ref)", number)
        pull = None
    except GithubException as exc:
        if exc.status == 404:
            logger.warning("Skipping PR #%s (not found; likely an issue or cross-repo ref)", number)
            pull = None
        else:
            raise
    cache[number] = pull
    return pull


# A recovered #N is trusted only when its title matches the source title the backport
# embeds, and that title is distinctive enough for a match to be evidence. These floors
# reject titles too short or too generic to distinguish two PRs ("Fix CI", "Update
# copyright year"), where a wrong-but-real #N could share the title and slip through.
_MIN_DISTINCTIVE_TITLE_CHARS = 15
_MIN_DISTINCTIVE_TITLE_WORDS = 3
# Recurring chore/boilerplate titles, normalized (lowercased, whitespace-collapsed). A
# title equal to one of these is never distinctive, regardless of length/word count.
_GENERIC_TITLES = frozenset({
    "fix ci",
    "fix flaky test",
    "fix flaky tests",
    "update copyright year",
    "update copyright years",
    "bump dependencies",
    "bump version",
    "update changelog",
})
# Titles opening with this are automated (dependabot, revert boilerplate): the same
# stem recurs across many PRs, so a match on it is not evidence of the same change.
_GENERIC_TITLE_PREFIX_RE = re.compile(
    r"^(?:bump\b|build\(deps\)|revert\b|merge\b)", re.IGNORECASE
)
# The normalized-title similarity above which two titles are considered the same change.
# High enough that an unrelated title (ratio ~ 0) never passes, but tolerant of a benign
# post-merge retitle (a fixed typo, an added period).
_TITLE_SIMILARITY_MIN = 0.90


def _norm_title(title: Any) -> str:
    """Normalize a title for comparison: collapse whitespace and casefold."""
    if not isinstance(title, str):
        return ""
    return re.sub(r"\s+", " ", title).strip().casefold()


def _is_distinctive_title(normalized: str) -> bool:
    """True if *normalized* (already :func:`_norm_title`d) is specific enough to match on.

    A title carries evidence about *which* PR is the source only when it is not a
    recurring chore title. Requires a length and word-count floor and rejects a small
    stop-list / automated prefixes, so a match on "Fix CI" can never be mistaken for
    proof that a recovered #N is the right source.
    """
    if len(normalized) < _MIN_DISTINCTIVE_TITLE_CHARS:
        return False
    if len(normalized.split()) < _MIN_DISTINCTIVE_TITLE_WORDS:
        return False
    if normalized in _GENERIC_TITLES:
        return False
    if _GENERIC_TITLE_PREFIX_RE.match(normalized):
        return False
    return True


def _title_core(title: Any) -> str:
    """The source title carried by *title*, stripped of a ``[Backport ...]`` prefix.

    A source PR that is itself an intermediate backport carries the original title
    behind its own ``[Backport <branch>] `` prefix (in its title and in the summary
    ``Source title`` row). Reducing every title to this "core" before comparison lets
    a chained backport-of-backport hop match the same underlying change. A plain
    (non-backport) title has no prefix and is returned unchanged.
    """
    if not isinstance(title, str):
        return ""
    stripped = source_title_from_backport_title(title)
    return stripped if stripped is not None else title


def _expected_source_titles(backport_pull: Any) -> set[str]:
    """Distinctive source-title cores the *backport_pull* embeds, normalized.

    A backport carries the source PR's title verbatim in two places written at
    creation time: the ``[Backport <branch>] <source title>`` prefix and the
    ``## Backport Summary`` ``Source title`` row. Both are witnesses to the recovered
    ``#N`` that are independent of the number itself. Each is reduced to its
    :func:`_title_core` (so a chained backport source matches), normalized, and kept
    only when distinctive (see :func:`_is_distinctive_title`); an empty set means the
    backport offers nothing to cross-check, which the caller treats as fail-closed.
    """
    titles: set[str] = set()
    for raw in (
        source_title_from_backport_title(backport_pull.title or ""),
        summary_source_title_from_body(backport_pull.body or ""),
    ):
        normalized = _norm_title(_title_core(raw)) if raw else ""
        if normalized and _is_distinctive_title(normalized):
            titles.add(normalized)
    return titles


def _titles_consistent(expected: set[str], actual: Any) -> bool:
    """True if *actual* matches any title in *expected* (exact-normalized or >= 0.90).

    *expected* is a set of already-normalized distinctive title cores; *actual* is the
    recovered PR's raw title, reduced to its :func:`_title_core` (so a recovered source
    that is itself a backport compares on the underlying change). A wrong-but-real #N
    has an unrelated title (ratio ~ 0) and fails; a source retitled slightly after the
    backport was cut still passes via the similarity fallback.
    """
    a = _norm_title(_title_core(actual))
    if not a:
        return False
    for e in expected:
        if e == a or difflib.SequenceMatcher(None, e, a).ratio() >= _TITLE_SIMILARITY_MIN:
            return True
    return False


def _source_is_trusted(src_pull: Any, backport_pull: Any) -> bool:
    """True if *src_pull* is a credible original source for *backport_pull*.

    Two cheap, zero-extra-API checks on the already-fetched *src_pull*:

    1. It must be merged. A PR that introduced a shipped change is always merged, so
       an open/closed-unmerged/issue-shaped ref that a wrong ``#N`` resolved to is
       rejected.
    2. Its title must match a distinctive source title the backport embeds (the
       ``[Backport ...] <title>`` prefix or the ``## Backport Summary`` ``Source
       title`` row). This is the signal that ties ``#N`` to *what the backport claims
       it carried*: a mistyped ``Source PR`` cell, a stale branch, or a stray commit
       ``(#N)`` resolves to a PR whose title is unrelated and is rejected.

    When the backport embeds no distinctive title to compare against (a label-only or
    free-form human backport), this returns ``False`` (fail-closed): the recovery is
    not trusted and the caller keeps crediting the backport, flagging it for review.
    Over-flagging a correct recovery is the accepted trade against silently crediting
    the wrong author.
    """
    if not (getattr(src_pull, "merged", None) or getattr(src_pull, "merged_at", None) is not None):
        return False
    expected = _expected_source_titles(backport_pull)
    if not expected:
        return False
    return _titles_consistent(expected, src_pull.title)


def _recover_source_pr(repo: Any, pull: Any) -> int | None:
    """Return the original PR of a *per-PR* backport *pull*, or ``None``.

    The sweep and ``-x`` cases are already resolved at the commit level in
    :func:`resolve_commit_prs`. What reaches here is a per-PR ``[Backport ...]`` PR
    whose squash subject was rewritten to the backport PR number and which carries
    no ``## Applied`` table or ``-x`` trailer. Its origin is recoverable only from
    the backport PR object, tried cheapest first:

    1. its ``## Backport Summary`` table's ``Source PR`` row (offline, from the raw
       body already fetched);
    2. the trailing ``(#N)`` of the backport PR's own commits (one API call): a
       squash rewrites only the *merge* subject, so the PR's constituent commits
       still name the original;
    3. the ``backport/<n>-to-<branch>`` head-branch name (offline).

    Returns ``None`` when none yields a number (the caller then keeps crediting the
    backport, never dropping the change).
    """
    source = summary_source_pr_from_body(pull.body or "")
    if source is not None:
        return source
    try:
        commits = retry_github_call(
            lambda: list(pull.get_commits()), retries=3,
            description=f"commits of PR #{pull.number}",
        )
        subjects = [c.commit.message.splitlines()[0] for c in commits if c.commit.message]
        numbers = _pr_numbers_from_subjects(subjects)
        # A single-commit backport gives one original PR; a multi-commit backport
        # that spans several source PRs is a sweep and would have been resolved by
        # the ## Applied path already, so take the sole number when unambiguous.
        if len(numbers) == 1:
            return next(iter(numbers))
    except Exception as exc:  # noqa: BLE001 - a lookup miss must not abort discovery
        logger.warning("Could not read commits of PR #%s: %s", pull.number, exc)
    head_ref = getattr(getattr(pull, "head", None), "ref", "") or ""
    return source_pr_from_branch(head_ref)


def _is_backport_pull(pull: Any) -> bool:
    """True if *pull* is a backport PR.

    Any one of four offline signals marks a backport: a ``[Backport ...]`` title,
    a ``backport`` label, a ``## Backport Summary`` table with a parseable
    ``Source PR`` row, or a ``backport/<n>-to-<branch>`` head branch. The latter
    two are structural markers :mod:`scripts.backport.pr_creator` stamps on every
    tool-made backport, so one whose author retitled it off the ``[Backport ...]``
    prefix *and* dropped the label is still detected here and routed into recovery
    (:func:`_recover_source_pr`, which trusts the same two markers) rather than
    silently credited as an original. All four read already-fetched fields, so
    detection stays zero extra API.
    """
    title = pull.title or ""
    labels = tuple(label.name for label in pull.labels)
    if is_backport_title(title) or "backport" in labels:
        return True
    if summary_source_pr_from_body(pull.body or "") is not None:
        return True
    head_ref = getattr(getattr(pull, "head", None), "ref", "") or ""
    return source_pr_from_branch(head_ref) is not None


def _build_merged_pr(pull: Any, number: int, merge_commit_sha: str) -> MergedPR:
    """Build a :class:`MergedPR` from *pull* under *number* and *merge_commit_sha*.

    *number* is the authoritative PR number (the resolved key, or the recovered
    source PR after a backport remap), passed in rather than read from ``pull`` so
    it is always the identity the rest of the pipeline dedups and renders on.
    *merge_commit_sha* is the commit on *this* release line, chosen by the caller
    (a direct PR's own merge, or the backport's range commit for a remapped
    source), not read from ``pull``: a remapped source's own ``merge_commit_sha``
    would point at its merge on unstable, not this line.
    """
    author = ""
    if pull.user is not None and pull.user.login:
        author = pull.user.login
    return MergedPR(
        number=number,
        title=pull.title or "",
        author=author,
        url=pull.html_url or "",
        body=_clean_pr_body(pull.body),
        labels=tuple(label.name for label in pull.labels),
        merge_commit_sha=merge_commit_sha,
    )


def hydrate_prs(
    repo: Any, pr_to_sha: dict[int, str]
) -> tuple[list[MergedPR], list[UnresolvedBackport], list[UnresolvedPR]]:
    """Fetch title/author/labels for each PR number.

    Returns ``(prs, unresolved_backports, unresolved_prs)``. Disposition is left
    at its default (TRIAGE) here; :mod:`classify` assigns the real value. A number
    that 404s (an issue reference, a moved/deleted PR, or a ``(#N)`` from a
    different repo) cannot be hydrated into a note; because its range commit still
    shipped a change, it is recorded in ``unresolved_prs`` (with the range sha) so
    the cut surfaces it for a maintainer instead of dropping it silently past
    valkey's label-only gate. Any non-404 failure is re-raised.

    When a resolved PR is itself a *per-PR* backport (its commit carried no
    ``## Applied`` table or ``-x`` trailer for :func:`resolve_commit_prs` to walk
    back), recovery runs here, where the backport PR object is in hand: its
    ``## Backport Summary`` row, then its own commits' ``(#N)``, then its
    ``backport/<n>-to-<branch>`` head branch (see :func:`_recover_source_pr`). A
    recovered source (that differs from the backport number) is fetched and the
    :class:`MergedPR` is built from *it* (original number, title, author, body,
    and labels), so the note credits the change's author and the original labels
    drive classification. The ``merge_commit_sha`` stays the range (backport)
    commit. If nothing is recovered, the backport is credited as before, a warning
    fires, and the backport is added to the returned ``unresolved_backports`` so
    the cut can flag the suspect credit in the PR body (a log line alone is easy to
    miss). Results are deduplicated by final PR number (two backports of one
    source, or a source also present as a direct range commit, collapse to one
    entry, first-seen wins), so a change is never noted twice.
    """
    pull_cache: dict[int, Any] = {}
    final: dict[int, MergedPR] = {}
    unresolved_backports: list[UnresolvedBackport] = []
    unresolved_prs: list[UnresolvedPR] = []
    for number in sorted(pr_to_sha):
        sha = pr_to_sha[number]
        pull = _fetch_pull(repo, number, pull_cache)
        if pull is None:
            # The range commit resolved to this number, but the PR could not be
            # fetched (not-found: a moved/deleted PR, an issue, or a cross-repo
            # (#N)). The change still shipped, so record it against its range sha
            # for the PR body rather than dropping it silently.
            logger.warning(
                "Commit %s resolved to PR #%s but it could not be fetched; "
                "surfacing it as unresolved so the shipped change is not dropped.",
                sha[:12], number,
            )
            unresolved_prs.append(UnresolvedPR(number=number, sha=sha))
            continue

        target_pull, target_number = pull, number
        if _is_backport_pull(pull):
            source = _recover_source_pr(repo, pull)
            depth = 0
            visited = {number}
            # Walk to the original, one hop at a time; a per-PR backport of a
            # backport is not expected, but the depth + visited guards keep a bad
            # branch name or cyclic table from looping.
            while source is not None and source not in visited and depth < _MAX_BACKPORT_DEPTH:
                src_pull = _fetch_pull(repo, source, pull_cache)
                if src_pull is None:
                    # The recovered source does not resolve (deleted, cross-repo):
                    # keep the backport rather than drop the change.
                    break
                # `source` was recovered from `target_pull` (the original backport on
                # hop 1, an intermediate on later hops), so validate the fetched source
                # against that pull's embedded source title. A rejection keeps the
                # backport credit: `target_pull` is left unchanged, so the post-loop
                # `_is_backport_pull(target_pull)` block flags it as an unresolved
                # backport for the PR body. Distinct from the "no source found" case so
                # the CI log tells the two apart.
                if not _source_is_trusted(src_pull, target_pull):
                    logger.warning(
                        "Recovered source PR #%s for backport PR #%s failed validation "
                        "(title mismatch or unmerged); keeping the backport credit.",
                        source, target_number,
                    )
                    break
                visited.add(source)
                target_pull, target_number = src_pull, source
                if not _is_backport_pull(src_pull):
                    break
                source = _recover_source_pr(repo, src_pull)
                depth += 1
            if not _is_backport_pull(target_pull):
                logger.info(
                    "Backport PR #%s credited to its original source PR #%s.",
                    number, target_number,
                )

        if target_number in final:
            continue  # dedup: first-seen wins
        # The commit on THIS line: a direct PR's own merge (or the range sha when
        # GitHub gives none), but always the backport's range commit for a remapped
        # source, whose own merge_commit_sha points at unstable, not this line.
        if target_number == number:
            merge_sha = target_pull.merge_commit_sha or sha
        else:
            merge_sha = sha
        final[target_number] = _build_merged_pr(target_pull, target_number, merge_sha)
        # The credited entry is still a backport: recovery found no distinct source
        # (or a backport-of-backport chain we couldn't finish). Credit it, but warn
        # and record it for the PR body so the suspect credit is visible to a
        # reviewer, not only in the CI log. Recorded once, aligned with the deduped
        # `final` entry, so a source reached via two backports is flagged at most once.
        if _is_backport_pull(target_pull):
            logger.warning(
                "PR #%s credited for a range commit is itself a backport (%r); "
                "the original PR could not be recovered from an ## Applied table, a "
                "-x trailer, the subject, its ## Backport Summary, its own commits, "
                "or its branch name. The note will credit this backport.",
                target_number, (target_pull.title or "")[:80],
            )
            unresolved_backports.append(
                UnresolvedBackport(
                    number=target_number,
                    title=target_pull.title or "",
                    url=target_pull.html_url or "",
                )
            )
    return list(final.values()), unresolved_backports, unresolved_prs


def _resolve_base_ref(repo_dir: str, base_ref: str) -> str:
    """Return a ref name for *base_ref* that resolves in *repo_dir*.

    The clone is made with ``git clone --branch <source>``, so only the source
    branch becomes a local ref; every other branch exists solely as its
    remote-tracking ref ``origin/<name>``. A ``--base-ref`` naming such a branch
    (e.g. a fork passing ``unstable``) therefore fails a bare ``rev-parse``. Try
    the name as given first, which covers tags, SHAs, and the source branch, and
    fall back to ``origin/<name>`` for any other branch. The returned name is
    used both to resolve the base SHA and as the range/contributor baseline, so
    every downstream ``base..head`` walk sees a name git can resolve.
    """
    try:
        git_output(repo_dir, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}")
        return base_ref
    except subprocess.CalledProcessError:
        remote = f"origin/{base_ref}"
        try:
            git_output(repo_dir, "rev-parse", "--verify", "--quiet", f"{remote}^{{commit}}")
        except subprocess.CalledProcessError as exc:
            # Neither the name as given nor origin/<name> resolves (a typo'd
            # branch/tag). Raise a ValueError naming the ref, mirroring
            # resolve_last_tag, rather than leaking a raw CalledProcessError.
            raise ValueError(
                f"base ref {base_ref!r} resolves neither as given nor as {remote!r}"
            ) from exc
        logger.info("Base ref %r resolved via remote-tracking ref %r", base_ref, remote)
        return remote


def discover(
    repo: Any, repo_dir: str, head_ref: str, *,
    tag_glob: str | None = None, base_ref: str | None = None,
) -> DiscoveryResult:
    """Resolve the release range and return a deduplicated :class:`DiscoveryResult`.

    ``repo`` is a PyGithub repository; ``repo_dir`` is a full-depth local clone
    of the same repo with tags fetched (a shallow clone breaks ``describe`` and
    the range walk). Dispositions are unset; :func:`classify.classify` fills
    them.

    ``base_ref`` is an explicit baseline (a branch, tag, or SHA) that overrides
    tag resolution, the escape hatch for a line with no reachable tag (e.g. a
    fork that carries no release tags). When set, the range is ``base_ref..head``
    directly; otherwise the most recent tag (optionally filtered by ``tag_glob``)
    is used.
    """
    if base_ref:
        # Resolve to a name git can use in a fresh --branch clone (the bare
        # branch may only exist as origin/<name>); reuse it as the range and
        # contributor baseline so every base..head walk resolves identically.
        base_tag = _resolve_base_ref(repo_dir, base_ref)
    else:
        base_tag, _base_sha = resolve_last_tag(repo_dir, head_ref, tag_glob=tag_glob)
    commits = list_range_commits(repo_dir, base_tag, head_ref)
    pr_to_sha, unresolved, cherry_pick_suspects, collided = resolve_commit_prs(repo, commits)
    prs, unresolved_backports, unresolved_prs = hydrate_prs(repo, pr_to_sha)
    unresolved_cherry_picks = _reconcile_cherry_pick_suspects(
        cherry_pick_suspects, prs, unresolved_backports, unresolved_prs
    )
    return DiscoveryResult(
        base_tag=base_tag,
        head_ref=head_ref,
        prs=tuple(prs),
        unresolved=tuple(unresolved),
        unresolved_backports=tuple(unresolved_backports),
        unresolved_prs=tuple(unresolved_prs),
        unresolved_cherry_picks=tuple(unresolved_cherry_picks),
        collided=tuple(collided),
    )


def _reconcile_cherry_pick_suspects(
    suspects: dict[int, UnresolvedCherryPick],
    prs: list[MergedPR],
    unresolved_backports: list[UnresolvedBackport],
    unresolved_prs: list[UnresolvedPR],
) -> list[UnresolvedCherryPick]:
    """Keep only the cherry-pick suspects whose credit still needs a maintainer's eye.

    A suspect from :func:`resolve_commit_prs` is a note credited past an
    unresolvable ``-x`` trailer. Most such credits are handled by a stronger signal
    downstream and need no separate flag; drop those here so the flag stays
    low-noise and never double-reports:

    * :func:`hydrate_prs` remapped the number to a recovered original (the
      credited number is no longer in ``prs`` under its own key) -> the credit was
      corrected, no flag.
    * :func:`hydrate_prs` already flagged the credited PR as an
      :class:`UnresolvedBackport` (it carried backport markers) -> flagging it again
      as a cherry-pick would double-report the same suspect credit.
    * the number 404'd into ``unresolved_prs`` (the note was never built) -> it is
      already surfaced there, not as a miscredit.

    What survives is exactly the hole: the credited PR is present in ``prs`` under
    its own number and carries no backport markers, so nothing else tells a reviewer
    its origin could not be confirmed.
    """
    if not suspects:
        return []
    credited = {pr.number for pr in prs}
    flagged_backports = {bp.number for bp in unresolved_backports}
    fetch_failed = {u.number for u in unresolved_prs}
    return [
        suspect
        for number, suspect in suspects.items()
        if number in credited
        and number not in flagged_backports
        and number not in fetch_failed
    ]
