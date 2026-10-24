"""Fill the CI health store from GitHub Actions.

The store (:mod:`scripts.ci_health.store`) holds every run of a workflow,
the jobs of each failed run, and the failures parsed from each failed job's
log. This module is the only part of ``ci_health`` that talks to GitHub,
and every call it makes is a read: run and job listings, job logs, and the
commit comparison blame needs.

Syncing is incremental. A run already stored is not listed again, a job
list already stored is not fetched again unless a job was still running,
and a log already stored is never downloaded again. One run of the workflow
on a day with no new Daily run costs a single API call per workflow file.

GitHub keeps job logs for about 90 days. A failed job whose log has expired
is recorded as such so it is never asked for again; its conclusion still
counts in the run's history, it just has no parsed failures.

The weekly workflow fires once on Sunday and runs ``daily.yml`` once per
release branch inside a single run. :meth:`HistorySync.sync_weekly_branches`
splits that run into one synthetic run per branch (see
:mod:`scripts.ci_health.weekly`) and fetches the failure logs GitHub still
retains, within a budget of API calls, so the backlog of old weekly runs is
worked through across several days rather than in one run that trips the
rate limit.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol
from urllib.error import HTTPError

from github import Github, GithubException, RateLimitExceededException

from scripts.ci_health.log_parser import parse_job_log
from scripts.ci_health.store import Cache
from scripts.ci_health.weekly import (
    WEEKLY_SPLIT_WORKFLOW,
    build_synthetic_runs,
    split_jobs_by_branch,
)
from scripts.common.github_client import retry_github_call
from scripts.common.workflow_artifacts import ArtifactClient

logger = logging.getLogger(__name__)

ProgressCallback = Optional[Callable[[str], None]]

# The workflow files of valkey-io/valkey that the store tracks, by the short
# name the command line accepts.
WORKFLOW_FILES = {
    "daily": "daily.yml",
    "weekly": "weekly.yml",
    "ci": "ci.yml",
}

# ci.yml runs on every push and every pull request. Only the post-merge runs
# on the target branch describe the branch's health, so it is listed with
# the ``push`` event only. Files absent here are listed for every event.
_WORKFLOW_EVENTS = {
    "ci.yml": "push",
}

_PER_PAGE = 100


class RunsClient(Protocol):
    """What the sync needs from GitHub: listings, logs, and a commit range.

    ``requests_made`` counts API calls, so a budgeted pass can stop before
    the rate limit does. The protocol also satisfies
    :class:`scripts.ci_health.blame.CommitComparer`.
    """

    repo: str
    requests_made: int

    def list_runs(
        self,
        workflow_file: str,
        *,
        branch: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        event: Optional[str] = None,
    ) -> List[Dict]: ...

    def list_jobs(self, run_id: int) -> List[Dict]: ...

    def job_log(self, job_id: int) -> str:
        """The job's console log, or ``""`` when GitHub no longer has it."""
        ...

    def compare_commits(self, base_sha: str, head_sha: str) -> List[Dict]: ...


