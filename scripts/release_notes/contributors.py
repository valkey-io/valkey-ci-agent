"""Generate the deduplicated, alpha-sorted contributor list for a release.

Collects the GitHub authors of every commit in a ``base..head`` range and
returns them as ``Full Name @handle`` strings, sorted by display name (the
``* `` bullet prefix is added downstream by ``render_contributors_footer``). The
commit range and author logins come from the GitHub compare API; each unique
login is then resolved to a display name via the users API. When the API is
unavailable (no token / offline), it falls back to ``git shortlog`` over the
same range for names only. Either way, ``Co-authored-by`` trailers are read from
the commit bodies (offline) and unioned in: a squash-merge, most notably a
backport sweep whose sole commit author is the bot, records its real human
authors only in those trailers, invisible to both the compare API and shortlog.

Stdlib only (urllib) so it runs in a minimal environment with no third-party
dependencies. Upstream ``valkey-io/valkey`` ships no equivalent tool;
:mod:`release_cut` calls :func:`list_contributors` directly.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import urllib.error
import urllib.request
from typing import List, Optional

logger = logging.getLogger(__name__)

_API_ROOT = "https://api.github.com"

# The value of a ``Co-authored-by`` trailer, ``Display Name <email>``, capturing
# the display name (everything before the ``<email>``). Applied to the values
# git's own trailer parser extracts (``--format=%(trailers:...valueonly)``), so
# the ``Co-authored-by:`` key is already stripped. A squash-merge appends one such
# trailer per co-author, so this is the only offline signal for the humans a
# squash collapsed out of the single commit author, most importantly the
# source-PR authors of a squash-merged backport sweep whose sole commit author is
# the bot.
_COAUTHOR_VALUE_RE = re.compile(r"^(.+?)[ \t]*<[^>]*>[ \t]*$")
# Commit boundary in the ``-z`` git-log stream (a NUL after each record).
_NUL = "\x00"


def _is_bot(identity: str) -> bool:
    """Return ``True`` for a machine account, which must never be credited.

    GitHub's App/bot convention is a ``[bot]`` login suffix (``dependabot[bot]``,
    ``github-actions[bot]``, this repo's own ``valkey-ci-agent[bot]``). The commit
    *author* name of a bot carries the same suffix, so this one predicate screens
    all three identity sources (compare-API logins, ``git shortlog`` author names,
    and ``Co-authored-by`` display names) with no per-path special case. Matched on
    the trailing ``[bot]`` (case-insensitive, whitespace-tolerant), so a human
    whose name merely contains the word "bot" is not caught.
    """
    return identity.strip().casefold().endswith("[bot]")


def _api_get(url: str, token: Optional[str]) -> object:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "valkey-release-tools",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer {}".format(token)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted host)
        return json.loads(resp.read().decode("utf-8"))


def _compare_logins(
    repo: str, base_ref: str, head_ref: str, token: Optional[str]
) -> "tuple[List[str], bool, List[str]]":
    """Return ``(logins, truncated, git_names)`` for commits in ``base..head``.

    ``logins`` is the unique, bot-filtered author logins; ``truncated`` is True
    when the API reported more commits than it returned. ``git_names`` is the
    unique git-level author names (``commit.commit.author.name``) from the same
    commits, used to dedup the shortlog supplement against API-covered authors
    whose profile display name differs from their git author name.

    The compare endpoint paginates commits; we walk pages until fewer than the
    page size are returned. It returns at most 250 commits total
    (``total_commits`` can exceed that), so a range wider than 250 commits
    credits only the first 250. We log a warning when that cap is hit and return
    ``truncated=True`` so the caller can supplement the tail from ``git
    shortlog`` rather than shipping a contributor list that silently drops
    authors on a wide GA cut.
    """
    logins: List[str] = []
    git_names: List[str] = []
    seen = set()
    seen_git_names: "set[str]" = set()
    page = 1
    # GitHub caps per_page at 100 and clamps larger values down. A higher number
    # would make the "short page" termination check below fire after page 1 and
    # drop authors past the first 100 commits.
    per_page = 100
    # The compare endpoint returns at most 250 commits, so 3 pages of 100 exhausts
    # it. A small ceiling stops an endpoint/proxy that ignores `page` and returns a
    # full page forever from looping unbounded; the "short page" check ends it
    # first in every normal case.
    max_pages = 5
    seen_commits = 0
    total_commits = None
    while page <= max_pages:
        url = "{}/repos/{}/compare/{}...{}?per_page={}&page={}".format(
            _API_ROOT, repo, base_ref, head_ref, per_page, page
        )
        data = _api_get(url, token)
        if not isinstance(data, dict):
            break
        if total_commits is None and isinstance(data.get("total_commits"), int):
            total_commits = data["total_commits"]
        commits = data.get("commits")
        # A well-formed payload lists commits; anything else (a scalar, a dict, or a
        # non-dict entry) must not be iterated into an AttributeError that aborts the
        # cut. Treat a non-list as an empty page and skip non-dict entries.
        if not isinstance(commits, list):
            break
        seen_commits += len(commits)
        for commit in commits:
            if not isinstance(commit, dict):
                continue
            author = commit.get("author") or {}
            login = author.get("login") if isinstance(author, dict) else None
            # Collect the git-level author name (commit.commit.author.name) for
            # dedup: shortlog produces this value, which may differ from the
            # profile display name the API path resolves the login to. Recording
            # it lets the shortlog supplement match API-covered authors even when
            # their display name diverges from their git config user.name.
            inner = commit.get("commit")
            if isinstance(inner, dict):
                git_author = inner.get("author")
                if isinstance(git_author, dict):
                    git_name = git_author.get("name")
                    if isinstance(git_name, str) and git_name.strip():
                        key = git_name.strip().casefold()
                        if key not in seen_git_names and not _is_bot(git_name):
                            seen_git_names.add(key)
                            git_names.append(git_name.strip())
            # A non-string login (a malformed payload) would raise on .endswith and
            # abort the cut; treat anything but a non-empty str as no login.
            if not isinstance(login, str) or not login or login in seen or _is_bot(login):
                continue
            seen.add(login)
            logins.append(login)
        if len(commits) < per_page:
            break
        page += 1
    truncated = total_commits is not None and seen_commits < total_commits
    if truncated:
        logger.warning(
            "Contributor range %s..%s spans %d commits but the compare API "
            "returned only %d; supplementing the tail from git shortlog.",
            base_ref, head_ref, total_commits, seen_commits,
        )
    return logins, truncated, git_names


def _display_name(repo_login: str, token: Optional[str]) -> Optional[str]:
    """Resolve a login to its profile full name, or None if unavailable."""
    try:
        data = _api_get("{}/users/{}".format(_API_ROOT, repo_login), token)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError):
        # OSError covers socket read timeouts, which are not URLError subclasses.
        return None
    if isinstance(data, dict):
        name = data.get("name")
        if name and name.strip():
            return name.strip()
    return None


def _git_shortlog_names(base_ref: str, head_ref: str, repo_dir: str) -> List[str]:
    """Fallback: author names from ``git shortlog -sn base..head`` (no handles).

    Bot authors are skipped, matching the compare-API path: shortlog credits the
    commit author, so a range whose only commits are a bot's (e.g. an offline cut
    over a backport sweep) would otherwise list the bot.
    """
    try:
        out = subprocess.run(
            ["git", "shortlog", "-sn", "{}..{}".format(base_ref, head_ref)],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    names = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # Format: "<count>\t<name>"
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].strip() and not _is_bot(parts[1]):
            names.append(parts[1].strip())
    return names


def _coauthors_in_range(base_ref: str, head_ref: str, repo_dir: str) -> List[str]:
    """Co-author display names from ``Co-authored-by`` trailers in ``base..head``.

    Reads every commit over the range with ``git log`` (offline, no token) and
    collects the display name of each ``Co-authored-by`` trailer, deduplicated
    case-insensitively and returned in first-seen order.

    Only real trailers are read. The extraction is delegated to git's own
    trailer parser (``--format=%(trailers:key=Co-authored-by,valueonly)``), which
    recognizes only the commit message's terminal trailer block. A
    ``Co-authored-by:``-shaped line quoted in the body prose (a PR description, a
    docs example) is NOT a trailer and must not publish a contributor credit; a
    plain body-wide regex would wrongly match it. ``-z`` separates commits so a
    trailer value that spans context is scoped to its own commit.

    This recovers the humans a squash-merge collapsed out of the single commit
    author. It matters most for a squash-merged backport sweep: its sole commit
    author is the bot (filtered out of the compare/shortlog paths), so without the
    trailers the source-PR authors would be credited nowhere. Names only (no
    ``@handle``): a trailer carries a display name and email, not a GitHub login,
    matching the name-only shape of the ``git shortlog`` fallback that render
    already tolerates. Degrades to ``[]`` on any git failure, like that fallback.
    """
    try:
        out = subprocess.run(
            # --reverse: oldest first, so "first-seen" dedup order is chronological
            # (matches discover.list_range_commits). The final list is alpha-sorted
            # regardless, so this only makes the intermediate order predictable.
            # git parses the terminal trailer block itself; one trailer value per
            # line within a commit, commits separated by NUL (-z).
            ["git", "log", "--reverse", "-z",
             "--format=%(trailers:key=Co-authored-by,valueonly,separator=%x0a)",
             "{}..{}".format(base_ref, head_ref)],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    names: List[str] = []
    seen = set()
    # Each NUL-terminated record holds one commit's Co-authored-by trailer values,
    # one per line; a commit with none contributes an empty record.
    for record in out.split(_NUL):
        for line in record.splitlines():
            line = line.strip()
            if not line:
                continue
            m = _COAUTHOR_VALUE_RE.match(line)
            name = m.group(1).strip() if m else line
            # Skip bot co-authors (a tool can list itself as a co-author) and dedup
            # the rest case-insensitively.
            if name and not _is_bot(name) and name.casefold() not in seen:
                seen.add(name.casefold())
                names.append(name)
    return names


def _sort_key(entry: str) -> str:
    """Case-insensitive sort key on the display name portion.

    Splits on the *last* ``" @"`` so a display name that itself contains ``" @"``
    (e.g. ``"Foo @ Bar @foobar"``) keeps its full name and only the trailing
    ``@handle`` is stripped; splitting on the first ``" @"`` would truncate the
    name and collapse two distinct people onto the same key.
    """
    name = entry.rsplit(" @", 1)[0]
    return name.casefold()


def list_contributors(
    repo: str,
    base_ref: str,
    head_ref: str,
    token: Optional[str] = None,
    *,
    repo_dir: str = ".",
) -> List[str]:
    """Return alpha-sorted ``"Full Name @handle"`` strings for the commit range.

    Falls back to git-shortlog names (no handles) if the compare API yields no
    logins (e.g. no token, network error, or unknown range).
    """
    truncated = False
    git_names: List[str] = []
    try:
        logins, truncated, git_names = _compare_logins(repo, base_ref, head_ref, token)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError):
        # OSError covers socket read timeouts, which are not URLError subclasses;
        # any of these degrade to the git-shortlog fallback below.
        logins = []

    entries: List[str] = []
    if logins:
        for login in logins:
            name = _display_name(login, token) or login
            entries.append("{} @{}".format(name, login))
    else:
        # Fallback path: names only, deduplicated preserving first sight.
        seen = set()
        for name in _git_shortlog_names(base_ref, head_ref, repo_dir):
            if name not in seen:
                seen.add(name)
                entries.append(name)

    have = {_sort_key(e) for e in entries}
    # Also seed with git-level author names from the compare API. Shortlog
    # produces git author names, which may differ from the profile display name
    # the API resolved the login to. Without this, an author whose display name
    # is "Bob Developer" but whose git user.name is "Bob Dev" would pass the
    # display-name dedup and be added a second time by the shortlog supplement.
    for gn in git_names:
        have.add(gn.casefold())

    # The compare API caps at 250 commits, so a wide GA range (a minor GA can
    # span many hundreds) credits only the first 250 logins. Supplement the tail
    # from git shortlog (name-only, no @handle) so authors past the cap are still
    # listed rather than silently dropped. Dedup by display name AND git author
    # name against what the API already credited, so an author present in both
    # paths is not doubled; this only adds the ones the API's window missed.
    if truncated:
        for name in _git_shortlog_names(base_ref, head_ref, repo_dir):
            if name.casefold() not in have:
                have.add(name.casefold())
                entries.append(name)

    # Union in co-authors that neither path above sees. A squash-merge (most
    # notably a backport sweep, whose only commit author is the bot) records its
    # real human authors only as Co-authored-by trailers in the commit body; the
    # compare API and shortlog both key on the single commit author and miss
    # them. Dedup by the display-name portion of existing entries (an author
    # already credited as "Name @handle" is not re-added name-only), so the same
    # person is never listed twice. Added name-only, like the shortlog fallback.
    for name in _coauthors_in_range(base_ref, head_ref, repo_dir):
        if name.casefold() not in have:
            have.add(name.casefold())
            entries.append(name)

    entries.sort(key=_sort_key)
    return entries
