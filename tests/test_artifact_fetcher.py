"""Tests for artifact download logic."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest

# PyGithub requires urllib3 v2 + OpenSSL 1.1.1+. On older dev hosts the import
# fails at collection time. Guard with a skip so the test file is still valid.
try:
    from scripts.test_failure_detector.download import (
        get_job_urls,
        get_latest_daily_run,
    )

    _SKIP_REASON = None
except ImportError as _exc:
    _SKIP_REASON = f"PyGithub import failed: {_exc}"

pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


class TestGetLatestDailyRun:
    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_returns_first_completed_non_cancelled_run(self, mock_retry) -> None:
        """Should skip cancelled runs and return the first completed one."""
        mock_run_cancelled = MagicMock()
        mock_run_cancelled.conclusion = "cancelled"
        mock_run_cancelled.status = "completed"

        mock_run_good = MagicMock()
        mock_run_good.conclusion = "failure"
        mock_run_good.status = "completed"
        mock_run_good.id = 12345
        mock_run_good.run_number = 100
        mock_run_good.created_at = "2025-01-01T00:00:00Z"

        mock_workflow = MagicMock()
        mock_workflow.name = "Daily"
        mock_workflow.get_runs.return_value = [mock_run_cancelled, mock_run_good]

        mock_repo = MagicMock()
        mock_repo.get_workflows.return_value = [mock_workflow]

        mock_gh = MagicMock()

        # Make retry just call the operation
        mock_retry.side_effect = lambda op, **kwargs: op()
        mock_gh.get_repo.return_value = mock_repo

        result = get_latest_daily_run(mock_gh, "valkey-io/valkey")
        assert result == mock_run_good

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_returns_none_when_no_workflow_found(self, mock_retry) -> None:
        mock_repo = MagicMock()
        mock_repo.get_workflows.return_value = []

        mock_gh = MagicMock()
        mock_retry.side_effect = lambda op, **kwargs: op()
        mock_gh.get_repo.return_value = mock_repo

        result = get_latest_daily_run(mock_gh, "valkey-io/valkey")
        assert result is None

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_returns_none_when_all_runs_cancelled(self, mock_retry) -> None:
        mock_run = MagicMock()
        mock_run.conclusion = "cancelled"
        mock_run.status = "completed"

        mock_workflow = MagicMock()
        mock_workflow.name = "Daily"
        mock_workflow.get_runs.return_value = [mock_run]

        mock_repo = MagicMock()
        mock_repo.get_workflows.return_value = [mock_workflow]

        mock_gh = MagicMock()
        mock_retry.side_effect = lambda op, **kwargs: op()
        mock_gh.get_repo.return_value = mock_repo

        result = get_latest_daily_run(mock_gh, "valkey-io/valkey")
        assert result is None


class TestGetJobUrls:
    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_maps_job_names_to_urls(self, mock_retry) -> None:
        mock_job1 = MagicMock()
        mock_job1.name = "test-ubuntu-latest"
        mock_job1.html_url = "https://github.com/valkey-io/valkey/actions/runs/1/job/100"

        mock_job2 = MagicMock()
        mock_job2.name = "test-alpine (jemalloc)"
        mock_job2.html_url = "https://github.com/valkey-io/valkey/actions/runs/1/job/200"

        mock_run = MagicMock()
        mock_run.jobs.return_value = [mock_job1, mock_job2]

        mock_repo = MagicMock()
        mock_repo.get_workflow_run.return_value = mock_run

        mock_gh = MagicMock()
        mock_retry.side_effect = lambda op, **kwargs: op()
        mock_gh.get_repo.return_value = mock_repo

        result = get_job_urls(mock_gh, "valkey-io/valkey", 1)

        assert result["test-ubuntu-latest"] == mock_job1.html_url
        assert result["test-alpine (jemalloc)"] == mock_job2.html_url

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_includes_normalized_names(self, mock_retry) -> None:
        """Job names with parentheses should also be stored in normalized form."""
        mock_job = MagicMock()
        mock_job.name = "test-alpine (jemalloc)"
        mock_job.html_url = "https://example.com/job/1"

        mock_run = MagicMock()
        mock_run.jobs.return_value = [mock_job]

        mock_repo = MagicMock()
        mock_repo.get_workflow_run.return_value = mock_run

        mock_gh = MagicMock()
        mock_retry.side_effect = lambda op, **kwargs: op()
        mock_gh.get_repo.return_value = mock_repo

        result = get_job_urls(mock_gh, "valkey-io/valkey", 1)

        # Original name
        assert "test-alpine (jemalloc)" in result
        # Normalized: spaces around parens become dash, parens removed
        assert "test-alpine-jemalloc" in result
