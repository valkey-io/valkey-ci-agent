"""Integration tests for the CVE scan sweep: real settings loading + real output emission.

load_settings and _emit_outputs are NOT mocked; only scan_images, the HTTP
fetch, and base package reads are patched. Covers fixable/not-fixable output
emission, config-error regression, dynamic resolution, base pre-check
downgrades, static-mode dispatch disable, and job summary content.
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


@pytest.fixture(autouse=True)
def _mock_native_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch _native_compare with a deterministic stub (no real Docker).

    Parses the 'X.Y.Z[-rN]' shapes used in these tests as tuples of ints;
    anything else returns None (fail-closed, like the native comparator).
    """
    def parse(version: str) -> "tuple[int, ...] | None":
        nums, _, rev = version.partition("-r")
        try:
            return tuple(int(p) for p in nums.split(".")) + (int(rev) if rev else 0,)
        except ValueError:
            return None

    def stub_compare(a: str, b: str, flavor: str, base_image: str | None = None) -> int | None:
        pa, pb = parse(a), parse(b)
        if pa is None or pb is None:
            return None
        return (pa > pb) - (pa < pb)

    monkeypatch.setattr(
        "scripts.cve_scan.base_precheck._native_compare",
        stub_compare,
    )


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
    resp.__enter__ = lambda self: self  # type: ignore[attr-defined]
    resp.__exit__ = lambda self, *a: None  # type: ignore[attr-defined]
    return resp


SAMPLE_VERSIONS = {
    "7.2": {"version": "7.2.13", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
    "8.0": {"version": "8.0.9", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
    "8.1": {"version": "8.1.8", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
    "9.0": {"version": "9.0.4", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
    "9.1": {"version": "9.1.0", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
    "unstable": {"version": "unstable", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
}


class TestIntegrationFixable:
    """Real load_settings + real _emit_outputs with a fixable finding."""

    def test_fixable_finding_emits_true(
        self,
        monkeypatch: pytest.MonkeyPatch,
        github_output_file: Path,
    ) -> None:
        """A finding with installed < fixed_version produces fixable=true."""
        fixable_findings = [
            Finding(
                image="valkey/valkey:8.0-alpine",
                package="openssl",
                installed_version="3.0.12-r0",
                cve_id="CVE-2024-1234",
                severity=Severity.HIGH,
                fixed_version="3.0.13-r0",
            ),
        ]

        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output_file))
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: fixable_findings,
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )
        # Base pre-check: base has the fix (package at fixed version)
        monkeypatch.setattr(
            "scripts.cve_scan.base_precheck.get_base_packages",
            lambda base_ref, platform="": {"openssl": "3.0.13-r0"},
        )

        settings = load_settings()
        run_sweep(
            settings=settings,
            dry_run=True,
        )

        output = github_output_file.read_text()
        lines = output.strip().splitlines()
        assert "fixable=true" in lines

    def test_multiple_fixable_images_emits_true(
        self,
        monkeypatch: pytest.MonkeyPatch,
        github_output_file: Path,
    ) -> None:
        """Multiple fixable images still just emit fixable=true."""
        findings = [
            Finding(
                image="valkey/valkey:9.0-alpine",
                package="zlib",
                installed_version="1.2.13-r0",
                cve_id="CVE-2024-5678",
                severity=Severity.CRITICAL,
                fixed_version="1.2.14-r0",
            ),
            Finding(
                image="valkey/valkey:7.2-alpine",
                package="openssl",
                installed_version="3.0.10-r0",
                cve_id="CVE-2024-1111",
                severity=Severity.HIGH,
                fixed_version="3.0.11-r0",
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
        monkeypatch.setattr(
            "scripts.cve_scan.base_precheck.get_base_packages",
            lambda base_ref, platform="": {"openssl": "3.0.11-r0", "zlib": "1.2.14-r0"},
        )

        settings = load_settings()
        run_sweep(
            settings=settings,
            dry_run=True,
        )

        output = github_output_file.read_text()
        assert "fixable=true" in output.strip().splitlines()


class TestIntegrationNotFixable:
    """Real load_settings + real _emit_outputs with only non-fixable findings."""

    def test_no_fix_available_emits_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
        github_output_file: Path,
    ) -> None:
        """Findings with fixed_version=None produce fixable=false."""
        not_fixable_findings = [
            Finding(
                image="valkey/valkey:8.0-alpine",
                package="busybox",
                installed_version="1.36.1-r0",
                cve_id="CVE-2024-9999",
                severity=Severity.HIGH,
                fixed_version=None,
            ),
        ]

        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output_file))
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: not_fixable_findings,
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )

        settings = load_settings()
        run_sweep(
            settings=settings,
            dry_run=True,
        )

        output = github_output_file.read_text()
        lines = output.strip().splitlines()
        assert "fixable=false" in lines

    def test_zero_findings_emits_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
        github_output_file: Path,
    ) -> None:
        """Zero findings from scanner produces fixable=false."""
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output_file))
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: [],
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )

        settings = load_settings()
        run_sweep(
            settings=settings,
            dry_run=True,
        )

        output = github_output_file.read_text()
        lines = output.strip().splitlines()
        assert "fixable=false" in lines


