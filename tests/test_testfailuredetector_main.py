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
        assert "123" in summary
