"""Tests for ``scripts.ci_health.sync``: the store is filled incrementally from a fake GitHub."""

from __future__ import annotations

from typing import Dict, List, Optional
from urllib.error import HTTPError

import pytest
from github import GithubException, RateLimitExceededException

from scripts.ci_health.store import Cache
from scripts.ci_health.sync import (
    LISTING_MARGIN_DAYS,
    GitHubRunsClient,
    HistorySync,
    _compact_commit,
    is_auth_failure,
    listing_since,
    map_job,
    map_run,
)
from scripts.ci_health.weekly import WEEKLY_SPLIT_WORKFLOW

# ---------------------------------------------------------------------------
# A fake GitHub
# ---------------------------------------------------------------------------


def _api_run(run_id: int, conclusion: str = "failure", branch: str = "unstable",
             workflow: str = "daily.yml", date: str = "2026-01-15") -> Dict:
    return {
        "id": run_id,
        "path": f".github/workflows/{workflow}",
        "name": workflow,
        "conclusion": conclusion,
        "status": "completed",
        "head_branch": branch,
        "head_sha": f"sha_{run_id}",
        "run_started_at": f"{date}T08:00:00Z",
        "created_at": f"{date}T08:00:00Z",
        "updated_at": f"{date}T09:00:00Z",
    }


def _api_job(job_id: int, conclusion: str = "failure", name: Optional[str] = None,
             status: str = "completed") -> Dict:
    return {"id": job_id, "name": name or f"build-{job_id}", "status": status, "conclusion": conclusion}


def _branch_job(job_id: int, branch: str, name: str, conclusion: str) -> Dict:
    return _api_job(job_id, conclusion, name=f"run-daily-for-release-branches ({branch}) / {name}")


FAILING_LOG = "[err]: some_test in tests/unit/type/string.tcl\nExpected OK but got ERR\n"


class FakeGitHub:
    """A RunsClient over in-memory data that counts requests like the real one.

    ``logs`` maps a job id to its log text, ``""`` for an expired log, or an
    exception to raise.
    """

    def __init__(self, runs: Optional[Dict[str, List[Dict]]] = None,
                 jobs: Optional[Dict[int, List[Dict]]] = None,
                 logs: Optional[Dict[int, object]] = None) -> None:
        self.repo = "valkey-io/valkey"
        self.requests_made = 0
        self.runs = runs or {}
        self.jobs = jobs or {}
        self.logs = logs or {}
        self.calls: List[str] = []
        self.listings: List[Dict] = []

    def list_runs(self, workflow_file: str, *, branch=None, since=None, until=None, event=None) -> List[Dict]:
        self.requests_made += 1
        self.calls.append(f"runs:{workflow_file}:{event}")
        self.listings.append({"workflow_file": workflow_file, "branch": branch, "since": since, "until": until})
        result = self.runs.get(workflow_file, [])
        if isinstance(result, Exception):
            raise result
        return list(result)

    def list_jobs(self, run_id: int) -> List[Dict]:
        self.requests_made += 1
        self.calls.append(f"jobs:{run_id}")
        return list(self.jobs.get(run_id, []))

    def job_log(self, job_id: int) -> str:
        self.requests_made += 1
        self.calls.append(f"log:{job_id}")
        result = self.logs.get(job_id, "")
        if isinstance(result, Exception):
            raise result
        return str(result)

    def compare_commits(self, base_sha: str, head_sha: str) -> List[Dict]:
        self.requests_made += 1
        return []


def _one_failed_run(temp_db_path: str, log: object = FAILING_LOG):
    client = FakeGitHub(
        runs={"daily.yml": [_api_run(10)]},
        jobs={10: [_api_job(500), _api_job(501, conclusion="success")]},
        logs={500: log},
    )
    return HistorySync(client, Cache(temp_db_path)), client


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