class TestIntegrationConfigError:
    """Proves the REAL load path is exercised (not mocked away)."""

    def test_invalid_scanner_raises_cve_scan_config_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CVE_SCAN_SCANNER", "unknown-scanner")
        with pytest.raises(CveScanConfigError, match="Invalid CVE_SCAN_SCANNER"):
            load_settings()

    def test_invalid_severity_raises_cve_scan_config_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CVE_SCAN_SEVERITY_THRESHOLD", "INVALID")
        with pytest.raises(CveScanConfigError, match="Invalid CVE_SCAN_SEVERITY_THRESHOLD"):
            load_settings()


class TestIntegrationDynamic:
    """Dynamic settings with mocked HTTP fetch. Only scan_images and urlopen mocked."""

    def test_dynamic_settings_resolves_and_scans(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Dynamic settings fetches versions.json, resolves images, passes to scanner."""
        github_output = tmp_path / "github_output"
        github_output.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

        # Track what images were passed to scan_images
        scanned_images: list[str] = []

        def mock_scan(images, scanner, threshold, **_kw):
            scanned_images.extend(images)
            return [
                Finding(
                    image="valkey/valkey:8.0-alpine",
                    package="openssl",
                    installed_version="3.0.12-r0",
                    cve_id="CVE-2024-1234",
                    severity=Severity.HIGH,
                    fixed_version="3.0.13-r0",
                ),
            ]

        monkeypatch.setattr("scripts.cve_scan.sweep.scan_images", mock_scan)
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )
        monkeypatch.setattr(
            "scripts.cve_scan.base_precheck.get_base_packages",
            lambda base_ref, platform="": {"openssl": "3.0.13-r0"},
        )

        settings = load_settings()
        run_sweep(
            settings=settings,
            dry_run=True,
        )

        # Verify images were resolved (10 stable: 5 alpine + 5 bare)
        assert len(scanned_images) == 10
        assert "valkey/valkey:7.2-alpine" in scanned_images
        assert "valkey/valkey:9.1" in scanned_images
        # Unstable excluded
        assert "valkey/valkey:unstable-alpine" not in scanned_images
        assert "valkey/valkey:unstable" not in scanned_images

        # Verify outputs emitted
        output = github_output.read_text()
        assert "fixable=true" in output


class TestIntegrationBasePrecheck:
    """Integration: dynamic settings with base pre-check wired into sweep."""

    def test_stale_base_downgrades_fixable_to_not_fixable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Fixable finding whose base is stale -> fixable=false."""
        github_output = tmp_path / "github_output"
        github_output.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

        fixable_finding = Finding(
            image="valkey/valkey:9.1-alpine",
            package="openssl",
            installed_version="3.0.12-r0",
            cve_id="CVE-2024-5555",
            severity=Severity.HIGH,
            fixed_version="3.0.13-r0",
        )
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: [fixable_finding],
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )
        monkeypatch.setattr(
            "scripts.cve_scan.base_precheck.get_base_packages",
            lambda base_ref, platform="": {"openssl": "3.0.12-r0"},
        )
        # Stale base now consults the repo candidate (Item 6); unavailable
        # here keeps the finding downgraded.
        monkeypatch.setattr(
            "scripts.cve_scan.base_precheck.get_repo_candidates",
            lambda base_ref, package, platform="": [],
        )

        settings = load_settings()
        run_sweep(
            settings=settings,
            dry_run=True,
        )

        output = github_output.read_text()
        assert "fixable=false" in output

    def test_stale_base_finding_appears_in_dry_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Downgraded finding appears in dry-run not-fixable output."""
        github_output = tmp_path / "github_output"
        github_output.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

        fixable_finding = Finding(
            image="valkey/valkey:9.1-alpine",
            package="openssl",
            installed_version="3.0.12-r0",
            cve_id="CVE-2024-5555",
            severity=Severity.HIGH,
            fixed_version="3.0.13-r0",
        )
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: [fixable_finding],
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )
        monkeypatch.setattr(
            "scripts.cve_scan.base_precheck.get_base_packages",
            lambda base_ref, platform="": {"openssl": "3.0.12-r0"},
        )
        monkeypatch.setattr(
            "scripts.cve_scan.base_precheck.get_repo_candidates",
            lambda base_ref, package, platform="": [],
        )

        settings = load_settings()
        run_sweep(
            settings=settings,
            dry_run=True,
        )

        captured = capsys.readouterr()
        assert "NOT-FIXABLE" in captured.out
        assert "CVE-2024-5555" in captured.out
        assert "still ships" in captured.out

    def test_confirmed_base_keeps_fixable_true(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Fixable finding confirmed by base pre-check -> fixable=true."""
        github_output = tmp_path / "github_output"
        github_output.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

        fixable_finding = Finding(
            image="valkey/valkey:9.1-alpine",
            package="openssl",
            installed_version="3.0.12-r0",
            cve_id="CVE-2024-5555",
            severity=Severity.HIGH,
            fixed_version="3.0.13-r0",
        )
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: [fixable_finding],
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )
        monkeypatch.setattr(
            "scripts.cve_scan.base_precheck.get_base_packages",
            lambda base_ref, platform="": {"openssl": "3.0.13-r0"},
        )

        settings = load_settings()
        run_sweep(
            settings=settings,
            dry_run=True,
        )

        output = github_output.read_text()
        assert "fixable=true" in output


