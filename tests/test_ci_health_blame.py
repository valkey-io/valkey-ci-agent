"""Tests for the blame module."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from scripts.ci_health.blame import compute_blame
from scripts.ci_health.store import Cache

# Anchor test runs to a recent window so they fall inside compute_blame's
# rolling `days` window regardless of the calendar date the suite runs on.
_BASE = datetime.now(timezone.utc) - timedelta(days=15)


def _day(offset):
    return (_BASE + timedelta(days=offset)).strftime("%Y-%m-%d")


@pytest.fixture
def cache(tmp_path):
    return Cache(str(tmp_path / "test.db"))


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.repo = "valkey-io/valkey"
    client.compare_commits.return_value = [
        {
            "sha": "aaa111",
            "message": "fix: something",
            "author": "dev1",
            "date": "2026-05-03T10:00:00Z",
        },
        {
            "sha": "bbb222",
            "message": "feat: new thing",
            "author": "dev2",
            "date": "2026-05-03T11:00:00Z",
        },
    ]
    return client


def _make_run(run_id, date_str, status="failure", sha=None):
    return {
        "run_id": run_id,
        "repo": "valkey-io/valkey",
        "workflow_file": "daily.yml",
        "status": status,
        "branch": "unstable",
        "commit_sha": sha or f"sha{run_id}",
        "run_date": f"{date_str}T06:00:00Z",
    }


def _make_job(job_id, run_id, conclusion="failure"):
    return {
        "job_id": job_id,
        "name": f"job-{job_id}",
        "status": "completed",
        "conclusion": conclusion,
    }


def _store(cache, rid, off, fail_test=None):
    """Store one CI day; fails `fail_test` if given, else a clean run."""
    status = "failure" if fail_test else "success"
    cache.store_runs([_make_run(rid, _day(off), status, f"s{rid}")])
    if fail_test:
        cache.store_jobs(rid, [_make_job(rid + 1000, rid)])
        cache.store_failures(
            rid + 1000,
            [{"test_name": fail_test, "error_summary": "e", "log_lines": "x"}],
        )
    else:
        cache.store_jobs(rid, [_make_job(rid + 1000, rid, "success")])


class TestComputeBlame:
    def test_empty_cache(self, cache, mock_client):
        result = compute_blame(cache, mock_client, days=14)
        assert result == []

    def test_green_to_red_transition(self, cache, mock_client):
        """Test that a green→red transition identifies blame commits."""
        # Day 1: passing
        cache.store_runs([_make_run(100, _day(0), "success", "aaa")])
        cache.store_jobs(100, [_make_job(200, 100, "success")])

        # Day 2: passing
        cache.store_runs([_make_run(101, _day(1), "success", "bbb")])
        cache.store_jobs(101, [_make_job(201, 101, "success")])

        # Day 3: failing with a new test failure
        cache.store_runs([_make_run(102, _day(2), "failure", "ccc")])
        cache.store_jobs(102, [_make_job(202, 102)])
        cache.store_failures(
            202,
            [
                {
                    "test_name": "flaky test in tests/unit/foo.tcl",
                    "error_summary": "assertion failed",
                    "log_lines": "x",
                }
            ],
        )

        result = compute_blame(cache, mock_client, days=30)
        assert len(result) == 1

        rec = result[0]
        assert rec["test_name"] == "flaky test in tests/unit/foo.tcl"
        assert rec["regression_date"] == _day(2)
        assert rec["last_pass_date"] == _day(1)
        assert rec["last_pass_sha"] == "bbb"
        assert rec["first_fail_sha"] == "ccc"
        assert rec["commit_count"] == 2
        mock_client.compare_commits.assert_called_once_with("bbb", "ccc")

    def test_already_failing_at_window_start(self, cache, mock_client):
        """Test that tests failing from the start get a note."""
        cache.store_runs([_make_run(100, _day(0), "failure", "aaa")])
        cache.store_jobs(100, [_make_job(200, 100)])
        cache.store_failures(
            200,
            [
                {
                    "test_name": "old_flaky",
                    "error_summary": "err",
                    "log_lines": "x",
                }
            ],
        )

        result = compute_blame(cache, mock_client, days=30)
        assert len(result) == 1
        assert result[0]["last_pass_sha"] is None
        assert result[0]["confidence"] == "unknown"
        assert "extend --days" in result[0]["note"]
        mock_client.compare_commits.assert_not_called()

    def test_multiple_tests_different_regression_dates(self, cache, mock_client):
        """Tests regressing on different days are sorted newest first."""
        # Day 1: all green
        cache.store_runs([_make_run(100, _day(0), "success", "a1")])
        cache.store_jobs(100, [_make_job(200, 100, "success")])

        # Day 2: test_A starts failing
        cache.store_runs([_make_run(101, _day(1), "failure", "a2")])
        cache.store_jobs(101, [_make_job(201, 101)])
        cache.store_failures(
            201,
            [
                {
                    "test_name": "test_A",
                    "error_summary": "err",
                    "log_lines": "x",
                }
            ],
        )

        # Day 3: test_B also starts failing
        cache.store_runs([_make_run(102, _day(2), "failure", "a3")])
        cache.store_jobs(102, [_make_job(202, 102)])
        cache.store_failures(
            202,
            [
                {"test_name": "test_A", "error_summary": "err", "log_lines": "x"},
                {"test_name": "test_B", "error_summary": "err", "log_lines": "x"},
            ],
        )

        result = compute_blame(cache, mock_client, days=30)
        assert len(result) == 2
        # Newest regression first
        assert result[0]["test_name"] == "test_B"
        assert result[0]["regression_date"] == _day(2)
        assert result[1]["test_name"] == "test_A"
        assert result[1]["regression_date"] == _day(1)

    def test_api_error_gracefully_handled(self, cache, mock_client):
        """If compare_commits fails, blame_commits is empty."""
        mock_client.compare_commits.side_effect = Exception("API error")

        cache.store_runs([_make_run(100, _day(0), "success", "aaa")])
        cache.store_jobs(100, [_make_job(200, 100, "success")])
        cache.store_runs([_make_run(101, _day(1), "failure", "bbb")])
        cache.store_jobs(101, [_make_job(201, 101)])
        cache.store_failures(
            201,
            [
                {
                    "test_name": "test_X",
                    "error_summary": "err",
                    "log_lines": "x",
                }
            ],
        )

        result = compute_blame(cache, mock_client, days=30)
        assert len(result) == 1
        assert result[0]["blame_commits"] == []
        assert result[0]["commit_count"] == 0

    def test_confidence_high_for_novel_durable_regression(self, cache, mock_client):
        """Clean history then durable failures -> high confidence, low p0_hat."""
        rid = 100
        for off in range(0, 15):  # 15 clean days
            _store(cache, rid, off)
            rid += 1
        for off in range(15, 20):  # then fails every day (novel + durable)
            _store(cache, rid, off, "novel_test")
            rid += 1

        result = compute_blame(cache, mock_client, days=60)
        rec = next(r for r in result if r["test_name"] == "novel_test")
        assert rec["post_onset_rate"] == 1.0
        assert rec["confidence"] == "high", rec
        assert rec["p0_hat"] < 0.05
        assert rec["ongoing"] is True  # still failing on the most recent run

    def test_daily_series_and_onset_index(self, cache, mock_client):
        """Record carries a chronological 0/1 window series marked at onset."""
        rid = 100
        for off in range(0, 6):  # 6 clean days
            _store(cache, rid, off)
            rid += 1
        for off in range(6, 10):  # then 4 failing days
            _store(cache, rid, off, "novel_test")
            rid += 1

        result = compute_blame(cache, mock_client, days=60)
        rec = next(r for r in result if r["test_name"] == "novel_test")

        series = rec["daily_series"]
        onset = rec["onset_index"]
        assert len(series) == 10  # one entry per in-window run
        assert onset == 6
        assert series[onset] == 1
        assert all(v == 0 for v in series[:onset])  # clean before onset
        assert all(v == 1 for v in series[onset:])  # failing from onset on

    def test_regression_marked_fixed_when_quiet(self, cache, mock_client):
        """A regression whose test goes quiet for >= threshold runs is fixed."""
        from scripts.ci_health.blame import REGRESSION_ONGOING_QUIET_RUNS as q

        rid = 100
        for off in range(0, 3):  # clean
            _store(cache, rid, off)
            rid += 1
        for off in range(3, 5):  # breaks
            _store(cache, rid, off, "brk")
            rid += 1
        for off in range(5, 5 + q + 2):  # then quiet for q+2 runs
            _store(cache, rid, off)
            rid += 1

        result = compute_blame(cache, mock_client, days=90)
        rec = next(r for r in result if r["test_name"] == "brk")
        assert rec["runs_since_last_fail"] >= q
        assert rec["ongoing"] is False

    def test_confidence_low_for_known_flake(self, cache, mock_client):
        """A historically flaky test flagging again -> low confidence.

        The windowed post_onset_rate is 1.0 (it fails on its only in-window
        run), which the OLD logic would call 'high'. But the all-history
        baseline knows this test is a ~30% flake, so the prior-aware
        confidence correctly says 'low' -- the burst isn't surprising.
        """
        rid = 100
        # Old flaky history OUTSIDE the 20-day detection window: fails ~1/3.
        for i, off in enumerate(range(-40, -28)):  # 12 old days (~40-28d ago)
            _store(cache, rid, off, "flaky_test" if i % 3 == 0 else None)
            rid += 1
        # Recent window: one green day, then a single blip.
        _store(cache, rid, 0)
        rid += 1
        _store(cache, rid, 1, "flaky_test")

        result = compute_blame(cache, mock_client, days=20)  # excludes old days
        rec = next(r for r in result if r["test_name"] == "flaky_test")
        assert rec["post_onset_rate"] == 1.0  # naive windowed rate says "durable"
        assert rec["confidence"] == "low", rec  # ...but prior knows it's a flake
        assert rec["p0_hat"] > 0.1
