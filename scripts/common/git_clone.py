"""Shallow git-clone helpers.

``shallow_clone_at_sha(repo, dest, sha)`` clones the given ``owner/name``
into ``dest`` at depth 1, optionally fetching and checking out a specific
commit. Validates ``repo`` and ``sha`` shapes to defend against argument
injection. Returns ``True`` on success, ``False`` on any failure.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

_CLONE_TIMEOUT_S = 120
_CHECKOUT_TIMEOUT_S = 30


def shallow_clone_at_sha(repo: str, dest: Path, sha: str | None = None) -> bool:
    """Clone ``repo`` (e.g. ``"valkey-io/valkey"``) into ``dest``.

    If ``sha`` is provided, blobless-clones the full history and checks out
    that exact commit (GitHub refuses fetching an arbitrary non-tip SHA, so a
    shallow clone + fetch cannot reach it). If ``sha`` is None, does a
    ``--depth 1`` clone of the default branch.

    Returns True on success, False on any failure. Failures are logged at
    warning level — callers are expected to keep going (e.g. tell their
    AI subprocess that source is unavailable) rather than abort.

    Inputs are validated to defend against argument injection into git:
    ``repo`` must match ``owner/name``, ``sha`` must match a hex commit hash.
    """
    if not _REPO_RE.fullmatch(repo):
        logger.warning("Refusing to clone unrecognized repo identifier: %r", repo)
        return False
    if sha is not None and not _SHA_RE.fullmatch(sha):
        logger.warning("Refusing to clone %s at non-SHA value: %r", repo, sha)
        return False

    url = f"https://github.com/{repo}.git"
    if sha is None:
        # No specific commit wanted: a shallow clone of the default branch tip
        # is enough and cheapest.
        ok = _run(
            ["git", "clone", "--filter=blob:none", "--depth", "1", url, str(dest)],
            timeout=_CLONE_TIMEOUT_S, desc=f"clone {repo}",
        )
        return ok

    # GitHub's HTTPS protocol refuses `git fetch <commit-sha>` for a commit
    # that is not a ref tip, so a `--depth 1` clone + fetch can't reach an
    # arbitrary tested SHA. Clone the full (but blobless) history instead and
    # check the commit out; blobs are still fetched on demand.
    if not _run(
        ["git", "clone", "--filter=blob:none", url, str(dest)],
        timeout=_CLONE_TIMEOUT_S, desc=f"clone {repo}",
    ):
        return False
    return _run(
        ["git", "checkout", sha],
        cwd=dest, timeout=_CHECKOUT_TIMEOUT_S, desc=f"checkout {sha[:12]} in {repo}",
    )


def _run(args: list[str], *, timeout: int, desc: str, cwd: Path | None = None) -> bool:
    try:
        result = subprocess.run(
            args, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("git %s timed out after %ds", desc, timeout)
        return False
    except OSError as exc:
        logger.warning("git %s failed to start: %s", desc, exc)
        return False
    if result.returncode != 0:
        logger.warning("git %s failed: %s", desc, result.stderr[:200].strip())
        return False
    return True
