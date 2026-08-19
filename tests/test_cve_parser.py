"""Tests for scripts/parsers/cve_findings_parser.py (Req 1.4/1.5).

Covers parse_trivy (including strict schema validation via ParseError),
parse_findings dispatcher, filter_by_threshold, and the scan_image
ParseError-to-ScanError wrapping. All use small inline JSON fixtures.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts.cve_scan.models import Finding, Severity
from scripts.cve_scan.scanner import ScanError, scan_image
from scripts.parsers.cve_findings_parser import (
    ParseError,
    filter_by_threshold,
    parse_findings,
    parse_trivy,
)

IMAGE = "valkey/valkey:7.2"


class TestParseTrivy:
    def test_basic_finding(self) -> None:
        trivy_json = {
            "SchemaVersion": 2,
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
            "SchemaVersion": 2,
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
            "SchemaVersion": 2,
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
            "SchemaVersion": 2,
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
        assert parse_trivy({"SchemaVersion": 2, "Results": []}, IMAGE) == []

    def test_missing_results_key_is_valid_clean_scan(self) -> None:
        """Trivy omits 'Results' on a clean scan: valid, returns []."""
        assert parse_trivy({"SchemaVersion": 2}, IMAGE) == []

    def test_vulnerabilities_absent_or_none_is_valid(self) -> None:
        """A result target without 'Vulnerabilities' (or null) is valid."""
        trivy_json = {
            "SchemaVersion": 2,
            "Results": [{"Target": "img"}, {"Vulnerabilities": None}],
        }
        assert parse_trivy(trivy_json, IMAGE) == []

    @pytest.mark.parametrize("sev", ["UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"])
    def test_severity_mapping(self, sev: str) -> None:
        trivy_json = {
            "SchemaVersion": 2,
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


class TestParseTrivyStrictValidation:
    """Malformed Trivy output raises ParseError instead of a silent all-clear."""

    def test_top_level_not_a_dict_raises(self) -> None:
        with pytest.raises(ParseError, match="top-level must be a JSON object"):
            parse_trivy([], IMAGE)  # type: ignore[arg-type]

    def test_missing_schema_version_raises(self) -> None:
        with pytest.raises(ParseError, match="SchemaVersion"):
            parse_trivy({}, IMAGE)

    def test_non_integer_schema_version_raises(self) -> None:
        with pytest.raises(ParseError, match="SchemaVersion"):
            parse_trivy({"SchemaVersion": "2"}, IMAGE)

    def test_boolean_schema_version_raises(self) -> None:
        with pytest.raises(ParseError, match="SchemaVersion"):
            parse_trivy({"SchemaVersion": True}, IMAGE)

    def test_results_not_a_list_raises(self) -> None:
        with pytest.raises(ParseError, match="'Results' must be a list"):
            parse_trivy({"SchemaVersion": 2, "Results": "invalid"}, IMAGE)

    def test_result_entry_not_a_dict_raises(self) -> None:
        with pytest.raises(ParseError, match=r"Results\[0\] must be an object"):
            parse_trivy({"SchemaVersion": 2, "Results": ["not-a-dict"]}, IMAGE)

    def test_vulnerabilities_not_a_list_raises(self) -> None:
        trivy_json = {"SchemaVersion": 2, "Results": [{"Vulnerabilities": "not-a-list"}]}
        with pytest.raises(ParseError, match=r"Results\[0\].Vulnerabilities\s+must be a list"):
            parse_trivy(trivy_json, IMAGE)

    def test_vulnerability_entry_not_a_dict_raises(self) -> None:
        trivy_json = {"SchemaVersion": 2, "Results": [{"Vulnerabilities": ["CVE-as-string"]}]}
        with pytest.raises(ParseError, match=r"Results\[0\].Vulnerabilities\[0\] must be an object"):
            parse_trivy(trivy_json, IMAGE)

    def test_vulnerability_missing_pkgname_raises(self) -> None:
        trivy_json = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-1234",
                            "InstalledVersion": "3.0.12-r0",
                            "Severity": "HIGH",
                        }
                    ]
                }
            ],
        }
        with pytest.raises(ParseError, match=r"Results\[0\].Vulnerabilities\[0\].*'PkgName'"):
            parse_trivy(trivy_json, IMAGE)

    def test_vulnerability_mistyped_required_key_raises(self) -> None:
        trivy_json = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-1234",
                            "PkgName": 12345,
                            "InstalledVersion": "3.0.12-r0",
                            "Severity": "HIGH",
                        }
                    ]
                }
            ],
        }
        with pytest.raises(ParseError, match="'PkgName'"):
            parse_trivy(trivy_json, IMAGE)

    def test_non_string_fixed_version_raises(self) -> None:
        """A non-string FixedVersion (e.g. 123) is rejected before it can reach subprocess args."""
        trivy_json = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-1234",
                            "PkgName": "openssl",
                            "InstalledVersion": "3.0.12-r0",
                            "FixedVersion": 123,
                            "Severity": "HIGH",
                        }
                    ]
                }
            ],
        }
        with pytest.raises(ParseError, match=r"Results\[0\].Vulnerabilities\[0\].*FixedVersion"):
            parse_trivy(trivy_json, IMAGE)

    def test_unknown_severity_raises_parse_error(self) -> None:
        trivy_json = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-1234",
                            "PkgName": "openssl",
                            "InstalledVersion": "3.0.12-r0",
                            "Severity": "BOGUS",
                        }
                    ]
                }
            ],
        }
        with pytest.raises(ParseError, match="Unknown severity"):
            parse_trivy(trivy_json, IMAGE)


class TestScanImageWrapsParseError:
    """scan_image surfaces parser schema violations as ScanError (fail loudly)."""

    def test_malformed_output_raises_scan_error(self) -> None:
        with patch(
            "scripts.cve_scan.scanner._run_scanner",
            return_value={"SchemaVersion": 2, "Results": "invalid"},
        ):
            with pytest.raises(ScanError, match="schema validation") as exc_info:
                scan_image("valkey/valkey:8.0-alpine", "trivy")
        assert isinstance(exc_info.value.__cause__, ParseError)

    def test_missing_schema_version_raises_scan_error(self) -> None:
        with patch("scripts.cve_scan.scanner._run_scanner", return_value={}):
            with pytest.raises(ScanError, match="SchemaVersion"):
                scan_image("valkey/valkey:8.0-alpine", "trivy")


class TestParseFindings:
    def test_dispatches_trivy(self) -> None:
        trivy_json = {
            "SchemaVersion": 2,
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

    def test_platform_stamped_on_findings(self) -> None:
        """Platform argument is stamped on each Finding."""
        trivy_json = {
            "SchemaVersion": 2,
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
        findings = parse_findings("trivy", trivy_json, IMAGE, platform="linux/arm64")
        assert len(findings) == 1
        assert findings[0].platform == "linux/arm64"

    def test_platform_defaults_to_empty(self) -> None:
        """Platform defaults to empty string when not provided."""
        trivy_json = {
            "SchemaVersion": 2,
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
        assert findings[0].platform == ""


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
