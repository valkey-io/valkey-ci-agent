"""Findings table renderer for CVE scan job summaries.

Produces a grouped one-row-per-CVE markdown table suitable for GitHub Actions
job summaries. Rows are grouped by (cve_id, severity, rationale); packages,
installed versions, fixed versions, and images are aggregated per group.

Columns: | CVE | Severity | Packages | Installed | Fixed | Images | Rationale |
Sorted: severity descending, then CVE ID ascending.

This module is deterministic: no AI, no network, no subprocess.
"""

from __future__ import annotations

from scripts.cve_scan.models import Classification


def _strip_repo_prefix(image: str) -> str:
    """Strip the repository prefix from an image reference, returning the tag.

    Example: 'valkey/valkey:8.0-alpine' -> '8.0-alpine'
    """
    return image.rsplit(":", 1)[-1] if ":" in image else image


def render_findings_table(
    classifications: list[Classification],
) -> str:
    """Render a grouped findings table for job summaries.

    Rows are grouped by (cve_id, severity value, rationale). For each group,
    packages, installed versions, fixed versions, and images are aggregated.
    Columns: | CVE | Severity | Packages | Installed | Fixed | Images | Rationale |

    Args:
        classifications: List of classifications to render.

    Returns:
        Markdown table string.
    """
    # Group by (cve_id, severity value, rationale)
    groups: dict[tuple[str, int, str], list[Classification]] = {}
    for c in classifications:
        key = (c.finding.cve_id, c.finding.severity.value, c.rationale)
        groups.setdefault(key, []).append(c)

    lines: list[str] = [
        "### Findings",
        "",
        "| CVE | Severity | Packages | Installed | Fixed | Images | Rationale |",
        "|-----|----------|----------|-----------|-------|--------|-----------|",
    ]

    # Sort rows by severity descending then cve_id ascending
    for key in sorted(groups, key=lambda k: (-k[1], k[0])):
        cve_id, _sev_val, rationale = key
        items = groups[key]
        severity_name = items[0].finding.severity.name
        packages = ", ".join(sorted({c.finding.package for c in items}))
        installed = ", ".join(sorted({c.finding.installed_version for c in items}))
        fixed_versions = sorted({c.finding.fixed_version or "N/A" for c in items})
        fixed = ", ".join(fixed_versions)
        images = ", ".join(sorted({_strip_repo_prefix(c.finding.image) for c in items}))
        lines.append(
            f"| {cve_id} | {severity_name} | {packages} | {installed} "
            f"| {fixed} | {images} | {rationale} |"
        )

    lines.append("")
    return "\n".join(lines)
