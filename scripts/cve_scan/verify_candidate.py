"""Artifact verification for a locally built CVE-rebuild candidate.

The build-and-verify workflow builds the candidate image locally, then invokes
this CLI to prove the targeted CVEs are actually gone from the real artifact
before valkey-container does its normal build-and-publish. Verification
replaces prediction.

For one (line, variant, platform), it scans the local image with Trivy (OS
packages only, same flag family as scanner.py), parses the output with the
existing strict parser, and asserts that NONE of the contract's (cve, package)
pairs for this (line, variant, platform) are still present. Exit 0 only when
every targeted pair is absent. Any surviving pair, Trivy failure, parse
failure, or unexpected condition exits nonzero (fail closed).

Usage:
    python -m scripts.cve_scan.verify_candidate \\
        --image-ref localbuild:8.0-alpine \\
        --targets <base64> --line 8.0 --variant alpine --platform linux/amd64
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys

from scripts.cve_scan.targets import TargetDecodeError, decode_targets
from scripts.parsers.cve_findings_parser import ParseError, parse_findings

logger = logging.getLogger(__name__)

#: Trivy subprocess timeout in seconds (local image, no registry pull).
_SCAN_TIMEOUT_SECONDS = 300


class VerifyError(Exception):
    """Raised when the candidate image cannot be scanned or parsed (fail closed)."""


def _build_trivy_command(trivy_bin: str, image_ref: str, platform: str) -> list[str]:
    """Build the Trivy command (argv list, no shell interpolation).

    Same flag family as scanner.py: OS packages only, vuln scanner, JSON.
    """
    cmd = [
        trivy_bin, "image", "--format", "json", "--quiet",
        "--scanners", "vuln", "--pkg-types", "os",
    ]
    if platform:
        cmd.extend(["--platform", platform])
    cmd.append(image_ref)
    return cmd


def _run_trivy(command: list[str]) -> dict:
    """Run Trivy and return parsed JSON. Raises VerifyError on any failure."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_SCAN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VerifyError(
            f"Trivy timed out after {_SCAN_TIMEOUT_SECONDS}s: {' '.join(command)}"
        ) from exc
    except OSError as exc:
        raise VerifyError(
            f"Failed to execute Trivy: {' '.join(command)}: {exc}"
        ) from exc

    if result.returncode != 0:
        stderr_snippet = result.stderr[:500] if result.stderr else "(no stderr)"
        raise VerifyError(
            f"Trivy exited with code {result.returncode}: {' '.join(command)}\n"
            f"stderr: {stderr_snippet}"
        )

    if not result.stdout.strip():
        raise VerifyError(f"Trivy produced empty output: {' '.join(command)}")

    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VerifyError(
            f"Trivy output is not valid JSON: {' '.join(command)}: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise VerifyError(
            f"Trivy output is not a JSON object: {' '.join(command)}: "
            f"got {type(parsed).__name__}"
        )
    return parsed


def _installed_by_pair(
    scanner: str, json_obj: dict, image_ref: str, platform: str
) -> dict[tuple[str, str], str]:
    """Parse the scan and index installed versions by (cve, package).

    Raises VerifyError (fail closed) if the strict parser rejects the output.
    """
    try:
        findings = parse_findings(scanner, json_obj, image_ref, platform=platform)
    except ParseError as exc:
        raise VerifyError(
            f"Trivy output failed schema validation for {image_ref}: {exc}"
        ) from exc
    present: dict[tuple[str, str], str] = {}
    for f in findings:
        present[(f.cve_id, f.package)] = f.installed_version
    return present


def verify(
    *,
    image_ref: str,
    targets_b64: str,
    line: str,
    variant: str,
    platform: str,
    trivy_bin: str = "trivy",
) -> tuple[bool, list[tuple[str, str, str]]]:
    """Verify a built candidate image against the targets contract.

    Returns (passed, survivors) where survivors is a list of
    (cve, package, installed_version) tuples still present for this
    (line, variant, platform). passed is True only when survivors is empty.

    Raises:
        TargetDecodeError: If the contract is malformed.
        VerifyError: On any Trivy or parse failure (fail closed).
    """
    all_targets = decode_targets(targets_b64)
    targeted = {
        (t.cve, t.package)
        for t in all_targets
        if t.line == line and t.variant == variant and t.platform == platform
    }
    logger.info(
        "Verifying %s (line=%s variant=%s platform=%s): %d targeted (cve, package) pair(s).",
        image_ref, line, variant, platform, len(targeted),
    )

    command = _build_trivy_command(trivy_bin, image_ref, platform)
    json_obj = _run_trivy(command)
    present = _installed_by_pair("trivy", json_obj, image_ref, platform)

    survivors = sorted(
        (cve, package, present[(cve, package)])
        for (cve, package) in targeted
        if (cve, package) in present
    )
    return (not survivors, survivors)


