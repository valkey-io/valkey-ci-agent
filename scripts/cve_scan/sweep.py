"""CVE scan sweep: scheduled scan + decision + job summary reporting.

Entry point for the CVE scan workflow. Scans images across configured
platforms, classifies findings, verifies fixes against base images
(dynamic mode), and reports everything in the job summary. Emits job
outputs ``fixable`` and ``versions`` for the rebuild job. Static override
mode reclassifies all rebuild candidates as not-fixable (base verification
unavailable) and always emits fixable=false to prevent unverified rebuilds.

Usage: python -m scripts.cve_scan.sweep [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.cve_scan.base_precheck import verify_fixable_in_base
from scripts.cve_scan.config import CveScanSettings, load_settings
from scripts.cve_scan.image_matrix import resolve_matrix
from scripts.cve_scan.models import Classification
from scripts.cve_scan.rebuild_decider import classify_all
from scripts.cve_scan.scanner import scan_images
from scripts.cve_scan.summary import render_findings_table

logger = logging.getLogger(__name__)


def _split_classifications(
    classifications: list[Classification],
) -> tuple[list[Classification], list[Classification]]:
    """Split classifications into fixable and not-fixable groups."""
    fixable = [c for c in classifications if c.fixable]
    not_fixable = [c for c in classifications if not c.fixable]
    return fixable, not_fixable


def _fixable_versions(fixable: list[Classification]) -> list[str]:
    """Derive sorted deduplicated version lines from confirmed-fixable images.

    'valkey/valkey:8.0-alpine' -> '8.0' (for --field version= to ci.yml).
    """
    versions: set[str] = set()
    for c in fixable:
        tag = c.finding.image.rsplit(":", 1)[-1] if ":" in c.finding.image else c.finding.image
        line = tag.replace("-alpine", "")
        if line:
            versions.add(line)
    return sorted(versions)


def _emit_outputs(fixable: bool, versions: list[str] | None = None) -> None:
    """Emit GitHub Actions job outputs (fixable, versions) to $GITHUB_OUTPUT.

    When GITHUB_OUTPUT is unset (local/dry-run), prints the values instead.
    """
    versions_str = " ".join(versions or [])
    fixable_str = "true" if fixable else "false"

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"fixable={fixable_str}\n")
            f.write(f"versions={versions_str}\n")
        logger.info(
            "Wrote outputs to GITHUB_OUTPUT: fixable=%s versions=%s",
            fixable_str, versions_str or "(empty)",
        )
    else:
        print(f"fixable={fixable_str}")
        print(f"versions={versions_str}")


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
        print("[DRY RUN] DISPATCH BEHAVIOR")
        print("=" * 72)
        print(f"Would dispatch rebuild for versions: {' '.join(versions)}")
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
    static_mode: bool,
    dispatched_versions: list[str] | None = None,
) -> None:
    """Write a run summary to the GitHub Actions job summary page."""
    from scripts.common.job_summary import emit_job_summary

    lines = ["## CVE Scan Summary", ""]
    mode_bits = []
    if dry_run:
        mode_bits.append("dry run")
    if static_mode:
        mode_bits.append("static image override")
    if mode_bits:
        lines.append(f"Mode: {', '.join(mode_bits)}")
        lines.append("")
    lines.append(f"| Images scanned | Findings ({threshold_name}+) | Confirmed fixable | Not fixable |")
    lines.append("|---|---|---|---|")
    lines.append(f"| {len(images)} | {findings_count} | {len(fixable)} | {len(not_fixable)} |")
    lines.append("")

    if fixable:
        # Static mode never reaches here: its candidates are reclassified as
        # not fixable before the summary is rendered.
        versions = " ".join(dispatched_versions or [])
        verb = "would be" if dry_run else "will be"
        lines.append(f"### Confirmed fixable (rebuild {verb} dispatched for versions: {versions})")
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
            lines.append("No rebuild dispatched: no finding has a fix verified present in the base image.")
        else:
            lines.append("No findings at or above the severity threshold.")
        lines.append("")

    emit_job_summary("\n".join(lines))


def run_sweep(
    *,
    settings: CveScanSettings,
    dry_run: bool = False,
) -> None:
    """Execute the CVE scan sweep pipeline.

    Args:
        settings: Loaded CveScanSettings instance.
        dry_run: If True, print findings and skip dispatch.
    """
    logger.info(
        "Loaded settings: scanner=%s, threshold=%s, platforms=%s",
        settings.scanner,
        settings.severity_threshold.name,
        ",".join(settings.platforms),
    )

    # Resolve image matrix (static override or dynamic from versions manifest)
    images, base_map = resolve_matrix(settings)
    static_mode = bool(settings.images)
    logger.info("Resolved %d image(s) to scan: %s", len(images), ", ".join(images))

    logger.info(
        "Scanning %d image(s) x %d platform(s) with %s...",
        len(images), len(settings.platforms), settings.scanner,
    )
    findings = scan_images(
        images, settings.scanner, settings.severity_threshold,
        platforms=settings.platforms,
    )
    logger.info("Found %d finding(s) above %s threshold.", len(findings), settings.severity_threshold.name)

    if not findings:
        logger.info("No findings above threshold. Exiting cleanly.")
        _emit_outputs(False)
        if dry_run:
            print("[DRY RUN] No findings. No rebuild needed.")
        _emit_run_summary(
            images=images, findings_count=0, fixable=[], not_fixable=[],
            threshold_name=settings.severity_threshold.name,
            dry_run=dry_run, static_mode=static_mode,
        )
        return

    classifications = classify_all(findings)
    fixable, not_fixable = _split_classifications(classifications)
    logger.info(
        "Classification: %d fixable, %d not fixable.",
        len(fixable),
        len(not_fixable),
    )

    # Base pre-check (dynamic mode) or static-mode reclassification, BEFORE
    # outputs/summary/dry-run rendering so reporting reflects reality.
    if fixable and static_mode:
        # Static override: base verification is unavailable, so nothing is
        # auto-fixable. Reclassify candidates instead of leaving them in
        # `fixable` (which would render a bogus dispatch section).
        logger.info(
            "Static mode: rebuild dispatch disabled; reclassifying %d candidate(s) as not fixable.",
            len(fixable),
        )
        not_fixable = not_fixable + [
            Classification(
                finding=c.finding,
                fixable=False,
                rationale="static image override: base verification unavailable, not auto-fixable",
            )
            for c in fixable
        ]
        fixable = []
    elif fixable:
        logger.info("Running base package check for %d fixable finding(s)...", len(fixable))
        confirmed, downgraded = verify_fixable_in_base(fixable, base_map)
        logger.info(
            "Base pre-check: %d confirmed, %d downgraded (base not yet updated).",
            len(confirmed),
            len(downgraded),
        )
        fixable = confirmed
        not_fixable = not_fixable + downgraded
    else:
        logger.info("Skipping base pre-check (no fixable findings).")

    versions = _fixable_versions(fixable)

    # Static mode always emits fixable=false (no unverified rebuild)
    if static_mode:
        _emit_outputs(False)
    else:
        _emit_outputs(len(fixable) > 0, versions)

    if dry_run:
        _print_dry_run(fixable, not_fixable)
        _emit_run_summary(
            images=images, findings_count=len(findings), fixable=fixable,
            not_fixable=not_fixable, threshold_name=settings.severity_threshold.name,
            dry_run=True, static_mode=static_mode, dispatched_versions=versions,
        )
        return

    # Live mode: rebuild dispatch happens in the workflow YAML
    _emit_run_summary(
        images=images, findings_count=len(findings), fixable=fixable,
        not_fixable=not_fixable, threshold_name=settings.severity_threshold.name,
        dry_run=False, static_mode=static_mode, dispatched_versions=versions,
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
        "--verbose", "-v",
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