class TestMapping:
    def test_run_row_takes_file_name_and_duration(self) -> None:
        row = map_run(_api_run(7), "valkey-io/valkey")
        assert row["workflow_file"] == "daily.yml"
        assert row["status"] == "failure"
        assert row["duration_secs"] == 3600
        assert row["commit_sha"] == "sha_7"

    def test_run_row_without_conclusion_uses_status(self) -> None:
        raw = _api_run(8, conclusion="")
        raw["conclusion"] = None
        raw["status"] = "in_progress"
        assert map_run(raw, "valkey-io/valkey")["status"] == "in_progress"

    def test_run_row_duration_unknown_when_timestamps_missing(self) -> None:
        raw = _api_run(9)
        del raw["updated_at"]
        assert map_run(raw, "valkey-io/valkey")["duration_secs"] is None

    def test_job_row(self) -> None:
        row = map_job(_api_job(5, name="test-ubuntu"))
        assert row == {
            "job_id": 5, "name": "test-ubuntu", "status": "completed", "conclusion": "failure",
            "raw_json": row["raw_json"],
        }

    def test_compact_commit_keeps_subject_line(self) -> None:
        raw = {"sha": "abc", "commit": {"message": "Fix it\n\nLong body", "author": {"name": "A", "date": "D"}}}
        assert _compact_commit(raw) == {"sha": "abc", "message": "Fix it", "author": "A", "date": "D"}


# ---------------------------------------------------------------------------
# The ordinary sync
# ---------------------------------------------------------------------------


