"""Tests for scripts/cve_scan/rebuild_decider.py (Req 2).

Verifies fixability classification logic:
- fixable: installed < fixed (rebuild would pick up the fix)
- not fixable: no fixed_version available
- already fixed: installed >= fixed_version (stale/resolved finding)
"""

from __future__ import annotations

import pytest

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


# ---------------------------------------------------------------------------
# Fixable: installed < fixed
# ---------------------------------------------------------------------------


class TestFixable:
    def test_simple_version_bump(self) -> None:
        result = classify(_make_finding(installed="1.0.0", fixed="1.0.1"))
        assert result.fixable is True
        assert "1.0.0" in result.rationale
        assert "1.0.1" in result.rationale

    def test_major_version_upgrade(self) -> None:
        result = classify(_make_finding(installed="2.0.0", fixed="3.0.0"))
        assert result.fixable is True

    def test_alpine_revision_bump(self) -> None:
        result = classify(_make_finding(installed="3.0.12-r0", fixed="3.0.12-r1"))
        assert result.fixable is True

    def test_rationale_mentions_cve_id(self) -> None:
        result = classify(_make_finding(cve_id="CVE-2024-9999"))
        assert "CVE-2024-9999" in result.rationale

    def test_rationale_mentions_package(self) -> None:
        result = classify(_make_finding(package="libcurl"))
        assert "libcurl" in result.rationale

    def test_rationale_mentions_upgrade_action(self) -> None:
        result = classify(_make_finding(installed="1.0", fixed="2.0"))
        assert "upgrade" in result.rationale.lower() or "rebuild" in result.rationale.lower()

    def test_result_preserves_finding(self) -> None:
        finding = _make_finding()
        result = classify(finding)
        assert result.finding is finding


# ---------------------------------------------------------------------------
# Not fixable: no fixed_version
# ---------------------------------------------------------------------------


class TestNoFixAvailable:
    def test_none_fixed_version(self) -> None:
        result = classify(_make_finding(fixed=None))
        assert result.fixable is False

    def test_rationale_mentions_no_upstream_fix(self) -> None:
        result = classify(_make_finding(fixed=None, cve_id="CVE-2024-7777"))
        assert "no upstream fix" in result.rationale.lower() or "not" in result.rationale.lower()


# ---------------------------------------------------------------------------
# Already fixed: installed >= fixed
# ---------------------------------------------------------------------------


class TestAlreadyFixed:
    def test_installed_equals_fixed(self) -> None:
        result = classify(_make_finding(installed="1.0.1", fixed="1.0.1"))
        assert result.fixable is False

    def test_installed_greater_than_fixed(self) -> None:
        result = classify(_make_finding(installed="2.0.0", fixed="1.9.9"))
        assert result.fixable is False

    def test_alpine_revision_already_patched(self) -> None:
        result = classify(_make_finding(installed="3.0.12-r2", fixed="3.0.12-r1"))
        assert result.fixable is False

    def test_rationale_mentions_already_at_or_above(self) -> None:
        result = classify(_make_finding(installed="2.0.0", fixed="1.0.0"))
        assert "already" in result.rationale.lower() or "stale" in result.rationale.lower()


# ---------------------------------------------------------------------------
# classify_all batch
# ---------------------------------------------------------------------------


class TestClassifyAll:
    def test_returns_one_per_finding(self) -> None:
        findings = [
            _make_finding(installed="1.0", fixed="2.0"),
            _make_finding(installed="3.0", fixed=None),
            _make_finding(installed="5.0", fixed="4.0"),
        ]
        results = classify_all(findings)
        assert len(results) == 3
        assert all(isinstance(r, Classification) for r in results)

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


# ---------------------------------------------------------------------------
# Edge cases in version comparison
# ---------------------------------------------------------------------------


class TestVersionComparison:
    @pytest.mark.parametrize(
        "installed,fixed,expected_fixable",
        [
            ("1.2.3", "1.2.4", True),
            ("1.2.10", "1.2.9", False),
            ("1.2.3-r0", "1.2.3-r1", True),
            ("0.9.99", "1.0.0", True),
            ("10.0.0", "9.99.99", False),
            ("1.0", "1.0.1", True),  # shorter version = older
        ],
    )
    def test_version_comparison_cases(
        self, installed: str, fixed: str, expected_fixable: bool
    ) -> None:
        result = classify(_make_finding(installed=installed, fixed=fixed))
        assert result.fixable is expected_fixable


