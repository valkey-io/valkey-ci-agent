"""Which CI-fix run and job to fix.

Every front door reads its run's jobs through ``scripts.common.workflow_jobs``.
Code (not the AI) determines which jobs actually failed, and ``select_job``
chooses the failure most likely to have a deterministic fix.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from scripts.common.github_client import retry_github_call
from scripts.common.workflow_jobs import FAILED_JOB_CONCLUSIONS, jobs_for_run_attempt

logger = logging.getLogger(__name__)

FAILED_RUN_CONCLUSIONS = frozenset({"failure", "timed_out", "cancelled"})


@dataclass(frozen=True)
class FailedJob:
    """One concrete failed GitHub Actions job selected by code."""

    name: str
    id: int = 0


def workflow_identity(run: Any) -> str:
    """Return the most stable available identity for a workflow run."""
    for attribute in ("workflow_id", "path", "name"):
        value = str(getattr(run, attribute, "") or "").strip()
        if value:
            return f"{attribute}:{value}"
    return f"run:{int(getattr(run, 'id', 0) or 0)}"


def workflow_file(run: Any) -> str:
    """The run's workflow file name without ``.yml`` (``ci`` for ``.github/workflows/ci.yml``), or ""."""
    name = str(getattr(run, "path", "") or "").rsplit("/", 1)[-1].split("@", 1)[0]
    for suffix in (".yml", ".yaml"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return ""


def newest_runs_by_workflow(runs: Iterable[Any]) -> tuple[Any, ...]:
    """Collapse reruns to the newest exact run attempt per workflow.

    GitHub can return multiple runs of the same workflow for one commit. An
    automatic selector must not act on an older failure after a newer run of
    that workflow has passed (or while its replacement is still running).
    """
    newest: dict[tuple[str, str], Any] = {}
    for run in runs:
        identity = (
            workflow_identity(run),
            str(getattr(run, "event", "") or "").strip(),
        )
        previous = newest.get(identity)
        if previous is None or _run_order(run) > _run_order(previous):
            newest[identity] = run
    return tuple(
        sorted(
            newest.values(),
            key=_run_order,
            reverse=True,
        )
    )


def newest_runs_for_head(
    gh: Any,
    repo_full_name: str,
    *,
    branch: str,
    head_sha: str,
    retries: int = 2,
) -> tuple[Any, ...]:
    """Load the newest workflow executions for one exact branch head.

    GitHub's branch/head filters are useful query hints, but the exact SHA is
    checked locally before stale workflow executions are collapsed. Callers
    therefore share one definition of "current-head CI".
    """
    if not head_sha:
        raise ValueError("head SHA must not be empty")

    def _fetch() -> list[Any]:
        repo = gh.get_repo(repo_full_name)
        return list(repo.get_workflow_runs(branch=branch, head_sha=head_sha))

    runs = retry_github_call(
        _fetch,
        retries=retries,
        description=f"list workflow runs for {repo_full_name}@{head_sha}",
    )
    return newest_runs_by_workflow(
        run
        for run in runs
        if str(getattr(run, "head_sha", "") or "") == head_sha
        and str(getattr(run, "head_branch", "") or "") == branch
    )


def _run_order(run: Any) -> tuple[float, int, int]:
    """Order workflow executions by observed execution time, then identity."""
    return (
        _execution_timestamp(run),
        int(getattr(run, "id", 0) or 0),
        int(getattr(run, "run_attempt", 1) or 1),
    )


def _execution_timestamp(run: Any) -> float:
    """Prefer start time so a slow older run cannot supersede a newer run."""
    started = _timestamp(getattr(run, "run_started_at", None))
    if started:
        return started
    updated = _timestamp(getattr(run, "updated_at", None))
    if updated:
        return updated
    return _timestamp(getattr(run, "created_at", None))


def _timestamp(value: Any) -> float:
    """Normalize PyGithub datetimes and ISO-8601 API strings for sorting."""
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return 0.0
    else:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def failed_jobs_for_run(
    gh: Any,
    repo_full_name: str,
    run_id: int,
    *,
    run_attempt: int,
    retries: int = 2,
) -> list[FailedJob]:
    """Return genuine failures from one shared exact-attempt jobs snapshot."""
    return failed_jobs(
        jobs_for_run_attempt(
            gh,
            repo_full_name,
            run_id,
            run_attempt=run_attempt,
            retries=retries,
        )
    )


def failed_jobs(jobs: Iterable[Any]) -> list[FailedJob]:
    """Project a raw jobs snapshot down to genuine actionable failures."""
    return [
        FailedJob(name=str(getattr(j, "name", "") or ""), id=int(getattr(j, "id", 0) or 0))
        for j in jobs
        if str(getattr(j, "conclusion", "") or "") in FAILED_JOB_CONCLUSIONS
    ]


def job_priority(name: str) -> int:
    """Rank a failed job by how likely the bot is to fix it and show the fix works.

    A test failure comes first: code reproduces it, and verifies the fix with
    it. A formatting, lint or build job comes next. Its fix is often in the
    CI definition or the toolchain, which the bot never changes, and a build
    break in the code also fails every test job, whose log shows it. A
    sanitizer or valgrind failure is the most likely to be flaky or to need a
    judgement call. Unrecognised names sort last.
    """
    lowered = name.lower()
    if any(token in lowered for token in ("asan", "ubsan", "tsan", "sanitizer", "valgrind")):
        return 2
    if any(
        token in lowered
        # Not "schema": Daily's reply-schemas-validator runs the whole test
        # suite; the reply-schemas-linter is already matched by "lint".
        for token in ("format", "lint", "generated", "build", "compile")
    ):
        return 1
    if any(token in lowered for token in ("unit", "integration", "test")):
        return 0
    return 3


def most_actionable_failure(
    gh: Any,
    repo_full_name: str,
    runs: Iterable[Any],
    *,
    keep: Callable[[Any, FailedJob], bool] = lambda _run, _job: True,
) -> tuple[Any, FailedJob, list[FailedJob]] | None:
    """The completed failed run among ``runs``, its failed job most likely to have a fix, and all its failed jobs.

    Only the jobs ``keep`` accepts compete. The best ``job_priority`` wins,
    then the newest run.
    """
    best: tuple[tuple[int, int, str], Any, FailedJob, list[FailedJob]] | None = None
    for run in runs:
        if str(getattr(run, "status", "") or "") != "completed":
            continue
        if str(getattr(run, "conclusion", "") or "") not in FAILED_RUN_CONCLUSIONS:
            continue
        failed = failed_jobs_for_run(
            gh, repo_full_name, int(run.id), run_attempt=int(getattr(run, "run_attempt", 1) or 1),
        )
        candidates = [job for job in failed if keep(run, job)]
        if not candidates:
            continue
        selected = select_job(candidates)
        key = (job_priority(selected.name), -int(run.id), selected.name.casefold())
        if best is None or key < best[0]:
            best = (key, run, selected, failed)
    return (best[1], best[2], best[3]) if best is not None else None


def select_job(jobs: Iterable[FailedJob]) -> FailedJob:
    """Choose one failure: the tier most likely fixed and verified, then the lowest job id."""
    return min(
        jobs,
        key=lambda job: (job_priority(job.name), int(job.id), job.name.lower()),
    )