class TestSync:
    def test_failed_run_is_fetched_parsed_and_stored(self, temp_db_path: str) -> None:
        syncer, client = _one_failed_run(temp_db_path)
        summary = syncer.sync(workflow="daily")
        assert summary["new_runs_fetched"] == 1
        assert summary["new_jobs_fetched"] == 2
        assert summary["new_logs_fetched"] == 1
        assert summary["new_failures_parsed"] >= 1
        assert summary["errors"] == []
        assert not summary["auth_failed"]
        # Only the failed job's log was asked for.
        assert "log:500" in client.calls and "log:501" not in client.calls

    def test_second_sync_costs_one_listing_and_stores_nothing(self, temp_db_path: str) -> None:
        syncer, client = _one_failed_run(temp_db_path)
        syncer.sync(workflow="daily")
        before = client.requests_made
        summary = syncer.sync(workflow="daily")
        assert client.requests_made - before == 1
        assert summary["new_runs_fetched"] == 0
        assert summary["new_logs_fetched"] == 0

    def test_successful_runs_do_not_fetch_jobs(self, temp_db_path: str) -> None:
        client = FakeGitHub(runs={"daily.yml": [_api_run(1, conclusion="success"), _api_run(2)]},
                            jobs={2: [_api_job(20)]}, logs={20: FAILING_LOG})
        summary = HistorySync(client, Cache(temp_db_path)).sync(workflow="daily")
        assert summary["new_runs_fetched"] == 2
        assert "jobs:1" not in client.calls and "jobs:2" in client.calls

    def test_all_workflows_when_none_named(self, temp_db_path: str) -> None:
        client = FakeGitHub()
        HistorySync(client, Cache(temp_db_path)).sync()
        assert client.calls == ["runs:daily.yml:None", "runs:weekly.yml:None", "runs:ci.yml:push"]

    def test_only_the_named_workflow(self, temp_db_path: str) -> None:
        client = FakeGitHub()
        HistorySync(client, Cache(temp_db_path)).sync(workflow="weekly")
        assert client.calls == ["runs:weekly.yml:None"]

    def test_log_download_error_is_recorded_and_sync_goes_on(self, temp_db_path: str) -> None:
        client = FakeGitHub(runs={"daily.yml": [_api_run(20), _api_run(21)]},
                            jobs={20: [_api_job(600)], 21: [_api_job(610)]},
                            logs={600: RuntimeError("network error"), 610: FAILING_LOG})
        summary = HistorySync(client, Cache(temp_db_path)).sync(workflow="daily")
        assert summary["new_runs_fetched"] == 2
        assert summary["new_logs_fetched"] == 1
        assert len(summary["errors"]) == 1 and "network error" in summary["errors"][0]
        assert not summary["auth_failed"]

    def test_expired_log_is_counted_and_never_asked_again(self, temp_db_path: str) -> None:
        syncer, client = _one_failed_run(temp_db_path, log="")
        summary = syncer.sync(workflow="daily")
        assert summary["logs_expired"] == 1
        assert summary["new_logs_fetched"] == 0
        assert syncer._cache.has_failures_for_job(500)
        assert not syncer._cache.has_log(500)

    def test_unrecognised_log_is_marked_unparseable(self, temp_db_path: str) -> None:
        syncer, _ = _one_failed_run(temp_db_path, log="nothing the parser knows\n")
        summary = syncer.sync(workflow="daily")
        assert summary["new_failures_parsed"] == 0
        assert syncer._cache.has_failures_for_job(500)

    def test_listing_error_skips_that_workflow(self, temp_db_path: str) -> None:
        client = FakeGitHub(runs={"daily.yml": RuntimeError("boom"), "weekly.yml": []})  # type: ignore[dict-item]
        summary = HistorySync(client, Cache(temp_db_path)).sync()
        assert any(e.startswith("fetch_runs(daily)") for e in summary["errors"])
        assert "runs:weekly.yml:None" in client.calls

    def test_jobs_refetched_while_one_was_still_running(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        client = FakeGitHub(runs={"daily.yml": [_api_run(30)]},
                            jobs={30: [_api_job(300, conclusion=None, status="in_progress")]})  # type: ignore[arg-type]
        syncer = HistorySync(client, cache)
        syncer.fetch_runs("daily")
        syncer.fetch_jobs(30)
        client.jobs[30] = [_api_job(300, conclusion="failure")]
        jobs = syncer.fetch_jobs(30)
        assert client.calls.count("jobs:30") == 2
        assert jobs[0]["conclusion"] == "failure"

    def test_reparses_logs_whose_results_were_wiped(self, temp_db_path: str) -> None:
        syncer, client = _one_failed_run(temp_db_path)
        syncer.sync(workflow="daily")
        syncer._cache._conn.execute("DELETE FROM parse_status")
        syncer._cache._conn.execute("DELETE FROM test_failures")
        syncer._cache._conn.commit()
        before = client.requests_made
        summary = syncer.sync(workflow="daily")
        assert summary["new_failures_parsed"] >= 1
        assert client.requests_made - before == 1  # the listing; no log download


class TestIncrementalListing:
    def test_first_listing_is_unbounded_and_the_next_starts_near_the_newest_run(self, temp_db_path: str) -> None:
        client = FakeGitHub(runs={"daily.yml": [_api_run(1, date="2026-01-10"), _api_run(2, date="2026-01-15")]})
        syncer = HistorySync(client, Cache(temp_db_path))
        syncer.sync(workflow="daily")
        syncer.sync(workflow="daily")
        assert client.listings[0]["since"] is None
        assert client.listings[1]["since"] == "2026-01-12T08:00:00Z"  # newest minus the margin

    def test_callers_window_wins_over_the_derived_one(self, temp_db_path: str) -> None:
        client = FakeGitHub(runs={"daily.yml": [_api_run(1)]})
        syncer = HistorySync(client, Cache(temp_db_path))
        syncer.sync(workflow="daily")
        syncer.sync(workflow="daily", since="2025-01-01T00:00:00Z")
        assert client.listings[1]["since"] == "2025-01-01T00:00:00Z"

    def test_listing_since_reaches_back_to_an_unfinished_run(self) -> None:
        stored = [
            {"run_id": 1, "status": "in_progress", "run_date": "2025-11-01T00:00:00Z"},
            {"run_id": 2, "status": "success", "run_date": "2026-01-15T08:00:00Z"},
        ]
        assert listing_since(stored) == "2025-11-01T00:00:00Z"
        assert listing_since([stored[1]]) == f"2026-01-{15 - LISTING_MARGIN_DAYS:02d}T08:00:00Z"
        assert listing_since([]) is None

    def test_run_seen_in_progress_is_refreshed_and_its_failures_ingested(self, temp_db_path: str) -> None:
        running = _api_run(40, conclusion="")
        running["conclusion"] = None
        running["status"] = "in_progress"
        client = FakeGitHub(runs={"daily.yml": [running]}, jobs={40: [_api_job(400)]}, logs={400: FAILING_LOG})
        cache = Cache(temp_db_path)
        syncer = HistorySync(client, cache)
        first = syncer.sync(workflow="daily")
        assert first["new_runs_fetched"] == 1 and "jobs:40" not in client.calls
        assert cache.query_runs(workflow="daily.yml")[0]["status"] == "in_progress"

        client.runs["daily.yml"] = [_api_run(40, conclusion="failure")]
        second = syncer.sync(workflow="daily")
        assert second["new_runs_fetched"] == 0 and second["runs_refreshed"] == 1
        assert cache.query_runs(workflow="daily.yml")[0]["status"] == "failure"
        assert "jobs:40" in client.calls and second["new_failures_parsed"] >= 1

    def test_finished_runs_are_not_rewritten(self, temp_db_path: str) -> None:
        client = FakeGitHub(runs={"daily.yml": [_api_run(50, conclusion="success")]})
        cache = Cache(temp_db_path)
        syncer = HistorySync(client, cache)
        syncer.sync(workflow="daily")
        client.runs["daily.yml"] = [_api_run(50, conclusion="failure")]  # GitHub would not do this
        summary = syncer.sync(workflow="daily")
        assert summary["runs_refreshed"] == 0
        assert cache.query_runs(workflow="daily.yml")[0]["status"] == "success"


class TestAuthFailure:
    def test_401_is_fatal(self, temp_db_path: str) -> None:
        client = FakeGitHub(runs={"daily.yml": GithubException(401, {"message": "Bad credentials"})})  # type: ignore[dict-item]
        summary = HistorySync(client, Cache(temp_db_path)).sync(workflow="daily")
        assert summary["auth_failed"]

    def test_403_is_fatal(self, temp_db_path: str) -> None:
        client = FakeGitHub(runs={"daily.yml": GithubException(403, {"message": "Resource not accessible"})})  # type: ignore[dict-item]
        summary = HistorySync(client, Cache(temp_db_path)).sync(workflow="daily")
        assert summary["auth_failed"]

    def test_rate_limit_is_not_an_auth_failure(self, temp_db_path: str) -> None:
        client = FakeGitHub(runs={"daily.yml": RateLimitExceededException(403, {"message": "rate"})})  # type: ignore[dict-item]
        summary = HistorySync(client, Cache(temp_db_path)).sync(workflow="daily")
        assert not summary["auth_failed"]
        assert summary["errors"]

    def test_log_download_403_is_fatal(self, temp_db_path: str) -> None:
        error = HTTPError("https://api.github.com/x", 403, "Forbidden", {}, None)  # type: ignore[arg-type]
        syncer, _ = _one_failed_run(temp_db_path, log=error)
        assert syncer.sync(workflow="daily")["auth_failed"]

    def test_classifier(self) -> None:
        assert is_auth_failure(GithubException(401, {}))
        assert is_auth_failure(HTTPError("u", 401, "m", {}, None))  # type: ignore[arg-type]
        assert not is_auth_failure(GithubException(404, {}))
        assert not is_auth_failure(RateLimitExceededException(403, {}))
        assert not is_auth_failure(RuntimeError("x"))


# ---------------------------------------------------------------------------
# Weekly release-branch runs
# ---------------------------------------------------------------------------


def _two_branch_setup(temp_db_path: str, log_result: object = FAILING_LOG):
    run = _api_run(1000, workflow="weekly.yml", date="2026-07-12")
    jobs = {1000: [
        _api_job(10, "success", name="setup"),
        _branch_job(11, "7.2", "test-a", "failure"),
        _branch_job(12, "8.0", "test-a", "success"),
    ]}
    client = FakeGitHub(runs={"weekly.yml": [run]}, jobs=jobs, logs={11: log_result})
    cache = Cache(temp_db_path)
    return HistorySync(client, cache), client, cache


class TestSyncWeeklyBranches:
    def test_full_ingest_creates_synthetic_series(self, temp_db_path: str) -> None:
        syncer, client, cache = _two_branch_setup(temp_db_path)
        summary = syncer.sync_weekly_branches(budget=100)
        assert summary["runs_split"] == 1
        assert summary["runs_completed"] == 1
        assert not summary["budget_exhausted"]
        split_72 = cache.query_runs(workflow=WEEKLY_SPLIT_WORKFLOW, branch="7.2")
        split_80 = cache.query_runs(workflow=WEEKLY_SPLIT_WORKFLOW, branch="8.0")
        assert len(split_72) == 1 and split_72[0]["status"] == "failure"
        assert len(split_80) == 1 and split_80[0]["status"] == "success"
        assert [j["name"] for j in cache.query_jobs(split_72[0]["run_id"])] == ["test-a"]

    def test_second_pass_costs_only_the_listing(self, temp_db_path: str) -> None:
        syncer, client, _ = _two_branch_setup(temp_db_path)
        syncer.sync_weekly_branches(budget=100)
        before = client.requests_made
        summary = syncer.sync_weekly_branches(budget=100)
        assert client.requests_made - before == 1
        assert summary["runs_split"] == 0 and summary["runs_completed"] == 0

    def test_budget_exhaustion_resumes_next_pass(self, temp_db_path: str) -> None:
        syncer, client, cache = _two_branch_setup(temp_db_path)
        # listing (1) + jobs (1): exhausted before the log.
        first = syncer.sync_weekly_branches(budget=2)
        assert first["runs_split"] == 1 and first["budget_exhausted"]
        assert cache.get_weekly_ingest_status(1000) == "split"
        second = syncer.sync_weekly_branches(budget=100)
        assert second["logs_fetched"] == 1 and second["runs_completed"] == 1
        assert cache.get_weekly_ingest_status(1000) == "done"

    def test_expired_log_completes_the_run(self, temp_db_path: str) -> None:
        syncer, _, cache = _two_branch_setup(temp_db_path, log_result="")
        summary = syncer.sync_weekly_branches(budget=100)
        assert summary["logs_expired"] == 1 and summary["runs_completed"] == 1
        assert cache.has_failures_for_job(11)

    def test_rate_limit_stops_the_pass_without_completing_the_run(self, temp_db_path: str) -> None:
        syncer, _, cache = _two_branch_setup(
            temp_db_path, log_result=RateLimitExceededException(403, {"message": "rate"}),
        )
        summary = syncer.sync_weekly_branches(budget=100)
        assert summary["budget_exhausted"] and summary["runs_completed"] == 0
        assert cache.get_weekly_ingest_status(1000) == "split"

    def test_reuses_jobs_the_ordinary_sync_stored(self, temp_db_path: str) -> None:
        syncer, client, cache = _two_branch_setup(temp_db_path)
        cache.store_runs([map_run(client.runs["weekly.yml"][0], client.repo)])
        cache.store_jobs(1000, [map_job(j) for j in client.jobs[1000]])
        summary = syncer.sync_weekly_branches(budget=100)
        assert client.requests_made == 2  # listing + one log
        assert summary["runs_split"] == 1 and summary["runs_completed"] == 1

    def test_refresh_runs_false_skips_the_listing(self, temp_db_path: str) -> None:
        syncer, client, cache = _two_branch_setup(temp_db_path)
        syncer.sync(workflow="weekly", branch="unstable")  # lists weekly.yml once
        before = client.requests_made
        summary = syncer.sync_weekly_branches(budget=100, refresh_runs=False)
        assert not any(c.startswith("runs:") for c in client.calls[before:])
        assert summary["runs_split"] == 1 and summary["runs_completed"] == 1

    def test_run_without_branch_jobs_is_done_at_once(self, temp_db_path: str) -> None:
        client = FakeGitHub(runs={"weekly.yml": [_api_run(1, workflow="weekly.yml")]},
                            jobs={1: [_api_job(5, name="setup")]})
        cache = Cache(temp_db_path)
        summary = HistorySync(client, cache).sync_weekly_branches(budget=100)
        assert summary["runs_completed"] == 1 and summary["runs_split"] == 0
        assert cache.get_weekly_ingest_status(1) == "done"


# ---------------------------------------------------------------------------
# The PyGithub-backed client
# ---------------------------------------------------------------------------


class _Requester:
    def __init__(self, pages: Dict[str, List[Dict]]) -> None:
        self.pages = pages
        self.requests: List[tuple] = []

    def requestJsonAndCheck(self, verb: str, path: str, parameters: Dict) -> tuple:  # noqa: N802 - PyGithub's name
        self.requests.append((verb, path, dict(parameters)))
        page = parameters["page"]
        key = "workflow_runs" if "workflows" in path else "jobs"
        items = self.pages.get(path, [])
        start = (page - 1) * parameters["per_page"]
        return {}, {key: items[start:start + parameters["per_page"]]}


class _Repo:
    def __init__(self, requester: _Requester) -> None:
        self._requester = requester

    def compare(self, base: str, head: str):
        class _Commit:
            raw_data = {"sha": "c1", "commit": {"message": "Subject\n\nbody", "author": {"name": "N", "date": "D"}}}

        class _Comparison:
            commits = [_Commit()]

        return _Comparison()


class _Github:
    def __init__(self, requester: _Requester) -> None:
        self.repo = _Repo(requester)

    def get_repo(self, name: str) -> _Repo:
        return self.repo


class TestGitHubRunsClient:
    def test_lists_runs_across_pages_with_created_range(self) -> None:
        path = "/repos/valkey-io/valkey/actions/workflows/daily.yml/runs"
        requester = _Requester({path: [{"id": i} for i in range(150)]})
        client = GitHubRunsClient(_Github(requester), "valkey-io/valkey", token="t")  # type: ignore[arg-type]
        runs = client.list_runs("daily.yml", branch="unstable", since="2026-01-01", until="2026-02-01", event="schedule")
        assert [r["id"] for r in runs] == list(range(150))
        assert client.requests_made == 2
        first = requester.requests[0][2]
        assert first["branch"] == "unstable" and first["event"] == "schedule"
        assert first["created"] == "2026-01-01..2026-02-01"

    def test_since_alone_is_a_lower_bound(self) -> None:
        path = "/repos/valkey-io/valkey/actions/workflows/daily.yml/runs"
        requester = _Requester({path: []})
        client = GitHubRunsClient(_Github(requester), "valkey-io/valkey", token="t")  # type: ignore[arg-type]
        client.list_runs("daily.yml", since="2026-01-01")
        assert requester.requests[0][2]["created"] == ">=2026-01-01"
        assert "branch" not in requester.requests[0][2]

    def test_lists_jobs(self) -> None:
        path = "/repos/valkey-io/valkey/actions/runs/42/jobs"
        requester = _Requester({path: [{"id": 1}, {"id": 2}]})
        client = GitHubRunsClient(_Github(requester), "valkey-io/valkey", token="t")  # type: ignore[arg-type]
        assert [j["id"] for j in client.list_jobs(42)] == [1, 2]
        assert client.requests_made == 1

    def test_compare_commits_is_compacted(self) -> None:
        client = GitHubRunsClient(_Github(_Requester({})), "valkey-io/valkey", token="t")  # type: ignore[arg-type]
        assert client.compare_commits("a", "b") == [{"sha": "c1", "message": "Subject", "author": "N", "date": "D"}]
        assert client.requests_made == 1

    def test_requires_a_token(self) -> None:
        with pytest.raises(ValueError):
            GitHubRunsClient(_Github(_Requester({})), "valkey-io/valkey", token="")  # type: ignore[arg-type]
