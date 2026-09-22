"""Tests for the test-failure-detector entry point (mocked GitHub + I/O)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# PyGithub requires urllib3 v2 + OpenSSL 1.1.1+. On older dev hosts the import
# fails at collection time. Guard with a skip so the test file is still valid.
try:
    from scripts.test_failure_detector import main as detector_main
    from scripts.test_failure_detector.download import JobInfo
    from scripts.test_failure_detector.parse_failures import (
        FailureType,
        JobReference,
        UniqueFailure,
    )

    _SKIP_REASON = None
except ImportError as _exc:
    _SKIP_REASON = f"PyGithub import failed: {_exc}"

pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


class TestRunArtifactJSONGuard:
    """A malformed artifact must be reported, not crash the run."""

    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_malformed_artifact_returns_nonzero_and_reports(
        self, _mock_gh, _mock_client, mock_download, mock_emit,
    ) -> None:
        # A truncated/invalid artifact body: json.loads would raise.
        mock_download.return_value = b"{not valid json"

        rc = detector_main.run(
            github_token="t", repo_full_name="valkey-io/valkey", run_id=123,
        )

        assert rc == 1
        # The failure is surfaced in the job summary rather than crashing.
        mock_emit.assert_called_once()
        summary = mock_emit.call_args.args[0]
        assert "Could not parse" in summary

    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_scalar_json_artifact_returns_nonzero_and_reports(
        self, _mock_gh, _mock_client, mock_download, mock_emit,
    ) -> None:
        # A bare scalar: json.loads succeeds, then len() would crash. Reported
        # as malformed rather than propagating a TypeError.
        mock_download.return_value = b"123"

        rc = detector_main.run(
            github_token="t", repo_full_name="valkey-io/valkey", run_id=123,
        )

        assert rc == 1
        mock_emit.assert_called_once()
        assert "unexpected format" in mock_emit.call_args.args[0]

    @patch("scripts.test_failure_detector.main.parse_and_deduplicate")
    @patch("scripts.test_failure_detector.main.get_job_info")
    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_list_json_artifact_returns_nonzero_and_reports(
        self, _mock_gh, _mock_client, mock_download, mock_emit,
        mock_job_info, mock_parse,
    ) -> None:
        # A top-level list parses fine but is the wrong shape: without the guard
        # it slips past parse_and_deduplicate as "no failures" and exits 0.
        mock_download.return_value = b"[1, 2, 3]"

        rc = detector_main.run(
            github_token="t", repo_full_name="valkey-io/valkey", run_id=123,
        )

        assert rc == 1
        # Bailed before parsing the wrong-shaped artifact.
        mock_parse.assert_not_called()
        mock_emit.assert_called_once()
        assert "unexpected format" in mock_emit.call_args.args[0]


class TestRunProcessingErrorsExitCode:
    """Per-failure processing errors must exit non-zero so CI does not stay
    green while issue updates were skipped."""

    @patch("scripts.test_failure_detector.main.process_failures")
    @patch("scripts.test_failure_detector.main.parse_and_deduplicate")
    @patch("scripts.test_failure_detector.main.recover_timeouts")
    @patch("scripts.test_failure_detector.main.get_job_info")
    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_returns_nonzero_when_process_failures_reports_errors(
        self, _mock_gh, _mock_client, mock_download, mock_emit,
        mock_job_info, mock_recover, mock_parse, mock_process,
    ) -> None:
        mock_download.return_value = b'{"job": {"suite": []}}'
        mock_recover.return_value = []
        mock_parse.return_value = [MagicMock(display_name="t", jobs=[])]
        mock_process.return_value = {
            "created": 1, "updated": 0, "skipped": 0, "errors": 1,
        }

        rc = detector_main.run(
            github_token="t", repo_full_name="valkey-io/valkey", run_id=123,
        )

        assert rc == 1
        # Summary still emitted before exiting non-zero.
        mock_emit.assert_called_once()

    @patch("scripts.test_failure_detector.main.process_failures")
    @patch("scripts.test_failure_detector.main.parse_and_deduplicate")
    @patch("scripts.test_failure_detector.main.recover_timeouts")
    @patch("scripts.test_failure_detector.main.get_job_info")
    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_returns_zero_when_no_processing_errors(
        self, _mock_gh, _mock_client, mock_download, mock_emit,
        mock_job_info, mock_recover, mock_parse, mock_process,
    ) -> None:
        mock_download.return_value = b'{"job": {"suite": []}}'
        mock_recover.return_value = []
        mock_parse.return_value = [MagicMock(display_name="t", jobs=[])]
        mock_process.return_value = {
            "created": 1, "updated": 1, "skipped": 0, "errors": 0,
        }

        rc = detector_main.run(
            github_token="t", repo_full_name="valkey-io/valkey", run_id=123,
        )

        assert rc == 0


class TestExplicitRunIdMissingArtifact:
    """A red run with no artifact must be reported as a problem even when the
    run was named with --run-id rather than discovered. That is the path used
    to triage a run whose consolidate step died, so treating it as a clean pass
    would hide the failure behind a green sweep."""

    # A run killed on the workflow timeout concludes "timed_out"; one that died
    # before any job started concludes "startup_failure". Both leave failures
    # unreported, so a missing artifact for them is a reporting gap.
    @pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure"])
    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.get_run_conclusion")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_failure_like_run_without_artifact_is_reported(
        self, _mock_gh, _mock_client, mock_download, mock_conclusion, mock_emit,
        conclusion,
    ) -> None:
        mock_download.return_value = None
        mock_conclusion.return_value = conclusion

        rc = detector_main.run(
            github_token="t", repo_full_name="valkey-io/valkey", run_id=123,
        )

        assert rc == 1
        summary = mock_emit.call_args.args[0]
        assert "uploaded no" in summary
        assert conclusion in summary

    # A run that never executed tests (cancelled, skipped, neutral) has nothing
    # to report, so a missing artifact is not treated as a failure.
    @pytest.mark.parametrize("conclusion", ["success", "cancelled"])
    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.get_run_conclusion")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_non_failure_run_without_artifact_is_a_clean_pass(
        self, _mock_gh, _mock_client, mock_download, mock_conclusion, mock_emit,
        conclusion,
    ) -> None:
        mock_download.return_value = None
        mock_conclusion.return_value = conclusion

        rc = detector_main.run(
            github_token="t", repo_full_name="valkey-io/valkey", run_id=123,
        )

        assert rc == 0


class TestRunIncompleteArtifact:
    """An artifact with unreadable members means the run was only partly
    analyzed, so the detector must report that and exit non-zero even when
    every readable failure got an issue."""

    @patch("scripts.test_failure_detector.main.process_failures")
    @patch("scripts.test_failure_detector.main.enrich_log_only_errors")
    @patch("scripts.test_failure_detector.main.recover_timeouts")
    @patch("scripts.test_failure_detector.main.get_job_info")
    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_damaged_member_fails_run_and_is_surfaced(
        self, _mock_gh, _mock_client, mock_download, mock_emit,
        mock_job_info, mock_recover, _mock_enrich, mock_process,
    ) -> None:
        def _download(*_args, **kwargs):
            kwargs["damaged"].append("bad.bin: NotImplementedError: nope")
            return b'{"job": {"valkey": [{"test_name": "t", "test_file": "f.tcl", "error": "e"}]}}'

        mock_download.side_effect = _download
        mock_job_info.return_value = JobInfo(
            urls={"job": "http://x"}, failed=set(),
        )
        mock_recover.return_value = []
        mock_process.return_value = {
            "created": 1, "updated": 0, "skipped": 0, "skipped_closed": 0, "errors": 0,
        }

        rc = detector_main.run(
            github_token="t", repo_full_name="valkey-io/valkey", run_id=123,
        )

        assert rc == 1
        summary = mock_emit.call_args.args[0]
        assert "Incomplete artifact" in summary
        assert "was not analyzed" in summary
        # The issue really was filed; the non-zero exit is about coverage.
        assert "| Issues created | 1 |" in summary

    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_damaged_archive_with_no_content_still_fails(
        self, _mock_gh, _mock_client, mock_download, mock_emit,
    ) -> None:
        """Nothing readable plus recorded damage is a failure, not a clean pass."""
        def _download(*_args, **kwargs):
            kwargs["damaged"].append("whole archive: BadZipFile: bad")
            return None

        mock_download.side_effect = _download

        rc = detector_main.run(
            github_token="t", repo_full_name="valkey-io/valkey", run_id=123,
        )

        assert rc == 1
        assert "Incomplete artifact" in mock_emit.call_args.args[0]

    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_missing_artifact_without_damage_still_passes(
        self, _mock_gh, _mock_client, mock_download, mock_emit,
    ) -> None:
        """A run with no artifact and no damage keeps the clean-pass path."""
        mock_download.return_value = None

        rc = detector_main.run(
            github_token="t", repo_full_name="valkey-io/valkey", run_id=123,
        )

        assert rc == 0
        assert "Incomplete artifact" not in mock_emit.call_args.args[0]


class TestJobSummaryIncompleteSection:
    """The incomplete-artifact section is only for runs that actually lost
    part of the artifact; a healthy run's summary must not mention it."""

    def test_section_absent_without_damage(self) -> None:
        for damaged in (None, []):
            summary = detector_main._build_job_summary(
                1, "owner/repo", 2, {"created": 2}, damaged,
            )
            assert "Incomplete artifact" not in summary

    def test_section_lists_each_damaged_entry(self) -> None:
        summary = detector_main._build_job_summary(
            1, "owner/repo", 2, {"created": 2}, ["a.json: X", "b.json: Y"],
        )
        assert "### Incomplete artifact" in summary
        assert "2 part(s)" in summary
        assert "- `a.json: X`" in summary
        assert "- `b.json: Y`" in summary

    def test_section_entries_stay_inside_one_code_span(self) -> None:
        """Entries carry a member name and an exception message, so a backtick
        or newline in either must not break the markdown list."""
        summary = detector_main._build_job_summary(
            1, "owner/repo", 0, {}, ["we`ird.json: X", "multi\nline: Y"],
        )
        items = [l for l in summary.split("\n") if l.startswith("- `")]
        assert len(items) == 2
        for item in items:
            assert item.count("`") == 2
            assert "\n" not in item