def _write_step_summary(
    *,
    image_ref: str,
    line: str,
    variant: str,
    platform: str,
    targeted_count: int,
    passed: bool,
    survivors: list[tuple[str, str, str]],
) -> None:
    """Append a short markdown block to GITHUB_STEP_SUMMARY when it is set."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## CVE Candidate Verification",
        "",
        f"Image: `{image_ref}` (line `{line}`, variant `{variant}`, "
        f"platform `{platform}`)",
        "",
    ]
    if passed:
        if targeted_count == 0:
            lines.append(
                "PASS: no targeted (cve, package) pairs for this "
                "(line, variant, platform); nothing to verify."
            )
        else:
            lines.append(
                f"PASS: all {targeted_count} targeted (cve, package) pair(s) "
                "are absent from the built artifact."
            )
    else:
        lines.append(
            f"FAIL: {len(survivors)} targeted (cve, package) pair(s) survived "
            "in the built artifact:"
        )
        lines.append("")
        lines.append("| CVE | Package | Installed |")
        lines.append("|-----|---------|-----------|")
        for cve, package, installed in survivors:
            lines.append(f"| {cve} | {package} | {installed} |")
    lines.append("")
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code (0 pass, nonzero fail)."""
    parser = argparse.ArgumentParser(
        description=(
            "Verify a locally built CVE-rebuild candidate: prove the targeted "
            "CVEs are absent from the real artifact."
        ),
    )
    parser.add_argument("--image-ref", required=True, help="Locally built image tag to scan.")
    parser.add_argument("--targets", required=True, help="Base64 targets contract.")
    parser.add_argument("--line", required=True, help="Version line (e.g. 8.0).")
    parser.add_argument("--variant", required=True, help="Variant (debian or alpine).")
    parser.add_argument("--platform", required=True, help="Platform (e.g. linux/amd64).")
    parser.add_argument(
        "--trivy-bin", default="trivy", help="Trivy binary (default: trivy)."
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    try:
        all_targets = decode_targets(args.targets)
        targeted_count = sum(
            1
            for t in all_targets
            if t.line == args.line
            and t.variant == args.variant
            and t.platform == args.platform
        )
        passed, survivors = verify(
            image_ref=args.image_ref,
            targets_b64=args.targets,
            line=args.line,
            variant=args.variant,
            platform=args.platform,
            trivy_bin=args.trivy_bin,
        )
    except (TargetDecodeError, VerifyError) as exc:
        # Fail closed: any contract, Trivy, or parse failure is a nonzero exit.
        logger.error("Verification failed: %s", exc)
        _fail_closed_summary(args, exc)
        return 2

    _write_step_summary(
        image_ref=args.image_ref,
        line=args.line,
        variant=args.variant,
        platform=args.platform,
        targeted_count=targeted_count,
        passed=passed,
        survivors=survivors,
    )

    if passed:
        logger.info(
            "PASS: %d targeted pair(s) absent from %s.",
            targeted_count, args.image_ref,
        )
        return 0

    logger.error(
        "FAIL: %d targeted pair(s) survived in %s: %s",
        len(survivors),
        args.image_ref,
        ", ".join(f"{cve}/{pkg}@{ver}" for cve, pkg, ver in survivors),
    )
    return 1


def _fail_closed_summary(args: argparse.Namespace, exc: Exception) -> None:
    """Record a fail-closed markdown block naming the (line, variant, platform)."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## CVE Candidate Verification",
        "",
        f"Image: `{args.image_ref}` (line `{args.line}`, variant "
        f"`{args.variant}`, platform `{args.platform}`)",
        "",
        f"FAIL (fail closed): {exc}",
        "",
    ]
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
