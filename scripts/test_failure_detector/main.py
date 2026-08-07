"""Test Failure Detector — main entry point"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from github import Auth, Github

from scripts.common.job_summary import emit_job_summary
from scripts.common.workflow_artifacts import ArtifactClient
from scripts.test_failure_detector.download import (
    download_all_test_failures,
    get_job_info,
    get_latest_daily_run,
    get_run_conclusion,
    normalize_job_name,
)
from scripts.test_failure_detector.manage_issues import process_failures
from scripts.test_failure_detector.parse_failures import (
    FailureType,
    UniqueFailure,
    parse_and_deduplicate,
)
from scripts.test_failure_detector.timeout_recovery import (
    RunLogs,
    enrich_log_only_errors,
    recover_timeouts,
)

logger = logging.getLogger(__name__)

# Run conclusions that mean tests ran and did not pass, so a missing artifact is
# a reporting gap rather than a clean run. A run killed on the workflow timeout
# concludes "timed_out", and one that died before any job started concludes
# "startup_failure"; both leave failures unreported. Conclusions that mean no
# tests ran (cancelled, skipped, neutral) stay out: there is nothing to report.
_FAILED_RUN_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})


def _build_job_summary(
    run_id: int,
    repo_full_name: str,
    num_failures: int,
    result: dict[str, int],
    damaged: list[str] | None = None,
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
    if unexplained:
        # These jobs failed, but nothing in the artifact or the logs says why, so
        # no issue was filed for them and a reader has to open the run.
        lines.extend([
            "### Failed jobs with no reported failure",
            "",
            f"{len(unexplained)} job(s) failed without any failure the sweep "
            "could attribute to them. Their cause is not in the artifact or the "
            "console logs, so no issue was filed:",
            "",
        ])
        lines.extend(f"- `{_one_line(job)}`" for job in unexplained)
        lines.append("")
    return "\n".join(lines)


def _one_line(text: str) -> str:
    """Collapse text to a single line safe inside a markdown code span.

    A damage entry carries a zip member name and an exception message, either of
    which can hold a newline or a backtick that would break the list item.
    """
    return " ".join(text.split()).replace("`", "'")


def _unexplained_failed_jobs(
    failures: list[UniqueFailure],
    failed_jobs: set[str],
) -> list[str]:
    """Failed job names no reported failure accounts for.

    A red job whose failures all reached the artifact is represented by at least
    one entry naming it. One that is not represented failed for a reason the
    sweep cannot see: the suite crashed before writing its file, the upload was
    skipped, or the failure is of a kind no producer records. Returning it lets
    the caller exit non-zero rather than report the run as clean, which is the
    one outcome that hides a red job behind a green sweep.

    Both spellings of a matrix job are compared, since the artifact and the API
    name it differently.
    """
    represented: set[str] = set()
    for failure in failures:
        for job_ref in failure.jobs:
            represented.add(job_ref.job)
            represented.add(normalize_job_name(job_ref.job))
    return sorted(
        job for job in failed_jobs
        if job not in represented and normalize_job_name(job) not in represented
    )


def _merge_timeout_recoveries(
    unique_failures: list[UniqueFailure],
    timeout_failures: list[UniqueFailure],
) -> list[UniqueFailure]:
    """Merge log-recovered timeouts into the artifact-derived failure list.

    If the same test already appears as a TIMEOUT from the artifact (the
    runner captured it before the watchdog fired), fold the recovered job
    references into the existing entry rather than creating a duplicate.
    """
    if not timeout_failures:
        return unique_failures

    # Nameless timeouts are routine (a volatile runner name like "pid:NNNN" or
    # "hang" is demoted to "" on both the artifact and recovery sides), and they
    # group by test_file, which the ("", file) key handles. Excluding them from
    # the index would report one timeout as two failures.
    existing_timeouts: dict[tuple[str, str], UniqueFailure] = {}
    for f in unique_failures:
        if f.failure_type == FailureType.TIMEOUT:
            existing_timeouts[(f.test_name, f.test_file)] = f

    for recovered in timeout_failures:
        key = (recovered.test_name, recovered.test_file)
        if key in existing_timeouts:
            existing = existing_timeouts[key]
            for job_ref in recovered.jobs:
                if not any(j.job == job_ref.job for j in existing.jobs):
                    existing.jobs.append(job_ref)
        else:
            unique_failures.append(recovered)
            existing_timeouts[key] = recovered

    return unique_failures


def run(
    *,
    github_token: str,
    repo_full_name: str,
    run_id: int | None = None,
    workflow_name: str = "Daily",
    branch: str = "unstable",
    dry_run: bool = False,
    verbose: bool = False,
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
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    # Auth.Token raises a bare AssertionError on an empty token, which reads as
    # an internal error rather than a missing secret.
    if not github_token:
        raise ValueError("GitHub token is required")

    gh = Github(auth=Auth.Token(github_token))
    artifact_client = ArtifactClient(gh, token=github_token)

    # Step 1: Find the workflow run
    run_conclusion: str | None = None
    conclusion_known = True
    if run_id is None:
        logger.info("Looking for latest %s run on %s/%s...", workflow_name, repo_full_name, branch)
        daily_run = get_latest_daily_run(gh, repo_full_name, workflow_name, branch)
        if daily_run is None:
            logger.error("No qualifying workflow run found.")
            emit_job_summary(
                f"### ⚠️ Test Failure Detector\n\n"
                f"No qualifying `{workflow_name}` run found on "
                f"`{repo_full_name}` (branch `{branch}`)."
            )
            return 1
        run_id = daily_run.id
        run_conclusion = daily_run.conclusion
    else:
        logger.info("Using specified run ID: %d", run_id)
        # An explicitly named run needs its conclusion looked up, or the
        # missing-artifact branch below cannot tell a red run from a clean one.
        # None means the lookup itself failed, which is not evidence of a clean
        # run: treated as unknown below rather than folded into the clean path.
        run_conclusion = get_run_conclusion(gh, repo_full_name, run_id)
        conclusion_known = run_conclusion is not None

    # Step 2: Download the all-test-failures artifact
    logger.info("Downloading all-test-failures artifact from run %d...", run_id)
    # Unreadable members of the artifact zip. The failures JSON can survive
    # alongside them, in which case the run is analyzed from an artifact known
    # to be incomplete: every report below has to say so.
    damaged: list[str] = []
    artifact_content = download_all_test_failures(
        gh, repo_full_name, run_id, github_token,
        artifact_client=artifact_client, damaged=damaged,
    )
    if damaged:
        logger.error(
            "%d part(s) of the artifact from run %d could not be read: %s",
            len(damaged), run_id, "; ".join(damaged),
        )
    if artifact_content is None:
        # A red run with no artifact is not a clean pass: the artifact expired,
        # the upload failed, or the run died before the consolidate step. Saying
        # "passed cleanly" there hides a real failure behind a green sweep, so
        # report it as a problem instead.
        if run_conclusion in _FAILED_RUN_CONCLUSIONS:
            logger.error(
                "Run %d concluded %r but has no test failures artifact; "
                "its failures cannot be reported.", run_id, run_conclusion,
            )
            emit_job_summary(
                f"### ⚠️ Test Failure Detector\n\n"
                f"[Run #{run_id}](https://github.com/{repo_full_name}/actions/runs/{run_id}) "
                f"concluded `{run_conclusion}` but uploaded no "
                f"`all-test-failures` artifact, so its failures could not be "
                f"analyzed. The artifact may have expired, or the run may have "
                f"failed before consolidating results."
            )
            return 1
        if not conclusion_known:
            # The conclusion lookup failed, so a red run and a clean one are
            # indistinguishable here. Reporting "passed cleanly" on an API error
            # is the one outcome that hides a real failure, so fail closed.
            logger.error(
                "Run %d has no test failures artifact and its conclusion could "
                "not be established; treating as unreportable.", run_id,
            )
            emit_job_summary(
                f"### ⚠️ Test Failure Detector\n\n"
                f"[Run #{run_id}](https://github.com/{repo_full_name}/actions/runs/{run_id}) "
                f"uploaded no `all-test-failures` artifact and its conclusion "
                f"could not be read, so it is not known whether it failed."
            )
            return 1
        logger.info("No test failures artifact found; CI run likely passed cleanly.")
        emit_job_summary(_build_job_summary(run_id, repo_full_name, 0, {}, damaged))
        return 1 if damaged else 0

    try:
        all_failures = json.loads(artifact_content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # A malformed or truncated artifact must not crash the run before we
        # report; surface it in the job summary and exit non-zero instead.
        # UnicodeDecodeError is separate: an artifact written in the system
        # encoding rather than UTF-8 fails in the decode before the parse.
        logger.error(
            "Could not parse all-test-failures artifact from run %d: %s", run_id, exc,
        )
        emit_job_summary(
            f"### ⚠️ Test Failure Detector\n\n"
            f"Could not parse the `all-test-failures` artifact from "
            f"[run #{run_id}](https://github.com/{repo_full_name}/actions/runs/{run_id}). "
            f"The artifact is malformed or truncated."
        )
        return 1

    # The artifact is {job_name: {suite_name: [...]}}. Valid JSON of the wrong
    # shape still gets here: a bare scalar would crash on len() below, and a
    # top-level list would parse as "no failures". Require a dict.
    if not isinstance(all_failures, dict):
        logger.error(
            "Unexpected all-test-failures artifact from run %d: expected a JSON "
            "object, got %s",
            run_id, type(all_failures).__name__,
        )
        emit_job_summary(
            f"### ⚠️ Test Failure Detector\n\n"
            f"The `all-test-failures` artifact from "
            f"[run #{run_id}](https://github.com/{repo_full_name}/actions/runs/{run_id}) "
            f"has an unexpected format; expected a JSON object."
        )
        return 1
    logger.info("Loaded failures from %d job(s)", len(all_failures))

    # Step 3: Get job metadata (URLs + failed job names)
    logger.info("Fetching job metadata...")
    job_info = get_job_info(gh, repo_full_name, run_id)

    # Step 4: Recover timeout failures from logs
    # The Tcl test runner excludes timeouts from the artifact (and its
    # watchdog may kill the process before write_test_failures runs at all),
    # so scan the console logs of failed jobs that captured no timeout entry.
    # Both this and the enrichment below read the run's log zip; share one
    # RunLogs so it is downloaded at most once.
    run_logs = RunLogs(artifact_client, repo_full_name, run_id)
    timeout_failures = recover_timeouts(all_failures, job_info, run_logs)

    # Step 5: Parse and deduplicate
    logger.info("Parsing and deduplicating failures...")
    unique_failures = parse_and_deduplicate(
        all_failures, job_info.urls, job_info.step_urls,
    )
    unique_failures = _merge_timeout_recoveries(unique_failures, timeout_failures)

    # gtest failures and timeouts reach the artifact with a placeholder instead
    # of a diagnostic; both have their detail only in the job log.
    enrich_log_only_errors(unique_failures, job_info, run_logs)

    # A red job the reported failures do not account for must not pass as clean,
    # whether or not anything else was reported.
    unexplained = _unexplained_failed_jobs(unique_failures, job_info.failed)
    if unexplained:
        logger.error(
            "%d failed job(s) are not accounted for by any reported failure: %s",
            len(unexplained), ", ".join(unexplained),
        )

    if not unique_failures:
        logger.info("No test failures to report.")
        emit_job_summary(
            _build_job_summary(run_id, repo_full_name, 0, {}, damaged, unexplained)
        )
        return 1 if (damaged or unexplained) else 0

    logger.info("Found %d unique failure(s)", len(unique_failures))

    if dry_run:
        logger.info("Dry run — skipping issue creation/update.")
        for f in unique_failures:
            envs = ", ".join(j.job for j in f.jobs)
            logger.info("  %s [%s]", f.display_name, envs)
        emit_job_summary(
            _build_job_summary(
                run_id, repo_full_name, len(unique_failures), {}, damaged, unexplained
            )
        )
        return 1 if (damaged or unexplained) else 0

    # Step 6: Create or update issues
    logger.info("Processing issues on %s...", repo_full_name)
    result = process_failures(gh, repo_full_name, unique_failures, run_id=run_id)

    emit_job_summary(
        _build_job_summary(
            run_id, repo_full_name, len(unique_failures), result, damaged, unexplained
        )
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
    # Some job failed for a reason nothing in the artifact or the logs explains.
    # Its cause got no issue, so the sweep must not report the run as clean.
    if unexplained:
        return 1
    return 0


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
        )
    )

if __name__ == "__main__":
    main()