class TestUnknownConclusionFailsClosed:
    """An explicit run whose conclusion cannot be read is not evidence of a
    clean run. get_run_conclusion returns None on any API error, so folding
    that into the clean path would report a red run as green whenever the
    lookup itself failed."""

    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.get_run_conclusion")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_unreadable_conclusion_without_artifact_is_reported(
        self, _mock_gh, _mock_client, mock_download, mock_conclusion, mock_emit,
    ) -> None:
        mock_download.return_value = None
        mock_conclusion.return_value = None

        rc = detector_main.run(
            github_token="t", repo_full_name="valkey-io/valkey", run_id=123,
        )

        assert rc == 1
        assert "conclusion" in mock_emit.call_args.args[0]


class TestUnexplainedFailedJobs:
    """A red job no reported failure accounts for must not pass as clean."""

    def _failure(self, job: str):
        return UniqueFailure(
            test_name="t", test_file="f.tcl",
            failure_type=FailureType.ASSERTION,
            error="boom",
            jobs=[JobReference(job=job, suite="valkey", url="u")],
        )

    def test_a_represented_job_is_not_flagged(self) -> None:
        assert detector_main._unexplained_failed_jobs(
            [self._failure("test-ubuntu")], {"test-ubuntu"},
        ) == []

    def test_a_job_with_no_failure_is_flagged(self) -> None:
        assert detector_main._unexplained_failed_jobs(
            [], {"test-sanitizer-address"},
        ) == ["test-sanitizer-address"]

    def test_matrix_spellings_count_as_the_same_job(self) -> None:
        """The artifact keys a matrix job "base-value" while the API names it
        "base (value)". Comparing one spelling would flag every matrix job."""
        assert detector_main._unexplained_failed_jobs(
            [self._failure("test-sanitizer-address-gcc")],
            {"test-sanitizer-address (gcc)"},
        ) == []

    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.enrich_log_only_errors")
    @patch("scripts.test_failure_detector.main.recover_timeouts")
    @patch("scripts.test_failure_detector.main.get_job_info")
    @patch("scripts.test_failure_detector.main.get_run_conclusion")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_a_red_job_with_an_empty_artifact_exits_nonzero(
        self, _mock_gh, _mock_client, mock_download, mock_conclusion,
        mock_job_info, mock_recover, _mock_enrich, mock_emit,
    ) -> None:
        """The artifact parses but names no failure, so without the cross-check
        the run would report "No test failures" and exit 0."""
        mock_download.return_value = "{}"
        mock_conclusion.return_value = "failure"
        mock_recover.return_value = []
        mock_job_info.return_value = JobInfo(
            urls={"test-sanitizer-address": "u"}, step_urls={},
            failed={"test-sanitizer-address"},
        )

        rc = detector_main.run(
            github_token="t", repo_full_name="valkey-io/valkey", run_id=123,
        )

        assert rc == 1
        assert "no reported failure" in mock_emit.call_args.args[0]
