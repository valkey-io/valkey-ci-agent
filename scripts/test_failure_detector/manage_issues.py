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
from datetime import timedelta

from github import Github

from scripts.common.issue_dedup import IssueDedupPublisher
from scripts.test_failure_detector import issue_renderer
from scripts.test_failure_detector.parse_failures import UniqueFailure

logger = logging.getLogger(__name__)

# The detector runs ~23 hours after the Daily CI it analyzes, so a failure can
# be fixed (and its issue closed) in between. One day covers that gap; a longer
# window would suppress genuine recurrences.
CLOSED_ISSUE_LOOKBACK = timedelta(days=1)


def _merge_same_fingerprint_failures(
    failures: list[UniqueFailure],
) -> list[UniqueFailure]:
    """Merge failures that share a dedup fingerprint so they publish as one.

    The parser groups by normalized error text, which keeps small run-specific
    numbers distinct (41- vs 49-byte variants of one leak), while the
    fingerprint normalizes digits away. Same-run failures that hash to one
    fingerprint would otherwise race for one issue: the first upsert creates it
    and stamps the run id, the second is rejected by the idempotency key and
    its jobs (environments, CI links) silently vanish. Merging by the
    published identity keeps every job reference on the surviving failure.

    A merge across tools (a valgrind and a sanitizer report of one bug) also
    carries the absorbed failure's trace onto the survivor, so the issue shows
    what each tool said rather than only whichever was processed first.

    Failures are ordered by type before merging, so which one survives does not
    depend on the order the jobs appear in the artifact. The survivor supplies
    the issue title, and the publisher re-titles on every update, so an
    order-dependent survivor made one issue's title alternate between the two
    tools' wording from run to run.
    """
    merged: dict[str, UniqueFailure] = {}
    unmergeable: list[UniqueFailure] = []
    for failure in sorted(failures, key=lambda f: f.failure_type.value):
        try:
            fingerprint = issue_renderer.fingerprint_for(failure)
        except Exception:
            # Merging is best-effort and must not abort the batch; pass the
            # failure through so the publish loop reports it as an error.
            logger.warning(
                "Could not fingerprint %s for same-run merging; passing it through",
                failure.display_name, exc_info=True,
            )
            unmergeable.append(failure)
            continue
        existing = merged.get(fingerprint)
        if existing is None:
            merged[fingerprint] = failure
            continue
        logger.info(
            "Merging %s into %s: both hash to fingerprint %s",
            failure.display_name, existing.display_name, fingerprint,
        )
        for job_ref in failure.jobs:
            if not any(j.job == job_ref.job for j in existing.jobs):
                existing.jobs.append(job_ref)
        _absorb_trace(existing, failure)
    return list(merged.values()) + unmergeable


def _absorb_trace(survivor: UniqueFailure, absorbed: UniqueFailure) -> None:
    """Carry *absorbed*'s trace onto *survivor* when it adds something.

    Only a differently-typed report is kept. Two failures of the same type that
    hash together are the same tool describing the same bug (the identity
    already scrubbed what differs between runs), so keeping both traces would
    show near-duplicate text.
    """
    if absorbed.failure_type == survivor.failure_type:
        return
    if not absorbed.error.strip():
        return
    label = issue_renderer.trace_label_for(absorbed)
    if any(existing_label == label for existing_label, _ in survivor.extra_traces):
        return
    survivor.extra_traces.append((label, absorbed.error))


def process_failures(
    gh: Github,
    repo_full_name: str,
    failures: list[UniqueFailure],
    *,
    run_id: int | None = None,
) -> dict[str, int]:
    """Create or update GitHub issues for each unique failure.

    Failures whose dedup fingerprints collide are merged into one upsert
    before publishing (see :func:`_merge_same_fingerprint_failures`), so the
    issue records every failing job instead of dropping all but the first.

    ``run_id``, when supplied, is used as the dedup idempotency key so a
    re-triggered cron analyzing the same CI run does not inflate the
    occurrence counter or post a duplicate comment.

    Returns a summary dict with counts:
    ``{created, updated, skipped, skipped_closed, errors}``. ``skipped_closed``
    counts failures whose matching issue was recently closed (the failure was
    likely already fixed). ``errors`` counts failures whose issue could not be
    processed; they are logged and skipped so one bad failure cannot abort the
    rest of the batch.
    """
    idempotency_key = str(run_id) if run_id is not None else None

    summary = {"created": 0, "updated": 0, "skipped": 0, "skipped_closed": 0, "errors": 0}

    failures = _merge_same_fingerprint_failures(failures)

    # One publisher per marker namespace, reused across the failures that share
    # it. The publisher caches the issue listing for its lifetime, so building a
    # fresh one per failure would re-list the repository's issues for every
    # failure in the batch.
    publishers: dict[str, IssueDedupPublisher] = {}

    for failure in failures:
        # Isolate each failure: a raised exception (e.g. a GitHub API error that
        # outlasts retries, or an unexpected upsert action) must not abort the
        # loop and silently drop every remaining failure. Log it, count it, and
        # move on so the rest of the batch is still processed.
        try:
            # Use the type-specific marker namespace so different failure types
            # get distinct issue search scopes and cannot collide. The
            # recently-closed suppression must be carried over here too, or a
            # failure fixed since the Daily run would be re-filed.
            namespace = issue_renderer.marker_namespace_for(failure)
            publisher = publishers.get(namespace)
            if publisher is None:
                publisher = IssueDedupPublisher(
                    gh,
                    marker_namespace=namespace,
                    closed_lookback=CLOSED_ISSUE_LOOKBACK,
                    filter_label=issue_renderer.label_for(failure),
                )
                publishers[namespace] = publisher

            # The render and body_transform hooks are coupled (they share the
            # set of newly failing environments), so they come from one renderer.
            renderer = issue_renderer.renderer_for(failure)
            action, url = publisher.upsert(
                repo_full_name,
                fingerprint=issue_renderer.fingerprint_for(failure),
                render=renderer.render,
                idempotency_key=idempotency_key,
                body_transform=renderer.merge_environments,
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
            elif action == "skipped-recently-closed":
                logger.info(
                    "Skipped %s (issue recently closed): %s",
                    failure.display_name, url,
                )
                summary["skipped_closed"] += 1
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
        "Done. Created %d, updated %d, skipped %d, skipped-closed %d, errored %d issue(s).",
        summary["created"], summary["updated"], summary["skipped"],
        summary["skipped_closed"], summary["errors"],
    )
    return summary
