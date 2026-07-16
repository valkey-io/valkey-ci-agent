"""Build a deduplicated, alpha-sorted contributor list for a release range.

Uses the GitHub compare API for logins (with git-shortlog fallback) and unions
in Co-authored-by trailers to credit authors collapsed by squash merges.
Stdlib only (urllib), no third-party dependencies.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import urllib.error
import urllib.request
from typing import List, Optional

logger = logging.getLogger(__name__)

_API_ROOT = "https://api.github.com"
_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
_API_RETRIES = 3

# Captures the display name from a Co-authored-by value: "Name <email>".
_COAUTHOR_VALUE_RE = re.compile(r"^(.+?)[ \t]*<[^>]*>[ \t]*$")
# Commit boundary in the -z git-log stream.
_NUL = "\x00"


def _is_bot(identity: str) -> bool:
    """True if *identity* is a bot account (ends in [bot])."""
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


def _api_get_with_retry(url: str, token: Optional[str], label: str) -> object:
    """Call _api_get with retries on transient HTTP errors (429/5xx)."""
    for attempt in range(_API_RETRIES):
        try:
            return _api_get(url, token)
        except urllib.error.HTTPError as exc:
            if exc.code in _RETRYABLE_HTTP_CODES and attempt < _API_RETRIES - 1:
                delay = min(8.0, 1.0 * (2 ** attempt))
                logger.warning(
                    "Retrying %s after %.1fs (HTTP %d)", label, delay, exc.code,
                )
                time.sleep(delay)
                continue
            raise
    raise RuntimeError("unreachable")


def _compare_logins(
    repo: str, base_ref: str, head_ref: str, token: Optional[str]
) -> "tuple[List[str], bool, List[str]]":
    """Return ``(logins, truncated, git_names)`` via the GitHub compare API.

    ``truncated`` is True when the API's 250-commit cap was hit, signaling the
    caller to supplement from git-shortlog.
    """
    logins: List[str] = []
    git_names: List[str] = []
    seen = set()
    seen_git_names: "set[str]" = set()
    page = 1
    per_page = 100  # GitHub max per_page
    max_pages = 5  # compare endpoint caps at 250 commits total
    seen_commits = 0
    total_commits = None
    while page <= max_pages:
        url = "{}/repos/{}/compare/{}...{}?per_page={}&page={}".format(
            _API_ROOT, repo, base_ref, head_ref, per_page, page
        )
        data = _api_get_with_retry(url, token, "compare page {}".format(page))
        if not isinstance(data, dict):
            break
        if total_commits is None and isinstance(data.get("total_commits"), int):
            total_commits = data["total_commits"]
        commits = data.get("commits")
        # Guard against malformed payloads.
        if not isinstance(commits, list):
            break
        seen_commits += len(commits)
        for commit in commits:
            if not isinstance(commit, dict):
                continue
            author = commit.get("author") or {}
            login = author.get("login") if isinstance(author, dict) else None
            # Collect git author name for dedup against shortlog.
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
        data = _api_get_with_retry(
            "{}/users/{}".format(_API_ROOT, repo_login),
            token,
            "/users/{}".format(repo_login),
        )
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError):
        return None
    if isinstance(data, dict):
        name = data.get("name")
        if name and name.strip():
            return name.strip()
    return None


def _git_shortlog_names(base_ref: str, head_ref: str, repo_dir: str) -> List[str]:
    """Author names from ``git shortlog -sn`` over the range. Bots filtered out."""
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
    """Collect display names from Co-authored-by trailers in the range (offline).

    Uses git's trailer parser so body-prose mentions are not misread as trailers.
    Returns names only (no handles), deduplicated case-insensitively.
    """
    try:
        out = subprocess.run(
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
    for record in out.split(_NUL):
        for line in record.splitlines():
            line = line.strip()
            if not line:
                continue
            m = _COAUTHOR_VALUE_RE.match(line)
            name = m.group(1).strip() if m else line
            if name and not _is_bot(name) and name.casefold() not in seen:
                seen.add(name.casefold())
                names.append(name)
    return names


def _sort_key(entry: str) -> str:
    """Case-insensitive sort key on the display name (strips trailing @handle)."""
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

    Falls back to name-only entries from git-shortlog when the API is unavailable.
    """
    truncated = False
    git_names: List[str] = []
    try:
        logins, truncated, git_names = _compare_logins(repo, base_ref, head_ref, token)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError):
        logins = []

    entries: List[str] = []
    if logins:
        for login in logins:
            name = _display_name(login, token) or login
            entries.append("{} @{}".format(name, login))
    else:
        seen = set()
        for name in _git_shortlog_names(base_ref, head_ref, repo_dir):
            if name not in seen:
                seen.add(name)
                entries.append(name)

    have = {_sort_key(e) for e in entries}
    # Seed git author names so shortlog dedup works when display name differs.
    for gn in git_names:
        have.add(gn.casefold())

    # Supplement from shortlog when the API's 250-commit cap was hit.
    if truncated:
        for name in _git_shortlog_names(base_ref, head_ref, repo_dir):
            if name.casefold() not in have:
                have.add(name.casefold())
                entries.append(name)

    # Union in co-authors invisible to both the compare API and shortlog.
    for name in _coauthors_in_range(base_ref, head_ref, repo_dir):
        if name.casefold() not in have:
            have.add(name.casefold())
            entries.append(name)

    entries.sort(key=_sort_key)
    return entries
