"""Parse CVE scanner JSON output into structured Finding objects.

Supports Trivy output format. All functions are pure (no I/O,
no side effects) and deterministic.
"""

from __future__ import annotations

from typing import Any

from scripts.cve_scan.models import Finding, Severity


def parse_trivy(json_obj: dict[str, Any], image: str, platform: str = "") -> list[Finding]:
    """Parse Trivy JSON output into Finding objects.

    Reads Results[].Vulnerabilities[] (VulnerabilityID, PkgName,
    InstalledVersion, FixedVersion, Severity); FixedVersion may be
    absent or empty (mapped to None).
    """
    findings: list[Finding] = []
    results = json_obj.get("Results")
    if not isinstance(results, list):
        return findings

    for result in results:
        vulns = result.get("Vulnerabilities")
        if not isinstance(vulns, list):
            continue
        for vuln in vulns:
            fixed = vuln.get("FixedVersion", "")
            findings.append(
                Finding(
                    image=image,
                    package=vuln["PkgName"],
                    installed_version=vuln["InstalledVersion"],
                    cve_id=vuln["VulnerabilityID"],
                    severity=Severity.from_str(vuln["Severity"]),
                    fixed_version=fixed if fixed else None,
                    platform=platform,
                )
            )
    return findings


def parse_findings(scanner: str, json_obj: dict[str, Any], image: str, platform: str = "") -> list[Finding]:
    """Dispatch to the correct parser based on scanner name; raises ValueError if unrecognized."""
    if scanner == "trivy":
        return parse_trivy(json_obj, image, platform=platform)
    raise ValueError(f"Unsupported scanner: {scanner!r}. Must be 'trivy'.")


def filter_by_threshold(findings: list[Finding], threshold: Severity) -> list[Finding]:
    """Return findings at or above the given severity threshold (inclusive)."""
    return [f for f in findings if f.severity >= threshold]
