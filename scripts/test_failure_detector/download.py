"""Download test failure artifacts from a Valkey CI workflow run"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from itertools import islice
from typing import Any

from github import Github
from github.WorkflowRun import WorkflowRun

from scripts.common.github_client import retry_github_call
from scripts.common.workflow_artifacts import ArtifactClient

logger = logging.getLogger(__name__)

# Name of the JSON file the Valkey CI workflow uploads inside its artifact zip.
_FAILURES_JSON_NAME = "all-test-failures.json"
_FAILURES_ARTIFACT_NAME = "all-test-failures"

# How many runs to scan for the newest usable one. Only pull_request runs sit
# between nightly crons, a handful a day at most, so this reaches well past
# yesterday's schedule run; beyond it, reporting nothing found beats paging
# through the whole history.
_MAX_RUNS_SCANNED = 50

# Only the nightly schedule is discovered automatically. A dispatched Daily is
# not the same run: its inputs can skip jobs and point the checkout at another
# repository or ref, so its failures need not belong to this branch's code, and
# a partial run's absent jobs would read as passing. A maintainer who does want
# one analyzed can name it with --run-id, which bypasses this filter.
_ANALYZABLE_EVENTS = frozenset({"schedule"})

def get_latest_daily_run(
    gh: Github,
    repo_full_name: str,
    workflow_name: str = "Daily",
    branch: str = "unstable",
) -> WorkflowRun | None:
    """Find the most recent completed (non-cancelled) Daily workflow run."""
    repo = retry_github_call(
        lambda: gh.get_repo(repo_full_name),
        retries=3,
        description=f"get repo {repo_full_name}",
    )

    workflows = retry_github_call(
        lambda: repo.get_workflows(),
        retries=3,
        description="list workflows",
    )

    daily_workflow = None
    for wf in workflows:
        if wf.name == workflow_name:
            daily_workflow = wf
            break

    if daily_workflow is None:
        logger.warning("Workflow %r not found in %s", workflow_name, repo_full_name)
        return None

    # Events are filtered locally, alongside the conclusion check below, so both
    # rejection reasons are visible in one place and log the run they skipped.
    # The scan is bounded instead: pull_request runs interleave with the nightly
    # cron, so the newest schedule run sits a few entries down.
    #
    # get_runs() is lazy, so the islice must run inside the retried call for the
    # retries to cover the actual request.
    runs = retry_github_call(
        lambda: list(islice(
            daily_workflow.get_runs(branch=branch, status="completed"),
            _MAX_RUNS_SCANNED,
        )),
        retries=3,
        description=f"list runs for {workflow_name}",
    )

    for run in runs:
        # The Daily workflow also runs on pull_request. Such a run tests the
        # PR's merge commit, not the branch, so its failures belong to the PR
        # and filing them as branch failures would blame the wrong code. Most
        # sit at action_required and would be dropped below anyway, but a PR
        # from a branch in the same repo runs without approval and reaches a
        # real conclusion, so the event must be checked explicitly.
        if run.event not in _ANALYZABLE_EVENTS:
            logger.debug(
                "Skipping run #%d (event=%s)", run.run_number, run.event,
            )
            continue
        # Skip runs that never actually executed: cancelled/skipped, runs
        # awaiting approval (action_required, e.g. fork PRs), expired (stale),
        # runs that died before any job started (startup_failure, e.g. invalid
        # workflow YAML), and runs with no conclusion yet. These produce no test
        # artifacts and would be mistaken for a clean pass.
        if run.conclusion in (
            "cancelled", "skipped", "action_required", "stale",
            "startup_failure", None,
        ):
            logger.debug(
                "Skipping run #%d (conclusion=%s)", run.run_number, run.conclusion,
            )
            continue
        logger.info(
            "Found daily run #%d (id=%d, conclusion=%s, created=%s)",
            run.run_number, run.id, run.conclusion, run.created_at,
        )
        return run

    logger.warning("No completed non-cancelled run found for %s/%s", workflow_name, branch)
    return None

def download_all_test_failures(
    gh: Github,
    repo_full_name: str,
    run_id: int,
    github_token: str,
    *,
    artifact_client: ArtifactClient | None = None,
    damaged: list[str] | None = None,
) -> bytes | None:
    """Download the 'all-test-failures' artifact from a workflow run.

    Returns the raw JSON content as bytes, or None if the artifact (or the
    JSON file inside it) is not found. Delegates the listing, download, and
    zip extraction to the shared :class:`ArtifactClient`, which handles the
    auth-stripping redirect, transient-failure retries, expired (404)
    artifacts, and a runaway-extraction cap.

    Pass ``damaged`` to collect the zip members that could not be read. The
    failures JSON can survive alongside them, so the caller needs this to know
    the run was only partly analyzed.
    """
    client = artifact_client or ArtifactClient(gh, token=github_token)

    artifacts = client.list_run_artifacts(
        repo_full_name, run_id, name=_FAILURES_ARTIFACT_NAME,
    )
    matches = [a for a in artifacts if a.name == _FAILURES_ARTIFACT_NAME]
    if not matches:
        logger.info(
            "No %r artifact found in run %d", _FAILURES_ARTIFACT_NAME, run_id
        )
        return None

    # Re-running a workflow leaves one artifact per attempt under the same run
    # and name, each expiring on its own clock. Take the newest live one: a
    # stale earlier attempt must not shadow the re-run's usable artifact.
    live = [a for a in matches if not a.expired]
    if not live:
        logger.warning(
            "All %d %r artifact(s) in run %d have expired",
            len(matches), _FAILURES_ARTIFACT_NAME, run_id,
        )
        return None
    target = max(live, key=lambda a: a.artifact_id)

    logger.info("Downloading artifact: %s (id=%d)", target.name, target.artifact_id)
    files = client.download_artifact(
        repo_full_name, target.artifact_id, damaged=damaged,
    )

    content = files.get(_FAILURES_JSON_NAME)
    if content is None:
        logger.warning(
            "Artifact zip for run %d does not contain %s; found: %s",
            run_id, _FAILURES_JSON_NAME, sorted(files),
        )
        return None

    logger.info("Extracted %s from artifact zip", _FAILURES_JSON_NAME)
    return content

def get_run_conclusion(
    gh: Github,
    repo_full_name: str,
    run_id: int,
) -> str | None:
    """A workflow run's conclusion, or None if it cannot be determined.

    Used when the run was named explicitly rather than discovered, so the
    caller can still tell a red run apart from a clean one. Returns None on
    any API failure: the conclusion only sharpens an error message, so it must
    not turn a usable run into a hard failure.
    """
    try:
        repo = retry_github_call(
            lambda: gh.get_repo(repo_full_name),
            retries=3,
            description=f"get repo {repo_full_name}",
        )
        run = retry_github_call(
            lambda: repo.get_workflow_run(run_id),
            retries=3,
            description=f"get run {run_id}",
        )
    except Exception:
        logger.warning(
            "Could not fetch conclusion for run %d", run_id, exc_info=True,
        )
        return None
    return run.conclusion


@dataclass(frozen=True)
class JobInfo:
    """Job metadata derived from a workflow run's job list.

    ``urls`` maps job name (and normalized aliases) to the job's HTML URL.
    ``step_urls`` maps job name -> suite -> a URL anchored to the step that
    ran that suite, so a failure links to its own step rather than the job's
    first failed step. ``failed`` holds the names of jobs that failed.
    """

    urls: dict[str, str]
    failed: set[str]
    step_urls: dict[str, dict[str, str]] = field(default_factory=dict)

    def url_for(self, job_name: str, suite: str) -> str:
        """URL for a suite's failure in a job, anchored to its step if known.

        Falls back to the plain job URL when the suite's step cannot be
        identified (an unmapped suite, or a job whose steps were unavailable).
        """
        step_url = self.step_urls.get(job_name, {}).get(suite)
        return step_url or self.urls.get(job_name, "")


# Job conclusions that mean the job did not pass and so may hold a failure the
# artifact never captured. A job the runner killed on the job timeout concludes
# "timed_out" rather than "failure", and that is exactly the case whose console
# log still carries the [TIMEOUT] lines timeout recovery reads, so leaving it out
# skipped the jobs the scan exists for.
_FAILED_JOB_CONCLUSIONS = frozenset({"failure", "timed_out"})

# The Daily workflow runs each test suite in a named step. A suite's failures
# live in <suite>.json, so link a failure to the step that produced it instead
# of the job's first failed step. Keys are artifact suite names; values match a
# step name. gtest failures come from the unittest step but are extracted in a
# following step, so both spellings map to the unittest suite.
_SUITE_STEP_NAMES: dict[str, str] = {
    "valkey": "test",
    "moduleapi": "module api test",
    "sentinel": "sentinel tests",
    "unittest": "unittest",
}


def _suite_step_number(suite: str, steps: list[Any]) -> int | None:
    """Return the 1-based step number that ran ``suite``, or None.

    Matches the mapped step name case-insensitively. Returns None for a suite
    with no mapping or when no step name matches, so the caller keeps the plain
    job URL.
    """
    step_name = _SUITE_STEP_NAMES.get(suite)
    if step_name is None:
        return None
    for step in steps:
        if step.name and step.name.lower() == step_name.lower():
            return step.number
    return None


def normalize_job_name(job_name: str) -> str:
    """Convert an API job name to the spelling the artifact uses.

    A matrix job is named ``base (value)`` by the API, but its artifact is
    uploaded as ``base-value`` because the workflow interpolates the matrix
    variable into ``job-name``. Callers matching one against the other must
    normalize first.
    """
    collapsed = re.sub(r"\s*\(([^)]+)\)", r"-\1", job_name)
    return re.sub(r"\s+", "-", collapsed)


def get_job_info(
    gh: Github,
    repo_full_name: str,
    run_id: int,
) -> JobInfo:
    """Fetch job metadata for a workflow run in a single API call.

    Returns a :class:`JobInfo` containing:
    - ``urls``: job name -> HTML URL (includes normalized aliases for fuzzy
      matching against artifact names).
    - ``failed``: names of jobs whose conclusion indicates failure.
    """

    repo = retry_github_call(
        lambda: gh.get_repo(repo_full_name),
        retries=3,
        description=f"get repo {repo_full_name}",
    )

    run = retry_github_call(
        lambda: repo.get_workflow_run(run_id),
        retries=3,
        description=f"get run {run_id}",
    )

    # The list() must happen inside the retried call: jobs() returns a lazy
    # PaginatedList that issues no request until iterated, so retrying only the
    # construction would leave the actual HTTP call unprotected.
    job_list = retry_github_call(
        lambda: list(run.jobs()),
        retries=3,
        description=f"list jobs for run {run_id}",
    )

    job_url_map: dict[str, str] = {job.name: job.html_url for job in job_list}
    failed_jobs: set[str] = set()
    step_url_map: dict[str, dict[str, str]] = {}

    for job in job_list:
        normalized = normalize_job_name(job.name)
        if job.conclusion in _FAILED_JOB_CONCLUSIONS:
            # Both spellings are recorded. The API names a matrix job
            # "base (value)" while the artifact keys it "base-value", and
            # callers join on one or the other: log recovery looks up the
            # artifact's key, enrichment intersects the failure's job names
            # (artifact spelling) with this set. Holding only the API name left
            # every matrix job out of that intersection, so a matrix job's
            # gtest and timeout placeholders were never enriched.
            failed_jobs.add(job.name)
            failed_jobs.add(normalized)

        if normalized != job.name and normalized not in job_url_map:
            job_url_map[normalized] = job.html_url

        # Anchor each suite to the step that ran it. The list endpoint returns
        # steps inline, so this costs no extra API call. A job with no matching
        # steps contributes nothing and keeps the plain job URL.
        suite_urls: dict[str, str] = {}
        for suite in _SUITE_STEP_NAMES:
            number = _suite_step_number(suite, job.steps)
            if number is not None:
                suite_urls[suite] = f"{job.html_url}#step:{number}:1"
        if suite_urls:
            step_url_map[job.name] = suite_urls
            if normalized != job.name:
                step_url_map.setdefault(normalized, suite_urls)

    logger.info(
        "Found %d job URL mappings (%d failed) for run %d",
        len(job_url_map), len(failed_jobs), run_id,
    )
    return JobInfo(urls=job_url_map, failed=failed_jobs, step_urls=step_url_map)


def get_job_urls(
    gh: Github,
    repo_full_name: str,
    run_id: int,
) -> dict[str, str]:
    """Get a mapping of job name -> HTML URL for all jobs in a workflow run.

    Also includes normalized variants (parentheses replaced with dashes,
    spaces replaced with dashes) for fuzzy matching.

    Timeout recovery also needs to know which jobs failed, so the detector
    calls :func:`get_job_info` and reads both fields off one response rather
    than paying for a second job listing. This narrower view is kept for
    callers that only want the URLs.
    """
    return get_job_info(gh, repo_full_name, run_id).urls
