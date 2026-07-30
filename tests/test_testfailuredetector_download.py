"""Tests for artifact download logic (mocked GitHub API)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

try:
    from scripts.test_failure_detector.download import (
        download_all_test_failures,
        get_job_info,
        get_job_urls,
        get_latest_daily_run,
    )

    _SKIP_REASON = None
except ImportError as _exc:
    _SKIP_REASON = f"Import failed: {_exc}"

pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


def _make_mock_run(
    run_number: int,
    run_id: int,
    conclusion: str,
    status: str = "completed",
    event: str = "schedule",
):
    run = MagicMock()
    run.run_number = run_number
    run.id = run_id
    run.conclusion = conclusion
    run.status = status
    run.event = event
    run.created_at = "2026-06-01 00:00:00+00:00"
    return run


class TestGetLatestDailyRun:
    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_skips_cancelled_runs(self, mock_retry) -> None:
        """Cancelled runs should be skipped."""
        cancelled_run = _make_mock_run(10, 100, "cancelled")
        success_run = _make_mock_run(9, 99, "success")

        mock_workflow = MagicMock()
        mock_workflow.name = "Daily"
        mock_workflow.get_runs.return_value = [cancelled_run, success_run]

        mock_repo = MagicMock()
        mock_repo.get_workflows.return_value = [mock_workflow]

        mock_retry.side_effect = lambda op, **kwargs: op()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        result = get_latest_daily_run(mock_gh, "owner/repo")
        assert result == success_run

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_skips_skipped_runs(self, mock_retry) -> None:
        """Skipped runs should be skipped."""
        skipped_run = _make_mock_run(13, 200, "skipped")
        failure_run = _make_mock_run(12, 199, "failure")

        mock_workflow = MagicMock()
        mock_workflow.name = "Daily"
        mock_workflow.get_runs.return_value = [skipped_run, failure_run]

        mock_repo = MagicMock()
        mock_repo.get_workflows.return_value = [mock_workflow]

        mock_retry.side_effect = lambda op, **kwargs: op()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        result = get_latest_daily_run(mock_gh, "owner/repo")
        assert result == failure_run

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_skips_startup_failure_runs(self, mock_retry) -> None:
        """A run that died before any job started uploads no artifact. Returning
        it would report the older run's real failures as a clean pass."""
        startup_failed = _make_mock_run(14, 200, "startup_failure")
        failure_run = _make_mock_run(13, 199, "failure")

        mock_workflow = MagicMock()
        mock_workflow.name = "Daily"
        mock_workflow.get_runs.return_value = [startup_failed, failure_run]

        mock_repo = MagicMock()
        mock_repo.get_workflows.return_value = [mock_workflow]
        mock_retry.side_effect = lambda op, **kwargs: op()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        assert get_latest_daily_run(mock_gh, "owner/repo") == failure_run

    def test_run_listing_is_retried_around_iteration(self) -> None:
        """get_runs() returns a lazy PaginatedList that issues no request until
        iterated, so the retry must wrap the iteration, not the construction."""
        from github import GithubException

        state = {"calls": 0}

        class _LazyRuns:
            def __iter__(self):
                state["calls"] += 1
                if state["calls"] == 1:
                    raise GithubException(502, {"message": "Server Error"}, None)
                return iter([_make_mock_run(13, 199, "failure")])

        mock_workflow = MagicMock()
        mock_workflow.name = "Daily"
        mock_workflow.get_runs.return_value = _LazyRuns()

        mock_repo = MagicMock()
        mock_repo.get_workflows.return_value = [mock_workflow]
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        with patch("scripts.common.github_client.time.sleep"):
            result = get_latest_daily_run(mock_gh, "owner/repo")

        assert result.id == 199
        assert state["calls"] == 2

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_returns_first_success_or_failure(self, mock_retry) -> None:
        """Should return the most recent run with conclusion success or failure."""
        runs = [
            _make_mock_run(15, 300, "skipped"),
            _make_mock_run(14, 299, "cancelled"),
            _make_mock_run(13, 298, "success"),
            _make_mock_run(12, 297, "failure"),
        ]

        mock_workflow = MagicMock()
        mock_workflow.name = "Daily"
        mock_workflow.get_runs.return_value = runs

        mock_repo = MagicMock()
        mock_repo.get_workflows.return_value = [mock_workflow]

        mock_retry.side_effect = lambda op, **kwargs: op()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        result = get_latest_daily_run(mock_gh, "owner/repo")
        assert result.id == 298
        assert result.conclusion == "success"

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_returns_none_when_no_qualifying_run(self, mock_retry) -> None:
        """Should return None if all runs are cancelled/skipped."""
        runs = [
            _make_mock_run(10, 100, "cancelled"),
            _make_mock_run(9, 99, "skipped"),
        ]

        mock_workflow = MagicMock()
        mock_workflow.name = "Daily"
        mock_workflow.get_runs.return_value = runs

        mock_repo = MagicMock()
        mock_repo.get_workflows.return_value = [mock_workflow]

        mock_retry.side_effect = lambda op, **kwargs: op()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        result = get_latest_daily_run(mock_gh, "owner/repo")
        assert result is None

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_returns_none_when_workflow_not_found(self, mock_retry) -> None:
        """Should return None if the workflow doesn't exist."""
        mock_repo = MagicMock()
        mock_repo.get_workflows.return_value = []

        mock_retry.side_effect = lambda op, **kwargs: op()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        result = get_latest_daily_run(mock_gh, "owner/repo")
        assert result is None

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_accepts_scheduled_and_dispatched_runs(self, mock_retry) -> None:
        """A manually dispatched Daily run is as valid as a scheduled one.

        Both test the branch itself, so a maintainer re-running Daily by hand
        gets its failures reported. The API filters a single event at a time,
        so the listing stays unfiltered and the event check is local.
        """
        for event in ("schedule", "workflow_dispatch"):
            run = _make_mock_run(12, 199, "failure", event=event)

            mock_workflow = MagicMock()
            mock_workflow.name = "Daily"
            mock_workflow.get_runs.return_value = [run]

            mock_repo = MagicMock()
            mock_repo.get_workflows.return_value = [mock_workflow]

            mock_retry.side_effect = lambda op, **kwargs: op()

            mock_gh = MagicMock()
            mock_gh.get_repo.return_value = mock_repo

            assert get_latest_daily_run(mock_gh, "owner/repo") == run
            mock_workflow.get_runs.assert_called_once_with(
                branch="unstable", status="completed",
            )

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_skips_pull_request_runs(self, mock_retry) -> None:
        """A pull_request run tests the PR's merge commit, not the branch, so
        its failures belong to the PR. A PR opened from a branch in the same
        repo needs no approval and reaches a real conclusion, so the
        conclusion check alone would let it through and the detector would
        file PR failures against the branch.
        """
        pr_run = _make_mock_run(14, 299, "failure", event="pull_request")
        nightly_run = _make_mock_run(13, 298, "failure", event="schedule")

        mock_workflow = MagicMock()
        mock_workflow.name = "Daily"
        mock_workflow.get_runs.return_value = [pr_run, nightly_run]

        mock_repo = MagicMock()
        mock_repo.get_workflows.return_value = [mock_workflow]

        mock_retry.side_effect = lambda op, **kwargs: op()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        assert get_latest_daily_run(mock_gh, "owner/repo") == nightly_run

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_skips_action_required_runs(self, mock_retry) -> None:
        """Runs awaiting approval (e.g. fork PRs) never executed and have no
        artifacts, so they must be skipped rather than treated as a pass."""
        pending_run = _make_mock_run(14, 299, "action_required")
        failure_run = _make_mock_run(12, 297, "failure")

        mock_workflow = MagicMock()
        mock_workflow.name = "Daily"
        mock_workflow.get_runs.return_value = [pending_run, failure_run]

        mock_repo = MagicMock()
        mock_repo.get_workflows.return_value = [mock_workflow]

        mock_retry.side_effect = lambda op, **kwargs: op()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        result = get_latest_daily_run(mock_gh, "owner/repo")
        assert result == failure_run


