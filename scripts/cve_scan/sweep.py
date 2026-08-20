"""CVE scan sweep: scheduled scan, classification, and plan emission.

Entry point for the CVE scan workflow. Scans images across configured
platforms, classifies findings (a published fix makes a finding a rebuild
CANDIDATE), and reports everything in the job summary. Emits the job outputs
the build-and-verify workflow codes against: ``plan`` (JSON matrix legs
whose ``cves`` list is verified against that leg's rebuilt artifact).

Verification is no longer predicted here: the downstream workflow BUILDS the
candidate image, SCANS the real artifact, and proves the targeted CVEs are
gone before valkey-container does its normal build-and-publish. So these
findings are candidates pending artifact verification, not confirmed fixes.

Usage: python -m scripts.cve_scan.sweep [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.common.job_summary import emit_job_summary
from scripts.cve_scan.config import CveScanSettings, load_settings
from scripts.cve_scan.image_matrix import resolve_matrix
from scripts.cve_scan.models import Classification
from scripts.cve_scan.rebuild_decider import classify_all
from scripts.cve_scan.scanner import ScanError, scan_images
from scripts.cve_scan.summary import render_findings_table

logger = logging.getLogger(__name__)


def _line_variant(image: str) -> tuple[str, str]:
    """Derive the valkey-container line and variant from an image reference."""
    tag = image.rsplit(":", 1)[-1] if ":" in image else image
    if tag.endswith("-alpine"):
        return tag[: -len("-alpine")], "alpine"
    return tag, "debian"


def _fixable_versions(fixable: list[Classification]) -> list[str]:
    """Derive sorted deduplicated version lines from fixable-candidate images.

    'valkey/valkey:8.0-alpine' -> '8.0' (for --field version= to ci.yml).
    """
    versions: set[str] = set()
    for c in fixable:
        line, _variant = _line_variant(c.finding.image)
        if line:
            versions.add(line)
    return sorted(versions)


def _verification_plan(fixable: list[Classification]) -> str:
    """Group candidates into order-stable matrix legs with unique CVE IDs."""
    legs: dict[tuple[str, str, str], dict[str, object]] = {}
    for c in fixable:
        f = c.finding
        line, variant = _line_variant(f.image)
        key = (line, variant, f.platform)
        leg = legs.setdefault(
            key,
            {
                "line": line,
                "variant": variant,
                "platform": f.platform,
                "cves": [],
            },
        )
        cves = leg["cves"]
        assert isinstance(cves, list)
        if f.cve_id not in cves:
            cves.append(f.cve_id)
    return json.dumps(list(legs.values()), separators=(",", ":"))


def _emit_outputs(plan: str = "[]") -> None:
    """Emit the GitHub Actions verification plan."""
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"plan={plan}\n")
        logger.info("Wrote %d plan leg(s) to GITHUB_OUTPUT", len(json.loads(plan)))
    else:
        print(f"plan={plan}")


def _print_dry_run(
    fixable: list[Classification],
    not_fixable: list[Classification],
) -> None:
    """Print what WOULD happen to stdout for dry-run mode."""
    if not_fixable:
        table = render_findings_table(not_fixable)
        print("=" * 72)
        print("[DRY RUN] NOT-FIXABLE FINDINGS (reported in job summary)")
        print("=" * 72)
        print(table)
        print()

    if fixable:
        versions = _fixable_versions(fixable)
        table = render_findings_table(fixable)
        print("=" * 72)
        print("[DRY RUN] VERIFICATION TARGETS")
        print("=" * 72)
        print(f"Would emit verification targets (pending artifact verification) for versions: {' '.join(versions)}")
        print("-" * 72)
        print(table)
        print()

    if not fixable and not not_fixable:
        print("[DRY RUN] No findings above threshold. No rebuild needed.")


def _emit_run_summary(
    *,
    images: list[str],
    findings_count: int,
    fixable: list[Classification],
    not_fixable: list[Classification],
    threshold_name: str,
    dry_run: bool,
    candidate_versions: list[str] | None = None,
) -> None:
    """Write a run summary to the GitHub Actions job summary page."""
    lines = ["## CVE Scan Summary", ""]
    if dry_run:
        lines.append("Mode: dry run")
        lines.append("")
    lines.append(f"| Images scanned | Findings ({threshold_name}+) | Fixable candidates | Not fixable |")
    lines.append("|---|---|---|---|")
    lines.append(f"| {len(images)} | {findings_count} | {len(fixable)} | {len(not_fixable)} |")
    lines.append("")

    if fixable:
        versions = " ".join(candidate_versions or [])
        # These are candidates pending artifact verification: the downstream
        # workflow builds the image and rescans it before any rebuild. The
        # scan job never claims a fix is confirmed.
        lines.append(f"### Fixable candidates (pending artifact verification for versions: {versions})")
        lines.append("")
        lines.append(render_findings_table(fixable))
        lines.append("")

    if not_fixable:
        lines.append("### Unresolved findings (no rebuild)")
        lines.append("")
        lines.append(render_findings_table(not_fixable))
        lines.append("")

    if not fixable and not not_fixable:
        if findings_count > 0:
            lines.append("No fixable candidates: no finding has a published upstream fix.")
        else:
            lines.append("No findings at or above the severity threshold.")
        lines.append("")

    emit_job_summary("\n".join(lines))


def _emit_failure_summary(stage: str, exc: Exception) -> None:
    """Write a job summary reporting that a pipeline stage failed.

    ``stage`` names the failing stage (currently only "Scan"); the exception
    message identifies the failing image/platform. No contract is emitted: the
    failure is reported here, then re-raised by the caller so the job fails
    loudly.
    """
    lines = [
        "## CVE Scan Summary",
        "",
        f"### {stage} failed",
        "",
        f"The CVE {stage.lower()} stage did not complete, so no rebuild will be dispatched.",
        "",
        f"Error: {exc}",
        "",
    ]
    emit_job_summary("\n".join(lines))


def run_sweep(
    *,
    settings: CveScanSettings,
    dry_run: bool = False,
) -> None:
    """Execute the CVE scan sweep pipeline.

    Args:
        settings: Loaded CveScanSettings instance.
        dry_run: If True, print findings and suppress the contract emission
            side of dispatch (outputs are still written so the caller can
            inspect them).
    """
    logger.info(
        "Loaded settings: threshold=%s, platforms=%s",
        settings.severity_threshold.name,
        ",".join(settings.platforms),
    )

    images = resolve_matrix(settings)
    logger.info("Resolved %d image(s) to scan: %s", len(images), ", ".join(images))

    logger.info(
        "Scanning %d image(s) x %d platform(s) with Trivy...",
        len(images),
        len(settings.platforms),
    )
    try:
        findings = scan_images(
            images,
            settings.severity_threshold,
            platforms=settings.platforms,
        )
    except ScanError as exc:
        # A single image/platform scan failure would otherwise abort the run
        # before any summary is emitted, discarding the whole report. Emit a
        # failure summary (the message names the failing image/platform), then
        # re-raise so the job still fails loudly. Do not swallow the error.
        logger.error("Scan failed: %s", exc)
        _emit_failure_summary("Scan", exc)
        raise
    logger.info(
        "Found %d finding(s) above %s threshold.",
        len(findings),
        settings.severity_threshold.name,
    )

    if not findings:
        logger.info("No findings above threshold. Exiting cleanly.")
        _emit_outputs()
        if dry_run:
            print("[DRY RUN] No findings. No rebuild needed.")
        _emit_run_summary(
            images=images,
            findings_count=0,
            fixable=[],
            not_fixable=[],
            threshold_name=settings.severity_threshold.name,
            dry_run=dry_run,
        )
        return

    classifications = classify_all(findings)
    fixable = [c for c in classifications if c.fixable]
    not_fixable = [c for c in classifications if not c.fixable]
    logger.info(
        "Classification: %d fixable candidate(s), %d not fixable.",
        len(fixable),
        len(not_fixable),
    )

    versions = _fixable_versions(fixable)
    plan = _verification_plan(fixable)
    _emit_outputs(plan)

    if dry_run:
        _print_dry_run(fixable, not_fixable)
        _emit_run_summary(
            images=images,
            findings_count=len(findings),
            fixable=fixable,
            not_fixable=not_fixable,
            threshold_name=settings.severity_threshold.name,
            dry_run=True,
            candidate_versions=versions,
        )
        return

    # Live mode: rebuild dispatch happens in the workflow YAML after the
    # candidate image is built and its artifact verified.
    _emit_run_summary(
        images=images,
        findings_count=len(findings),
        fixable=fixable,
        not_fixable=not_fixable,
        threshold_name=settings.severity_threshold.name,
        dry_run=False,
        candidate_versions=versions,
    )


def main() -> None:
    """CLI entry point for the CVE scan sweep."""
    parser = argparse.ArgumentParser(
        description="CVE Scan Sweep: scan images, classify findings, report in job summary.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print findings to stdout; skip dispatch.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    settings = load_settings()

    run_sweep(
        settings=settings,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