# ---------------------------------------------------------------------------
# Tilde ordering (Debian policy: ~ sorts before everything)
# ---------------------------------------------------------------------------


class TestTildeOrdering:
    """Tilde (~) sorts before release: 1.0~rc1 < 1.0."""

    def test_tilde_prerelease_is_older(self) -> None:
        """installed=1.0~rc1 vs fixed=1.0: tilde is older -> fixable."""
        result = classify(_make_finding(installed="1.0~rc1", fixed="1.0"))
        assert result.fixable is True

    def test_release_is_newer_than_tilde(self) -> None:
        """installed=1.0 vs fixed=1.0~rc1: release is newer -> not fixable."""
        result = classify(_make_finding(installed="1.0", fixed="1.0~rc1"))
        assert result.fixable is False

    def test_tilde_both_sides_compared_correctly(self) -> None:
        """installed=1.0~alpha vs fixed=1.0~beta: alpha < beta -> fixable."""
        result = classify(_make_finding(installed="1.0~alpha", fixed="1.0~beta"))
        assert result.fixable is True

    def test_tilde_beta_vs_alpha(self) -> None:
        """installed=1.0~beta vs fixed=1.0~alpha: beta > alpha -> not fixable."""
        result = classify(_make_finding(installed="1.0~beta", fixed="1.0~alpha"))
        assert result.fixable is False


# ---------------------------------------------------------------------------
# Epoch handling (Debian N:version)
# ---------------------------------------------------------------------------


class TestEpochHandling:
    """Epoch prefix is compared numerically before the version body."""

    def test_higher_epoch_installed_not_fixable(self) -> None:
        """installed=1:1.0.0 vs fixed=2.0.0 (epoch 0): epoch 1 > 0 -> not fixable."""
        result = classify(_make_finding(installed="1:1.0.0", fixed="2.0.0"))
        assert result.fixable is False

    def test_lower_epoch_installed_fixable(self) -> None:
        """installed=1.0.0 (epoch 0) vs fixed=1:1.0.0: epoch 0 < 1 -> fixable."""
        result = classify(_make_finding(installed="1.0.0", fixed="1:1.0.0"))
        assert result.fixable is True

    def test_same_epoch_version_compared(self) -> None:
        """installed=1:1.0.0 vs fixed=1:2.0.0: same epoch, version decides -> fixable."""
        result = classify(_make_finding(installed="1:1.0.0", fixed="1:2.0.0"))
        assert result.fixable is True

    def test_same_epoch_installed_newer(self) -> None:
        """installed=2:3.0.0 vs fixed=2:2.0.0: same epoch, installed newer -> not fixable."""
        result = classify(_make_finding(installed="2:3.0.0", fixed="2:2.0.0"))
        assert result.fixable is False


# ---------------------------------------------------------------------------
# Ambiguous comparison -> fail closed (fixable=False)
# ---------------------------------------------------------------------------


class TestAmbiguousFailClosed:
    """Mixed int-vs-alpha at same position returns None -> not fixable."""

    def test_ambiguous_mixed_int_alpha_not_fixable(self) -> None:
        """A genuinely ambiguous version pair triggers fail-closed behavior."""
        # "1.0.abc" vs "1.0.2": at third segment, "abc" (alpha) vs 2 (int)
        result = classify(_make_finding(installed="1.0.abc", fixed="1.0.2"))
        assert result.fixable is False

    def test_ambiguous_rationale_mentions_conservative(self) -> None:
        """Ambiguous classification rationale explains conservative decision."""
        result = classify(_make_finding(installed="1.0.abc", fixed="1.0.2"))
        rationale_lower = result.rationale.lower()
        assert "ambiguous" in rationale_lower or "conservativ" in rationale_lower

    def test_ambiguous_reverse_not_fixable(self) -> None:
        """Reverse ambiguous pair also fails closed (not fixable)."""
        result = classify(_make_finding(installed="1.0.2", fixed="1.0.abc"))
        assert result.fixable is False

    def test_ambiguous_preserves_finding(self) -> None:
        """Finding is preserved in the classification even on ambiguity."""
        finding = _make_finding(installed="1.0.xyz", fixed="1.0.99")
        result = classify(finding)
        assert result.finding is finding
        assert result.fixable is False
