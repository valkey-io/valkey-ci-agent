"""Focused tests for multi-architecture scanning and deduplication."""

from __future__ import annotations

from unittest.mock import patch

from scripts.cve_scan.config import DEFAULT_PLATFORMS
from scripts.cve_scan.models import Finding, Severity
from scripts.cve_scan.scanner import _build_command, _dedup_findings, scan_images


def _finding(platform: str, *, severity: Severity = Severity.HIGH) -> Finding:
    return Finding(
        image="valkey/valkey:8.0",
        package="openssl",
        installed_version="1",
        cve_id="CVE-1",
        severity=severity,
        fixed_version="2",
        platform=platform,
    )


def test_trivy_command_scopes_scan_to_platform() -> None:
    command = _build_command("valkey/valkey:8.0", "linux/arm64")
    assert command[-3:] == ["--platform", "linux/arm64", "valkey/valkey:8.0"]
    assert "--platform" not in _build_command("image")
    assert _build_command("image", trivy_bin="custom-trivy")[0] == "custom-trivy"


def test_all_published_platforms_are_scanned_and_preserved() -> None:
    calls: list[str] = []

    def scan(_image: str, platform: str | None = None) -> list[Finding]:
        assert platform
        calls.append(platform)
        return [_finding(platform)]

    with patch("scripts.cve_scan.scanner.scan_image", side_effect=scan):
        findings = scan_images(["valkey/valkey:8.0"], Severity.HIGH)
    assert calls == DEFAULT_PLATFORMS
    assert {finding.platform for finding in findings} == set(DEFAULT_PLATFORMS)
    assert "linux/386" not in calls


def test_same_platform_duplicates_collapse_but_other_architectures_remain() -> None:
    amd64 = _finding("linux/amd64")
    arm64 = _finding("linux/arm64")
    assert _dedup_findings([amd64, amd64, arm64]) == [amd64, arm64]


def test_threshold_filtering_is_applied_after_multi_arch_scan() -> None:
    with patch(
        "scripts.cve_scan.scanner.scan_image",
        return_value=[_finding("linux/amd64", severity=Severity.LOW)],
    ):
        assert (
            scan_images(
                ["valkey/valkey:8.0"],
                Severity.HIGH,
                platforms=["linux/amd64"],
            )
            == []
        )
