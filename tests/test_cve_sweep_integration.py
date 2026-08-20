"""Integration tests for the CVE scan sweep: real settings + real output emission.

load_settings and _emit_outputs are NOT mocked; only scan_images and the HTTP
manifest fetch are patched. Base verification is gone: the sweep now emits the
build-and-verify contract (versions/plan) instead of predicting
fixes. Covers contract emission, the grouped plan, config-error
regression, dynamic resolution, the scan-failure summary, and job summary
content.
"""

from __future__ import annotations

import json
import os
from io import BytesIO
from pathlib import Path

import pytest

from scripts.cve_scan.config import CveScanConfigError, load_settings
from scripts.cve_scan.models import Finding, Severity
from scripts.cve_scan.scanner import ScanError
from scripts.cve_scan.sweep import run_sweep


@pytest.fixture(autouse=True)
def _clean_cve_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all CVE_SCAN_* env vars for a clean slate."""
    for key in list(os.environ):
        if key.startswith("CVE_SCAN_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def github_output_file(tmp_path: Path) -> Path:
    """Create a temp file to act as GITHUB_OUTPUT."""
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    return output_file


def _mock_urlopen_response(data: dict) -> BytesIO:
    """Create a mock response object for urllib.request.urlopen."""
    body = json.dumps(data).encode("utf-8")
    resp = BytesIO(body)
    resp.status = 200  # type: ignore[attr-defined]
    return resp


def _outputs(text: str) -> dict[str, str]:
    """Parse a GITHUB_OUTPUT file's key=value lines into a dict (last wins)."""
    result: dict[str, str] = {}
    for line in text.strip().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            result[key] = value
    return result


SAMPLE_VERSIONS = {
    "7.2": {"version": "7.2.13", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
    "8.0": {"version": "8.0.9", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
    "8.1": {"version": "8.1.8", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
    "9.0": {"version": "9.0.4", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
    "9.1": {"version": "9.1.0", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
    "unstable": {"version": "unstable", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
}


def _fixable_finding(
    *,
    image: str = "valkey/valkey:8.0-alpine",
    package: str = "openssl",
    cve_id: str = "CVE-2024-1234",
    installed: str = "3.0.12-r0",
    fixed: str = "3.0.13-r0",
    platform: str = "linux/amd64",
) -> Finding:
    return Finding(
        image=image,
        package=package,
        installed_version=installed,
        cve_id=cve_id,
        severity=Severity.HIGH,
        fixed_version=fixed,
        platform=platform,
    )


class TestContractEmission:
    """Real load_settings + real output emission produces the verification plan."""

    def test_fixable_finding_emits_true_versions_and_plan(
        self, monkeypatch: pytest.MonkeyPatch, github_output_file: Path
    ) -> None:
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output_file))
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: [_fixable_finding()],
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )

        run_sweep(settings=load_settings(), dry_run=True)

        out = _outputs(github_output_file.read_text())
        assert out["versions"] == "8.0"
        assert json.loads(out["plan"]) == [
            {
                "line": "8.0",
                "variant": "alpine",
                "platform": "linux/amd64",
                "cves": ["CVE-2024-1234"],
            }
        ]

    def test_multiple_fixable_images_versions_sorted_and_plan_complete(
        self, monkeypatch: pytest.MonkeyPatch, github_output_file: Path
    ) -> None:
        findings = [
            _fixable_finding(
                image="valkey/valkey:9.0-alpine",
                package="zlib",
                cve_id="CVE-2024-5678",
                installed="1.2.13-r0",
                fixed="1.2.14-r0",
            ),
            _fixable_finding(
                image="valkey/valkey:7.2-alpine",
                package="openssl",
                cve_id="CVE-2024-1111",
                installed="3.0.10-r0",
                fixed="3.0.11-r0",
            ),
        ]
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output_file))
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: findings,
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )

        run_sweep(settings=load_settings(), dry_run=True)

        out = _outputs(github_output_file.read_text())
        assert out["versions"] == "7.2 9.0"
        plan = json.loads(out["plan"])
        assert {cve for leg in plan for cve in leg["cves"]} == {
            "CVE-2024-5678",
            "CVE-2024-1111",
        }


class TestNotFixable:
    def test_no_fix_available_emits_false_empty_versions_and_plan(
        self, monkeypatch: pytest.MonkeyPatch, github_output_file: Path
    ) -> None:
        not_fixable = [
            Finding(
                image="valkey/valkey:8.0-alpine",
                package="busybox",
                installed_version="1.36.1-r0",
                cve_id="CVE-2024-9999",
                severity=Severity.HIGH,
                fixed_version=None,
                platform="linux/amd64",
            ),
        ]
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output_file))
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: not_fixable,
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )

        run_sweep(settings=load_settings(), dry_run=True)

        out = _outputs(github_output_file.read_text())
        assert out["versions"] == ""
        assert out["plan"] == "[]"

    def test_zero_findings_emits_false(self, monkeypatch: pytest.MonkeyPatch, github_output_file: Path) -> None:
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output_file))
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: [],
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )

        run_sweep(settings=load_settings(), dry_run=True)

        out = _outputs(github_output_file.read_text())
        assert out["plan"] == "[]"