class TestDownloadAllTestFailures:
    """Download now delegates to a (mocked) ArtifactClient."""

    @staticmethod
    def _make_artifact(name: str, artifact_id: int = 555, expired: bool = False):
        from scripts.common.workflow_artifacts import WorkflowArtifact

        return WorkflowArtifact(
            artifact_id=artifact_id, name=name, size_in_bytes=10, expired=expired,
        )

    def test_downloads_and_extracts_json(self) -> None:
        """Should locate the artifact and return the extracted JSON content."""
        failures_data = {"job-1": {"suite": [{"test_name": "t", "test_file": "f.tcl", "error": "e"}]}}

        client = MagicMock()
        client.list_run_artifacts.return_value = [self._make_artifact("all-test-failures")]
        client.download_artifact.return_value = {
            "all-test-failures.json": json.dumps(failures_data).encode(),
        }

        result = download_all_test_failures(
            MagicMock(), "owner/repo", 123, "fake-token", artifact_client=client,
        )
        assert result is not None
        assert json.loads(result) == failures_data
        client.download_artifact.assert_called_once_with(
            "owner/repo", 555, damaged=None,
        )

    def test_returns_none_when_no_artifact(self) -> None:
        """Should return None if no all-test-failures artifact exists."""
        client = MagicMock()
        client.list_run_artifacts.return_value = [self._make_artifact("some-other-artifact")]

        result = download_all_test_failures(
            MagicMock(), "owner/repo", 123, "fake-token", artifact_client=client,
        )
        assert result is None
        client.download_artifact.assert_not_called()

    def test_returns_none_when_no_artifacts_at_all(self) -> None:
        """Should return None if the run has no artifacts."""
        client = MagicMock()
        client.list_run_artifacts.return_value = []

        result = download_all_test_failures(
            MagicMock(), "owner/repo", 123, "fake-token", artifact_client=client,
        )
        assert result is None

    def test_returns_none_when_artifact_expired(self) -> None:
        """Should return None (without downloading) if the artifact is expired."""
        client = MagicMock()
        client.list_run_artifacts.return_value = [
            self._make_artifact("all-test-failures", expired=True)
        ]

        result = download_all_test_failures(
            MagicMock(), "owner/repo", 123, "fake-token", artifact_client=client,
        )
        assert result is None
        client.download_artifact.assert_not_called()

    def test_returns_none_when_json_missing_from_zip(self) -> None:
        """Should return None if the zip lacks the expected JSON file."""
        client = MagicMock()
        client.list_run_artifacts.return_value = [self._make_artifact("all-test-failures")]
        client.download_artifact.return_value = {"something-else.txt": b"nope"}

        result = download_all_test_failures(
            MagicMock(), "owner/repo", 123, "fake-token", artifact_client=client,
        )
        assert result is None

    def test_filters_by_name_so_siblings_cannot_hide_the_artifact(self) -> None:
        """The listing must be name-filtered: a Daily run uploads one artifact
        per job, and an unfiltered first page can omit the one we want."""
        client = MagicMock()
        client.list_run_artifacts.return_value = [self._make_artifact("all-test-failures")]
        client.download_artifact.return_value = {"all-test-failures.json": b"{}"}

        download_all_test_failures(
            MagicMock(), "owner/repo", 123, "fake-token", artifact_client=client,
        )
        assert client.list_run_artifacts.call_args.kwargs["name"] == "all-test-failures"

    def test_prefers_newest_live_artifact_over_expired_attempt(self) -> None:
        """Re-running a workflow leaves one artifact per attempt under the same
        name. An expired earlier attempt must not shadow the usable one."""
        client = MagicMock()
        client.list_run_artifacts.return_value = [
            self._make_artifact("all-test-failures", artifact_id=1, expired=True),
            self._make_artifact("all-test-failures", artifact_id=2, expired=False),
        ]
        client.download_artifact.return_value = {"all-test-failures.json": b'{"ok":1}'}

        result = download_all_test_failures(
            MagicMock(), "owner/repo", 123, "fake-token", artifact_client=client,
        )
        assert result == b'{"ok":1}'
        client.download_artifact.assert_called_once_with(
            "owner/repo", 2, damaged=None,
        )

    def test_returns_none_when_every_attempt_expired(self) -> None:
        client = MagicMock()
        client.list_run_artifacts.return_value = [
            self._make_artifact("all-test-failures", artifact_id=1, expired=True),
            self._make_artifact("all-test-failures", artifact_id=2, expired=True),
        ]

        result = download_all_test_failures(
            MagicMock(), "owner/repo", 123, "fake-token", artifact_client=client,
        )
        assert result is None
        client.download_artifact.assert_not_called()


