"""Tests for scripts/cve_scan/rebuild_decider.py.

Two-rule contract: no fixed_version -> not fixable; fixed_version present ->
candidate fixable, pending base pre-check verification. Version ordering
semantics live only in version_compare.py (native dpkg/apk), which has its
own tests.
"""

from __future__ import annotations

from scripts.cve_scan.models import Classification, Finding, Severity
from scripts.cve_scan.rebuild_decider import classify, classify_all


def _make_finding(
    installed: str = "1.0.0",
    fixed: str | None = "1.0.1",
    cve_id: str = "CVE-2024-0001",
    package: str = "openssl",
) -> Finding:
    """Helper to create a Finding with sensible defaults."""
    return Finding(
        image="valkey/valkey:7.2",
        package=package,
        installed_version=installed,
        cve_id=cve_id,
        severity=Severity.HIGH,
        fixed_version=fixed,
    )


class TestNoFixAvailable:
    def test_none_fixed_version_not_fixable(self) -> None:
        result = classify(_make_finding(fixed=None))
        assert result.fixable is False

    def test_empty_fixed_version_not_fixable(self) -> None:
        result = classify(_make_finding(fixed=""))
        assert result.fixable is False

    def test_rationale_mentions_no_upstream_fix(self) -> None:
        result = classify(_make_finding(fixed=None))
        assert "no upstream fix" in result.rationale.lower()

    def test_result_preserves_finding(self) -> None:
        finding = _make_finding(fixed=None)
        result = classify(finding)
        assert result.finding is finding


class TestCandidateFixable:
    def test_fix_present_is_candidate(self) -> None:
        result = classify(_make_finding(installed="1.0.0", fixed="1.0.1"))
        assert result.fixable is True

    def test_rationale_mentions_pending_base_verification(self) -> None:
        result = classify(_make_finding())
        assert "pending base verification" in result.rationale

    def test_rationale_mentions_versions(self) -> None:
        result = classify(_make_finding(installed="3.0.12-r0", fixed="3.0.13-r0"))
        assert "3.0.13-r0" in result.rationale
        assert "3.0.12-r0" in result.rationale

    def test_candidacy_does_not_compare_versions(self) -> None:
        """Trivy's matching is trusted: even installed >= fixed is a candidate here."""
        result = classify(_make_finding(installed="2.0.0", fixed="1.9.9"))
        assert result.fixable is True

    def test_result_preserves_finding(self) -> None:
        finding = _make_finding()
        result = classify(finding)
        assert result.finding is finding


class TestClassifyAll:
    def test_returns_one_per_finding(self) -> None:
        findings = [
            _make_finding(installed="1.0", fixed="2.0"),
            _make_finding(installed="3.0", fixed=None),
        ]
        results = classify_all(findings)
        assert len(results) == 2
        assert all(isinstance(r, Classification) for r in results)
        assert results[0].fixable is True
        assert results[1].fixable is False

    def test_preserves_order(self) -> None:
        findings = [
            _make_finding(cve_id="CVE-A", installed="1.0", fixed="2.0"),
            _make_finding(cve_id="CVE-B", installed="2.0", fixed=None),
        ]
        results = classify_all(findings)
        assert results[0].finding.cve_id == "CVE-A"
        assert results[1].finding.cve_id == "CVE-B"

    def test_empty_input_returns_empty(self) -> None:
        assert classify_all([]) == []
