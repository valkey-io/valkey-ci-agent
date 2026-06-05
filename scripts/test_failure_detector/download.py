"""Download test failure artifacts from a Valkey CI workflow run"""

from __future__ import annotations

import logging
import re

from github import Github
from github.WorkflowRun import WorkflowRun

from scripts.common.github_client import retry_github_call
from scripts.common.workflow_artifacts import ArtifactClient

logger = logging.getLogger(__name__)

# Name of the JSON file the Valkey CI workflow uploads inside its artifact zip.
_FAILURES_JSON_NAME = "all-test-failures.json"
_FAILURES_ARTIFACT_NAME = "all-test-failures"

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

    runs = retry_github_call(
        lambda: daily_workflow.get_runs(branch=branch, status="completed"),
        retries=3,
        description=f"list runs for {workflow_name}",
    )

    for run in runs:
        if run.conclusion in ("cancelled", "skipped", None):
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
) -> bytes | None:
    """Download the 'all-test-failures' artifact from a workflow run.

    Returns the raw JSON content as bytes, or None if the artifact (or the
    JSON file inside it) is not found. Delegates the listing, download, and
    zip extraction to the shared :class:`ArtifactClient`, which handles the
    auth-stripping redirect, transient-failure retries, expired (404)
    artifacts, and a runaway-extraction cap.
    """
    client = artifact_client or ArtifactClient(gh, token=github_token)

    artifacts = client.list_run_artifacts(repo_full_name, run_id)
    target = next(
        (a for a in artifacts if a.name == _FAILURES_ARTIFACT_NAME), None
    )
    if target is None:
        logger.info(
            "No %r artifact found in run %d", _FAILURES_ARTIFACT_NAME, run_id
        )
        return None
    if target.expired:
        logger.warning(
            "Artifact %r (id=%d) in run %d has expired",
            target.name, target.artifact_id, run_id,
        )
        return None

    logger.info("Downloading artifact: %s (id=%d)", target.name, target.artifact_id)
    files = client.download_artifact(repo_full_name, target.artifact_id)

    content = files.get(_FAILURES_JSON_NAME)
    if content is None:
        logger.warning(
            "Artifact zip for run %d does not contain %s; found: %s",
            run_id, _FAILURES_JSON_NAME, sorted(files),
        )
        return None

    logger.info("Extracted %s from artifact zip", _FAILURES_JSON_NAME)
    return content

def get_job_urls(
    gh: Github,
    repo_full_name: str,
    run_id: int,
) -> dict[str, str]:
    """Get a mapping of job name -> HTML URL for all jobs in a workflow run.

    Also includes normalized variants (parentheses replaced with dashes,
    spaces replaced with dashes) for fuzzy matching.
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

    jobs = retry_github_call(
        lambda: run.jobs(),
        retries=3,
        description=f"list jobs for run {run_id}",
    )

    job_url_map: dict[str, str] = {}
    for job in jobs:
        job_url_map[job.name] = job.html_url

        # Also store a normalized version for matching against artifact names
        normalized = re.sub(r"\s*\(([^)]+)\)", r"-\1", job.name)
        normalized = re.sub(r"\s+", "-", normalized)
        if normalized != job.name:
            job_url_map[normalized] = job.html_url

    logger.info("Found %d job URL mappings for run %d", len(job_url_map), run_id)
    return job_url_map
