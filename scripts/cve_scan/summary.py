"""Findings table renderer for CVE scan job summaries.

Renders a grouped one-row-per-CVE markdown table for GitHub Actions job
summaries. Deterministic: no AI, no network, no subprocess.
"""

from __future__ import annotations

from scripts.cve_scan.models import Classification


def _strip_repo_prefix(image: str) -> str:
    """Return the tag part of an image reference ('valkey/valkey:8.0-alpine' -> '8.0-alpine')."""
    return image.rsplit(":", 1)[-1] if ":" in image else image


def render_findings_table(
    classifications: list[Classification],
) -> str:
    """Render a grouped findings table (markdown) for job summaries.

    Rows grouped by (cve_id, severity, rationale); packages, versions, and
    images aggregated per group. Sorted severity desc, then CVE ID asc.

    Args:
        classifications: List of classifications to render.

    Returns:
        Markdown table string.
    """
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

    # Severity descending, then cve_id ascending
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
