"""Create or update GitHub issues for detected test failures.

Dedup, occurrence counting, and idempotency are owned by
:class:`scripts.common.issue_dedup.IssueDedupPublisher`; the test-failure-
specific title/body/comment rendering lives in
:mod:`scripts.test_failure_detector.issue_renderer`. The running list of
failing environments is carried forward across recurrences via the publisher's
``body_transform`` hook so it can read and edit the previously published body.
"""

from __future__ import annotations

import logging

from github import Github

from scripts.common.issue_dedup import IssueDedupPublisher
from scripts.test_failure_detector import issue_renderer
from scripts.test_failure_detector.parse_failures import UniqueFailure

logger = logging.getLogger(__name__)


def process_failures(
    gh: Github,
    repo_full_name: str,
    failures: list[UniqueFailure],
    *,
    run_id: int | None = None,
) -> dict[str, int]:
    """Create or update GitHub issues for each unique failure.

    ``run_id``, when supplied, is used as the dedup idempotency key so a
    re-triggered cron analyzing the same CI run does not inflate the
    occurrence counter or post a duplicate comment.

    Returns a summary dict with counts:
    ``{created, updated, skipped, errors}``. ``errors`` counts failures whose
    issue could not be processed; they are logged and skipped so one bad
    failure cannot abort the rest of the batch.
    """
    publisher = IssueDedupPublisher(gh, marker_namespace=issue_renderer.MARKER_NAMESPACE)
    idempotency_key = str(run_id) if run_id is not None else None

    summary = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

    for failure in failures:
        # Isolate each failure: a raised exception (e.g. a GitHub API error that
        # outlasts retries, or an unexpected upsert action) must not abort the
        # loop and silently drop every remaining failure. Log it, count it, and
        # move on so the rest of the batch is still processed.
        try:
            # The render and body_transform hooks are coupled (they share the
            # set of newly failing environments), so they come from one renderer.
            renderer = issue_renderer.renderer_for(failure)
            action, url = publisher.upsert(
                repo_full_name,
                fingerprint=issue_renderer.fingerprint_for(failure),
                render=renderer.render,
                idempotency_key=idempotency_key,
                body_transform=renderer.merge_environments,
                # The title is unchanged by the switch to hashed fingerprints, so
                # an exact title match adopts issues from the old raw-fingerprint
                # scheme and re-stamps them instead of creating duplicates.
                title_fallback=issue_renderer.title_for(failure),
            )
            if action == "created":
                logger.info("Created issue for %s: %s", failure.display_name, url)
                summary["created"] += 1
            elif action == "updated":
                logger.info("Updated issue for %s: %s", failure.display_name, url)
                summary["updated"] += 1
            elif action == "skipped-duplicate":
                logger.info("Skipped duplicate for %s: %s", failure.display_name, url)
                summary["skipped"] += 1
            else:
                raise RuntimeError(f"Unexpected upsert action: {action}")
        except Exception:
            logger.warning(
                "Failed to process failure %s; skipping it",
                failure.display_name, exc_info=True,
            )
            summary["errors"] += 1
            continue

    logger.info(
        "Done. Created %d, updated %d, skipped %d, errored %d issue(s).",
        summary["created"], summary["updated"], summary["skipped"], summary["errors"],
    )
    return summary
