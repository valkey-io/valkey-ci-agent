"""Strict Trivy parsing and threshold behavior."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts.cve_scan.models import Finding, Severity
from scripts.cve_scan.scanner import ScanError, scan_image
from scripts.parsers.cve_findings_parser import (
    ParseError,
    filter_by_threshold,
    parse_trivy,
)

_IMAGE = "valkey/valkey:8.0"


def _vulnerability(**changes: object) -> dict:
    value = {
        "VulnerabilityID": "CVE-1",
        "PkgName": "openssl",
        "InstalledVersion": "1",
        "FixedVersion": "2",
        "Severity": "HIGH",
    }
    value.update(changes)
    return value


def _document(*vulnerabilities: object) -> dict:
    return {
        "SchemaVersion": 2,
        "Results": [{"Vulnerabilities": list(vulnerabilities)}],
    }


def test_parses_complete_finding_and_platform() -> None:
    (finding,) = parse_trivy(_document(_vulnerability()), _IMAGE, platform="linux/arm64")
    assert finding == Finding(
        _IMAGE,
        "openssl",
        "1",
        "CVE-1",
        Severity.HIGH,
        "2",
        "linux/arm64",
    )


@pytest.mark.parametrize("fixed", [None, ""])
def test_missing_or_empty_fixed_version_becomes_none(fixed: object) -> None:
    vuln = _vulnerability(FixedVersion=fixed)
    if fixed is None:
        vuln.pop("FixedVersion")
    assert parse_trivy(_document(vuln), _IMAGE)[0].fixed_version is None


def test_clean_and_multi_result_documents() -> None:
    assert parse_trivy({"SchemaVersion": 2}, _IMAGE) == []
    assert parse_trivy({"SchemaVersion": 2, "Results": []}, _IMAGE) == []
    assert (
        parse_trivy(
            {
                "SchemaVersion": 2,
                "Results": [{}, {"Vulnerabilities": None}],
            },
            _IMAGE,
        )
        == []
    )
    document = {
        "SchemaVersion": 2,
        "Results": [
            {"Vulnerabilities": [_vulnerability(VulnerabilityID="CVE-1")]},
            {"Vulnerabilities": [_vulnerability(VulnerabilityID="CVE-2")]},
        ],
    }
    assert [item.cve_id for item in parse_trivy(document, _IMAGE)] == [
        "CVE-1",
        "CVE-2",
    ]


@pytest.mark.parametrize("severity", list(Severity))
def test_all_severities_map(severity: Severity) -> None:
    finding = parse_trivy(_document(_vulnerability(Severity=severity.name)), _IMAGE)[0]
    assert finding.severity is severity


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ([], "top-level"),
        ({}, "SchemaVersion"),
        ({"SchemaVersion": True}, "SchemaVersion"),
        ({"SchemaVersion": "2"}, "SchemaVersion"),
        ({"SchemaVersion": 2, "Results": "bad"}, "Results"),
        ({"SchemaVersion": 2, "Results": ["bad"]}, "must be an object"),
        (
            {"SchemaVersion": 2, "Results": [{"Vulnerabilities": "bad"}]},
            "must be a list",
        ),
        (_document("bad"), "must be an object"),
    ],
)
def test_malformed_structure_fails_closed(document: object, message: str) -> None:
    with pytest.raises(ParseError, match=message):
        parse_trivy(document, _IMAGE)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("VulnerabilityID", None),
        ("PkgName", None),
        ("PkgName", 1),
        ("InstalledVersion", None),
        ("Severity", None),
    ],
)
def test_required_vulnerability_fields_are_strings(key: str, value: object) -> None:
    vuln = _vulnerability()
    if value is None:
        vuln.pop(key)
    else:
        vuln[key] = value
    with pytest.raises(ParseError, match=key):
        parse_trivy(_document(vuln), _IMAGE)


@pytest.mark.parametrize(
    "vulnerability",
    [_vulnerability(FixedVersion=2), _vulnerability(Severity="BOGUS")],
)
def test_invalid_optional_version_or_severity_fails(
    vulnerability: dict,
) -> None:
    with pytest.raises(ParseError):
        parse_trivy(_document(vulnerability), _IMAGE)


def test_scan_image_wraps_schema_error() -> None:
    with patch(
        "scripts.cve_scan.scanner._run_scanner",
        return_value={"SchemaVersion": 2, "Results": "bad"},
    ):
        with pytest.raises(ScanError, match="schema validation") as error:
            scan_image(_IMAGE)
    assert isinstance(error.value.__cause__, ParseError)


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [
        (Severity.UNKNOWN, list(Severity)),
        (Severity.LOW, [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]),
        (Severity.HIGH, [Severity.HIGH, Severity.CRITICAL]),
        (Severity.CRITICAL, [Severity.CRITICAL]),
    ],
)
def test_threshold_is_inclusive(threshold: Severity, expected: list[Severity]) -> None:
    findings = [Finding("image", "pkg", "1", f"CVE-{severity.name}", severity, None) for severity in Severity]
    assert [item.severity for item in filter_by_threshold(findings, threshold)] == expected
