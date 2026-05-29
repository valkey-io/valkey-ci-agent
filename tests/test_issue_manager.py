"""Tests for issue creation/update (mocked GitHub API)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, call, patch

import pytest

# PyGithub requires urllib3 v2 + OpenSSL 1.1.1+. On older dev hosts the import
# fails at collection time. Guard with a skip so the test file is still valid.
try:
    from github.GithubException import GithubException

    from scripts.test_failure_detector.manage_issues import (
        _build_issue_body,
        _build_issue_title,
        _extract_environments_from_body,
        _update_environments_in_body,
        ensure_label_exists,
        process_failures,
    )
    from scripts.test_failure_detector.parse_failures import JobReference, UniqueFailure

    _SKIP_REASON = None
except ImportError as _exc:
    _SKIP_REASON = f"PyGithub import failed: {_exc}"

pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


# --- Helper fixtures ---


def _make_failure(
    test_name: str = "PSYNC2 test",
    test_file: str = "tests/integration/replication-psync.tcl",
    error: str = "Expected replica to be in sync",
    jobs: list[tuple[str, str, str]] | None = None,
) -> UniqueFailure:
    if jobs is None:
        jobs = [("test-ubuntu-latest", "integration", "https://example.com/job/1")]
    return UniqueFailure(
        test_name=test_name,
        test_file=test_file,
        error=error,
        jobs=[JobReference(job=j, suite=s, url=u) for j, s, u in jobs],
    )


# --- Unit tests for helper functions ---


class TestBuildIssueTitle:
    def test_format(self) -> None:
        f = _make_failure()
        title = _build_issue_title(f)
        assert title == "[TEST-FAILURE] PSYNC2 test in tests/integration/replication-psync.tcl"


class TestBuildIssueBody:
    def test_contains_test_name(self) -> None:
        f = _make_failure()
        body = _build_issue_body(f)
        assert "`PSYNC2 test`" in body

    def test_contains_test_file(self) -> None:
        f = _make_failure()
        body = _build_issue_body(f)
        assert "`tests/integration/replication-psync.tcl`" in body

    def test_contains_error_trace(self) -> None:
        f = _make_failure(error="assertion failed at line 42")
        body = _build_issue_body(f)
        assert "assertion failed at line 42" in body

    def test_contains_environments(self) -> None:
        f = _make_failure(jobs=[
            ("job-a", "suite", "url1"),
            ("job-b", "suite", "url2"),
        ])
        body = _build_issue_body(f)
        assert "`job-a`" in body
        assert "`job-b`" in body

    def test_contains_ci_links(self) -> None:
        f = _make_failure(jobs=[("job-a", "suite", "https://example.com/run")])
        body = _build_issue_body(f)
        assert "[CI link](https://example.com/run)" in body

    def test_contains_auto_created_footer(self) -> None:
        f = _make_failure()
        body = _build_issue_body(f)
        assert "Auto-created by Test Failure Detector" in body


class TestExtractEnvironments:
    def test_extracts_backtick_envs(self) -> None:
        body = "**Environments:** `job-a`, `job-b`, `job-c`"
        envs = _extract_environments_from_body(body)
        assert envs == ["job-a", "job-b", "job-c"]

    def test_returns_empty_when_no_match(self) -> None:
        body = "No environments line here"
        envs = _extract_environments_from_body(body)
        assert envs == []


class TestUpdateEnvironments:
    def test_replaces_environments_line(self) -> None:
        body = "Some text\n**Environments:** `old-job`\nMore text"
        updated = _update_environments_in_body(body, ["old-job", "new-job"])
        assert "**Environments:** `old-job`, `new-job`" in updated
        assert "Some text" in updated
        assert "More text" in updated


# --- Integration tests with mocked GitHub API ---


class TestProcessFailures:
    @patch("scripts.test_failure_detector.manage_issues.retry_github_call")
    def test_creates_new_issue(self, mock_retry) -> None:
        """When no existing issue matches, a new one should be created."""
        mock_repo = MagicMock()
        mock_repo.full_name = "valkey-io/valkey"
        mock_repo.get_label.return_value = MagicMock()  # label exists
        mock_repo.get_issues.return_value = []  # no existing issues

        # Make retry_github_call just execute the operation
        mock_retry.side_effect = lambda op, **kwargs: op()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        failures = [_make_failure()]
        result = process_failures(mock_gh, "valkey-io/valkey", failures)

        assert result["created"] == 1
        mock_repo.create_issue.assert_called_once()
        create_kwargs = mock_repo.create_issue.call_args
        assert "[TEST-FAILURE]" in create_kwargs.kwargs.get("title", "") or "[TEST-FAILURE]" in (create_kwargs[1].get("title", "") if len(create_kwargs) > 1 else "")

    @patch("scripts.test_failure_detector.manage_issues.retry_github_call")
    def test_updates_existing_issue_with_comment(self, mock_retry) -> None:
        """When an existing issue matches, it should get a comment."""
        mock_issue = MagicMock()
        mock_issue.title = "[TEST-FAILURE] PSYNC2 test in tests/integration/replication-psync.tcl"
        mock_issue.number = 42
        mock_issue.body = "**Environments:** `test-ubuntu-latest`"

        mock_repo = MagicMock()
        mock_repo.full_name = "valkey-io/valkey"
        mock_repo.get_label.return_value = MagicMock()
        mock_repo.get_issues.return_value = [mock_issue]

        mock_retry.side_effect = lambda op, **kwargs: op()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        failures = [_make_failure()]
        result = process_failures(mock_gh, "valkey-io/valkey", failures)

        assert result["updated"] == 1
        mock_issue.create_comment.assert_called_once()

    @patch("scripts.test_failure_detector.manage_issues.retry_github_call")
    def test_updates_body_with_new_environments(self, mock_retry) -> None:
        """When a failure appears in a new environment, the body should be updated."""
        mock_issue = MagicMock()
        mock_issue.title = "[TEST-FAILURE] PSYNC2 test in tests/integration/replication-psync.tcl"
        mock_issue.number = 42
        mock_issue.body = "**Environments:** `old-job`"

        mock_repo = MagicMock()
        mock_repo.full_name = "valkey-io/valkey"
        mock_repo.get_label.return_value = MagicMock()
        mock_repo.get_issues.return_value = [mock_issue]

        mock_retry.side_effect = lambda op, **kwargs: op()

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        # Failure in a different job than what's already recorded
        failures = [_make_failure(jobs=[("new-job", "suite", "url")])]
        process_failures(mock_gh, "valkey-io/valkey", failures)

        mock_issue.edit.assert_called_once()
        updated_body = mock_issue.edit.call_args[1]["body"]
        assert "`old-job`" in updated_body
        assert "`new-job`" in updated_body

    @patch("scripts.test_failure_detector.manage_issues.retry_github_call")
    def test_creates_label_if_missing(self, mock_retry) -> None:
        """If the test-failure label doesn't exist, it should be created."""
        mock_repo = MagicMock()
        mock_repo.full_name = "valkey-io/valkey"
        mock_repo.get_issues.return_value = []

        call_count = {"n": 0}

        def side_effect(op, **kwargs):
            call_count["n"] += 1
            # First call is get_label which should raise 404
            if call_count["n"] == 1:
                raise GithubException(404, {"message": "Not Found"})
            return op()

        mock_retry.side_effect = side_effect

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        # This will trigger ensure_label_exists which should handle the 404
        # and create the label
        ensure_label_exists(mock_repo)
        mock_repo.create_label.assert_called_once()
