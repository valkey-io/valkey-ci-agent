"""Shared text processing utilities."""

from __future__ import annotations

import re

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Remove ANSI SGR color codes from text."""
    return _ANSI_ESCAPE_RE.sub("", text)


def compact_text(value: str, limit: int) -> str:
    """Collapse whitespace and truncate one human-facing text field."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


# What GitHub acts on in a comment, a pull request's description or a commit
# message: a Markdown link or image, a URL, a mention, an HTML tag, and a
# closing keyword before an issue reference.
_LINK_RE = re.compile(r"!?\[([^\]\n]*)\]\(([^)\s]+)[^)\n]*\)")
_ACTIVE_RE = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*:)(?=//)"
    r"|(?P<www>\bwww)(?=\.)"
    r"|(?<![A-Za-z0-9])(?P<at>@)(?=[A-Za-z0-9])"
    r"|(?P<tag><)(?=[A-Za-z/])"
    r"|(?P<closes>\b(?:clos(?:e[sd]?|ing)|fix(?:e[sd]|ing)?|resolv(?:e[sd]?|ing)):?\s+(?:(?:[\w.-]+/[\w.-]+)?#|GH-))(?=\d)",
    re.IGNORECASE,
)
# Invisible, and enough to keep GitHub from reading what follows as part of the same token.
_BREAK = "\u200b"


def defused(text: str) -> str:
    """``text`` with nothing in it that GitHub acts on.

    For text an agent wrote from input anyone can write, such as an issue.
    Published as it is, a mention notifies someone, a link leads anywhere,
    and "fixes #12" in a pull request's description or a commit message
    closes that issue on merge. A zero-width space after the ``@``, the
    URL's scheme, a tag's ``<`` or the reference's ``#`` leaves the text
    reading the same and GitHub with nothing to act on. No markup is added,
    so none can be played against the text's own: this holds inside a code
    span and outside one, wherever GitHub thinks the span ends.
    """
    text = _LINK_RE.sub(r"\1 (\2)", text)
    return _ACTIVE_RE.sub(lambda match: match[0] + _BREAK, text)


def undefused(text: str) -> str:
    """``text`` as it was before :func:`defused`, for where it is shown as code that nothing acts on."""
    return text.replace(_BREAK, "")
