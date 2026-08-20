"""Behavior tests for the grouped findings table."""

from __future__ import annotations

from scripts.cve_scan.models import Classification, Finding, Severity
from scripts.cve_scan.summary import _strip_repo_prefix, render_findings_table


def _item(
    *,
    cve: str = "CVE-1",
    package: str = "openssl",
    image: str = "valkey/valkey:8.0-alpine",
    severity: Severity = Severity.HIGH,
    rationale: str = "published fix",
    platform: str = "linux/amd64",
) -> Classification:
    return Classification(
        Finding(image, package, "1", cve, severity, "2", platform),
        True,
        rationale,
    )


def _rows(table: str) -> list[str]:
    return [line for line in table.splitlines() if line.startswith("| CVE-")]


def test_repository_prefix_uses_last_tag_separator() -> None:
    assert _strip_repo_prefix("valkey/valkey:8.0-alpine") == "8.0-alpine"
    assert _strip_repo_prefix("registry:5000/repo:tag") == "tag"
    assert _strip_repo_prefix("tag") == "tag"


def test_table_contains_the_public_columns_and_fields() -> None:
    table = render_findings_table([_item()])
    assert "| CVE | Severity | Packages | Installed | Fixed | Images | Platforms | Rationale |" in table
    for value in ("CVE-1", "openssl", "8.0-alpine", "amd64", "published fix"):
        assert value in table
    assert "valkey/valkey:" not in table


def test_same_cve_and_rationale_groups_images_packages_and_platforms() -> None:
    table = render_findings_table(
        [
            _item(package="openssl", image="valkey/valkey:8.0", platform="linux/arm64"),
            _item(package="zlib", image="valkey/valkey:9.1", platform="linux/amd64"),
            _item(package="openssl", image="valkey/valkey:8.0", platform="linux/arm64"),
        ]
    )
    (row,) = _rows(table)
    for value in ("openssl", "zlib", "8.0", "9.1", "amd64, arm64"):
        assert value in row


def test_different_rationales_form_distinct_rows() -> None:
    assert len(_rows(render_findings_table([_item(rationale="no fix"), _item(rationale="base stale")]))) == 2


def test_rows_sort_by_severity_descending() -> None:
    rows = _rows(
        render_findings_table(
            [
                _item(cve="CVE-HIGH", severity=Severity.HIGH),
                _item(cve="CVE-CRITICAL", severity=Severity.CRITICAL),
            ]
        )
    )
    assert "CVE-CRITICAL" in rows[0]
    assert "CVE-HIGH" in rows[1]


def test_missing_platform_uses_dash() -> None:
    assert "| - |" in _rows(render_findings_table([_item(platform="")]))[0]
