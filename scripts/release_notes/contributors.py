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
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

_API_ROOT = "https://api.github.com"
_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
_API_RETRIES = 3

# Captures identity fields from a Co-authored-by value: "Name <email>".
_COAUTHOR_VALUE_RE = re.compile(r"^(.+?)[ \t]*<([^>]*)>[ \t]*$")
# Commit boundary in the -z git-log stream.
_NUL = "\x00"


@dataclass(frozen=True)
class _CoauthorIdentity:
    name: str
    email: str = ""


@dataclass(frozen=True)
class _ResolvedIdentity:
    name: str
    aliases: frozenset[str]


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
            if not isinstance(login, str) or not login or _is_bot(login):
                continue
            login_key = login.casefold()
            if login_key in seen:
                continue
            seen.add(login_key)
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


def _identity_aliases(name: str, email: str = "") -> set[str]:
    """Return conservative normalized keys linking names, handles, and emails."""

    def normalize(value: str) -> str:
        return "".join(char for char in value.casefold() if char.isalnum())

    aliases = {key for key in (normalize(name),) if key}
    local = email.partition("@")[0]
    # A meaningful email local-part often bridges a login-shaped trailer name and
    # a human display name. Ignore very short locals to limit accidental matches.
    local_key = normalize(local)
    if len(local_key) >= 5:
        aliases.add(local_key)
    if "+" in local:
        suffix_key = normalize(local.rsplit("+", 1)[1])
        if len(suffix_key) >= 5:
            aliases.add(suffix_key)
    return aliases


def _coauthor_identities_in_range(
    base_ref: str, head_ref: str, repo_dir: str
) -> List[_CoauthorIdentity]:
    """Collect identities from Co-authored-by trailers in the range (offline).

    Uses git's trailer parser so body-prose mentions are not misread as trailers.
    Exact duplicate name/email pairs are removed while preserving first-seen order.
    Email is retained so callers can reconcile alternate names for one person.
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
    identities: List[_CoauthorIdentity] = []
    seen: set[tuple[str, str]] = set()
    for record in out.split(_NUL):
        for line in record.splitlines():
            line = line.strip()
            if not line:
                continue
            m = _COAUTHOR_VALUE_RE.match(line)
            name = m.group(1).strip() if m else line
            email = m.group(2).strip() if m else ""
            key = (name.casefold(), email.casefold())
            if name and not _is_bot(name) and key not in seen:
                seen.add(key)
                identities.append(_CoauthorIdentity(name=name, email=email))
    return identities


def _resolve_coauthor_aliases(
    identities: List[_CoauthorIdentity],
) -> List[_ResolvedIdentity]:
    """Join transitive alias matches and retain the clearest display name."""
    groups: List[_ResolvedIdentity] = []
    for identity in identities:
        aliases = _identity_aliases(identity.name, identity.email)
        matches = [
            index
            for index, group in enumerate(groups)
            if not aliases.isdisjoint(group.aliases)
        ]
        if not matches:
            groups.append(
                _ResolvedIdentity(identity.name, frozenset(aliases))
            )
            continue

        first = matches[0]
        names = [groups[index].name for index in matches]
        names.append(identity.name)
        # A spaced, capitalized display name is clearer than a login-shaped
        # trailer value. Stable max keeps the first name when quality ties.
        preferred = max(
            names,
            key=lambda name: (
                any(char.isspace() for char in name),
                any(char.isupper() for char in name)
                and any(char.islower() for char in name),
            ),
        )
        merged_aliases = set(aliases)
        for index in matches:
            merged_aliases.update(groups[index].aliases)
        for index in reversed(matches):
            del groups[index]
        groups.insert(
            first,
            _ResolvedIdentity(preferred, frozenset(merged_aliases)),
        )
    return groups


def _coauthors_in_range(base_ref: str, head_ref: str, repo_dir: str) -> List[str]:
    """Return co-author display names, deduplicated by resolved identity aliases."""
    return [
        group.name
        for group in _resolve_coauthor_aliases(
            _coauthor_identities_in_range(base_ref, head_ref, repo_dir)
        )
    ]


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
    pr_logins: Optional[List[str]] = None,
) -> List[str]:
    """Return alpha-sorted ``"Full Name @handle"`` strings for the commit range.

    Falls back to name-only entries from git-shortlog when the API is unavailable.
    *pr_logins* are GitHub logins resolved from PR metadata, giving contributors
    inside squash-merged commits proper @handles instead of bare trailer names.
    """
    truncated = False
    git_names: List[str] = []
    try:
        logins, truncated, git_names = _compare_logins(repo, base_ref, head_ref, token)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError):
        logins = []

    entries: List[str] = []
    have: set[str] = set()
    if logins:
        for login in logins:
            name = _display_name(login, token) or login
            entries.append("{} @{}".format(name, login))
            have.update(_identity_aliases(name))
            have.update(_identity_aliases(login))
    else:
        for name in _git_shortlog_names(base_ref, head_ref, repo_dir):
            aliases = _identity_aliases(name)
            if aliases.isdisjoint(have):
                entries.append(name)
            have.update(aliases)

    # Seed git author names so shortlog dedup works when display name differs.
    for gn in git_names:
        have.update(_identity_aliases(gn))

    # PR-resolved logins: resolve to Name @handle, upgrading any existing
    # bare-name entry from shortlog that matches.
    if pr_logins:
        for login in pr_logins:
            if _is_bot(login):
                continue
            login_aliases = _identity_aliases(login)
            name = _display_name(login, token) or login
            name_aliases = _identity_aliases(name)
            combined = login_aliases | name_aliases
            if combined.isdisjoint(have):
                entries.append("{} @{}".format(name, login))
            else:
                for i, entry in enumerate(entries):
                    if " @" in entry:
                        continue
                    entry_aliases = _identity_aliases(entry)
                    if not entry_aliases.isdisjoint(combined):
                        entries[i] = "{} @{}".format(name, login)
                        break
            have.update(combined)

    # Supplement from shortlog when the API's 250-commit cap was hit.
    if truncated:
        for name in _git_shortlog_names(base_ref, head_ref, repo_dir):
            aliases = _identity_aliases(name)
            if aliases.isdisjoint(have):
                entries.append(name)
            have.update(aliases)

    # Union in co-authors invisible to both the compare API and shortlog.
    coauthors = _resolve_coauthor_aliases(
        _coauthor_identities_in_range(base_ref, head_ref, repo_dir)
    )
    for identity in coauthors:
        if identity.aliases.isdisjoint(have):
            entries.append(identity.name)
        have.update(identity.aliases)

    entries.sort(key=_sort_key)
    return entries
