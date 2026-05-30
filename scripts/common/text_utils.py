"""Shared text processing utilities."""

from __future__ import annotations

import re

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Remove ANSI SGR color codes from text."""
    return _ANSI_ESCAPE_RE.sub("", text)
