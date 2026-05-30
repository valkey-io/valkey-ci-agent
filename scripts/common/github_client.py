"""Shared GitHub API retry helpers."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

from github.GithubException import GithubException

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
# HTTP statuses worth retrying. Shared with raw-urllib callers
# (workflow_artifacts) that can't use retry_github_call directly.
RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
_BASE_DELAY_SECONDS = 1.0
_MAX_DELAY_SECONDS = 8.0

# Substrings in 403 response messages that indicate rate limiting
# (as opposed to permanent permission errors).
_RATE_LIMIT_403_INDICATORS = (
    "rate limit",
    "abuse detection",
    "retry after",
    "secondary rate",
    "exceeded a secondary",
)


def _is_retryable_error(exc: Exception) -> bool:
    if not isinstance(exc, GithubException):
        return False
    if exc.status in RETRYABLE_HTTP_STATUS:
        return True
    # Only retry 403 when it's clearly rate-limiting, not a permission error.
    if exc.status == 403:
        msg = str(exc).lower()
        return any(indicator in msg for indicator in _RATE_LIMIT_403_INDICATORS)
    return False


def transient_backoff_delay(attempt: int) -> float:
    """Jittered, capped exponential backoff for transient-failure retries."""
    return random.uniform(0, min(_MAX_DELAY_SECONDS, _BASE_DELAY_SECONDS * (2 ** attempt)))


def retry_github_call(
    operation: Callable[[], _T],
    *,
    retries: int,
    description: str,
) -> _T:
    """Retry transient GitHub API failures with exponential backoff."""
    for attempt in range(retries):
        try:
            return operation()
        except Exception as exc:
            if not _is_retryable_error(exc) or attempt == retries - 1:
                raise
            wait_seconds = transient_backoff_delay(attempt)
            logger.warning(
                "Retrying GitHub API call for %s after %.2fs: %s",
                description,
                wait_seconds,
                exc,
            )
            time.sleep(wait_seconds)
    raise RuntimeError("unreachable: retry loop exited without returning or raising")
