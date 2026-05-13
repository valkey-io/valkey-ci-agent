"""Block accidental writes to upstream repositories.

Must be configured via configure_publish_guard() before use.
Fails closed: if not configured, all publish checks raise.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}

_configured: bool = False
_protected_repos: set[str] = set()


def configure_publish_guard(protected_repos: Iterable[str]) -> None:
    """Configure the publish guard with the set of protected repos.

    Must be called at startup before any check_publish_allowed() calls.
    """
    global _configured, _protected_repos
    _protected_repos = set(protected_repos)
    _configured = True
    logger.debug("Publish guard configured with %d protected repo(s)", len(_protected_repos))


def _env_true(name: str) -> bool:
    return (os.environ.get(name, "") or "").strip().lower() in _TRUTHY


def check_publish_allowed(
    target_repo: str,
    *,
    action: str = "write",
    context: str = "",
) -> None:
    """Check if publishing to target_repo is allowed.

    Raises RuntimeError if:
    - publish_guard has not been configured (fail closed)
    - target_repo is protected and the override env var is not set
    """
    if not _configured:
        raise RuntimeError(
            "publish_guard not configured — call configure_publish_guard() at startup"
        )
    if target_repo in _protected_repos and not _env_true(
        "VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH"
    ):
        raise RuntimeError(
            f"Blocked {action} on {target_repo}: upstream publishing requires "
            "VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH=1"
            + (f" ({context})" if context else "")
        )
    logger.debug("Publish guard OK: %s on %s (%s)", action, target_repo, context)
