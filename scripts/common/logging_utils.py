"""Small helpers for high-signal, user-controlled log context."""

from __future__ import annotations

import logging

from scripts.common.text_utils import strip_ansi

LOG_HIGHLIGHT_RULE = "*" * 88


def compact_log_value(
    value: object,
    *,
    fallback: str = "(untitled)",
    limit: int = 200,
) -> str:
    """Collapse untrusted text into one bounded, terminal-safe log value."""
    compact = " ".join(strip_ansi(str(value or "")).split())
    if not compact:
        return fallback
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def log_highlight(
    target_logger: logging.Logger,
    message: str,
    *args: object,
) -> None:
    """Surround one important log record with an easy-to-scan rule."""
    target_logger.info(LOG_HIGHLIGHT_RULE)
    target_logger.info(message, *args)
    target_logger.info(LOG_HIGHLIGHT_RULE)
