"""GitHub Actions run mechanics, isolated from the verifier logic.

Code (not the AI) determines which jobs actually failed in the linked run, so
the verifier layer can require the AI's hinted job to be a real failure and
classify that exact job. Keeping the GitHub specifics here keeps the verifier
backends focused on running and judging a command.
"""

from __future__ import annotations

import logging
from typing import Any

from scripts.ci_fix.verify.base import FailedJob
from scripts.common.github_client import retry_github_call

logger = logging.getLogger(__name__)

# A cancelled job is usually a fail-fast skip after another job failed, not a
# real failure; fixing it would be wrong. Only genuine failures are targets.
_FAILED_CONCLUSIONS = {"failure", "timed_out"}


def failed_jobs_for_run(gh: Any, repo_full_name: str, run_id: int, *, retries: int = 2) -> list[FailedJob]:
    """Return the jobs that did not succeed in ``run_id``.

    A read failure yields an empty list, which the caller treats as "cannot
    confirm the failed job" and refuses.
    """
    def _fetch() -> list[Any]:
        run = gh.get_repo(repo_full_name).get_workflow_run(run_id)
        return list(run.jobs())

    try:
        jobs = retry_github_call(_fetch, retries=retries, description=f"list jobs for run {run_id}")
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("Could not list jobs for run %s: %s", run_id, exc)
        return []
    return [
        FailedJob(name=str(getattr(j, "name", "") or ""),
                  conclusion=str(getattr(j, "conclusion", "") or ""))
        for j in jobs
        if str(getattr(j, "conclusion", "") or "") in _FAILED_CONCLUSIONS
    ]
