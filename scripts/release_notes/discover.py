"""Discover PRs accrued on a release line since its last tag.

Walks the graph (tag..head, first-parent) to enumerate merge/squash commits,
then resolves each to the original PR that introduced the change via a tiered
strategy (## Applied table, -x trailer, subject (#N), commit->PR API). Results
are deduplicated by PR number so each change appears once. Backport sweep merges
are expanded to their per-source cherry-picks. Commits that resolve to no PR are
tracked in DiscoveryResult.unresolved so shipped changes cannot vanish silently.
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

# Field separator for git log format strings. NUL (\x00) serves as the record
# separator via git's -z flag, which guarantees no format placeholder can emit it.
_FIELD_SEP = "\x1f"
_LOG_FORMAT = f"%H{_FIELD_SEP}%s{_FIELD_SEP}%b"
_NUL = "\x00"

# Detects a sweep merge commit so list_range_commits can splice its second-parent
# cherry-picks back into the walk.
_SWEEP_MERGE_RE = re.compile(r"agent/backport/sweep")

# Trailing (#N) in squash/normal merge subjects. Anchored to avoid matching a
# quoted inner ref (e.g. a reverted title).
_TRAILING_REF_RE = re.compile(r"\(#(\d+)\)[)\s.]*$")
# GitHub merge-commit subject: ``Merge pull request #N from ...``.
_MERGE_COMMIT_RE = re.compile(r"^Merge pull request #(\d+)\b")


def _pr_numbers_from_subjects(subjects: list[str]) -> set[int]:
    """Extract PR numbers from subjects via trailing (#N) or merge-commit prefix."""
    numbers: set[int] = set()
    for line in subjects:
        m = _TRAILING_REF_RE.search(line)
        if m is None:
            m = _MERGE_COMMIT_RE.search(line)
        if m:
            numbers.add(int(m.group(1)))
    return numbers


# Cap PR body length for prompt context; strip HTML comments and DCO trailers.
_MAX_PR_BODY_CHARS = 2000
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TRAILER_RE = re.compile(r"(?im)^[ \t]*(?:signed-off-by|co-authored-by):.*$")


def _clean_pr_body(body: Any) -> str:
    """Strip HTML comments, DCO trailers, and truncate to _MAX_PR_BODY_CHARS.

    Returns "" for missing/empty/non-string bodies. Clips on a word boundary.
    """
    if not isinstance(body, str) or not body:
        return ""
    text = _HTML_COMMENT_RE.sub("", body)
    text = _TRAILER_RE.sub("", text)
    # Normalize CRLF and collapse 3+ newlines to a blank line.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= _MAX_PR_BODY_CHARS:
        return text
    clipped = text[:_MAX_PR_BODY_CHARS]
    # Cut at last whitespace if reasonably close to the cap.
    cut = clipped.rfind(" ")
    if cut >= _MAX_PR_BODY_CHARS - 200:
        clipped = clipped[:cut]
    return clipped.rstrip() + "…"


# Parses release tags: [v]M.m.p[-rcN|-ga]. Non-matching tags are skipped.
_TAG_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:-rc([1-9]\d*)|-ga)?$", re.IGNORECASE)


def _tag_sort_key(tag: str) -> tuple[int, int, int, int, int] | None:
    """Return a version sort key for a release tag, or None if unparseable.

    Orders by (major, minor, patch), then GA above any rc of the same M.m.p.
    """
    m = _TAG_RE.fullmatch(tag.strip())
    if not m:
        return None
    major, minor, patch = (int(g) for g in m.group(1, 2, 3))
    rc = m.group(4)
    # rc -> (…, 0, N); GA (bare M.m.p or -ga) -> (…, 1, 0), which sorts above any rc.
    return (major, minor, patch, 0, int(rc)) if rc is not None else (major, minor, patch, 1, 0)


def _tag_matches_glob(tag: str, glob: str) -> bool:
    """Whether tag matches glob, stripping a leading 'v' if needed."""
    return fnmatch.fnmatch(tag, glob) or (
        tag[:1] in ("v", "V") and fnmatch.fnmatch(tag[1:], glob)
    )