class TestIntegrationConfigError:
    """Proves the REAL load path is exercised (not mocked away)."""

    def test_invalid_scanner_raises_cve_scan_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CVE_SCAN_SCANNER", "unknown-scanner")
        with pytest.raises(CveScanConfigError, match="Invalid CVE_SCAN_SCANNER"):
            load_settings()

    def test_invalid_severity_raises_cve_scan_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CVE_SCAN_SEVERITY_THRESHOLD", "INVALID")
        with pytest.raises(CveScanConfigError, match="Invalid CVE_SCAN_SEVERITY_THRESHOLD"):
            load_settings()


class TestIntegrationDynamic:
    """Dynamic settings with mocked HTTP fetch. Only scan_images and urlopen mocked."""

    def test_dynamic_settings_resolves_and_scans(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        github_output = tmp_path / "github_output"
        github_output.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

        scanned_images: list[str] = []

        def mock_scan(images, scanner, threshold, **_kw):
            scanned_images.extend(images)
            return [_fixable_finding()]

        monkeypatch.setattr("scripts.cve_scan.sweep.scan_images", mock_scan)
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )

        run_sweep(settings=load_settings(), dry_run=True)

        # 10 stable images resolved (5 alpine + 5 bare); unstable excluded.
        assert len(scanned_images) == 10
        assert "valkey/valkey:7.2-alpine" in scanned_images
        assert "valkey/valkey:9.1" in scanned_images
        assert "valkey/valkey:unstable-alpine" not in scanned_images
        assert "valkey/valkey:unstable" not in scanned_images

        out = _outputs(github_output.read_text())
        assert out["plan"] != "[]"


class TestSweepOutputNoEnvVar:
    """When GITHUB_OUTPUT is unset, run_sweep prints and does not raise."""

    def test_no_github_output_env_prints_contract(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: [_fixable_finding()],
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )

        run_sweep(settings=load_settings(), dry_run=True)

        captured = capsys.readouterr()
        assert "versions=8.0" in captured.out
        assert "plan=[" in captured.out

    def test_no_github_output_env_no_findings(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: [],
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )

        run_sweep(settings=load_settings(), dry_run=True)

        captured = capsys.readouterr()
        assert "plan=[]" in captured.out


class TestDryRunStdout:
    """Dry-run stdout frames candidates as pending artifact verification."""

    def test_fixable_candidate_printed_as_verification_target(
        self,
        monkeypatch: pytest.MonkeyPatch,
        github_output_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output_file))
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: [_fixable_finding()],
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )

        run_sweep(settings=load_settings(), dry_run=True)

        captured = capsys.readouterr()
        assert "VERIFICATION TARGETS" in captured.out
        assert "pending artifact verification" in captured.out
        assert "CVE-2024-1234" in captured.out


class TestJobSummaryContent:
    """Verify job summary includes findings tables with candidate wording."""

    def test_fixable_findings_appear_in_job_summary(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        github_output = tmp_path / "github_output"
        github_output.write_text("")
        summary_file = tmp_path / "step_summary"
        summary_file.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: [_fixable_finding()],
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )

        run_sweep(settings=load_settings(), dry_run=False)

        summary = summary_file.read_text()
        assert "CVE Scan Summary" in summary
        assert "CVE-2024-1234" in summary
        assert "Fixable candidates (pending artifact verification" in summary
        # The scan job must never claim a fix is confirmed.
        assert "Confirmed fixable" not in summary

    def test_not_fixable_findings_appear_in_job_summary(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        github_output = tmp_path / "github_output"
        github_output.write_text("")
        summary_file = tmp_path / "step_summary"
        summary_file.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

        findings = [
            Finding(
                image="valkey/valkey:8.0-alpine",
                package="busybox",
                installed_version="1.36.1-r0",
                cve_id="CVE-2024-9999",
                severity=Severity.HIGH,
                fixed_version=None,
                platform="linux/amd64",
            ),
        ]
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: findings,
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )

        run_sweep(settings=load_settings(), dry_run=False)

        summary = summary_file.read_text()
        assert "CVE Scan Summary" in summary
        assert "CVE-2024-9999" in summary
        assert "Unresolved findings" in summary


class TestScanFailureSummary:
    """A ScanError re-raises but first records a failure section in the summary."""

    def test_scan_error_emits_summary_and_reraises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        github_output = tmp_path / "github_output"
        github_output.write_text("")
        summary_file = tmp_path / "step_summary"
        summary_file.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

        def _raise(images, scanner, threshold, **_kw):
            raise ScanError("Scanner exited with code 1: trivy image valkey/valkey:8.0-alpine")

        monkeypatch.setattr("scripts.cve_scan.sweep.scan_images", _raise)
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )

        with pytest.raises(ScanError, match="valkey/valkey:8.0-alpine"):
            run_sweep(settings=load_settings(), dry_run=False)

        summary = summary_file.read_text()
        assert "Scan failed" in summary
        assert "no rebuild will be dispatched" in summary
        assert "valkey/valkey:8.0-alpine" in summary