class GitHubRunsClient:
    """A :class:`RunsClient` over PyGithub and the shared artifact client."""

    def __init__(self, gh: Github, repo: str, *, token: str, retries: int = 3) -> None:
        self._gh = gh
        self.repo = repo
        self.requests_made = 0
        self._retries = retries
        self._artifacts = ArtifactClient(gh, token=token, retries=retries)

    def _get_pages(self, path: str, key: str, params: Dict[str, object]) -> List[Dict]:
        # The REST pages are read directly, as the artifact client does for
        # artifact listings, so one API call is one request counted and the
        # ``created`` range filter can be passed through as GitHub spells it.
        requester = self._gh.get_repo(self.repo)._requester
        items: List[Dict] = []
        page = 1
        while True:
            page_params = {**params, "per_page": _PER_PAGE, "page": page}

            def _fetch() -> Any:
                _, data = requester.requestJsonAndCheck("GET", path, parameters=page_params)
                return data

            self.requests_made += 1
            data = retry_github_call(_fetch, retries=self._retries, description=f"GET {path}")
            batch = data.get(key, []) if isinstance(data, dict) else []
            items.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < _PER_PAGE:
                return items
            page += 1

    def list_runs(
        self,
        workflow_file: str,
        *,
        branch: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        event: Optional[str] = None,
    ) -> List[Dict]:
        params: Dict[str, object] = {}
        if branch is not None:
            params["branch"] = branch
        if event is not None:
            params["event"] = event
        if since and until:
            params["created"] = f"{since}..{until}"
        elif since:
            params["created"] = f">={since}"
        elif until:
            params["created"] = f"<={until}"
        return self._get_pages(
            f"/repos/{self.repo}/actions/workflows/{workflow_file}/runs", "workflow_runs", params,
        )

    def list_jobs(self, run_id: int) -> List[Dict]:
        return self._get_pages(f"/repos/{self.repo}/actions/runs/{int(run_id)}/jobs", "jobs", {})

    def job_log(self, job_id: int) -> str:
        self.requests_made += 1
        return self._artifacts.download_job_log(self.repo, job_id)

    def compare_commits(self, base_sha: str, head_sha: str) -> List[Dict]:
        def _fetch() -> List[Dict]:
            comparison = self._gh.get_repo(self.repo).compare(base_sha, head_sha)
            return [commit.raw_data for commit in comparison.commits]

        self.requests_made += 1
        raw = retry_github_call(
            _fetch, retries=self._retries, description=f"compare {base_sha[:7]}...{head_sha[:7]}",
        )
        return [_compact_commit(c) for c in raw]


def _compact_commit(commit: Dict) -> Dict:
    """The fields blame shows for a commit, from the REST representation."""
    details = commit.get("commit") or {}
    author = details.get("author") or {}
    return {
        "sha": commit.get("sha", ""),
        "message": (details.get("message") or "").split("\n")[0],
        "author": author.get("name", ""),
        "date": author.get("date", ""),
    }


def is_auth_failure(exc: BaseException) -> bool:
    """Whether ``exc`` means the token is dead, expired or under-scoped.

    A 401, or a 403 that is not the rate limit. The sync reports this as
    fatal so a broken token never passes as a quiet day.
    """
    if isinstance(exc, RateLimitExceededException):
        return False
    if isinstance(exc, GithubException):
        return exc.status in (401, 403)
    if isinstance(exc, HTTPError):
        return exc.code in (401, 403)
    return False


def _compute_duration(run: Dict) -> Optional[int]:
    """Seconds from ``run_started_at`` to ``updated_at``, or None if unknown."""
    started = run.get("run_started_at") or run.get("created_at")
    ended = run.get("updated_at")
    if not started or not ended:
        return None
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        start_dt = datetime.strptime(started, fmt).replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(ended, fmt).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    delta = int((end_dt - start_dt).total_seconds())
    return delta if delta >= 0 else None


def map_run(api_run: Dict, repo: str) -> Dict:
    """A workflow-run object from the API as a store row."""
    path = api_run.get("path")
    return {
        "run_id": api_run["id"],
        "repo": repo,
        "workflow_file": path.rsplit("/", 1)[-1] if path else api_run.get("name", ""),
        "status": api_run.get("conclusion") or api_run.get("status", "unknown"),
        "branch": api_run.get("head_branch", ""),
        "commit_sha": api_run.get("head_sha", ""),
        "run_date": api_run.get("run_started_at") or api_run.get("created_at", ""),
        "duration_secs": _compute_duration(api_run),
        "raw_json": json.dumps(api_run),
    }


def map_job(api_job: Dict) -> Dict:
    """A job object from the API as a store row."""
    return {
        "job_id": api_job["id"],
        "name": api_job.get("name", ""),
        "status": api_job.get("status", ""),
        "conclusion": api_job.get("conclusion"),
        "raw_json": json.dumps(api_job),
    }