class TestGetJobUrls:
    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_maps_job_names_to_urls(self, mock_retry) -> None:
        """Should return a mapping of job name to HTML URL."""
        job1 = MagicMock()
        job1.name = "test-ubuntu-latest"
        job1.html_url = "https://github.com/owner/repo/actions/runs/1/job/10"

        job2 = MagicMock()
        job2.name = "test-arm64"
        job2.html_url = "https://github.com/owner/repo/actions/runs/1/job/20"

        mock_run = MagicMock()
        mock_run.jobs.return_value = [job1, job2]

        mock_repo = MagicMock()
        mock_repo.get_workflow_run.return_value = mock_run

        mock_retry.side_effect = lambda op, **kwargs: op()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        result = get_job_urls(mock_gh, "owner/repo", 123)
        assert result["test-ubuntu-latest"] == job1.html_url
        assert result["test-arm64"] == job2.html_url

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_includes_normalized_names(self, mock_retry) -> None:
        """Job names with parens/spaces should also be stored in normalized form."""
        job = MagicMock()
        job.name = "test ubuntu (arm64)"
        job.html_url = "https://example.com/job/1"

        mock_run = MagicMock()
        mock_run.jobs.return_value = [job]

        mock_repo = MagicMock()
        mock_repo.get_workflow_run.return_value = mock_run

        mock_retry.side_effect = lambda op, **kwargs: op()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        result = get_job_urls(mock_gh, "owner/repo", 123)
        assert result["test ubuntu (arm64)"] == job.html_url
        assert result["test-ubuntu-arm64"] == job.html_url

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_alias_does_not_overwrite_exact_job_name(self, mock_retry) -> None:
        """A normalized alias of one job must not clobber another job's exact
        name mapping (which would attach the wrong CI URL)."""
        # job1's normalized form is "test-ubuntu-arm64", which is exactly job2's name.
        job1 = MagicMock()
        job1.name = "test ubuntu (arm64)"
        job1.html_url = "https://example.com/job/1"

        job2 = MagicMock()
        job2.name = "test-ubuntu-arm64"
        job2.html_url = "https://example.com/job/2"

        mock_run = MagicMock()
        mock_run.jobs.return_value = [job1, job2]

        mock_repo = MagicMock()
        mock_repo.get_workflow_run.return_value = mock_run

        mock_retry.side_effect = lambda op, **kwargs: op()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        result = get_job_urls(mock_gh, "owner/repo", 123)
        # The exact job name keeps its own URL; the colliding alias is dropped.
        assert result["test-ubuntu-arm64"] == job2.html_url
        assert result["test ubuntu (arm64)"] == job1.html_url

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_empty_jobs_returns_empty_dict(self, mock_retry) -> None:
        mock_run = MagicMock()
        mock_run.jobs.return_value = []

        mock_repo = MagicMock()
        mock_repo.get_workflow_run.return_value = mock_run

        mock_retry.side_effect = lambda op, **kwargs: op()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        result = get_job_urls(mock_gh, "owner/repo", 123)
        assert result == {}


