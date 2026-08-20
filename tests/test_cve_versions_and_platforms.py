"""Tests for B6 multi-arch scanner and B1 versions output.

B6 (scanner.py): multi-arch scan + dedup
B1 (sweep.py): versions output derivation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.cve_scan.config import CveScanSettings
from scripts.cve_scan.models import Classification, Finding, Severity


def _make_settings(severity: Severity = Severity.HIGH) -> CveScanSettings:
    return CveScanSettings(
        versions_url="https://example.com/versions.json",
        repository="valkey/valkey",
        include_unstable=False,
        scanner="trivy",
        severity_threshold=severity,
        platforms=["linux/amd64", "linux/arm64"],
    )


def _make_classification(
    cve_id: str = "CVE-2024-1234",
    package: str = "openssl",
    image: str = "valkey/valkey:8.0-alpine",
    fixable: bool = False,
) -> Classification:
    return Classification(
        finding=Finding(
            image=image,
            package=package,
            installed_version="3.0.12-r0",
            cve_id=cve_id,
            severity=Severity.HIGH,
            fixed_version="3.0.13-r0",
        ),
        fixable=fixable,
        rationale="Test rationale",
    )


class TestMultiArchScanAndDedup:
    """B6: scanner scans per platform and deduplicates findings."""

    def test_single_platform_no_dedup_needed(self) -> None:
        """Single platform: no dedup needed."""
        from scripts.cve_scan.scanner import scan_images

        finding = Finding(
            image="valkey/valkey:8.0",
            package="openssl",
            installed_version="3.0.12",
            cve_id="CVE-2024-1234",
            severity=Severity.HIGH,
            fixed_version="3.0.13",
            platform="linux/amd64",
        )
        with patch("scripts.cve_scan.scanner.scan_image", return_value=[finding]):
            results = scan_images(
                ["valkey/valkey:8.0"],
                "trivy",
                Severity.HIGH,
                platforms=["linux/amd64"],
            )
        assert len(results) == 1
        assert results[0].cve_id == "CVE-2024-1234"
        assert results[0].platform == "linux/amd64"

    def test_two_platforms_same_finding_kept_distinct(self) -> None:
        """Same finding on two platforms -> kept distinct (per-platform verification)."""
        from scripts.cve_scan.scanner import scan_images

        finding_amd64 = Finding(
            image="valkey/valkey:8.0",
            package="openssl",
            installed_version="3.0.12",
            cve_id="CVE-2024-1234",
            severity=Severity.HIGH,
            fixed_version="3.0.13",
            platform="linux/amd64",
        )
        finding_arm64 = Finding(
            image="valkey/valkey:8.0",
            package="openssl",
            installed_version="3.0.12",
            cve_id="CVE-2024-1234",
            severity=Severity.HIGH,
            fixed_version="3.0.13",
            platform="linux/arm64",
        )

        call_count = [0]

        def mock_scan_image(image, scanner, platform=None):
            call_count[0] += 1
            if platform == "linux/amd64":
                return [finding_amd64]
            elif platform == "linux/arm64":
                return [finding_arm64]
            return []

        with patch("scripts.cve_scan.scanner.scan_image", side_effect=mock_scan_image):
            results = scan_images(
                ["valkey/valkey:8.0"],
                "trivy",
                Severity.HIGH,
                platforms=["linux/amd64", "linux/arm64"],
            )
        # Per-platform findings kept distinct for base verification
        assert len(results) == 2
        platforms_found = {r.platform for r in results}
        assert platforms_found == {"linux/amd64", "linux/arm64"}

    def test_two_platforms_unique_findings_both_kept(self) -> None:
        """Different CVEs on different platforms -> both kept."""
        from scripts.cve_scan.scanner import scan_images

        finding_cve1 = Finding(
            image="valkey/valkey:8.0",
            package="openssl",
            installed_version="3.0.12",
            cve_id="CVE-2024-1111",
            severity=Severity.HIGH,
            fixed_version="3.0.13",
            platform="linux/amd64",
        )
        finding_cve2 = Finding(
            image="valkey/valkey:8.0",
            package="zlib",
            installed_version="1.2.13",
            cve_id="CVE-2024-2222",
            severity=Severity.HIGH,
            fixed_version="1.2.14",
            platform="linux/arm64",
        )
        call_count = [0]

        def mock_scan_image(image, scanner, platform=None):
            call_count[0] += 1
            # amd64 returns cve1, arm64 returns cve2
            if platform == "linux/amd64":
                return [finding_cve1]
            elif platform == "linux/arm64":
                return [finding_cve2]
            return []

        with patch("scripts.cve_scan.scanner.scan_image", side_effect=mock_scan_image):
            results = scan_images(
                ["valkey/valkey:8.0"],
                "trivy",
                Severity.HIGH,
                platforms=["linux/amd64", "linux/arm64"],
            )

        assert len(results) == 2
        assert call_count[0] == 2  # one call per platform

    def test_four_platforms_same_finding_kept_distinct(self) -> None:
        """Same finding across 4 platforms -> four results (per-platform)."""
        from scripts.cve_scan.scanner import scan_images

        def mock_scan_image(image, scanner, platform=None):
            return [Finding(
                image="valkey/valkey:8.0",
                package="openssl",
                installed_version="3.0.12",
                cve_id="CVE-2024-9999",
                severity=Severity.HIGH,
                fixed_version="3.0.13",
                platform=platform or "",
            )]

        with patch("scripts.cve_scan.scanner.scan_image", side_effect=mock_scan_image):
            results = scan_images(
                ["valkey/valkey:8.0"],
                "trivy",
                Severity.HIGH,
                platforms=["linux/amd64", "linux/arm64", "linux/arm/v7", "linux/ppc64le"],
            )
        # Per-platform findings kept distinct
        assert len(results) == 4
        platforms_found = {r.platform for r in results}
        assert platforms_found == {"linux/amd64", "linux/arm64", "linux/arm/v7", "linux/ppc64le"}

    def test_platform_passed_to_trivy_command(self) -> None:
        """Trivy --platform flag is passed for each platform."""
        from scripts.cve_scan.scanner import _build_command

        cmd_amd64 = _build_command("trivy", "valkey/valkey:8.0", "linux/amd64")
        assert "--platform" in cmd_amd64
        assert "linux/amd64" in cmd_amd64

        cmd_arm64 = _build_command("trivy", "valkey/valkey:8.0", "linux/arm64")
        assert "--platform" in cmd_arm64
        assert "linux/arm64" in cmd_arm64

    def test_platform_omitted_when_none(self) -> None:
        """Trivy --platform flag is NOT added when platform is None."""
        from scripts.cve_scan.scanner import _build_command

        cmd = _build_command("trivy", "valkey/valkey:8.0", platform=None)
        assert "--platform" not in cmd

    def test_default_platforms_are_four(self) -> None:
        """Default platform list has exactly 4 entries (verified published set)."""
        from scripts.cve_scan.config import DEFAULT_PLATFORMS

        assert len(DEFAULT_PLATFORMS) == 4
        assert "linux/amd64" in DEFAULT_PLATFORMS
        assert "linux/arm64" in DEFAULT_PLATFORMS
        assert "linux/arm/v7" in DEFAULT_PLATFORMS
        assert "linux/ppc64le" in DEFAULT_PLATFORMS
        assert "linux/386" not in DEFAULT_PLATFORMS

    def test_cve_scan_platforms_env_var(self) -> None:
        """CVE_SCAN_PLATFORMS env var is parsed into platforms list."""
        import os
        from unittest.mock import patch as _patch

        with _patch.dict(os.environ, {
            "CVE_SCAN_PLATFORMS": "linux/amd64,linux/arm64",
        }, clear=False):
            from scripts.cve_scan.config import load_settings
            settings = load_settings()

        assert settings.platforms == ["linux/amd64", "linux/arm64"]

    def test_multiple_images_multiple_platforms(self) -> None:
        """2 images x 2 platforms = 4 scanner invocations."""
        from scripts.cve_scan.scanner import scan_images

        call_args = []

        def mock_scan_image(image, scanner, platform=None):
            call_args.append((image, platform))
            if image == "valkey/valkey:8.0":
                return [Finding(
                    image="valkey/valkey:8.0",
                    package="openssl",
                    installed_version="3.0.12",
                    cve_id="CVE-2024-1234",
                    severity=Severity.HIGH,
                    fixed_version="3.0.13",
                    platform=platform or "",
                )]
            return []

        with patch("scripts.cve_scan.scanner.scan_image", side_effect=mock_scan_image):
            results = scan_images(
                ["valkey/valkey:8.0", "valkey/valkey:9.1"],
                "trivy",
                Severity.HIGH,
                platforms=["linux/amd64", "linux/arm64"],
            )

        # 2 images x 2 platforms = 4 calls
        assert len(call_args) == 4
        assert ("valkey/valkey:8.0", "linux/amd64") in call_args
        assert ("valkey/valkey:8.0", "linux/arm64") in call_args
        assert ("valkey/valkey:9.1", "linux/amd64") in call_args
        assert ("valkey/valkey:9.1", "linux/arm64") in call_args
        # 8.0 has findings on 2 platforms (kept distinct), 9.1 has none
        assert len(results) == 2

    def test_below_threshold_finding_excluded(self) -> None:
        """Findings below threshold are excluded even in multi-arch mode."""
        from scripts.cve_scan.scanner import scan_images

        low_finding = Finding(
            image="valkey/valkey:8.0",
            package="curl",
            installed_version="7.88.0",
            cve_id="CVE-2024-LOW",
            severity=Severity.LOW,
            fixed_version="7.89.0",
            platform="linux/amd64",
        )
        with patch("scripts.cve_scan.scanner.scan_image", return_value=[low_finding]):
            results = scan_images(
                ["valkey/valkey:8.0"],
                "trivy",
                Severity.HIGH,
                platforms=["linux/amd64"],
            )
        assert len(results) == 0

    def test_same_platform_duplicates_collapsed(self) -> None:
        """Exact duplicate findings on same platform are still collapsed."""
        from scripts.cve_scan.scanner import _dedup_findings

        f1 = Finding(
            image="valkey/valkey:8.0",
            package="openssl",
            installed_version="3.0.12",
            cve_id="CVE-2024-1234",
            severity=Severity.HIGH,
            fixed_version="3.0.13",
            platform="linux/amd64",
        )
        f2 = Finding(
            image="valkey/valkey:8.0",
            package="openssl",
            installed_version="3.0.12",
            cve_id="CVE-2024-1234",
            severity=Severity.HIGH,
            fixed_version="3.0.13",
            platform="linux/amd64",
        )
        results = _dedup_findings([f1, f2])
        assert len(results) == 1


class TestVersionsOutput:
    """B1: sweep emits versions output derived from fixable images."""

    def test_fixable_versions_derive_correctly(self) -> None:
        """Image tags are correctly mapped to version lines."""
        from scripts.cve_scan.sweep import _fixable_versions

        fixable = [
            _make_classification(image="valkey/valkey:8.0-alpine", fixable=True),
            _make_classification(image="valkey/valkey:8.0", fixable=True),
            _make_classification(image="valkey/valkey:9.1-alpine", fixable=True),
        ]
        versions = _fixable_versions(fixable)
        assert versions == ["8.0", "9.1"]

    def test_versions_deduplicated_and_sorted(self) -> None:
        """Duplicate version lines from multiple images are deduplicated."""
        from scripts.cve_scan.sweep import _fixable_versions

        fixable = [
            _make_classification(image="valkey/valkey:8.0-alpine", fixable=True),
            _make_classification(image="valkey/valkey:8.0-alpine", fixable=True),  # dup
            _make_classification(image="valkey/valkey:7.2", fixable=True),
        ]
        versions = _fixable_versions(fixable)
        assert versions == ["7.2", "8.0"]

    def test_empty_fixable_returns_empty(self) -> None:
        """Empty fixable list -> empty versions list."""
        from scripts.cve_scan.sweep import _fixable_versions

        assert _fixable_versions([]) == []

    def test_versions_output_written_to_github_output(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """versions= is written alongside fixable= to GITHUB_OUTPUT."""
        from scripts.cve_scan.sweep import _emit_outputs

        output_file = tmp_path / "github_output"
        output_file.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

        _emit_outputs(True, versions=["8.0", "9.1"])

        content = output_file.read_text()
        assert "fixable=true" in content
        assert "versions=8.0 9.1" in content