class HistorySync:
    """Brings the store up to date with what GitHub Actions has run."""

    def __init__(self, client: RunsClient, cache: Cache) -> None:
        self._client = client
        self._cache = cache

    # ------------------------------------------------------------------
    # One step at a time
    # ------------------------------------------------------------------

    def fetch_runs(
        self,
        workflow: str,
        branch: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> List[Dict]:
        """Store the runs GitHub lists that are new, then return all stored runs that match."""
        workflow_file = WORKFLOW_FILES.get(workflow, workflow)
        api_runs = self._client.list_runs(
            workflow_file, branch=branch, since=since, until=until,
            event=_WORKFLOW_EVENTS.get(workflow_file),
        )
        new_runs = [
            run for run in (map_run(raw, self._client.repo) for raw in api_runs)
            if not self._cache.has_run(run["run_id"])
        ]
        if new_runs:
            self._cache.store_runs(new_runs)
        return self._cache.query_runs(
            repo=self._client.repo, workflow=workflow_file, branch=branch, since=since, until=until,
        )

    def fetch_jobs(self, run_id: int) -> List[Dict]:
        """Return the run's jobs, fetching them if unknown or if one was still running."""
        if not self._cache.has_jobs_for_run(run_id) or self._cache.has_incomplete_jobs(run_id):
            jobs = [map_job(raw) for raw in self._client.list_jobs(run_id)]
            if jobs:
                self._cache.store_jobs(run_id, jobs)
        return self._cache.query_jobs(run_id)

    def fetch_log(self, job_id: int) -> Optional[str]:
        """Return the job's log, downloading it once; None when GitHub no longer has it.

        An expired log is recorded so the job is never asked for again.
        """
        cached = self._cache.get_log(job_id)
        if cached is not None:
            return cached
        raw_log = self._client.job_log(job_id)
        if not raw_log.strip():
            self._cache.mark_log_expired(job_id)
            return None
        self._cache.store_log(job_id, raw_log)
        return raw_log

    def parse_log(self, job_id: int) -> List[Dict]:
        """Parse the stored log of a job and store what it finds."""
        raw_log = self._cache.get_log(job_id)
        if raw_log is None:
            return []
        failures = parse_job_log(raw_log, job_name=self._cache.get_job_name(job_id))
        if not failures:
            self._cache.mark_unparseable(job_id)
            return []
        rows = [
            {"test_name": f.test_name, "error_summary": f.error_summary, "log_lines": f.log_lines}
            for f in failures
        ]
        self._cache.store_failures(job_id, rows)
        return rows

    # ------------------------------------------------------------------
    # The whole sync
    # ------------------------------------------------------------------

    def sync(
        self,
        workflow: Optional[str] = None,
        branch: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        progress: ProgressCallback = None,
    ) -> Dict:
        """List new runs; for each failed one, fetch and parse its failed jobs' logs.

        Returns a summary. A problem with one run or one job is recorded in
        ``errors`` and the sync goes on; an authentication failure sets
        ``auth_failed`` so the caller can exit non-zero.
        """

        def _log(msg: str) -> None:
            if progress:
                progress(msg)

        summary: Dict = {
            "new_runs_fetched": 0,
            "new_jobs_fetched": 0,
            "new_logs_fetched": 0,
            "logs_expired": 0,
            "new_failures_parsed": 0,
            "errors": [],
            "auth_failed": False,
        }
        workflows = [workflow] if workflow else list(WORKFLOW_FILES)

        for wf in workflows:
            workflow_file = WORKFLOW_FILES.get(wf, wf)
            _log(f"Fetching {wf} runs...")
            try:
                known = {
                    r["run_id"]
                    for r in self._cache.query_runs(repo=self._client.repo, workflow=workflow_file)
                }
                api_runs = self._client.list_runs(
                    workflow_file, branch=branch, since=since, until=until,
                    event=_WORKFLOW_EVENTS.get(workflow_file),
                )
                new_runs = [
                    run for run in (map_run(raw, self._client.repo) for raw in api_runs)
                    if run["run_id"] not in known
                ]
                if new_runs:
                    self._cache.store_runs(new_runs)
            except Exception as exc:  # noqa: BLE001 - every failure is reported in the summary
                summary["errors"].append(f"fetch_runs({wf}): {exc}")
                if is_auth_failure(exc):
                    summary["auth_failed"] = True
                _log(f"  Error fetching {wf} runs: {exc}")
                continue

            _log(f"  {len(new_runs)} new runs")
            summary["new_runs_fetched"] += len(new_runs)

            failed_runs = [r for r in new_runs if r["status"] == "failure"]
            if failed_runs:
                _log(f"  {len(failed_runs)} failed runs to process")

            for run in failed_runs:
                run_id = run["run_id"]
                _log(f"  Fetching jobs for run {run_id}...")
                try:
                    had_jobs = self._cache.has_jobs_for_run(run_id)
                    jobs = self.fetch_jobs(run_id)
                except Exception as exc:  # noqa: BLE001
                    summary["errors"].append(f"fetch_jobs(run={run_id}): {exc}")
                    if is_auth_failure(exc):
                        summary["auth_failed"] = True
                    _log(f"    Error: {exc}")
                    continue
                summary["new_jobs_fetched"] += 0 if had_jobs else len(jobs)

                failed_jobs = [j for j in jobs if j.get("conclusion") == "failure"]
                _log(f"    {len(failed_jobs)} failed jobs")
                for job in failed_jobs:
                    self._ingest_job_log(job, summary, _log)

        # Logs stored by an earlier parser version have had their results
        # wiped by the store; parse them again with this one.
        unparsed_jobs = self._cache.query_unparsed_jobs_with_logs()
        if unparsed_jobs:
            _log(f"Re-parsing {len(unparsed_jobs)} jobs with stale/missing parse results...")
            for job_id in unparsed_jobs:
                try:
                    summary["new_failures_parsed"] += len(self.parse_log(job_id))
                except Exception as exc:  # noqa: BLE001
                    summary["errors"].append(f"reparse(job={job_id}): {exc}")

        _log(
            f"Sync complete: {summary['new_runs_fetched']} runs, "
            f"{summary['new_failures_parsed']} failures parsed"
        )
        return summary

    def _ingest_job_log(self, job: Dict, summary: Dict, _log: Callable[[str], None]) -> None:
        job_id = job["job_id"]
        job_name = job.get("name", str(job_id))
        try:
            if not self._cache.has_log(job_id):
                _log(f"    Fetching log for {job_name}...")
                if self.fetch_log(job_id) is None:
                    summary["logs_expired"] += 1
                    return
                summary["new_logs_fetched"] += 1
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"fetch_log(job={job_id}): {exc}")
            if is_auth_failure(exc):
                summary["auth_failed"] = True
            _log(f"    Error fetching log: {exc}")
            return
        try:
            if not self._cache.has_failures_for_job(job_id):
                _log(f"    Parsing log for {job_name}...")
                failures = self.parse_log(job_id)
                summary["new_failures_parsed"] += len(failures)
                _log(f"      {len(failures)} failures found")
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"parse_log(job={job_id}): {exc}")
            _log(f"    Error parsing log: {exc}")

    # ------------------------------------------------------------------
    # Weekly release-branch runs, within a budget
    # ------------------------------------------------------------------

    def sync_weekly_branches(self, budget: int = 300, progress: ProgressCallback = None) -> Dict:
        """Split weekly runs into per-branch series and fetch the logs GitHub still has.

        Runs are worked newest-first, each marked ``split`` once its jobs
        are stored under synthetic per-branch runs and ``done`` once every
        failed job's log is fetched, parsed, or known to have expired. The
        pass stops when ``budget`` API calls are spent and the next one
        resumes where it left off, so steady state costs only the newest
        run. Jobs the ordinary sync already stored cost nothing.
        """

        def _log(msg: str) -> None:
            if progress:
                progress(msg)

        start_requests = self._client.requests_made

        def _budget_left() -> int:
            return budget - (self._client.requests_made - start_requests)

        summary: Dict = {
            "runs_split": 0,
            "runs_completed": 0,
            "logs_fetched": 0,
            "logs_expired": 0,
            "failures_parsed": 0,
            "budget_used": 0,
            "budget_exhausted": False,
            "errors": [],
            "auth_failed": False,
        }

        # Scheduled weekly runs only: a pull request that touches the
        # workflow file runs it without the release-branch fan-out.
        try:
            self.fetch_runs("weekly", branch="unstable")
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"fetch_runs(weekly): {exc}")
            if is_auth_failure(exc):
                summary["auth_failed"] = True
            summary["budget_used"] = self._client.requests_made - start_requests
            return summary

        weekly_runs = [
            r for r in self._cache.query_runs(
                repo=self._client.repo, workflow="weekly.yml", branch="unstable",
            )
            if r["status"] in ("success", "failure")
        ]  # newest first

        for run in weekly_runs:
            run_id = run["run_id"]
            status = self._cache.get_weekly_ingest_status(run_id)
            if status == "done":
                continue
            if _budget_left() <= 0:
                summary["budget_exhausted"] = True
                _log(f"Budget exhausted before run {run_id}; resuming next sync")
                break

            if status is None:
                _log(f"Splitting weekly run {run_id} ({run['run_date'][:10]})...")
                try:
                    jobs = self.fetch_jobs(run_id)
                except Exception as exc:  # noqa: BLE001
                    summary["errors"].append(f"fetch_jobs(run={run_id}): {exc}")
                    if is_auth_failure(exc):
                        summary["auth_failed"] = True
                        break
                    continue
                by_branch = split_jobs_by_branch(jobs)
                if not by_branch:
                    _log(f"  No release-branch jobs in run {run_id}; marking done")
                    self._cache.set_weekly_ingest_status(run_id, "done")
                    summary["runs_completed"] += 1
                    continue
                synthetic = build_synthetic_runs(run, by_branch)
                self._cache.store_runs(synthetic)
                # job_id is the primary key, so storing a job under its
                # synthetic run moves it there; logs and failures key off
                # job_id and follow it.
                for srun in synthetic:
                    self._cache.store_jobs(srun["run_id"], by_branch[srun["branch"]])
                self._cache.set_weekly_ingest_status(run_id, "split")
                summary["runs_split"] += 1

            failed_jobs: List[Dict] = []
            for srun in self._cache.query_runs(repo=self._client.repo, workflow=WEEKLY_SPLIT_WORKFLOW):
                if abs(srun["run_id"]) // 100 == run_id:
                    failed_jobs.extend(self._cache.query_jobs(srun["run_id"], failed_only=True))
            pending = [j for j in failed_jobs if not self._cache.has_failures_for_job(j["job_id"])]

            exhausted_mid_run = False
            for job in pending:
                job_id = job["job_id"]
                if _budget_left() <= 0:
                    summary["budget_exhausted"] = True
                    exhausted_mid_run = True
                    _log(f"Budget exhausted mid-run {run_id}; resuming next sync")
                    break
                try:
                    if not self._cache.has_log(job_id):
                        if self.fetch_log(job_id) is None:
                            summary["logs_expired"] += 1
                            continue
                        summary["logs_fetched"] += 1
                    summary["failures_parsed"] += len(self.parse_log(job_id))
                except RateLimitExceededException as exc:
                    summary["errors"].append(f"fetch_log(job={job_id}): {exc}")
                    summary["budget_exhausted"] = True
                    exhausted_mid_run = True
                    break
                except Exception as exc:  # noqa: BLE001
                    summary["errors"].append(f"log(job={job_id}): {exc}")
                    if is_auth_failure(exc):
                        summary["auth_failed"] = True
                        exhausted_mid_run = True
                        break

            if summary["auth_failed"]:
                break
            if not exhausted_mid_run:
                self._cache.set_weekly_ingest_status(run_id, "done")
                summary["runs_completed"] += 1

        summary["budget_used"] = self._client.requests_made - start_requests
        _log(
            f"Weekly ingest: {summary['runs_split']} runs split, "
            f"{summary['runs_completed']} completed, "
            f"{summary['logs_fetched']} logs fetched, "
            f"{summary['logs_expired']} expired, "
            f"budget {summary['budget_used']}/{budget}"
        )
        return summary
