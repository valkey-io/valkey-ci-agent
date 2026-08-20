"""Scan a rebuilt candidate and prove its targeted CVE IDs are absent."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from scripts.cve_scan.scanner import ScanError, scan_image

logger = logging.getLogger(__name__)
_SCAN_TIMEOUT_SECONDS = 300


class VerifyError(Exception):
    """The CVE contract or artifact scan could not be trusted."""


def parse_cves(raw: str) -> list[str]:
    """Parse a non-empty JSON list of unique, non-empty CVE IDs."""
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise VerifyError(f"cves is not valid JSON: {exc}") from exc
    if not isinstance(value, list) or not value:
        raise VerifyError("cves must be a non-empty JSON list")
    if any(not isinstance(cve, str) or not cve for cve in value):
        raise VerifyError("every cves entry must be a non-empty string")
    if len(value) != len(set(value)):
        raise VerifyError("cves contains duplicates")
    return value


def verify(*, image_ref: str, cves_json: str, platform: str, trivy_bin: str = "trivy") -> list[tuple[str, str, str]]:
    """Return targeted findings still present as (CVE, package, version)."""
    targeted = set(parse_cves(cves_json))
    try:
        findings = scan_image(
            image_ref,
            platform,
            trivy_bin=trivy_bin,
            timeout=_SCAN_TIMEOUT_SECONDS,
        )
    except ScanError as exc:
        raise VerifyError(str(exc)) from exc
    return sorted(
        (finding.cve_id, finding.package, finding.installed_version)
        for finding in findings
        if finding.cve_id in targeted
    )


def _summary(args: argparse.Namespace, cves: list[str], survivors: list[tuple[str, str, str]]) -> str:
    lines = [
        "## CVE Candidate Verification",
        "",
        f"Image: `{args.image_ref}` (`{args.line}` / `{args.variant}` / `{args.platform}`)",
        "",
    ]
    if not survivors:
        lines.append(f"PASS: all {len(cves)} targeted CVE(s) are absent.")
    else:
        lines += [
            f"FAIL: {len(survivors)} targeted finding(s) survived:",
            "",
            "| CVE | Package | Installed |",
            "|---|---|---|",
            *(f"| {cve} | {package} | {version} |" for cve, package, version in survivors),
        ]
    return "\n".join(lines) + "\n"


def _write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--cves-json", required=True)
    parser.add_argument("--line", required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--trivy-bin", default="trivy")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    try:
        cves = parse_cves(args.cves_json)
        survivors = verify(
            image_ref=args.image_ref,
            cves_json=args.cves_json,
            platform=args.platform,
            trivy_bin=args.trivy_bin,
        )
    except VerifyError as exc:
        logger.error("Verification failed: %s", exc)
        _write_summary(f"## CVE Candidate Verification\n\nFAIL (fail closed): {exc}\n")
        return 2

    _write_summary(_summary(args, cves, survivors))
    if survivors:
        logger.error("Targeted CVEs survived: %s", survivors)
        return 1
    logger.info("All %d targeted CVEs are absent", len(cves))
    return 0


if __name__ == "__main__":
    sys.exit(main())
