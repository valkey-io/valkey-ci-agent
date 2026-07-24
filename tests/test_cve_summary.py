"""Tests for scripts/cve_scan/summary.py -- grouped findings table renderer."""

from __future__ import annotations

from scripts.cve_scan.models import Classification, Finding, Severity
from scripts.cve_scan.summary import _strip_repo_prefix, render_findings_table


def _make_classification(
    cve_id: str = "CVE-2024-1234",
    package: str = "openssl",
    image: str = "valkey/valkey:8.0-alpine",
    fixed_version: str | None = None,
    fixable: bool = False,
    rationale: str = "Test rationale",
) -> Classification:
    """Build a Classification with sensible defaults."""
    return Classification(
        finding=Finding(
            image=image,
            package=package,
            installed_version="3.0.12-r0",
            cve_id=cve_id,
            severity=Severity.HIGH,
            fixed_version=fixed_version,
        ),
        fixable=fixable,
        rationale=rationale,
    )


class TestStripRepoPrefix:
    """Unit tests for _strip_repo_prefix."""

    def test_strips_prefix(self) -> None:
        assert _strip_repo_prefix("valkey/valkey:8.0-alpine") == "8.0-alpine"

    def test_no_colon_returns_as_is(self) -> None:
        assert _strip_repo_prefix("nocolon") == "nocolon"

    def test_multiple_colons_strips_after_last(self) -> None:
        assert _strip_repo_prefix("registry.io:5000/repo:tag") == "tag"


class TestRenderFindingsTable:
    """render_findings_table outputs correct grouped markdown table."""

    def test_basic_table_structure(self) -> None:
        classifications = [
            _make_classification(cve_id="CVE-2024-9999", package="busybox"),
        ]
        table = render_findings_table(classifications)
        assert "### Findings" in table
        assert "CVE-2024-9999" in table
        assert "busybox" in table
        assert "| CVE | Severity | Packages |" in table

    def test_multiple_images_grouped_into_single_row(self) -> None:
        """2 images sharing 1 CVE+severity+rationale -> 1 row with both tags."""
        classifications = [
            _make_classification(
                cve_id="CVE-2024-1234",
                package="openssl",
                image="valkey/valkey:8.0-alpine",
            ),
            _make_classification(
                cve_id="CVE-2024-1234",
                package="openssl",
                image="valkey/valkey:9.1-alpine",
            ),
        ]
        table = render_findings_table(classifications)
        # Exactly one data row (header + separator + 1 row + trailing blank)
        data_rows = [
            line for line in table.splitlines()
            if line.startswith("| CVE-")
        ]
        assert len(data_rows) == 1
        assert "8.0-alpine" in data_rows[0]
        assert "9.1-alpine" in data_rows[0]

    def test_grouping_multiple_packages_same_cve(self) -> None:
        """2 images x 2 packages sharing one CVE -> ONE data row with both packages and both tags."""
        classifications = [
            _make_classification(cve_id="CVE-2024-1000", package="zlib", image="valkey/valkey:8.0"),
            _make_classification(cve_id="CVE-2024-1000", package="openssl", image="valkey/valkey:8.0"),
            _make_classification(cve_id="CVE-2024-1000", package="zlib", image="valkey/valkey:9.1"),
            _make_classification(cve_id="CVE-2024-1000", package="openssl", image="valkey/valkey:9.1"),
        ]
        table = render_findings_table(classifications)
        data_rows = [line for line in table.splitlines() if line.startswith("| CVE-")]
        assert len(data_rows) == 1
        row = data_rows[0]
        assert "openssl" in row
        assert "zlib" in row
        assert "8.0" in row
        assert "9.1" in row

    def test_mixed_rationale_same_cve_produces_two_rows(self) -> None:
        """Same CVE with different rationale -> two data rows."""
        c1 = Classification(
            finding=Finding(
                image="valkey/valkey:8.0",
                package="openssl",
                installed_version="3.0.12",
                cve_id="CVE-2024-5000",
                severity=Severity.HIGH,
                fixed_version=None,
            ),
            fixable=False,
            rationale="No upstream fix yet.",
        )
        c2 = Classification(
            finding=Finding(
                image="valkey/valkey:9.1",
                package="openssl",
                installed_version="3.0.12",
                cve_id="CVE-2024-5000",
                severity=Severity.HIGH,
                fixed_version="3.0.13",
            ),
            fixable=False,
            rationale="Base image still ships old version.",
        )
        table = render_findings_table([c1, c2])
        data_rows = [line for line in table.splitlines() if line.startswith("| CVE-")]
        assert len(data_rows) == 2

    def test_repo_prefix_stripped_from_images(self) -> None:
        """Image tags have repo prefix stripped (e.g. 'valkey/valkey:8.0' -> '8.0')."""
        classifications = [
            _make_classification(image="valkey/valkey:8.0-alpine"),
        ]
        table = render_findings_table(classifications)
        assert "8.0-alpine" in table
        assert "valkey/valkey:" not in table

    def test_no_affected_images_section(self) -> None:
        """The old '### Affected Images' section is removed."""
        classifications = [
            _make_classification(cve_id="CVE-2024-9999", package="busybox"),
        ]
        table = render_findings_table(classifications)
        assert "### Affected Images" not in table

    def test_severity_sort_descending(self) -> None:
        """Higher severity rows appear first."""
        c_high = Classification(
            finding=Finding(
                image="valkey/valkey:8.0",
                package="openssl",
                installed_version="3.0.12",
                cve_id="CVE-2024-2000",
                severity=Severity.HIGH,
                fixed_version=None,
            ),
            fixable=False,
            rationale="No fix.",
        )
        c_crit = Classification(
            finding=Finding(
                image="valkey/valkey:8.0",
                package="zlib",
                installed_version="1.2.13",
                cve_id="CVE-2024-1000",
                severity=Severity.CRITICAL,
                fixed_version=None,
            ),
            fixable=False,
            rationale="No fix.",
        )
        table = render_findings_table([c_high, c_crit])
        data_rows = [line for line in table.splitlines() if line.startswith("| CVE-")]
        assert len(data_rows) == 2
        # CRITICAL first
        assert "CVE-2024-1000" in data_rows[0]
        assert "CVE-2024-2000" in data_rows[1]

    def test_no_urgency_column_when_map_is_none(self) -> None:
        """No 'Distro severity' column appears in the table."""
        classifications = [
            _make_classification(cve_id="CVE-2024-9999", package="busybox"),
        ]
        table = render_findings_table(classifications)
        assert "Distro severity" not in table

    def test_inline_render_2_cves_5_images_produces_2_rows(self) -> None:
        """Verify: 10 classifications (2 CVEs x 5 images) -> exactly 2 data rows."""
        images = [
            "valkey/valkey:7.2-alpine",
            "valkey/valkey:8.0-alpine",
            "valkey/valkey:8.1-alpine",
            "valkey/valkey:9.0-alpine",
            "valkey/valkey:9.1-alpine",
        ]
        classifications = []
        for img in images:
            classifications.append(
                _make_classification(
                    cve_id="CVE-2024-1111",
                    package="openssl",
                    image=img,
                    rationale="No upstream fix yet.",
                )
            )
            classifications.append(
                _make_classification(
                    cve_id="CVE-2024-2222",
                    package="zlib",
                    image=img,
                    rationale="Base stale.",
                )
            )
        table = render_findings_table(classifications)
        data_rows = [line for line in table.splitlines() if line.startswith("| CVE-")]
        assert len(data_rows) == 2