def resolve_last_tag(repo_dir: str, head_ref: str, *, tag_glob: str | None = None) -> tuple[str, str]:
    """Return (tag_name, tag_sha) for the highest-version tag reachable from head_ref.

    Lists all reachable tags (git tag --merged), filters by tag_glob in Python,
    parses to version keys, and picks the maximum. Raises ValueError if none match.
    """
    try:
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
    """Return (tag, sha) for the highest release tag strictly below target_version.

    Used for rc1 baselines. Considers all tags in the repo (reachability not
    required), selects the maximum whose M.m.p is strictly less than the target's.
    Returns None when no qualifying tag exists.
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
    """Parse git log -z --format=_LOG_FORMAT output into [(sha, subject, body)]."""
    records: list[tuple[str, str, str]] = []
    for record in out.split(_NUL):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, rest = record.partition(_FIELD_SEP)
        subject, _, body = rest.partition(_FIELD_SEP)
        records.append((sha, subject, body))
    return records


def list_range_commits(repo_dir: str, base: str, head_ref: str) -> list[tuple[str, str, str]]:
    """Return [(sha, subject, body), ...] for commits in base..head_ref, oldest first.

    Uses --first-parent to avoid intermediate branch commits. For sweep merges,
    splices in the second-parent cherry-picks (which carry source (#N)) and drops
    the sweep merge commit itself.
    """
    fmt = f"{_LOG_FORMAT}{_FIELD_SEP}%P"
    out = git_output(
        repo_dir, "log", "-z", "--reverse", "--first-parent",
        f"--format={fmt}", f"{base}..{head_ref}",
    )
    commits: list[tuple[str, str, str]] = []
    for record in out.split(_NUL):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, rest = record.partition(_FIELD_SEP)
        subject, _, remainder = rest.partition(_FIELD_SEP)
        # Body may itself contain \x1f, so parents are everything after the last \x1f.
        body, _, parents_raw = remainder.rpartition(_FIELD_SEP)
        parents = parents_raw.strip().split()
        if len(parents) >= 2 and _SWEEP_MERGE_RE.search(subject):
            sub = git_output(
                repo_dir, "log", "-z", "--reverse", f"--format={_LOG_FORMAT}",
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
    """Map each commit to its original PR number via a tiered resolution.

    Tiers (in priority order): ## Applied table, -x cherry-pick trailer,
    subject trailing (#N), commit->PR API. First commit seen per PR wins.
    Returns (pr_to_sha, unresolved, cherry_pick_suspects, collided).
    """
    pr_to_sha: dict[int, str] = {}
    # Winner subject for collision detection (not stored for Applied-table claims).
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
                # Trailer SHAs existed but none resolved; mark as unconfirmed.
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
                # Detect distinct-subject collisions on the same (#N).
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


def _same_change_subject(a: str, b: str) -> bool:
    """True if two commit subjects describe the same change (dedup is a correct collapse)."""
    na, nb = _norm_title(_title_core(a)), _norm_title(_title_core(b))
    if not na or not nb:
        return False
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= _TITLE_SIMILARITY_MIN


def _pr_from_cherry_pick_trailer(repo: Any, body: str) -> int | None:
    """Return the original PR from a -x cherry-pick trailer, or None.

    Tries source SHAs oldest-hop-first; the first that resolves to a PR is the
    original. Returns None when no trailer exists or no source SHA resolves.
    """
    source_shas = cherry_pick_source_shas(body)
    for source_sha in source_shas:  # oldest hop (the original) first
        number = _pr_from_commit_api(repo, source_sha)
        if number is not None:
            return number
    return None


def _pr_from_commit_api(repo: Any, sha: str) -> int | None:
    """Return the first PR number associated with sha via Commit.get_pulls(), or None."""
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


# Cap backport-of-backport depth to prevent loops from cyclic metadata.
_MAX_BACKPORT_DEPTH = 2


def _fetch_pull(repo: Any, number: int, cache: dict[int, Any]) -> Any:
    """Fetch and cache PR number, returning None on 404. Non-404 errors propagate."""
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


# Floors for title-match validation: reject titles too short/generic to be evidence.
_MIN_DISTINCTIVE_TITLE_CHARS = 15
_MIN_DISTINCTIVE_TITLE_WORDS = 3
# Normalized chore titles that are never distinctive.
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
# Automated prefixes whose stem recurs across unrelated PRs.
_GENERIC_TITLE_PREFIX_RE = re.compile(
    r"^(?:bump\b|build\(deps\)|revert\b|merge\b)", re.IGNORECASE
)
# Similarity threshold for title matching (tolerates minor post-merge retitles).
_TITLE_SIMILARITY_MIN = 0.90


def _norm_title(title: Any) -> str:
    """Normalize a title for comparison: collapse whitespace and casefold."""
    if not isinstance(title, str):
        return ""
    return re.sub(r"\s+", " ", title).strip().casefold()


def _is_distinctive_title(normalized: str) -> bool:
    """True if the normalized title is specific enough to serve as match evidence."""
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
    """Strip [Backport ...] prefix to get the underlying source title."""
    if not isinstance(title, str):
        return ""
    stripped = source_title_from_backport_title(title)
    return stripped if stripped is not None else title


def _expected_source_titles(backport_pull: Any) -> set[str]:
    """Return normalized distinctive source-title cores embedded in backport_pull.

    Extracts from the [Backport ...] prefix and ## Backport Summary Source title row.
    Empty set means nothing distinctive to cross-check (fail-closed).
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
    """True if actual title matches any in expected (exact or >= 0.90 similarity)."""
    a = _norm_title(_title_core(actual))
    if not a:
        return False
    for e in expected:
        if e == a or difflib.SequenceMatcher(None, e, a).ratio() >= _TITLE_SIMILARITY_MIN:
            return True
    return False


def _source_is_trusted(src_pull: Any, backport_pull: Any) -> bool:
    """True if src_pull is a credible original for backport_pull.

    Requires: src_pull is merged, and its title matches a distinctive source title
    the backport embeds. Returns False (fail-closed) when no title to compare.
    """
    if not (getattr(src_pull, "merged", None) or getattr(src_pull, "merged_at", None) is not None):
        return False
    expected = _expected_source_titles(backport_pull)
    if not expected:
        return False
    return _titles_consistent(expected, src_pull.title)


def _recover_source_pr(repo: Any, pull: Any) -> int | None:
    """Return the original PR of a per-PR backport, or None.

    Tries: ## Backport Summary Source PR row, PR's own commits' trailing (#N),
    backport/<n>-to-<branch> head branch. Returns None when unrecoverable.
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
        # Take the sole number when unambiguous.
        if len(numbers) == 1:
            return next(iter(numbers))
    except Exception as exc:  # noqa: BLE001 - a lookup miss must not abort discovery
        logger.warning("Could not read commits of PR #%s: %s", pull.number, exc)
    head_ref = getattr(getattr(pull, "head", None), "ref", "") or ""
    return source_pr_from_branch(head_ref)


def _is_backport_pull(pull: Any) -> bool:
    """True if pull is a backport (by title, label, summary table, or branch name)."""
    title = pull.title or ""
    labels = tuple(label.name for label in pull.labels)
    if is_backport_title(title) or "backport" in labels:
        return True
    if summary_source_pr_from_body(pull.body or "") is not None:
        return True
    head_ref = getattr(getattr(pull, "head", None), "ref", "") or ""
    return source_pr_from_branch(head_ref) is not None


def _build_merged_pr(pull: Any, number: int, merge_commit_sha: str) -> MergedPR:
    """Build a MergedPR from pull using the given number and merge_commit_sha.

    number is the authoritative identity (possibly remapped from a backport).
    merge_commit_sha is this line's commit, not the source's original merge.
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
    """Fetch PR metadata and remap per-PR backports to their original source.

    Returns (prs, unresolved_backports, unresolved_prs). For backport PRs, walks
    to the original via _recover_source_pr and validates with _source_is_trusted.
    Deduplicates by final PR number (first-seen wins).
    """
    pull_cache: dict[int, Any] = {}
    final: dict[int, MergedPR] = {}
    unresolved_backports: list[UnresolvedBackport] = []
    unresolved_prs: list[UnresolvedPR] = []
    for number in sorted(pr_to_sha):
        sha = pr_to_sha[number]
        pull = _fetch_pull(repo, number, pull_cache)
        if pull is None:
            # PR not fetchable; record as unresolved so the shipped change is visible.
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
            while source is not None and source not in visited and depth < _MAX_BACKPORT_DEPTH:
                src_pull = _fetch_pull(repo, source, pull_cache)
                if src_pull is None:
                    break
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
        if target_number == number:
            merge_sha = target_pull.merge_commit_sha or sha
        else:
            merge_sha = sha
        final[target_number] = _build_merged_pr(target_pull, target_number, merge_sha)
        # Still a backport after recovery: flag for maintainer review.
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
    """Return a resolvable ref for base_ref, falling back to origin/<name> if needed."""
    try:
        git_output(repo_dir, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}")
        return base_ref
    except subprocess.CalledProcessError:
        remote = f"origin/{base_ref}"
        try:
            git_output(repo_dir, "rev-parse", "--verify", "--quiet", f"{remote}^{{commit}}")
        except subprocess.CalledProcessError as exc:
            raise ValueError(
                f"base ref {base_ref!r} resolves neither as given nor as {remote!r}"
            ) from exc
        logger.info("Base ref %r resolved via remote-tracking ref %r", base_ref, remote)
        return remote


def discover(
    repo: Any, repo_dir: str, head_ref: str, *,
    tag_glob: str | None = None, base_ref: str | None = None,
) -> DiscoveryResult:
    """Resolve the release range and return a deduplicated DiscoveryResult.

    When base_ref is set, uses it directly; otherwise resolves via the highest
    reachable tag (filtered by tag_glob). repo_dir must be a full-depth clone.
    """
    if base_ref:
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
    """Keep only cherry-pick suspects not already flagged by another mechanism.

    Drops suspects that were remapped, flagged as unresolved backports, or 404'd.
    What remains is a PR credited under its own number with no other signal that
    its origin is unconfirmed.
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
