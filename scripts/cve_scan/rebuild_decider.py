"""Rebuild-fixability classification for CVE scan findings.

Two-rule classification: a finding without a published fixed_version is not
fixable; a finding with one is a rebuild CANDIDATE. Candidacy trusts Trivy's
distro-aware matching (Trivy only reports a finding when its matcher
determined the installed version is below the fix). The authoritative
version comparison is the native base pre-check (base_precheck.py, dpkg/apk
via docker). Deterministic: pure code, no network, no subprocess, no AI.
"""

from __future__ import annotations

from scripts.cve_scan.models import Classification, Finding


def classify(finding: Finding) -> Classification:
    """Classify a finding as a rebuild candidate or not fixable.

    Rules: no fixed_version -> not fixable; fixed_version present ->
    candidate fixable, pending base pre-check verification.
    """
    if not finding.fixed_version:
        return Classification(
            finding=finding,
            fixable=False,
            rationale="No upstream fix yet.",
        )

    return Classification(
        finding=finding,
        fixable=True,
        rationale=(
            f"Fix {finding.fixed_version} published (Trivy matched installed "
            f"{finding.installed_version} as affected); pending base "
            f"verification."
        ),
    )


def classify_all(findings: list[Finding]) -> list[Classification]:
    """Classify a list of findings. Returns one Classification per Finding."""
    return [classify(f) for f in findings]
