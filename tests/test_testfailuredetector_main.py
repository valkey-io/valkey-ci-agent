"""Tests for the test-failure-detector entry point (mocked GitHub + I/O)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# PyGithub requires urllib3 v2 + OpenSSL 1.1.1+. On older dev hosts the import
# fails at collection time. Guard with a skip so the test file is still valid.
try:
    from scripts.test_failure_detector import main as detector_main

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
        # A truncated/invalid artifact body — json.loads would raise.
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
    @patch("scripts.test_failure_detector.main.get_job_urls")
    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_list_json_artifact_returns_nonzero_and_reports(
        self, _mock_gh, _mock_client, mock_download, mock_emit,
        mock_job_urls, mock_parse,
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
    @patch("scripts.test_failure_detector.main.get_job_urls")
    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_returns_nonzero_when_process_failures_reports_errors(
        self, _mock_gh, _mock_client, mock_download, mock_emit,
        mock_job_urls, mock_parse, mock_process,
    ) -> None:
        mock_download.return_value = b'{"job": {"suite": []}}'
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
    @patch("scripts.test_failure_detector.main.get_job_urls")
    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_returns_zero_when_no_processing_errors(
        self, _mock_gh, _mock_client, mock_download, mock_emit,
        mock_job_urls, mock_parse, mock_process,
    ) -> None:
        mock_download.return_value = b'{"job": {"suite": []}}'
        mock_parse.return_value = [MagicMock(display_name="t", jobs=[])]
        mock_process.return_value = {
            "created": 1, "updated": 1, "skipped": 0, "errors": 0,
        }

        rc = detector_main.run(
            github_token="t", repo_full_name="valkey-io/valkey", run_id=123,
        )

        assert rc == 0


class TestRunIncompleteArtifact:
    """An artifact with unreadable members means the run was only partly
    analyzed, so the detector must report that and exit non-zero even when
    every readable failure got an issue."""

    @patch("scripts.test_failure_detector.main.process_failures")
    @patch("scripts.test_failure_detector.main.get_job_urls")
    @patch("scripts.test_failure_detector.main.emit_job_summary")
    @patch("scripts.test_failure_detector.main.download_all_test_failures")
    @patch("scripts.test_failure_detector.main.ArtifactClient")
    @patch("scripts.test_failure_detector.main.Github")
    def test_damaged_member_fails_run_and_is_surfaced(
        self, _mock_gh, _mock_client, mock_download, mock_emit,
        mock_urls, mock_process,
    ) -> None:
        def _download(*_args, **kwargs):
            kwargs["damaged"].append("bad.bin: NotImplementedError: nope")
            return b'{"job": {"valkey": [{"test_name": "t", "test_file": "f.tcl", "error": "e"}]}}'

        mock_download.side_effect = _download
        mock_urls.return_value = {"job": "http://x"}
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
