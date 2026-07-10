"""Discover the PRs a release line has accrued since its last tag.

Selection is by graph reachability, never by date: we resolve the most recent
tag reachable from the release-line tip and walk ``tag..head``. A backport
cherry-picked onto the line is a distinct commit with its own date, so a date
window would either miss it or double-count it; a graph range counts it exactly
when it is part of this line's history.

Each commit is resolved to the original PR that introduced the change (the PR
whose author the note should credit, not the backport PR that carried it onto
this line), and the set is deduplicated by that PR number rather than by commit
SHA. Resolution tries the markers that recover the original first (see
:func:`resolve_commit_prs`): the ``## Applied`` table a squash-merged sweep
writes into the commit body, then a ``-x`` cherry-pick trailer, then the
subject's trailing ``(#N)``, and last the GitHub "PRs associated with a commit"
API on the commit's own SHA. When only the backport PR resolves, the change is
still noted and a warning fires so a maintainer can check the original.

A commit that resolves to no PR at all (a hand-applied cherry-pick whose message
was rewritten, an unusual merge) is not dropped: it is returned in
``DiscoveryResult.unresolved`` and surfaced in the cut's PR body, so a shipped
change can never vanish past valkey's label-only gate un-noted.
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import Any

from github.GithubException import GithubException, UnknownObjectException

from scripts.backport.utils import pr_numbers_from_commit_subjects
from scripts.common.github_client import retry_github_call
from scripts.common.proc import git_output
from scripts.release_notes.backport_refs import (
    applied_source_prs_from_body,
    cherry_pick_source_shas,
    is_backport_title,
    source_pr_from_branch,
    summary_source_pr_from_body,
)
from scripts.release_notes.models import (
    DiscoveryResult,
    MergedPR,
    UnresolvedBackport,
    UnresolvedCommit,
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


def resolve_last_tag(repo_dir: str, head_ref: str, *, tag_glob: str | None = None) -> tuple[str, str]:
    """Return ``(tag_name, tag_sha)`` for the most recent tag reachable from *head_ref*.

    "Most recent" is by graph distance, not date: ``git describe --tags
    --abbrev=0`` reports the nearest tag that is an ancestor of *head_ref*.
    ``tag_glob`` (e.g. ``"9.1.*"``) restricts matching to one release line via
    ``--match``. Raises :class:`ValueError` when no tag is reachable.
    """
    args = ["describe", "--tags", "--abbrev=0"]
    if tag_glob:
        args += ["--match", tag_glob]
    args.append(head_ref)
    try:
        tag = git_output(repo_dir, *args).strip()
    except subprocess.CalledProcessError as exc:
        # Only a non-zero exit means "no tag reachable". An operational failure
        # (TimeoutExpired from a hung git) must propagate, not be disguised as a
        # missing baseline that would send the caller to the wrong range.
        raise ValueError(
            f"no tag reachable from {head_ref!r}"
            + (f" matching {tag_glob!r}" if tag_glob else "")
        ) from exc
    if not tag:
        raise ValueError(f"no tag reachable from {head_ref!r}")
    # Dereference the (possibly annotated) tag to the commit it points at.
    tag_sha = git_output(repo_dir, "rev-list", "-n", "1", tag).strip()
    logger.info("Last tag reachable from %s: %s (%s)", head_ref, tag, tag_sha[:12])
    return tag, tag_sha


def list_range_commits(repo_dir: str, base: str, head_ref: str) -> list[tuple[str, str, str]]:
    """Return ``[(sha, subject, body), ...]`` for commits in ``base..head_ref``, oldest first.

    ``base`` is the prior tag (or its SHA); the range excludes it and includes
    everything reachable from *head_ref* that it does not reach, exactly the
    line's new history. The body is carried alongside the subject so
    :func:`resolve_commit_prs` can recover the original PR of a backport from an
    ``## Applied`` table or a ``-x`` cherry-pick trailer.
    """
    out = git_output(
        repo_dir, "log", "--reverse", f"--format={_LOG_FORMAT}", f"{base}..{head_ref}"
    )
    commits: list[tuple[str, str, str]] = []
    # Split on the record separator we appended, not str.splitlines(): a body
    # spans many lines and legitimately contains \n, \v, \f, etc., so a
    # line-based split would tear one commit into bogus records.
    for record in out.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, rest = record.partition(_FIELD_SEP)
        subject, _, body = rest.partition(_FIELD_SEP)
        commits.append((sha, subject, body))
    logger.info("%d commit(s) in %s..%s", len(commits), base, head_ref)
    return commits


def resolve_commit_prs(
    repo: Any, commits: list[tuple[str, str, str]]
) -> tuple[dict[int, str], list[UnresolvedCommit]]:
    """Map *original* PR number -> representative commit SHA, plus unresolved commits.

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
       oldest hop (the original), so a per-PR backport whose subject is the
       backport PR still credits the original.
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
    """
    pr_to_sha: dict[int, str] = {}
    unresolved: list[UnresolvedCommit] = []
    for sha, subject, body in commits:
        numbers = applied_source_prs_from_body(body)
        if not numbers:
            number = _pr_from_cherry_pick_trailer(repo, body)
            if number is not None:
                numbers = {number}
        if not numbers:
            numbers = pr_numbers_from_commit_subjects([subject])
        if not numbers:
            number = _pr_from_commit_api(repo, sha)
            numbers = {number} if number is not None else set()
        if not numbers:
            logger.warning("Commit %s has no resolvable PR (subject: %s)", sha[:12], subject[:80])
            unresolved.append(UnresolvedCommit(sha=sha, subject=subject))
            continue
        for number in numbers:
            pr_to_sha.setdefault(number, sha)
    logger.info(
        "Resolved %d unique PR(s) from %d commit(s); %d unresolved",
        len(pr_to_sha), len(commits), len(unresolved),
    )
    return pr_to_sha, unresolved


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
        numbers = pr_numbers_from_commit_subjects(subjects)
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
    """True if *pull* is a backport PR (``[Backport ...]`` title or ``backport`` label)."""
    title = pull.title or ""
    labels = tuple(label.name for label in pull.labels)
    return is_backport_title(title) or "backport" in labels


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
) -> tuple[list[MergedPR], list[UnresolvedBackport]]:
    """Fetch title/author/labels for each PR number, returning ``(prs, unresolved_backports)``.

    Disposition is left at its default (TRIAGE) here; :mod:`classify` assigns the
    real value. A number that 404s (an issue reference, or a ``(#N)`` from a
    different repo) is skipped with a warning; any other failure is re-raised.

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
    for number in sorted(pr_to_sha):
        sha = pr_to_sha[number]
        pull = _fetch_pull(repo, number, pull_cache)
        if pull is None:
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
    return list(final.values()), unresolved_backports


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
    pr_to_sha, unresolved = resolve_commit_prs(repo, commits)
    prs, unresolved_backports = hydrate_prs(repo, pr_to_sha)
    return DiscoveryResult(
        base_tag=base_tag,
        head_ref=head_ref,
        prs=tuple(prs),
        unresolved=tuple(unresolved),
        unresolved_backports=tuple(unresolved_backports),
    )
