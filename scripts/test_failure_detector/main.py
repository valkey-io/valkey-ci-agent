"""Test Failure Detector — main entry point"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from github import Auth, Github

from scripts.backport.registry import load_registry
from scripts.common.github_client import retry_github_call
from scripts.common.job_summary import emit_job_summary
from scripts.common.logging_utils import configure_logging, log_outcome
from scripts.common.workflow_artifacts import ArtifactClient
from scripts.common.workflow_jobs import jobs_for_run_attempt
from scripts.test_failure_detector.download import (
    download_all_test_failures,
    get_latest_daily_run,
    job_urls_for_jobs,
)
from scripts.test_failure_detector.job_failures import (
    JobFailure,
    find_unrecorded_failures,
    merge_failures,
)
from scripts.test_failure_detector.manage_issues import process_failures
from scripts.test_failure_detector.parse_failures import parse_and_deduplicate

logger = logging.getLogger(__name__)

# New: Build a markdown summary for the GitHub Actions job summary.
def _build_job_summary(
    run_id: int,
    repo_full_name: str,
    num_failures: int,
    result: dict[str, int],
    damaged: list[str] | None = None,
    num_job_failures: int = 0,
    unread_logs: list[str] | None = None,
    unexplained: list[str] | None = None,
) -> str:
    lines = [
        "## Test Failure Detector",
        "",
        f"**Source:** [{repo_full_name}](https://github.com/{repo_full_name}) "
        f"— [Run #{run_id}](https://github.com/{repo_full_name}/actions/runs/{run_id})",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Unique failures detected | {num_failures} |",
        f"| Job failures without a test record | {num_job_failures} |",
        f"| Issues created | {result.get('created', 0)} |",
        f"| Issues skipped (duplicate run) | {result.get('skipped', 0)} |",
        f"| Issues skipped (recently closed) | {result.get('skipped_closed', 0)} |",
        f"| Issues updated | {result.get('updated', 0)} |",
        f"| Errors | {result.get('errors', 0)} |",
        "",
    ]
    if damaged:
        # The counts above cover only what could be read. Whatever the entries
        # below describe was never analyzed, so no issue exists for it.
        lines.extend([
            "### Incomplete artifact",
            "",
            f"{len(damaged)} part(s) of the `all-test-failures` artifact could "
            "not be read. Any failure recorded in them was not analyzed and is "
            "missing from the counts above:",
            "",
        ])
        lines.extend(f"- `{_one_line(entry)}`" for entry in damaged)
        lines.append("")
    if unread_logs:
        # These jobs failed, but how is unknown: no issue was filed for them.
        lines.extend([
            "### Unread job logs",
            "",
            f"The logs of {len(unread_logs)} failed job(s) could not be read, so "
            "any failure only their logs show was not analyzed:",
            "",
        ])
        lines.extend(f"- `{_one_line(name)}`" for name in unread_logs)
        lines.append("")
    if unexplained:
        # Nothing these jobs printed identifies what failed, so no issue is
        # filed for them; they are listed so the failure is still seen.
        lines.extend([
            "### Failed jobs with no recorded cause (not filed)",
            "",
            f"{len(unexplained)} failed job(s) printed no test failure, exception, or "
            "valgrind or sanitizer report, or failed C unit test (a build break, a crash, an outage):",
            "",
        ])
        lines.extend(f"- `{_one_line(name)}`" for name in unexplained)
        lines.append("")
    return "\n".join(lines)


def _one_line(text: str) -> str:
    """Collapse text to a single line safe inside a markdown code span.

    A damage entry carries a zip member name and an exception message, either of
    which can hold a newline or a backtick that would break the list item.
    """
    return " ".join(text.split()).replace("`", "'")

def run(
    *,
    github_token: str,
    repo_full_name: str,
    run_id: int | None = None,
    workflow_name: str = "Daily",
    branch: str = "unstable",
    dry_run: bool = False,
    verbose: bool = False,
    registry_path: str = "repos.yml",
) -> int:
    """Run the test failure detector pipeline.

    Args:
        github_token: GitHub token with issues:write and actions:read on the target repo.
        repo_full_name: The repository to monitor (e.g., "valkey-io/valkey").
        run_id: Specific workflow run ID to analyze. If None, uses the latest Daily run.
        workflow_name: Name of the workflow to look for (default: "Daily").
        branch: Branch to filter workflow runs (default: "unstable").
        dry_run: If True, parse and report but don't create/update issues.
        verbose: Enable debug logging.
    """
    configure_logging(verbose=verbose)

    gh = Github(auth=Auth.Token(github_token))
    artifact_client = ArtifactClient(gh, token=github_token)

    # Step 1: Find the workflow run
    source_run = None
    if run_id is None:
        logger.info("Looking for latest %s run on %s/%s...", workflow_name, repo_full_name, branch)
        latest_run = get_latest_daily_run(gh, repo_full_name, workflow_name, branch)
        if latest_run is None:
            log_outcome(logger, logging.ERROR, "No qualifying %s run found on %s (branch %s)",
                        workflow_name, repo_full_name, branch)
            emit_job_summary(
                f"### ⚠️ Test Failure Detector\n\n"
                f"No qualifying `{workflow_name}` run found on "
                f"`{repo_full_name}` (branch `{branch}`)."
            )
            return 1
        run_id = latest_run.id
        source_run = latest_run
    else:
        logger.info("Using specified run ID: %d", run_id)
        source_run = retry_github_call(
            lambda: gh.get_repo(repo_full_name).get_workflow_run(run_id),
            retries=3,
            description=f"get run {run_id}",
        )
    run_attempt = max(1, int(getattr(source_run, "run_attempt", 1) or 1))

    # Step 2: Download the all-test-failures artifact
    logger.info("Downloading all-test-failures artifact from run %d...", run_id)
    # Unreadable members of the artifact zip. The failures JSON can survive
    # alongside them, in which case the run is analyzed from an artifact known
    # to be incomplete: every report below has to say so.
    damaged: list[str] = []
    unusable: list[str] = []
    artifact_content = download_all_test_failures(
        gh, repo_full_name, run_id, github_token,
        artifact_client=artifact_client, damaged=damaged, unusable=unusable,
    )
    if damaged:
        logger.error(
            "%d part(s) of the artifact from run %d could not be read: %s",
            len(damaged), run_id, "; ".join(damaged),
        )
    if artifact_content is None:
        if unusable:
            # The artifact exists but was not analyzed: zero issues here does
            # not mean zero failures, and the log must not suggest otherwise.
            summary = _build_job_summary(run_id, repo_full_name, 0, {}, damaged)
            log_outcome(
                logger, logging.WARNING,
                "Run %d was NOT analyzed: %s. Any failure it recorded has no issue.",
                run_id, "; ".join(unusable),
            )
            summary += (
                "\n### Run not analyzed\n\n"
                + "".join(f"- {_one_line(reason)}\n" for reason in unusable)
            )
            emit_job_summary(summary)
            return 1
        # Without the artifact no failure is recorded, but a job can still have
        # failed (a build break leaves nothing to merge); the job check below
        # covers it. A clean run has no failed job and reports nothing.
        logger.info("Run %d has no all-test-failures artifact; checking failed jobs only.", run_id)
        artifact_content = b"{}"

    try:
        all_failures = json.loads(artifact_content)
    except json.JSONDecodeError as exc:
        # A malformed or truncated artifact must not crash the run before we
        # report; surface it in the job summary and exit non-zero instead.
        log_outcome(
            logger, logging.ERROR,
            "Could not parse the all-test-failures artifact from run %d: %s", run_id, exc,
        )
        emit_job_summary(
            f"### ⚠️ Test Failure Detector\n\n"
            f"Could not parse the `all-test-failures` artifact from "
            f"[run #{run_id}](https://github.com/{repo_full_name}/actions/runs/{run_id}) "
            f"— the artifact is malformed or truncated."
        )
        return 1

    # The artifact is {job_name: {suite_name: [...]}}. Valid JSON of the wrong
    # shape still gets here: a bare scalar would crash on len() below, and a
    # top-level list would parse as "no failures". Require a dict.
    if not isinstance(all_failures, dict):
        log_outcome(
            logger, logging.ERROR,
            "Unexpected all-test-failures artifact from run %d: expected a JSON "
            "object, got %s",
            run_id, type(all_failures).__name__,
        )
        emit_job_summary(
            f"### ⚠️ Test Failure Detector\n\n"
            f"The `all-test-failures` artifact from "
            f"[run #{run_id}](https://github.com/{repo_full_name}/actions/runs/{run_id}) "
            f"has an unexpected format — expected a JSON object."
        )
        return 1
    logger.info("Loaded failures from %d job(s)", len(all_failures))

    # Step 3: Fetch one immutable jobs view for this exact rerun occurrence.
    # URL mapping and log parsing below must not independently observe a newer
    # attempt if somebody clicks "Re-run jobs" while the detector is working.
    logger.info(
        "Fetching jobs from run %d attempt %d...",
        run_id,
        run_attempt,
    )
    try:
        jobs = jobs_for_run_attempt(
            gh,
            repo_full_name,
            run_id,
            run_attempt=run_attempt,
            retries=3,
        )
    except Exception as exc:  # noqa: BLE001 - incomplete inspection must fail visibly
        detail = " ".join(str(exc).split())[:300] or type(exc).__name__
        log_outcome(
            logger,
            logging.ERROR,
            "Could not inspect jobs in run %d attempt %d: %s",
            run_id,
            run_attempt,
            detail,
        )
        emit_job_summary(
            _build_job_summary(run_id, repo_full_name, 0, {}, damaged)
            + "\n### Incomplete CI inspection\n\n"
            + f"Could not inspect failed jobs: `{_one_line(detail)}`\n"
        )
        return 1
    job_urls = job_urls_for_jobs(jobs, run_id=run_id)

    # Step 4: Parse and deduplicate
    logger.info("Parsing and deduplicating failures...")
    unique_failures = parse_and_deduplicate(all_failures, job_urls)

    # Step 5: Failed jobs the artifact does not record (timeouts, valgrind or
    # sanitizer reports, build breaks), read from each job's own log.
    unread: list[str] = []
    unexplained: list[str] = []
    try:
        log_failures, job_failures = _unrecorded_failures(
            artifact_client, repo_full_name, all_failures, registry_path, jobs=jobs, unread=unread,
            unexplained=unexplained,
        )
    except Exception as exc:  # noqa: BLE001 - fail visibly instead of reporting a clean run
        detail = " ".join(str(exc).split())[:300] or type(exc).__name__
        log_outcome(
            logger,
            logging.ERROR,
            "Could not inspect failed jobs in run %d: %s",
            run_id,
            detail,
        )
        emit_job_summary(
            _build_job_summary(run_id, repo_full_name, len(unique_failures), {}, damaged)
            + "\n### Incomplete CI inspection\n\n"
            + f"Could not inspect failed jobs: `{_one_line(detail)}`\n"
        )
        return 1
    unique_failures = merge_failures(unique_failures, log_failures)
    if unread:
        logger.error(
            "Run %d: the logs of %d failed job(s) could not be read; nothing is filed for them: %s",
            run_id, len(unread), ", ".join(unread),
        )
    # Whatever could not be read was never analyzed, so the run is not a full pass.
    incomplete = bool(damaged or unread)

    if not unique_failures and not job_failures:
        log_outcome(logger, logging.INFO, "Run %d: no test failures to report", run_id)
        emit_job_summary(_build_job_summary(
            run_id, repo_full_name, 0, {}, damaged, unread_logs=unread, unexplained=unexplained,
        ))
        return 1 if incomplete else 0

    logger.info(
        "Found %d unique failure(s) and %d job failure(s) without a test record",
        len(unique_failures), len(job_failures),
    )

    if dry_run:
        logger.info("Dry run — skipping issue creation/update.")
        for f in unique_failures:
            envs = ", ".join(j.job for j in f.jobs)
            logger.info("  %s [%s]", f.display_name, envs)
        for job in job_failures:
            names = ", ".join(ref.job for ref in job.jobs) or job.job
            logger.info("  job %s (%s)", names, "; ".join(job.summary) or job.step)
        emit_job_summary(_build_job_summary(
            run_id, repo_full_name, len(unique_failures), {}, damaged, len(job_failures), unread, unexplained,
        ))
        log_outcome(logger, logging.INFO, "Dry run: %d unique failure(s) and %d job failure(s) "
                    "in run %d; no issues filed", len(unique_failures), len(job_failures), run_id)
        return 1 if incomplete else 0

    # Step 6: Create or update issues
    logger.info("Processing issues on %s...", repo_full_name)
    result = process_failures(
        gh,
        repo_full_name,
        unique_failures,
        run_id=run_id,
        job_failures=job_failures,
    )
    emit_job_summary(_build_job_summary(
        run_id, repo_full_name, len(unique_failures), result, damaged, len(job_failures), unread, unexplained,
    ))
    log_outcome(
        logger, logging.ERROR if result.get("errors") or incomplete else logging.INFO,
        "Run %d: %d unique failure(s) and %d job failure(s): %d issue(s) created, %d updated, "
        "%d skipped, %d error(s)",
        run_id, len(unique_failures), len(job_failures), result.get("created", 0), result.get("updated", 0),
        result.get("skipped", 0) + result.get("skipped_closed", 0), result.get("errors", 0),
    )

    # process_failures isolates per-failure errors so one bad failure can't
    # abort the batch, but those failures got no issue created or updated. Exit
    # non-zero so a GitHub outage that skips issue updates doesn't leave CI green.
    if result.get("errors", 0) > 0:
        logger.error(
            "%d failure(s) could not be processed; exiting non-zero.",
            result["errors"],
        )
        return 1
    # Issues were filed for everything readable, but the unreadable part of the
    # artifact was never analyzed, so the run cannot be reported as a full pass.
    if damaged:
        logger.error(
            "Artifact from run %d was incomplete; %d part(s) unreadable.",
            run_id, len(damaged),
        )
        return 1
    if unread:
        return 1
    return 0

def _unrecorded_failures(
    artifact_client: ArtifactClient,
    repo_full_name: str,
    all_failures: dict,
    registry_path: str,
    *,
    jobs: tuple[object, ...],
    unread: list[str] | None = None,
    unexplained: list[str] | None = None,
) -> tuple[list, list[JobFailure]]:
    """Find failed jobs without a recorded test failure.

    The registry's ``ci_followup_ignored_jobs`` names the informational jobs
    (failure aggregators, coverage) that fail only because another job did.
    Without it those jobs would be filed, so a registry that cannot be read
    stops the inspection, which the caller reports as incomplete.
    """
    try:
        ignored = load_registry(registry_path).get_repo(repo_full_name).ci_followup_ignored_jobs
    except (OSError, KeyError, ValueError) as exc:
        raise RuntimeError(f"cannot read the ignored jobs from {registry_path}: {exc}") from exc
    return find_unrecorded_failures(
        artifact_client, repo_full_name, all_failures, jobs=jobs, ignored_jobs=ignored, unread=unread,
        unexplained=unexplained,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect test failures from Valkey Daily CI and create GitHub issues.",
    )
    parser.add_argument(
        "--token",
        required=True,
        help="GitHub token with issues:write and actions:read permissions.",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Target repository (e.g., valkey-io/valkey).",
    )
    parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help="Specific workflow run ID to analyze. If omitted, uses the latest Daily run.",
    )
    parser.add_argument(
        "--workflow-name",
        default="Daily",
        help="Name of the CI workflow to monitor (default: Daily).",
    )
    parser.add_argument(
        "--branch",
        default="unstable",
        help="Branch to filter workflow runs (default: unstable).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report failures without creating/updating issues.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--registry",
        default="repos.yml",
        help="Registry naming informational jobs to ignore (default: repos.yml).",
    )

    args = parser.parse_args()

    sys.exit(
        run(
            github_token=args.token,
            repo_full_name=args.repo,
            run_id=args.run_id,
            workflow_name=args.workflow_name,
            branch=args.branch,
            dry_run=args.dry_run,
            verbose=args.verbose,
            registry_path=args.registry,
        )
    )

if __name__ == "__main__":
    main()