def _step(number, name, conclusion="success"):
    step = MagicMock()
    step.number = number
    step.name = name
    step.conclusion = conclusion
    return step


class TestGetJobInfoStepUrls:
    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_anchors_each_suite_to_its_step(self, mock_retry) -> None:
        """A suite links to the step that ran it, not the job's first failure."""
        job = MagicMock()
        job.name = "test-ubuntu-jemalloc"
        job.html_url = "https://example.com/job/1"
        job.conclusion = "failure"
        job.steps = [
            _step(9, "test"),
            _step(10, "module api test"),
            _step(11, "sentinel tests"),
            _step(12, "unittest", "failure"),
        ]

        mock_run = MagicMock()
        mock_run.jobs.return_value = [job]
        mock_repo = MagicMock()
        mock_repo.get_workflow_run.return_value = mock_run
        mock_retry.side_effect = lambda op, **kwargs: op()
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        info = get_job_info(mock_gh, "owner/repo", 123)
        assert info.url_for("test-ubuntu-jemalloc", "valkey") == "https://example.com/job/1#step:9:1"
        assert info.url_for("test-ubuntu-jemalloc", "unittest") == "https://example.com/job/1#step:12:1"

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_unmapped_suite_falls_back_to_plain_job_url(self, mock_retry) -> None:
        """A suite with no step mapping keeps the plain job URL."""
        job = MagicMock()
        job.name = "test-ubuntu-jemalloc"
        job.html_url = "https://example.com/job/1"
        job.conclusion = "failure"
        job.steps = [_step(9, "test")]

        mock_run = MagicMock()
        mock_run.jobs.return_value = [job]
        mock_repo = MagicMock()
        mock_repo.get_workflow_run.return_value = mock_run
        mock_retry.side_effect = lambda op, **kwargs: op()
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        info = get_job_info(mock_gh, "owner/repo", 123)
        assert info.url_for("test-ubuntu-jemalloc", "sentinel") == "https://example.com/job/1"

    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_matrix_alias_shares_step_urls(self, mock_retry) -> None:
        """A normalized job alias resolves the same step URLs as its exact name."""
        job = MagicMock()
        job.name = "test-valgrind-test (unit)"
        job.html_url = "https://example.com/job/2"
        job.conclusion = "failure"
        job.steps = [_step(7, "test", "failure")]

        mock_run = MagicMock()
        mock_run.jobs.return_value = [job]
        mock_repo = MagicMock()
        mock_repo.get_workflow_run.return_value = mock_run
        mock_retry.side_effect = lambda op, **kwargs: op()
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        info = get_job_info(mock_gh, "owner/repo", 123)
        assert info.url_for("test-valgrind-test-unit", "valkey") == "https://example.com/job/2#step:7:1"


class TestGetJobInfoFailedJobs:
    @patch("scripts.test_failure_detector.download.retry_github_call")
    def test_classifies_failure_and_timed_out_as_failed(self, mock_retry) -> None:
        """A job the runner killed on the timeout concludes "timed_out", not
        "failure". Both must land in ``failed`` so timeout recovery scans the
        console logs where the [TIMEOUT] lines live."""
        def _job(name, conclusion):
            job = MagicMock()
            job.name = name
            job.html_url = f"https://example.com/{name}"
            job.conclusion = conclusion
            job.steps = []
            return job

        jobs = [
            _job("job-failure", "failure"),
            _job("job-timed-out", "timed_out"),
            _job("job-success", "success"),
            _job("job-cancelled", "cancelled"),
        ]
        mock_run = MagicMock()
        mock_run.jobs.return_value = jobs
        mock_repo = MagicMock()
        mock_repo.get_workflow_run.return_value = mock_run
        mock_retry.side_effect = lambda op, **kwargs: op()
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        info = get_job_info(mock_gh, "owner/repo", 123)
        assert info.failed == {"job-failure", "job-timed-out"}
