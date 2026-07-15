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


def find_existing_pr(
    repo: Any, *, base_repo: str, push_repo: str | None, branch: str, base_branch: str
) -> Any | None:
    """Return the open PR whose head is *branch* and base is *base_branch*, or None.

    The head is already unique per cut (the prep branch is
    ``agent/release-cut/{version}-{stage}``), so scoping on *base_branch* too is
    belt-and-suspenders: it guarantees a reused PR targets the same release line
    even in the unlikely event a same-named branch had an open PR on a different
    base, so :func:`open_or_update_pr` never edits a PR pointed at the wrong line.
    """
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

    *draft* is the cut's hold decision (see :func:`release_cut._hold_reasons`): a
    draft PR cannot be merged, so opening the release PR as a draft holds the
    release line until a maintainer resolves what was flagged and marks it ready
    (or re-cuts with the hold cleared / overridden). On an update, the existing
    PR's draft state is reconciled to *draft*: each cut replaces the prep-branch
    content wholesale, so the draft flag always reflects the current cut's signals
    rather than a stale earlier decision.
    """
    if existing is not None:
        retry_github_call(
            lambda: existing.edit(title=title, body=body),
            retries=3, description=f"update PR #{existing.number}",
        )
        _reconcile_draft(existing, draft)
        logger.info("Updated release PR #%s (draft=%s)", existing.number, draft)
        return existing.html_url
    head_ref = build_pull_create_head_ref(base_repo, push_repo, branch)
    pr = retry_github_call(
        lambda: repo.create_pull(title=title, body=body, head=head_ref, base=base_branch, draft=draft),
        retries=3, description="create release PR",
    )
    logger.info("Opened release PR #%s (draft=%s)", pr.number, draft)
    return pr.html_url


def _reconcile_draft(existing: Any, draft: bool) -> None:
    """Flip *existing*'s draft state to *draft* if it differs.

    GitHub's draft toggle is two one-way transitions (``convert_to_draft`` /
    ``mark_ready_for_review``), each valid only from the opposite state, so guard
    on the current ``draft`` flag rather than calling unconditionally. A cut that
    re-runs with the same state is a no-op here.
    """
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
