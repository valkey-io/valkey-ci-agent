"""Open or update a GitHub PR for the release cut.

Owns PR-side primitives: finding an existing open PR for a branch, creating or
updating it, and a Markdown-table escape helper for the triage list in the PR body.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from scripts.backport.pr_creator import build_pull_create_head_ref, build_pull_search_head_ref
from scripts.common.github_client import retry_github_call

logger = logging.getLogger(__name__)


def find_existing_pr(
    repo: Any, *, base_repo: str, push_repo: str | None, branch: str, base_branch: str
) -> Any | None:
    """Return the open PR whose head is *branch* and base is *base_branch*, or None."""
    head_ref = build_pull_search_head_ref(base_repo, push_repo, branch)
    pulls = retry_github_call(
        lambda: list(repo.get_pulls(state="open", head=head_ref, base=base_branch)),
        retries=3, description=f"search open PR for {head_ref} into {base_branch}",
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
    draft: bool = False,
) -> str:
    """Update *existing* PR in place, or create a new one. Returns the PR URL.

    When *draft* is True the PR is held until a maintainer marks it ready.
    On updates the draft state is reconciled to match the current cut.
    """
    if existing is not None:
        retry_github_call(
            lambda: existing.edit(title=title, body=body),
            retries=3, description=f"update PR #{existing.number}",
        )
        reconcile_draft(existing, draft)
        logger.info("Updated release PR #%s (draft=%s)", existing.number, draft)
        return existing.html_url
    head_ref = build_pull_create_head_ref(base_repo, push_repo, branch)
    pr = retry_github_call(
        lambda: repo.create_pull(title=title, body=body, head=head_ref, base=base_branch, draft=draft),
        retries=3, description="create release PR",
    )
    logger.info("Opened release PR #%s (draft=%s)", pr.number, draft)
    return pr.html_url


def reconcile_draft(existing: Any, draft: bool) -> None:
    """Flip *existing*'s draft state to *draft* if it differs."""
    if bool(existing.draft) == draft:
        return
    if draft:
        retry_github_call(
            lambda: existing.convert_to_draft(),
            retries=3, description=f"convert PR #{existing.number} to draft",
        )
    else:
        retry_github_call(
            lambda: existing.mark_ready_for_review(),
            retries=3, description=f"mark PR #{existing.number} ready",
        )


_LINEBREAK_RE = re.compile(r"[\r\n]+")


def escape_cell(text: str) -> str:
    """Escape pipes and line breaks for a markdown table cell."""
    escaped = text.replace("\\", "\\\\").replace("|", "\\|")
    return _LINEBREAK_RE.sub(" ", escaped).strip()
