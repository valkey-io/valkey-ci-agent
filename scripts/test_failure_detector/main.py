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
    get_job_urls,
    get_latest_daily_run,
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
    return "\n".join(lines)

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

    gh = Github(auth=Auth.Token(github_token))
    artifact_client = ArtifactClient(gh, token=github_token)

    # Step 1: Find the workflow run
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
    else:
        logger.info("Using specified run ID: %d", run_id)

    # Step 2: Download the all-test-failures artifact
    logger.info("Downloading all-test-failures artifact from run %d...", run_id)
    artifact_content = download_all_test_failures(
        gh, repo_full_name, run_id, github_token, artifact_client=artifact_client,
    )
    if artifact_content is None:
        logger.info("No test failures artifact found — CI run likely passed cleanly.")
        emit_job_summary(_build_job_summary(run_id, repo_full_name, 0, {}))
        return 0

    try:
        all_failures = json.loads(artifact_content)
    except json.JSONDecodeError as exc:
        # A malformed or truncated artifact must not crash the run before we
        # report; surface it in the job summary and exit non-zero instead.
        logger.error(
            "Could not parse all-test-failures artifact from run %d: %s", run_id, exc,
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
        logger.error(
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

    # Step 3: Get job URLs for CI links
    logger.info("Fetching job URLs...")
    job_urls = get_job_urls(gh, repo_full_name, run_id)

    # Step 4: Parse and deduplicate
    logger.info("Parsing and deduplicating failures...")
    unique_failures = parse_and_deduplicate(all_failures, job_urls)

    if not unique_failures:
        logger.info("No test failures to report.")
        emit_job_summary(_build_job_summary(run_id, repo_full_name, 0, {}))
        return 0

    logger.info("Found %d unique failure(s)", len(unique_failures))

    if dry_run:
        logger.info("Dry run — skipping issue creation/update.")
        for f in unique_failures:
            envs = ", ".join(j.job for j in f.jobs)
            logger.info("  %s [%s]", f.display_name, envs)
        emit_job_summary(_build_job_summary(run_id, repo_full_name, len(unique_failures), {}))
        return 0

    # Step 5: Create or update issues
    logger.info("Processing issues on %s...", repo_full_name)
    result = process_failures(gh, repo_full_name, unique_failures, run_id=run_id)

    emit_job_summary(_build_job_summary(run_id, repo_full_name, len(unique_failures), result))

    # process_failures isolates per-failure errors so one bad failure can't
    # abort the batch, but those failures got no issue created or updated. Exit
    # non-zero so a GitHub outage that skips issue updates doesn't leave CI green.
    if result.get("errors", 0) > 0:
        logger.error(
            "%d failure(s) could not be processed; exiting non-zero.",
            result["errors"],
        )
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
