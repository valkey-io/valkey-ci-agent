"""CVE scan sweep: scheduled scan + decision + job summary reporting.

Entry point for the CVE scan workflow. Runs on a cron schedule or
workflow_dispatch. Loads settings from CVE_SCAN_* env vars, scans images
across all configured platforms, classifies findings, and reports results
in the GitHub Actions job summary.

Emits GitHub Actions job outputs:
  fixable  - 'true' when confirmed-fixable findings exist (for job 2 condition)
  versions - space-separated version lines derived from fixable images
             (e.g. '8.0 9.1'), passed as --field version= to the rebuild workflow

All findings (confirmed-fixable being rebuilt AND not-fixable) are reported in
the job summary. No GitHub issues are created.

In dynamic mode (default), a base-image pre-check verifies that the upstream
base image already carries the patched package before requesting a rebuild.
Findings where the base has not been updated are downgraded to not-fixable.
The pre-check is skipped in static override mode, and static mode always emits
fixable=false to prevent unverified rebuilds.

Usage:
    python -m scripts.cve_scan.sweep --repo valkey-io/valkey-container
    python -m scripts.cve_scan.sweep --repo valkey-io/valkey-container --dry-run
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
    """Derive space-separated version line names from confirmed-fixable images.

    Converts image tags to line-prefix version strings suitable for passing
    as ``--field version=`` to valkey-container ci.yml. For example:
      'valkey/valkey:8.0-alpine' -> '8.0'
      'valkey/valkey:9.1'        -> '9.1'

    Returns a sorted, deduplicated list of version lines.
    """
    versions: set[str] = set()
    for c in fixable:
        tag = c.finding.image.rsplit(":", 1)[-1] if ":" in c.finding.image else c.finding.image
        # Strip the '-alpine' suffix to get the line prefix
        line = tag.replace("-alpine", "")
        if line:
            versions.add(line)
    return sorted(versions)


def _emit_outputs(fixable: bool, versions: list[str] | None = None) -> None:
    """Emit GitHub Actions job outputs (fixable and versions).

    Writes to $GITHUB_OUTPUT when running in Actions. When unset (local/dry-run),
    logs the values instead.

    Args:
        fixable: Whether confirmed-fixable findings exist.
        versions: Sorted version lines for the rebuild dispatch (e.g. ['8.0', '9.1']).
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
        versions = dispatched_versions or []
        if dry_run:
            lines.append(f"### Confirmed fixable (rebuild would be dispatched for versions: {' '.join(versions) or '(none)'})")
        else:
            lines.append(f"### Confirmed fixable (rebuild will be dispatched for versions: {' '.join(versions) or '(none)'})")
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
    repo_full_name: str,
    settings: CveScanSettings,
    dry_run: bool = False,
) -> None:
    """Execute the CVE scan sweep pipeline.

    Args:
        repo_full_name: Target repo (e.g. "valkey-io/valkey-container").
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

    # Scan across all configured platforms
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

    # Classify
    classifications = classify_all(findings)
    fixable, not_fixable = _split_classifications(classifications)
    logger.info(
        "Classification: %d fixable, %d not fixable.",
        len(fixable),
        len(not_fixable),
    )

    # Base-image pre-check (dynamic mode only): verify fixes are present in base
    if fixable and not static_mode:
        logger.info("Running base package check for %d fixable finding(s)...", len(fixable))
        confirmed, downgraded = verify_fixable_in_base(fixable, base_map)
        logger.info(
            "Base pre-check: %d confirmed, %d downgraded (base not yet updated).",
            len(confirmed),
            len(downgraded),
        )
        fixable = confirmed
        not_fixable = not_fixable + downgraded
    elif fixable and static_mode:
        logger.info(
            "Static mode: rebuild dispatch disabled, findings not verified against base."
        )
    else:
        logger.info("Skipping base pre-check (no fixable findings).")

    # Derive versions string for the rebuild dispatch
    versions = _fixable_versions(fixable) if fixable and not static_mode else []

    # Emit outputs: static mode always emits fixable=false (no verified rebuild)
    if static_mode:
        _emit_outputs(False)
    else:
        _emit_outputs(len(fixable) > 0, versions)

    # Dry run: print and exit
    if dry_run:
        _print_dry_run(fixable, not_fixable)
        _emit_run_summary(
            images=images, findings_count=len(findings), fixable=fixable,
            not_fixable=not_fixable, threshold_name=settings.severity_threshold.name,
            dry_run=True, static_mode=static_mode, dispatched_versions=versions,
        )
        return

    # Live mode: emit run summary (rebuild dispatch happens in the workflow YAML)
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
        "--repo",
        required=True,
        help="Target repository (owner/repo), e.g. valkey-io/valkey-container",
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
        repo_full_name=args.repo,
        settings=settings,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
