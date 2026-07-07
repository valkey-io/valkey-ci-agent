"""Generate the deduplicated, alpha-sorted contributor list for a release.

Collects the GitHub authors of every commit in a ``base..head`` range and
returns them as ``Full Name @handle`` strings, sorted by display name (the
``* `` bullet prefix is added downstream by ``render_contributors_footer``). The
commit range and author logins come from the GitHub compare API; each unique
login is then resolved to a display name via the users API. When the API is
unavailable (no token / offline), it falls back to ``git shortlog`` over the
same range for names only.

Stdlib only (urllib) so it runs in a minimal environment with no third-party
dependencies. Upstream ``valkey-io/valkey`` ships no equivalent tool;
:mod:`release_cut` calls :func:`list_contributors` directly.
"""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.error
import urllib.request
from typing import List, Optional

logger = logging.getLogger(__name__)

_API_ROOT = "https://api.github.com"


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


def _compare_logins(repo: str, base_ref: str, head_ref: str, token: Optional[str]) -> List[str]:
    """Return unique author logins for commits in ``base..head`` (compare API).

    The compare endpoint paginates commits; we walk pages until fewer than the
    page size are returned. Bot authors (login ending in ``[bot]``) are skipped.

    The compare endpoint returns at most 250 commits total (``total_commits`` can
    exceed that), so a range wider than 250 commits credits only the first 250.
    We log a warning when that cap is hit rather than truncate silently, so a
    maintainer knows the contributor list may be short for a very wide GA cut.
    """
    logins: List[str] = []
    seen = set()
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
            # A non-string login (a malformed payload) would raise on .endswith and
            # abort the cut; treat anything but a non-empty str as no login.
            if not isinstance(login, str) or not login or login in seen or login.endswith("[bot]"):
                continue
            seen.add(login)
            logins.append(login)
        if len(commits) < per_page:
            break
        page += 1
    if total_commits is not None and seen_commits < total_commits:
        logger.warning(
            "Contributor range %s..%s spans %d commits but the compare API "
            "returned only %d; contributors beyond that may be missing.",
            base_ref, head_ref, total_commits, seen_commits,
        )
    return logins


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
    """Fallback: author names from ``git shortlog -sn base..head`` (no handles)."""
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
        if len(parts) == 2 and parts[1].strip():
            names.append(parts[1].strip())
    return names


def _sort_key(entry: str) -> str:
    """Case-insensitive sort key on the display name portion."""
    name = entry.split(" @", 1)[0]
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
    try:
        logins = _compare_logins(repo, base_ref, head_ref, token)
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

    entries.sort(key=_sort_key)
    return entries