class TestStaticModeDispatchDisabled:
    """Static mode reclassifies candidates as not-fixable and never dispatches."""

    STATIC_RATIONALE = "static image override: base verification unavailable, not auto-fixable"

    def test_static_mode_fixable_finding_emits_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
        github_output_file: Path,
    ) -> None:
        """Static mode: fixable finding still produces fixable=false."""
        fixable_findings = [
            Finding(
                image="valkey/valkey:8.0-alpine",
                package="openssl",
                installed_version="3.0.12-r0",
                cve_id="CVE-2024-1234",
                severity=Severity.HIGH,
                fixed_version="3.0.13-r0",
            ),
        ]

        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output_file))
        monkeypatch.setenv("CVE_SCAN_IMAGES", "valkey/valkey:8.0-alpine,valkey/valkey:7.2-alpine")
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: fixable_findings,
        )

        settings = load_settings()
        run_sweep(
            settings=settings,
            dry_run=True,
        )

        output = github_output_file.read_text()
        lines = output.strip().splitlines()
        assert "fixable=false" in lines

    def test_static_mode_candidates_reported_as_unresolved(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Static mode: candidates land under unresolved findings with the override rationale; no dispatch section."""
        github_output = tmp_path / "github_output"
        github_output.write_text("")
        summary_file = tmp_path / "step_summary"
        summary_file.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
        monkeypatch.setenv("CVE_SCAN_IMAGES", "valkey/valkey:8.0-alpine")

        fixable_findings = [
            Finding(
                image="valkey/valkey:8.0-alpine",
                package="openssl",
                installed_version="3.0.12-r0",
                cve_id="CVE-2024-1234",
                severity=Severity.HIGH,
                fixed_version="3.0.13-r0",
            ),
        ]
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: fixable_findings,
        )

        settings = load_settings()
        run_sweep(
            settings=settings,
            dry_run=False,
        )

        summary = summary_file.read_text()
        assert "Unresolved findings" in summary
        assert "CVE-2024-1234" in summary
        assert self.STATIC_RATIONALE in summary
        # No dispatch section of any kind in static mode
        assert "dispatched" not in summary
        assert "### Confirmed fixable" not in summary
        assert github_output.read_text().strip().splitlines().count("fixable=false") == 1

    def test_static_mode_dry_run_has_no_dispatch_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        github_output_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Static mode dry-run: candidates print as NOT-FIXABLE, never as would-dispatch."""
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output_file))
        monkeypatch.setenv("CVE_SCAN_IMAGES", "valkey/valkey:8.0-alpine")

        fixable_findings = [
            Finding(
                image="valkey/valkey:8.0-alpine",
                package="openssl",
                installed_version="3.0.12-r0",
                cve_id="CVE-2024-1234",
                severity=Severity.HIGH,
                fixed_version="3.0.13-r0",
            ),
        ]
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: fixable_findings,
        )

        settings = load_settings()
        run_sweep(
            settings=settings,
            dry_run=True,
        )

        captured = capsys.readouterr()
        assert "NOT-FIXABLE" in captured.out
        assert self.STATIC_RATIONALE in captured.out
        assert "DISPATCH BEHAVIOR" not in captured.out
        assert "Would dispatch" not in captured.out


