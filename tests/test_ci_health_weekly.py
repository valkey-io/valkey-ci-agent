"""Tests for weekly release-branch job splitting."""

from __future__ import annotations

from typing import Dict, List

from scripts.ci_health.store import Cache
from scripts.ci_health.weekly import (
    WEEKLY_SPLIT_WORKFLOW,
    build_synthetic_runs,
    parse_branch_job,
    split_jobs_by_branch,
    synthetic_run_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _api_weekly_run(run_id: int, date: str = "2026-07-12") -> Dict:
    return {
        "id": run_id,
        "path": ".github/workflows/weekly.yml",
        "name": "Weekly Test Workflow for Released Branches",
        "conclusion": "failure",
        "status": "completed",
        "head_branch": "unstable",
        "head_sha": f"sha_{run_id}",
        "run_started_at": f"{date}T06:00:00Z",
        "created_at": f"{date}T06:00:00Z",
        "updated_at": f"{date}T18:00:00Z",
    }


def _api_branch_job(job_id: int, branch: str, name: str, conclusion: str) -> Dict:
    return {
        "id": job_id,
        "name": f"run-daily-for-release-branches ({branch}) / {name}",
        "status": "completed",
        "conclusion": conclusion,
    }


class _CountingClient:
    """Fake GitHubActionsClient that counts requests like the real one."""

    def __init__(self, runs: List[Dict], jobs: Dict[int, List[Dict]], logs=None):
        self.repo = "valkey-io/valkey"
        self.requests_made = 0
        self._runs = runs
        self._jobs = jobs
        self._logs = logs or {}

    def get_workflow_runs(self, **kwargs) -> List[Dict]:
        self.requests_made += 1
        return self._runs

    def get_jobs_for_run(self, run_id: int) -> List[Dict]:
        self.requests_made += 1
        return self._jobs.get(run_id, [])

    def get_job_log(self, job_id: int) -> str:
        self.requests_made += 1
        result = self._logs.get(job_id, "")
        if isinstance(result, Exception):
            raise result
        return result


# ---------------------------------------------------------------------------
# Splitter unit tests
# ---------------------------------------------------------------------------


class TestParseBranchJob:
    def test_parses_branch_and_strips_prefix(self) -> None:
        assert parse_branch_job(
            "run-daily-for-release-branches (8.0) / test-ubuntu"
        ) == ("8.0", "test-ubuntu")

    def test_setup_job_returns_none(self) -> None:
        assert parse_branch_job("determine-release-branches") is None

    def test_empty_and_none_safe(self) -> None:
        assert parse_branch_job("") is None
        assert parse_branch_job(None) is None


class TestSplitJobsByBranch:
    def test_partitions_and_drops_setup(self) -> None:
        jobs = [
            {
                "job_id": 1,
                "name": "determine-release-branches",
                "conclusion": "success",
            },
            {
                "job_id": 2,
                "name": "run-daily-for-release-branches (7.2) / build",
                "conclusion": "success",
            },
            {
                "job_id": 3,
                "name": "run-daily-for-release-branches (8.0) / build",
                "conclusion": "failure",
            },
        ]
        by = split_jobs_by_branch(jobs)
        assert set(by) == {"7.2", "8.0"}
        assert by["7.2"][0]["name"] == "build"
        assert by["8.0"][0]["conclusion"] == "failure"


class TestBuildSyntheticRuns:
    def test_per_branch_status_and_stable_ids(self) -> None:
        run = {
            "run_id": 555,
            "repo": "valkey-io/valkey",
            "run_date": "2026-07-12T06:00:00Z",
            "duration_secs": 100,
        }
        by = {
            "8.0": [{"job_id": 1, "conclusion": "failure"}],
            "7.2": [{"job_id": 2, "conclusion": "success"}],
        }
        runs = build_synthetic_runs(run, by)
        assert [r["branch"] for r in runs] == ["7.2", "8.0"]
        assert runs[0]["status"] == "success"
        assert runs[1]["status"] == "failure"
        assert runs[0]["run_id"] == synthetic_run_id(555, 0)
        assert runs[1]["run_id"] == synthetic_run_id(555, 1)
        assert all(r["workflow_file"] == WEEKLY_SPLIT_WORKFLOW for r in runs)
        assert all(r["run_id"] < 0 for r in runs)
        assert all(r["commit_sha"] == "" for r in runs)


# ---------------------------------------------------------------------------
# Budgeted ingest tests
# ---------------------------------------------------------------------------
