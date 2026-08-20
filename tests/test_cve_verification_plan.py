"""Tests for the scan's single verification-plan contract."""

from __future__ import annotations

import json

from scripts.cve_scan.models import Classification, Finding, Severity
from scripts.cve_scan.sweep import _line_variant, _verification_plan


def _candidate(
    *,
    image: str = "valkey/valkey:8.0-alpine",
    platform: str = "linux/amd64",
    cve: str = "CVE-2026-1234",
    package: str = "openssl",
) -> Classification:
    return Classification(
        finding=Finding(
            image=image,
            package=package,
            installed_version="1",
            cve_id=cve,
            severity=Severity.HIGH,
            fixed_version="2",
            platform=platform,
        ),
        fixable=True,
        rationale="published fix",
    )


def test_line_variant_uses_only_trailing_alpine_suffix() -> None:
    assert _line_variant("valkey/valkey:8.0") == ("8.0", "debian")
    assert _line_variant("valkey/valkey:8.0-alpine") == ("8.0", "alpine")
    assert _line_variant("valkey/valkey:alpine-test") == ("alpine-test", "debian")


def test_plan_groups_each_architecture_and_deduplicates_cves() -> None:
    plan = json.loads(
        _verification_plan(
            [
                _candidate(cve="CVE-1"),
                _candidate(cve="CVE-1", package="renamed-openssl"),
                _candidate(cve="CVE-2"),
                _candidate(platform="linux/arm64", cve="CVE-1"),
            ]
        )
    )
    assert plan == [
        {
            "line": "8.0",
            "variant": "alpine",
            "platform": "linux/amd64",
            "cves": ["CVE-1", "CVE-2"],
        },
        {
            "line": "8.0",
            "variant": "alpine",
            "platform": "linux/arm64",
            "cves": ["CVE-1"],
        },
    ]


def test_empty_candidates_produce_empty_matrix_json() -> None:
    assert _verification_plan([]) == "[]"
