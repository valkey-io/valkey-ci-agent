"""Open or update a GitHub PR for the release cut.

The release cut pushes its promoted commit to an agent-namespaced prep branch
(see :mod:`release_cut`) and opens a PR from it into the release line. This
module owns only the PR-side primitives (finding an existing open PR for a
branch and creating/updating it) plus a small Markdown-table escape helper for
the triage list embedded in the PR body. The branch push discipline lives in
:mod:`release_cut`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from scripts.backport.pr_creator import build_pull_create_head_ref, build_pull_search_head_ref
from scripts.common.github_client import retry_github_call

logger = logging.getLogger(__name__)


def find_existing_pr(repo: Any, *, base_repo: str, push_repo: str | None, branch: str) -> Any | None:
    """Return the open PR whose head is *branch*, or None."""
    head_ref = build_pull_search_head_ref(base_repo, push_repo, branch)
    pulls = retry_github_call(
        lambda: list(repo.get_pulls(state="open", head=head_ref)),
        retries=3, description=f"search open PR for {head_ref}",
    )
    return pulls[0] if pulls else None


def open_or_update_pr(
    repo: Any,
    *,
    base_repo: str,
    push_repo: str | None,
    branch: str,
    base_branch: str,
    title: str,
    body: str,
    existing: Any | None,
) -> str:
    """Update *existing* PR in place, or create a new one. Returns the PR URL."""
    if existing is not None:
        retry_github_call(
            lambda: existing.edit(title=title, body=body),
            retries=3, description=f"update PR #{existing.number}",
        )
        logger.info("Updated release PR #%s", existing.number)
        return existing.html_url
    head_ref = build_pull_create_head_ref(base_repo, push_repo, branch)
    pr = retry_github_call(
        lambda: repo.create_pull(title=title, body=body, head=head_ref, base=base_branch, draft=False),
        retries=3, description="create release PR",
    )
    logger.info("Opened release PR #%s", pr.number)
    return pr.html_url


_LINEBREAK_RE = re.compile(r"[\r\n]+")


def escape_cell(text: str) -> str:
    """Escape a value for a Markdown table cell (pipes and line breaks).

    Backslashes are escaped before pipes so a pre-existing ``\\`` right before a
    ``|`` cannot consume the pipe's escape: ``a\\|b`` would otherwise become
    ``a\\\\|b`` (a literal backslash followed by an unescaped ``|`` that breaks
    the row); escaping the backslash first yields ``a\\\\\\|b`` (literal ``\\|``).

    Any run of CR/LF (a bare ``\\n``, a Windows ``\\r\\n``, a lone ``\\r``, or
    several in a row) collapses to a single space: a raw ``\\r`` left in the
    string would break the table row, and matching only ``\\n`` leaves the CR of
    a CRLF behind. A contributor PR title is arbitrary text, so it can carry any
    of these.
    """
    escaped = text.replace("\\", "\\\\").replace("|", "\\|")
    return _LINEBREAK_RE.sub(" ", escaped).strip()
