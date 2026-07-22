"""Tests for scripts/parsers/cve_findings_parser.py (Req 1.4/1.5).

Covers parse_trivy, parse_findings dispatcher, and
filter_by_threshold. All use small inline JSON fixtures.
"""

from __future__ import annotations

import pytest

from scripts.cve_scan.models import Finding, Severity
from scripts.parsers.cve_findings_parser import (
    filter_by_threshold,
    parse_findings,
    parse_trivy,
)

IMAGE = "valkey/valkey:7.2"


# ---------------------------------------------------------------------------
# Trivy parser
# ---------------------------------------------------------------------------


class TestParseTrivy:
    def test_basic_finding(self) -> None:
        trivy_json = {
            "Results": [
                {
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-1234",
                            "PkgName": "openssl",
                            "InstalledVersion": "3.0.12-r0",
                            "FixedVersion": "3.0.13-r0",
                            "Severity": "HIGH",
                        }
                    ]
                }
            ]
        }
        findings = parse_trivy(trivy_json, IMAGE)
        assert len(findings) == 1
        f = findings[0]
        assert f.image == IMAGE
        assert f.package == "openssl"
        assert f.installed_version == "3.0.12-r0"
        assert f.cve_id == "CVE-2024-1234"
        assert f.severity == Severity.HIGH
        assert f.fixed_version == "3.0.13-r0"

    def test_missing_fixed_version_becomes_none(self) -> None:
        trivy_json = {
            "Results": [
                {
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-9999",
                            "PkgName": "libcurl",
                            "InstalledVersion": "8.0.0",
                            "Severity": "CRITICAL",
                        }
                    ]
                }
            ]
        }
        findings = parse_trivy(trivy_json, IMAGE)
        assert findings[0].fixed_version is None

    def test_empty_fixed_version_becomes_none(self) -> None:
        trivy_json = {
            "Results": [
                {
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-5555",
                            "PkgName": "zlib",
                            "InstalledVersion": "1.2.13",
                            "FixedVersion": "",
                            "Severity": "MEDIUM",
                        }
                    ]
                }
            ]
        }
        findings = parse_trivy(trivy_json, IMAGE)
        assert findings[0].fixed_version is None

    def test_multiple_results_and_vulns(self) -> None:
        trivy_json = {
            "Results": [
                {
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-0001",
                            "PkgName": "pkg-a",
                            "InstalledVersion": "1.0",
                            "FixedVersion": "1.1",
                            "Severity": "LOW",
                        },
                        {
                            "VulnerabilityID": "CVE-2024-0002",
                            "PkgName": "pkg-b",
                            "InstalledVersion": "2.0",
                            "FixedVersion": "2.1",
                            "Severity": "HIGH",
                        },
                    ]
                },
                {
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-0003",
                            "PkgName": "pkg-c",
                            "InstalledVersion": "3.0",
                            "Severity": "CRITICAL",
                        }
                    ]
                },
            ]
        }
        findings = parse_trivy(trivy_json, IMAGE)
        assert len(findings) == 3
        assert findings[0].cve_id == "CVE-2024-0001"
        assert findings[2].fixed_version is None

    def test_empty_results_returns_empty_list(self) -> None:
        assert parse_trivy({"Results": []}, IMAGE) == []

    def test_missing_results_key_returns_empty_list(self) -> None:
        assert parse_trivy({}, IMAGE) == []

    def test_results_not_a_list_returns_empty(self) -> None:
        assert parse_trivy({"Results": "invalid"}, IMAGE) == []

    def test_vulnerabilities_not_a_list_skipped(self) -> None:
        trivy_json = {"Results": [{"Vulnerabilities": "not-a-list"}]}
        assert parse_trivy(trivy_json, IMAGE) == []

    @pytest.mark.parametrize("sev", ["UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"])
    def test_severity_mapping(self, sev: str) -> None:
        trivy_json = {
            "Results": [
                {
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-0000",
                            "PkgName": "test",
                            "InstalledVersion": "1.0",
                            "FixedVersion": "1.1",
                            "Severity": sev,
                        }
                    ]
                }
            ]
        }
        findings = parse_trivy(trivy_json, IMAGE)
        assert findings[0].severity == Severity[sev]


# ---------------------------------------------------------------------------
# Grype parser
# ---------------------------------------------------------------------------


class TestParseFindings:
    def test_dispatches_trivy(self) -> None:
        trivy_json = {
            "Results": [
                {
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-0001",
                            "PkgName": "pkg",
                            "InstalledVersion": "1.0",
                            "FixedVersion": "1.1",
                            "Severity": "HIGH",
                        }
                    ]
                }
            ]
        }
        findings = parse_findings("trivy", trivy_json, IMAGE)
        assert len(findings) == 1
        assert findings[0].cve_id == "CVE-2024-0001"

    def test_unknown_scanner_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="Unsupported scanner"):
            parse_findings("nessus", {}, IMAGE)

    def test_grype_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="Unsupported scanner"):
            parse_findings("grype", {}, IMAGE)


# ---------------------------------------------------------------------------
# filter_by_threshold
# ---------------------------------------------------------------------------


class TestFilterByThreshold:
    @pytest.fixture
    def mixed_findings(self) -> list[Finding]:
        """Findings spanning all severity levels."""
        return [
            Finding("img", "a", "1.0", "CVE-1", Severity.LOW, "1.1"),
            Finding("img", "b", "1.0", "CVE-2", Severity.MEDIUM, "1.1"),
            Finding("img", "c", "1.0", "CVE-3", Severity.HIGH, "1.1"),
            Finding("img", "d", "1.0", "CVE-4", Severity.CRITICAL, "1.1"),
            Finding("img", "e", "1.0", "CVE-5", Severity.UNKNOWN, None),
        ]

    def test_threshold_high_keeps_high_and_critical(self, mixed_findings) -> None:
        result = filter_by_threshold(mixed_findings, Severity.HIGH)
        assert len(result) == 2
        severities = {f.severity for f in result}
        assert severities == {Severity.HIGH, Severity.CRITICAL}

    def test_threshold_critical_keeps_only_critical(self, mixed_findings) -> None:
        result = filter_by_threshold(mixed_findings, Severity.CRITICAL)
        assert len(result) == 1
        assert result[0].severity == Severity.CRITICAL

    def test_threshold_low_keeps_all_except_unknown(self, mixed_findings) -> None:
        result = filter_by_threshold(mixed_findings, Severity.LOW)
        assert len(result) == 4
        assert all(f.severity >= Severity.LOW for f in result)

    def test_threshold_unknown_keeps_all(self, mixed_findings) -> None:
        result = filter_by_threshold(mixed_findings, Severity.UNKNOWN)
        assert len(result) == 5

    def test_empty_list_returns_empty(self) -> None:
        assert filter_by_threshold([], Severity.HIGH) == []

    def test_below_threshold_excluded(self) -> None:
        low_only = [Finding("img", "pkg", "1.0", "CVE-X", Severity.LOW, "1.1")]
        result = filter_by_threshold(low_only, Severity.HIGH)
        assert result == []
