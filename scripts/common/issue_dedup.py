"""Marker-based create-or-update for GitHub issues.

Embeds an HTML-comment marker (``<!-- <namespace>:<fingerprint> -->``) in
the first issue created for a finding. On subsequent calls with the same
fingerprint, edits the existing issue's body to bump an occurrence counter
and appends a comment.

Optional ``idempotency_key`` records the source event (e.g. a workflow run
id) and skips the update if the same key has already been seen, so a
re-triggered cron does not inflate the counter.

Callers supply rendered title, body, and comment via a render callback;
this module owns only the dedup machinery. Search failures are propagated
so a transient outage records as an error rather than silently creating a
duplicate issue on the next cron tick.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from scripts.common.github_client import retry_github_call

logger = logging.getLogger(__name__)


@dataclass
class IssueContent:
    """Pre-rendered issue text supplied by the caller."""

    title: str
    body: str
    comment: str
    labels: tuple[str, ...] = ()


class IssueDedupPublisher:
    """Create or update an issue, deduplicating on a fingerprint marker."""

    def __init__(self, github_client: Any, *, marker_namespace: str) -> None:
        """`marker_namespace` should be a stable workflow-scoped string,
        e.g. ``"valkey-ci-agent:fuzzer-issue"``. It appears literally in
        issue bodies and in search queries.
        """
        self._gh = github_client
        self._ns = marker_namespace

    def upsert(
        self,
        repo_name: str,
        *,
        fingerprint: str,
        render: Callable[[str, int], IssueContent],
        idempotency_key: str | None = None,
    ) -> tuple[str, str]:
        """Create or update the issue for ``fingerprint``.

        ``render(marker, occurrences)`` is called with the dedup marker and
        the occurrence count (1 for new issues, >=2 for updates) and must
        return a fully rendered :class:`IssueContent`. Returns
        ``(action, html_url)`` where action is ``"created"``, ``"updated"``,
        or ``"skipped-duplicate"`` (when ``idempotency_key`` matches the last
        recorded key on an existing issue).

        If ``idempotency_key`` is set, the publisher records it in the issue
        body as ``<!-- <ns>:last-key:<value> -->`` and refuses to bump the
        occurrence counter when the same key is seen again. Use this to
        guard against re-runs of the same source event (e.g. the same
        workflow run id) inflating the count.
        """
        repo = retry_github_call(
            lambda: self._gh.get_repo(repo_name),
            retries=2, description=f"get repo {repo_name}",
        )
        marker = f"<!-- {self._ns}:{fingerprint} -->"
        existing = self._find_existing(repo_name, marker)

        if existing is None:
            content = render(marker, 1)
            body = content.body
            if idempotency_key is not None:
                body = f"{body}\n{_last_key_marker(self._ns, idempotency_key)}"
            issue = retry_github_call(
                lambda: repo.create_issue(title=content.title, body=body),
                retries=2, description="create issue",
            )
            if content.labels:
                try:
                    issue.add_to_labels(*content.labels)
                except Exception as exc:
                    logger.info("Could not add labels to issue #%s: %s", issue.number, exc)
            logger.info("Created issue #%s for %s", issue.number, fingerprint)
            return "created", issue.html_url

        body = existing.body or ""

        if idempotency_key is not None:
            last = _last_key_re(self._ns).search(body)
            if last and last.group(1) == idempotency_key:
                logger.info(
                    "Issue #%s already records key %s; skipping update",
                    existing.number, idempotency_key,
                )
                return "skipped-duplicate", existing.html_url

        # Re-inject the marker if the body lost it (e.g. an editor stripped
        # HTML comments) so future runs continue to dedupe against this issue.
        if marker not in body:
            body = f"{marker}\n{body}".rstrip()
        m = _occurrence_re(self._ns).search(body)
        count = int(m.group(1)) + 1 if m else 2
        marker_occurrences = f"<!-- {self._ns}:occurrences:{count} -->"
        new_body = (
            _occurrence_re(self._ns).sub(marker_occurrences, body)
            if m else f"{body}\n{marker_occurrences}"
        )
        if idempotency_key is not None:
            replacement = _last_key_marker(self._ns, idempotency_key)
            if _last_key_re(self._ns).search(new_body):
                new_body = _last_key_re(self._ns).sub(replacement, new_body)
            else:
                new_body = f"{new_body}\n{replacement}"
        content = render(marker, count)
        retry_github_call(
            lambda: existing.edit(body=new_body, title=content.title),
            retries=2, description=f"update issue #{existing.number}",
        )
        retry_github_call(
            lambda: existing.create_comment(body=content.comment),
            retries=2, description=f"comment on issue #{existing.number}",
        )
        logger.info("Updated issue #%s (occurrence %d)", existing.number, count)
        return "updated", existing.html_url

    def _find_existing(self, repo_name: str, marker: str) -> Any:
        """Find an open issue containing the marker, or None."""
        query = f'"{marker}" in:body repo:{repo_name} is:issue is:open'
        results = retry_github_call(
            lambda: list(self._gh.search_issues(query)),
            retries=2, description="search issues",
        )
        for issue in results:
            if marker in (issue.body or ""):
                # Reload via the actual repo so we get a mutable issue handle.
                return retry_github_call(
                    lambda: self._gh.get_repo(repo_name).get_issue(issue.number),
                    retries=2, description=f"get issue #{issue.number}",
                )
        return None


def _occurrence_re(namespace: str) -> re.Pattern[str]:
    """A namespaced occurrence-counter regex: ``<!-- <ns>:occurrences:<n> -->``."""
    return re.compile(rf"<!-- {re.escape(namespace)}:occurrences:(\d+) -->")


def _last_key_marker(namespace: str, key: str) -> str:
    return f"<!-- {namespace}:last-key:{key} -->"


def _last_key_re(namespace: str) -> re.Pattern[str]:
    return re.compile(rf"<!-- {re.escape(namespace)}:last-key:([^\s>]+) -->")
