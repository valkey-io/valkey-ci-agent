"""Marker-based create-or-update for GitHub issues.

Embeds an HTML-comment marker (``<!-- <namespace>:<fingerprint> -->``) in
the first issue created for a finding. On subsequent calls with the same
fingerprint, edits the existing issue's body to bump an occurrence counter
and appends a comment.

Optional ``idempotency_key`` records the source event (e.g. a workflow run
id) and skips the update if the same key has already been seen, so a
re-triggered cron does not inflate the counter.

When no open issue matches and ``closed_lookback`` is set, the publisher
checks for a recently closed issue with the same marker (or, for legacy
issues, the exact fallback title). If one is found, creation is suppressed
and ``"skipped-recently-closed"`` is returned, avoiding duplicates for
failures that were fixed between the CI run and the detector. The check is
opt-in (disabled by default) so workflows that must never suppress a
recurrence, such as the fuzzer monitor, are unaffected.

Existing issues are discovered by listing the repository's issues via the
REST list endpoint and matching markers and titles locally, not via the
Search API. The list endpoint draws on the core rate limit (thousands of
requests per hour) instead of the Search API's 30-per-minute budget, which
a batch of failures could exhaust, and it is strongly consistent where
search results can lag the index or silently omit matches. Listings are
fetched once per publisher and reused across upserts in the same batch.

Callers supply rendered title, body, and comment via a render callback;
this module owns only the dedup machinery. Listing failures are propagated
so a transient outage records as an error rather than silently creating a
duplicate issue on the next cron tick.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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

    def __init__(
        self,
        github_client: Any,
        *,
        marker_namespace: str,
        closed_lookback: timedelta | None = None,
        filter_label: str | None = None,
    ) -> None:
        """`marker_namespace` should be a stable workflow-scoped string,
        e.g. ``"valkey-ci-agent:fuzzer-issue"``. It appears literally in
        issue bodies and drives the local marker matching.

        `closed_lookback` controls how far back to look for recently closed
        issues before creating a new one. ``None`` (the default) disables the
        check; pass a window such as ``timedelta(days=1)`` to opt in.

        `filter_label`, when set, scopes the issue listing server-side to
        only issues carrying that label, reducing data volume on repos with
        many open issues. Pass the same label that `render()` attaches to
        new issues.
        """
        self._gh = github_client
        self._ns = marker_namespace
        self._closed_lookback = closed_lookback
        self._filter_label = filter_label
        self._open_issues: dict[str, list[Any]] = {}
        self._recently_closed: dict[str, list[Any]] = {}

    def upsert(
        self,
        repo_name: str,
        *,
        fingerprint: str,
        render: Callable[[str, int], IssueContent],
        idempotency_key: str | None = None,
        body_transform: Callable[[str], str] | None = None,
        title_fallback: str | None = None,
    ) -> tuple[str, str]:
        """Create or update the issue for ``fingerprint``.

        ``render(marker, occurrences)`` is called with the dedup marker and
        the occurrence count (1 for new issues, >=2 for updates) and must
        return a fully rendered :class:`IssueContent`. Returns
        ``(action, html_url)`` where action is ``"created"``, ``"updated"``,
        ``"skipped-duplicate"`` (when ``idempotency_key`` matches the last
        recorded key on an existing issue), or ``"skipped-recently-closed"``
        (when no open issue exists but a matching issue was closed within the
        lookback window).

        If ``idempotency_key`` is set, the publisher records it in the issue
        body as ``<!-- <ns>:last-key:<value> -->`` and refuses to bump the
        occurrence counter when the same key is seen again. Use this to
        guard against re-runs of the same source event (e.g. the same
        workflow run id) inflating the count.

        ``body_transform`` is an optional hook applied only on the *update*
        path: it receives the existing issue body and returns a modified
        body, letting callers carry state forward that ``render`` can't see
        (e.g. merging a running list of failing environments into the
        already-published body). It runs before the marker/occurrence/
        idempotency machinery, so those markers stay authoritative regardless
        of what the transform returns. Ignored when creating a new issue.

        ``title_fallback`` migrates issues created under an older fingerprint
        scheme. When the marker match misses, the publisher looks for an open
        issue with this exact title; on a hit it adopts that issue and the
        update re-stamps it with the current marker, so a format change does
        not orphan it into a duplicate. The match must be exact and
        case-sensitive. Pass the same title ``render`` produces. The same
        fallback applies to the recently-closed check, so a legacy issue
        closed within the lookback window also suppresses creation.
        """
        repo = retry_github_call(
            lambda: self._gh.get_repo(repo_name),
            retries=2, description=f"get repo {repo_name}",
        )
        marker = f"<!-- {self._ns}:{fingerprint} -->"
        existing = self._find_existing(repo, repo_name, marker)

        # An issue from an older fingerprint scheme carries a different (or no)
        # marker, so the marker match misses it. Fall back to an exact title
        # match. The update path below re-stamps the current marker, so this
        # only fires once per issue; later runs match on the marker.
        if existing is None and title_fallback is not None:
            existing = self._find_by_title(repo, repo_name, title_fallback)
            if existing is not None:
                logger.info(
                    "Adopting legacy issue #%s for %s via title fallback",
                    existing.number, fingerprint,
                )

        if existing is None:
            recently_closed = self._find_recently_closed(
                repo, repo_name, marker, title_fallback,
            )
            if recently_closed is not None:
                logger.info(
                    "Issue #%s for %s was closed recently; skipping creation",
                    recently_closed.number, fingerprint,
                )
                return "skipped-recently-closed", recently_closed.html_url

            content = render(marker, 1)
            body = content.body
            if idempotency_key is not None:
                body = f"{body}\n{_last_key_marker(self._ns, idempotency_key)}"
            # Labels ride along on the create call so they are atomic with creation
            create_kwargs: dict[str, Any] = {"title": content.title, "body": body}
            if content.labels:
                create_kwargs["labels"] = list(content.labels)
            issue = retry_github_call(
                lambda: repo.create_issue(**create_kwargs),
                retries=2, description="create issue",
            )
            # Make the new issue visible to later upserts in this batch, so a
            # repeated fingerprint or title updates it instead of filing a
            # duplicate. _find_existing above guarantees the cache entry exists.
            self._open_issues[repo_name].append(issue)
            logger.info("Created issue #%s for %s", issue.number, fingerprint)
            return "created", issue.html_url

        body = existing.body or ""
        if body_transform is not None:
            body = body_transform(body)

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

    def _open_issues_for(self, repo: Any, repo_name: str) -> list[Any]:
        """Open issues of the repo, fetched once and cached.

        When ``filter_label`` is set, the listing is scoped server-side to
        that label. Issues this publisher creates always carry their labels
        (applied atomically on the create call), so they are never missed.
        Marker-bearing issues filed outside the publisher without the label
        (e.g. a human reusing the issue template) are invisible to the
        filtered listing and may be duplicated; that is inherent to label
        filtering.
        """
        if repo_name not in self._open_issues:
            kwargs: dict[str, Any] = {"state": "open"}
            if self._filter_label is not None:
                kwargs["labels"] = [self._filter_label]
            issues = retry_github_call(
                lambda: list(repo.get_issues(**kwargs)),
                retries=2, description="list open issues",
            )
            self._open_issues[repo_name] = _drop_pull_requests(issues)
            logger.info(
                "Cached %d open issue(s) from %s for dedup matching",
                len(self._open_issues[repo_name]), repo_name,
            )
        return self._open_issues[repo_name]

    def _recently_closed_for(self, repo: Any, repo_name: str) -> list[Any]:
        """Issues closed within the lookback window, fetched once and cached."""
        if repo_name not in self._recently_closed:
            assert self._closed_lookback is not None  # guarded by _find_recently_closed
            cutoff = datetime.now(timezone.utc) - self._closed_lookback
            # The list endpoint can't filter on close time; ``since`` filters
            # on update time server-side, which is a safe over-approximation
            # (closing an issue updates it), and the exact close-time check
            # happens locally on ``closed_at``.
            kwargs: dict[str, Any] = {"state": "closed", "since": cutoff}
            if self._filter_label is not None:
                kwargs["labels"] = [self._filter_label]
            issues = retry_github_call(
                lambda: list(repo.get_issues(**kwargs)),
                retries=2, description="list recently closed issues",
            )
            self._recently_closed[repo_name] = [
                issue for issue in _drop_pull_requests(issues)
                if issue.closed_at is not None and issue.closed_at >= cutoff
            ]
            logger.info(
                "Cached %d recently closed issue(s) from %s for dedup matching",
                len(self._recently_closed[repo_name]), repo_name,
            )
        return self._recently_closed[repo_name]

    def _find_existing(self, repo: Any, repo_name: str, marker: str) -> Any:
        """Find an open issue containing the marker, or None."""
        for issue in self._open_issues_for(repo, repo_name):
            if marker in (issue.body or ""):
                return self._reload(repo, issue.number)
        return None

    def _find_by_title(self, repo: Any, repo_name: str, title: str) -> Any:
        """Find an open issue whose title exactly equals ``title``, or None.

        Migration fallback for when the marker match misses. The comparison
        is exact and case-sensitive.
        """
        for issue in self._open_issues_for(repo, repo_name):
            if issue.title == title:
                return self._reload(repo, issue.number)
        return None

    def _find_recently_closed(
        self, repo: Any, repo_name: str, marker: str, title_fallback: str | None,
    ) -> Any:
        """Find an issue that was closed within the lookback window and
        matches the marker or, for legacy issues, ``title_fallback`` exactly.
        Returns None when nothing matches or the check is disabled.

        Guards against re-filing an issue for a failure that was already fixed
        and whose issue was closed between the CI run and the detector run.
        """
        if self._closed_lookback is None:
            return None
        closed_issues = self._recently_closed_for(repo, repo_name)
        for issue in closed_issues:
            if marker in (issue.body or ""):
                return issue

        # A legacy issue carries an older (or no) marker, so the marker match
        # misses it. Fall back to an exact title match, mirroring the open-issue
        # migration path, so a just-closed legacy issue is not duplicated.
        if title_fallback is None:
            return None
        for issue in closed_issues:
            if issue.title == title_fallback:
                logger.info(
                    "Matched recently closed legacy issue #%s via title fallback",
                    issue.number,
                )
                return issue
        return None

    def _reload(self, repo: Any, number: int) -> Any:
        """Reload an issue so the update path edits a fresh, mutable handle."""
        return retry_github_call(
            lambda: repo.get_issue(number),
            retries=2, description=f"get issue #{number}",
        )


def _drop_pull_requests(issues: list[Any]) -> list[Any]:
    """Filter pull requests out of an issue listing.

    The REST issues list endpoint returns pull requests alongside issues.
    We read the internal ``_rawData`` dict (the already-stored payload)
    rather than the public ``raw_data`` property or ``pull_request``
    attribute, both of which trigger ``_completeIfNeeded()`` and fire a
    per-issue GET that would defeat the single-listing rate-limit goal.
    """
    return [issue for issue in issues if "pull_request" not in issue._rawData]


def _occurrence_re(namespace: str) -> re.Pattern[str]:
    """A namespaced occurrence-counter regex: ``<!-- <ns>:occurrences:<n> -->``."""
    return re.compile(rf"<!-- {re.escape(namespace)}:occurrences:(\d+) -->")


def _last_key_marker(namespace: str, key: str) -> str:
    return f"<!-- {namespace}:last-key:{key} -->"


def _last_key_re(namespace: str) -> re.Pattern[str]:
    return re.compile(rf"<!-- {re.escape(namespace)}:last-key:([^\s>]+) -->")
