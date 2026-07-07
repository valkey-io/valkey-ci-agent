"""Discover the PRs a release line has accrued since its last tag.

Selection is by graph reachability, never by date: we resolve the most recent
tag reachable from the release-line tip and walk ``tag..head``. A backport
cherry-picked onto the line is a distinct commit with its own date, so a date
window would either miss it or double-count it; a graph range counts it exactly
when it is part of this line's history.

Each commit is resolved to its originating PR number so the set is deduplicated
by change identity rather than by commit SHA. The squash-merge subject's
trailing ``(#N)`` is the cheap offline path
(:func:`scripts.backport.utils.pr_numbers_from_commit_subjects`); commits
without one fall back to the GitHub "PRs associated with a commit" API.

Cross-line dedup (the same change shipping as a different PR on ``unstable``) is
intentionally out of scope: within a single release line, the PR that merged the
change onto this line is the right identity. A change that should have been
release-noted on another line surfaces as a triage signal, not an auto-merge.
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
from scripts.release_notes.models import DiscoveryResult, MergedPR

logger = logging.getLogger(__name__)

# NUL is illegal in a git ref/subject, so it is a safe field separator for the
# ``%H%x00%s`` log format (a subject may itself contain tabs or pipes).
_LOG_FORMAT = "%H%x00%s"

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


def list_range_commits(repo_dir: str, base: str, head_ref: str) -> list[tuple[str, str]]:
    """Return ``[(sha, subject), ...]`` for commits in ``base..head_ref``, oldest first.

    ``base`` is the prior tag (or its SHA); the range excludes it and includes
    everything reachable from *head_ref* that it does not reach, exactly the
    line's new history.
    """
    out = git_output(
        repo_dir, "log", "--reverse", f"--format={_LOG_FORMAT}", f"{base}..{head_ref}"
    )
    commits: list[tuple[str, str]] = []
    # Split on "\n" only, not str.splitlines(), which also breaks on \v, \f,
    # \x85, U+2028/2029, etc. A subject legitimately containing one of those
    # would otherwise be torn into a bogus extra record.
    for line in out.split("\n"):
        if not line:
            continue
        sha, _, subject = line.partition("\x00")
        commits.append((sha, subject))
    logger.info("%d commit(s) in %s..%s", len(commits), base, head_ref)
    return commits


def resolve_commit_prs(repo: Any, commits: list[tuple[str, str]]) -> dict[int, str]:
    """Map originating PR number -> representative commit SHA, deduplicated.

    Two-tier resolution:

    1. Subject parse (offline, free): the trailing ``(#N)`` of a squash-merge
       subject is the PR that merged the commit onto this line. This catches the
       overwhelming majority and is what makes a cherry-picked change collapse
       onto one key when its subject preserves the source ``(#N)``.
    2. API fallback: for a commit whose subject has no trailing ``(#N)`` (a
       hand-applied cherry-pick, a merge commit, very old history), ask GitHub
       for the PRs associated with that SHA and take the first.

    The first commit seen per PR number wins; later occurrences collapse onto
    it. Commits that resolve to no PR are dropped with a warning: they are
    invisible to dedup and carry no PR reference for a note.
    """
    pr_to_sha: dict[int, str] = {}
    for sha, subject in commits:
        numbers = pr_numbers_from_commit_subjects([subject])
        if not numbers:
            number = _pr_from_commit_api(repo, sha)
            numbers = {number} if number is not None else set()
        if not numbers:
            logger.warning("Commit %s has no resolvable PR (subject: %s)", sha[:12], subject[:80])
            continue
        for number in numbers:
            pr_to_sha.setdefault(number, sha)
    logger.info("Resolved %d unique PR(s) from %d commit(s)", len(pr_to_sha), len(commits))
    return pr_to_sha


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


def hydrate_prs(repo: Any, pr_to_sha: dict[int, str]) -> list[MergedPR]:
    """Fetch title/author/labels for each PR number, returning :class:`MergedPR`.

    Disposition is left at its default (TRIAGE) here; :mod:`classify` assigns
    the real value. A number that 404s (an issue reference, or a ``(#N)`` from a
    different repo) is skipped with a warning. Any other failure (a 5xx that
    outlasts retries, an auth error) is re-raised: silently dropping a real
    release-noted PR would ship it un-noted, and valkey's label gate would not
    catch it.
    """
    prs: list[MergedPR] = []
    for number in sorted(pr_to_sha):
        sha = pr_to_sha[number]
        try:
            pull = retry_github_call(
                lambda: repo.get_pull(number), retries=3, description=f"get PR #{number}"
            )
        except UnknownObjectException:
            logger.warning("Skipping PR #%s (not found; likely an issue or cross-repo ref)", number)
            continue
        except GithubException as exc:
            if exc.status == 404:
                logger.warning("Skipping PR #%s (not found; likely an issue or cross-repo ref)", number)
                continue
            raise
        author = ""
        if pull.user is not None and pull.user.login:
            author = pull.user.login
        labels = tuple(label.name for label in pull.labels)
        prs.append(
            MergedPR(
                number=number,
                title=pull.title or "",
                author=author,
                url=pull.html_url or "",
                body=_clean_pr_body(pull.body),
                labels=labels,
                merge_commit_sha=pull.merge_commit_sha or sha,
            )
        )
    return prs


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
    pr_to_sha = resolve_commit_prs(repo, commits)
    prs = hydrate_prs(repo, pr_to_sha)
    return DiscoveryResult(
        base_tag=base_tag,
        head_ref=head_ref,
        prs=tuple(prs),
    )