class TestSweepOutputNoEnvVar:
    """When GITHUB_OUTPUT is unset, run_sweep prints and does not raise."""

    def test_no_github_output_env_prints_fixable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

        fixable_findings = [
            Finding(
                image="valkey/valkey:8.0-alpine",
                package="openssl",
                installed_version="3.0.12-r0",
                cve_id="CVE-2024-1234",
                severity=Severity.HIGH,
                fixed_version="3.0.13-r0",
            ),
        ]
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: fixable_findings,
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )
        monkeypatch.setattr(
            "scripts.cve_scan.base_precheck.get_base_packages",
            lambda base_ref, platform="": {"openssl": "3.0.13-r0"},
        )

        settings = load_settings()
        run_sweep(
            settings=settings,
            dry_run=True,
        )

        captured = capsys.readouterr()
        assert "fixable=true" in captured.out

    def test_no_github_output_env_no_findings(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Zero findings + no GITHUB_OUTPUT: prints fixable=false, no exception."""
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        monkeypatch.setattr(
            "scripts.cve_scan.sweep.scan_images",
            lambda images, scanner, threshold, **_kw: [],
        )
        monkeypatch.setattr(
            "scripts.cve_scan.image_matrix.urllib.request.urlopen",
            lambda *a, **kw: _mock_urlopen_response(SAMPLE_VERSIONS),
        )

        settings = load_settings()
        run_sweep(
            settings=settings,
            dry_run=True,
        )

        captured = capsys.readouterr()
        assert "fixable=false" in captured.out


class TestJobSummaryContent:
    """Verify job summary includes findings tables."""

    def test_fixable_findings_appear_in_job_summary(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Fixable findings render in the job summary."""
        github_output = tmp_path / "github_output"
        github_output.write_text("")
        summary_file = tmp_path / "step_summary"
        summary_file.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

        findings = [
            Finding(
                image="valkey/valkey:8.0-alpine",
                package="openssl",
                installed_version="3.0.12-r0",
                cve_id="CVE-2024-1234",
                severity=Severity.HIGH,
                fixed_version="3.0.13-r0",
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
        monkeypatch.setattr(
            "scripts.cve_scan.base_precheck.get_base_packages",
            lambda base_ref, platform="": {"openssl": "3.0.13-r0"},
        )

        settings = load_settings()
        run_sweep(
            settings=settings,
            dry_run=False,
        )

        summary = summary_file.read_text()
        assert "CVE Scan Summary" in summary
        assert "CVE-2024-1234" in summary
        assert "Confirmed fixable (eligible for rebuild:" in summary

    def test_not_fixable_findings_appear_in_job_summary(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Not-fixable findings render in the job summary."""
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

        settings = load_settings()
        run_sweep(
            settings=settings,
            dry_run=False,
        )

        summary = summary_file.read_text()
        assert "CVE Scan Summary" in summary
        assert "CVE-2024-9999" in summary
        assert "Unresolved findings" in summary


class TestScanFailureSummary:
    """A ScanError re-raises but first records a failure section in the summary."""

    def test_scan_error_emits_summary_and_reraises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """scan_images raising ScanError -> failure summary written, then re-raised."""
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

        settings = load_settings()
        with pytest.raises(ScanError, match="valkey/valkey:8.0-alpine"):
            run_sweep(settings=settings, dry_run=False)

        summary = summary_file.read_text()
        assert "Scan failed" in summary
        assert "no rebuild will be dispatched" in summary
        assert "valkey/valkey:8.0-alpine" in summary
