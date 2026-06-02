"""Download test failure artifacts from a Valkey CI workflow run"""

from __future__ import annotations

import io
import logging
import re
import urllib.request
import zipfile

from github import Github
from github.WorkflowRun import WorkflowRun

from scripts.common.github_client import retry_github_call

logger = logging.getLogger(__name__)


class _NoAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strip Authorization header when following redirects.

    GitHub's artifact download endpoint returns a 302 to a temporary blob
    storage URL. That URL uses its own auth baked into the query string;
    forwarding the GitHub Authorization header causes a 401.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return urllib.request.Request(newurl, headers={
            "Accept": "application/octet-stream",
        })


def _download_artifact(url: str, github_token: str) -> bytes:
    """Download a GitHub artifact zip, handling the auth-stripping redirect."""
    opener = urllib.request.build_opener(_NoAuthRedirectHandler)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
        },
    )
    with opener.open(req, timeout=120) as resp:
        return resp.read()


def get_latest_daily_run(
    gh: Github,
    repo_full_name: str,
    workflow_name: str = "Daily",
    branch: str = "unstable",
) -> WorkflowRun | None:
    """Find the most recent completed (non-cancelled) Daily workflow run.

    Returns None if no qualifying run is found.
    """
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
        lambda: daily_workflow.get_runs(branch=branch),
        retries=3,
        description=f"list runs for {workflow_name}",
    )

    for run in runs:
        if run.conclusion in ("cancelled", "skipped"):
            continue
        if run.status == "completed":
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
) -> bytes | None:
    """Download the 'all-test-failures' artifact from a workflow run.

    Returns the raw JSON content as bytes, or None if the artifact is not found.
    """
    repo = retry_github_call(
        lambda: gh.get_repo(repo_full_name),
        retries=3,
        description=f"get repo {repo_full_name}",
    )

    artifacts = retry_github_call(
        lambda: repo.get_workflow_run(run_id).get_artifacts(),
        retries=3,
        description=f"list artifacts for run {run_id}",
    )

    target_artifact = None
    for artifact in artifacts:
        if artifact.name == "all-test-failures":
            target_artifact = artifact
            break

    if target_artifact is None:
        logger.info("No 'all-test-failures' artifact found in run %d", run_id)
        return None

    logger.info("Downloading artifact: %s (id=%d)", target_artifact.name, target_artifact.id)

    url = target_artifact.archive_download_url
    zip_bytes = retry_github_call(
        lambda: _download_artifact(url, github_token),
        retries=3,
        description="download artifact zip",
    )

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        if not names:
            logger.warning("Artifact zip is empty")
            return None
        # The zip should contain all-test-failures.json
        json_name = next((n for n in names if n.endswith(".json")), names[0])
        logger.info("Extracting %s from artifact zip", json_name)
        return zf.read(json_name)


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
