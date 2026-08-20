"""Build-conformance drift check for the CVE verification build.

Our verification build reproduces valkey-container's real image build so the
rescan sees the same artifact ci.yml publishes. That mirroring is only valid
while valkey-container's ``docker/build-push-action`` step keeps the shape we
assume. This CLI fetches valkey-container's ci.yml (mainline) and asserts:

  (a) the build-push-action ``platforms`` equal our configured platforms
      (order-independent);
  (b) it sets no ``context:``;
  (c) it passes no ``build-args:``;
  (d) it sets no ``target:``;
  (e) its ``file:`` still matches the ``<something>/Dockerfile`` shape;
  (f) its ``provenance`` is false (a plain build, no attestation manifest).

When our own workflow is supplied it also cross-checks our verify job against
theirs, reading refs from both files rather than restating any SHA in Python:

  (g) our verify job's build-push / setup-qemu / setup-buildx pins equal the
      refs valkey-container's ci.yml uses (so we build with the same BuildKit
      and QEMU they publish with);
  (h) our verify build keeps ``push: false`` / ``load: true`` (never inherited
      from theirs: inheriting a ``push: true`` would be dangerous) and
      ``provenance: false`` to match their plain build.

Any mismatch exits nonzero with a message naming exactly what changed and that
our verification build must be re-synced, converting silent drift into a loud
failure. Fetch or parse errors also exit nonzero (fail closed).

Usage:
    python -m scripts.cve_scan.build_conformance [--platforms linux/amd64,...]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import urllib.request
from pathlib import Path
from urllib.error import URLError

import yaml

from scripts.cve_scan.config import DEFAULT_PLATFORMS

logger = logging.getLogger(__name__)

#: valkey-container's CI workflow on branch mainline (raw content).
_CI_YAML_URL = (
    "https://raw.githubusercontent.com/valkey-io/valkey-container"
    "/mainline/.github/workflows/ci.yml"
)
#: HTTP timeout, matching image_matrix's fetch style.
_FETCH_TIMEOUT_SECONDS = 15
#: The build step we mirror is identified by this action (any pin/version).
_BUILD_PUSH_ACTION = "docker/build-push-action"
#: Docker build actions whose pins our verify job must keep equal to theirs.
_TRACKED_BUILD_ACTIONS = (
    _BUILD_PUSH_ACTION,
    "docker/setup-qemu-action",
    "docker/setup-buildx-action",
)
#: Our workflow, relative to the repo root (CWD when the job or pytest runs).
#: Read at runtime so the pinned SHAs live only in the YAML, not restated here.
_OUR_WORKFLOW_PATH = Path(".github/workflows/cve-scan.yml")
#: file: must reference a Dockerfile under some directory.
_DOCKERFILE_SHAPE = re.compile(r".+/Dockerfile$")

#: Common remediation suffix appended to the drift report.
_RESYNC_NOTE = (
    "Our CVE verification build mirrors this step, so it must be re-synced "
    "before the build-and-verify flow can be trusted again."
)


class ConformanceError(Exception):
    """Raised on fetch, parse, or structural failure (fail closed)."""


def fetch_ci_yaml(url: str = _CI_YAML_URL) -> str:
    """Fetch valkey-container's ci.yml text.

    Reuses the urllib pattern and timeout style from image_matrix. Raises
    ConformanceError on any network or non-200 status error (fail closed).
    """
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "valkey-ci-agent/cve-scan"}
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                raise ConformanceError(
                    f"Failed to fetch ci.yml: HTTP {resp.status} from {url}"
                )
            return resp.read().decode("utf-8")
    except (URLError, OSError, TimeoutError) as exc:
        raise ConformanceError(f"Failed to fetch ci.yml from {url}: {exc}") from exc


def _find_build_push_job_and_step(workflow: dict) -> tuple[dict, dict]:
    """Return the (job, step) that runs docker/build-push-action, or raise.

    Scans every job's steps for a ``uses:`` referencing docker/build-push-action.
    A missing step is itself drift the mirror cannot survive (fail closed). The
    owning job is returned too so the other tracked action pins (qemu/buildx)
    are read from the same job that actually builds.
    """
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        raise ConformanceError("ci.yml has no 'jobs' mapping.")
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict) and _BUILD_PUSH_ACTION in str(step.get("uses", "")):
                return job, step
    raise ConformanceError(
        f"No {_BUILD_PUSH_ACTION} step found in ci.yml; the build shape we "
        "mirror is gone."
    )


def _tracked_action_refs(job: dict) -> dict[str, str]:
    """Return {action_path: ref} for tracked docker build actions in a job.

    Only the given job's steps are scanned, so our scan job's separate QEMU pin
    (kept for multi-arch scanning of published images, a different purpose) is
    never compared against valkey-container's build pins.
    """
    refs: dict[str, str] = {}
    steps = job.get("steps")
    if not isinstance(steps, list):
        return refs
    for step in steps:
        if not isinstance(step, dict):
            continue
        uses = str(step.get("uses", ""))
        for action in _TRACKED_BUILD_ACTIONS:
            if uses.startswith(f"{action}@"):
                refs[action] = uses.split("@", 1)[1].strip()
    return refs


def parse_our_verify_contract(workflow_text: str) -> tuple[dict[str, str], dict[str, object]]:
    """Parse our cve-scan.yml; return the verify job's (action_refs, build with-block).

    Scoped to the verify job so the scan job's separate QEMU pin is excluded.
    Fail closed (ConformanceError) on parse failure or a missing verify job /
    build step, since a mirror we cannot read is a mirror we cannot trust.
    """
    try:
        workflow = yaml.safe_load(workflow_text)
    except yaml.YAMLError as exc:
        raise ConformanceError(f"Failed to parse our cve-scan.yml: {exc}") from exc
    if not isinstance(workflow, dict):
        raise ConformanceError("Our cve-scan.yml did not parse to a mapping.")
    jobs = workflow.get("jobs")
    verify = jobs.get("verify") if isinstance(jobs, dict) else None
    if not isinstance(verify, dict):
        raise ConformanceError("Our cve-scan.yml has no 'verify' job.")

    refs = _tracked_action_refs(verify)

    build_with: dict[str, object] = {}
    for step in verify.get("steps", []):
        if isinstance(step, dict) and _BUILD_PUSH_ACTION in str(step.get("uses", "")):
            with_block = step.get("with")
            build_with = with_block if isinstance(with_block, dict) else {}
            break
    else:
        raise ConformanceError(
            "Our verify job has no build-push-action step to check."
        )
    return refs, build_with


def read_our_workflow(path: str | Path = _OUR_WORKFLOW_PATH) -> str:
    """Read our cve-scan.yml text. Fail closed (ConformanceError) if unreadable."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConformanceError(f"Failed to read our workflow {path}: {exc}") from exc


def _parse_platforms(value: object) -> list[str]:
    """Normalize a build-push-action ``platforms`` value into a list.

    Accepts a comma/newline-separated string or a YAML list; other types
    yield an empty list (treated as drift by the caller).
    """
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[,\n]", value) if p.strip()]
    if isinstance(value, list):
        return [str(p).strip() for p in value if str(p).strip()]
    return []


def check_conformance(
    ci_text: str,
    expected_platforms: list[str],
    our_workflow_text: str | None = None,
) -> list[str]:
    """Check ci.yml's build-push-action against the mirrored contract.

    Returns a list of drift messages (empty means conformant). When
    ``our_workflow_text`` is supplied, also cross-checks our verify job's action
    pins and push/load/provenance against theirs. Raises ConformanceError when
    either YAML cannot be parsed or a required build step is absent (fail closed).
    """
    try:
        workflow = yaml.safe_load(ci_text)
    except yaml.YAMLError as exc:
        raise ConformanceError(f"Failed to parse ci.yml as YAML: {exc}") from exc
    if not isinstance(workflow, dict):
        raise ConformanceError("ci.yml did not parse to a mapping.")

    build_job, step = _find_build_push_job_and_step(workflow)
    with_block = step.get("with")
    if not isinstance(with_block, dict):
        raise ConformanceError(
            f"The {_BUILD_PUSH_ACTION} step has no 'with:' block."
        )

    drift: list[str] = []

    # (a) platforms equal our configured set (order-independent).
    actual_platforms = _parse_platforms(with_block.get("platforms"))
    if set(actual_platforms) != set(expected_platforms):
        drift.append(
            f"platforms drift: ci.yml build-push-action platforms "
            f"{sorted(actual_platforms)} != expected {sorted(expected_platforms)}."
        )

    # (b) no context:
    if "context" in with_block:
        drift.append(
            f"context drift: build-push-action now sets 'context: "
            f"{with_block['context']!r}' (we assume none)."
        )

    # (c) no build-args:
    if "build-args" in with_block:
        drift.append(
            "build-args drift: build-push-action now passes 'build-args:' "
            "(we assume none)."
        )

    # (d) no target:
    if "target" in with_block:
        drift.append(
            f"target drift: build-push-action now sets 'target: "
            f"{with_block['target']!r}' (we assume none)."
        )

    # (e) file: matches the <dir>/Dockerfile shape.
    file_value = with_block.get("file")
    if not isinstance(file_value, str) or not _DOCKERFILE_SHAPE.match(file_value):
        drift.append(
            f"file drift: build-push-action 'file: {file_value!r}' no longer "
            "matches the '<dir>/Dockerfile' shape we assume."
        )

    # (f) provenance: false on their side (a plain build, no attestation).
    if with_block.get("provenance") is not False:
        drift.append(
            f"provenance drift: build-push-action 'provenance: "
            f"{with_block.get('provenance')!r}' is not false (we mirror a plain "
            "build with provenance disabled)."
        )

    if our_workflow_text is not None:
        drift.extend(
            _check_our_side(build_job, our_workflow_text)
        )

    return drift


def _check_our_side(their_build_job: dict, our_workflow_text: str) -> list[str]:
    """Cross-check our verify job against valkey-container's build job.

    Compares the tracked action pins (build-push / setup-qemu / setup-buildx)
    ref-by-ref and asserts our verify build keeps push:false / load:true /
    provenance:false. Reads both refs from files (never restating a SHA here).
    """
    drift: list[str] = []
    their_refs = _tracked_action_refs(their_build_job)
    our_refs, our_with = parse_our_verify_contract(our_workflow_text)

    # (g) action pins: our verify job must match theirs ref-for-ref.
    for action in _TRACKED_BUILD_ACTIONS:
        their_ref = their_refs.get(action)
        our_ref = our_refs.get(action)
        if their_ref is None:
            drift.append(
                f"action ref drift: {action} is no longer used by "
                f"valkey-container ci.yml, but our verify job pins {our_ref!r}. "
                "Re-sync our verify job to match their build."
            )
        elif our_ref is None:
            drift.append(
                f"action ref drift: our verify job no longer pins {action}, but "
                f"valkey-container ci.yml uses {their_ref!r}. Re-sync our verify "
                "job to match their build."
            )
        elif their_ref != our_ref:
            drift.append(
                f"action ref drift: {action} in valkey-container ci.yml is "
                f"{their_ref!r} but our verify job pins {our_ref!r}. Re-sync our "
                "pin to match theirs."
            )

    # (h) our verify build must NOT inherit push/load from theirs: it must stay
    # push:false / load:true (inheriting a push:true would publish the candidate)
    # and provenance:false to match their plain build.
    if our_with.get("push") is not False:
        drift.append(
            f"our-side push drift: verify build 'push: {our_with.get('push')!r}' "
            "must stay false; the candidate image must never be pushed."
        )
    if our_with.get("load") is not True:
        drift.append(
            f"our-side load drift: verify build 'load: {our_with.get('load')!r}' "
            "must stay true so the candidate is scannable on the runner."
        )
    if our_with.get("provenance") is not False:
        drift.append(
            f"our-side provenance drift: verify build 'provenance: "
            f"{our_with.get('provenance')!r}' must be false to match "
            "valkey-container's plain build."
        )
    return drift


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 when conformant, nonzero on drift or error."""
    parser = argparse.ArgumentParser(
        description=(
            "Assert valkey-container's ci.yml build-push-action still matches "
            "the shape the CVE verification build mirrors."
        ),
    )
    parser.add_argument(
        "--platforms",
        default=os.environ.get("CVE_SCAN_PLATFORMS", ",".join(DEFAULT_PLATFORMS)),
        help=(
            "Comma-separated expected platforms. Defaults to $CVE_SCAN_PLATFORMS "
            "or the built-in DEFAULT_PLATFORMS."
        ),
    )
    parser.add_argument(
        "--ci-url", default=_CI_YAML_URL, help="Override the ci.yml URL (testing)."
    )
    parser.add_argument(
        "--our-workflow",
        default=str(_OUR_WORKFLOW_PATH),
        help="Path to our cve-scan.yml (default: repo-root relative).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    expected_platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
    if not expected_platforms:
        logger.error("No expected platforms provided (--platforms / CVE_SCAN_PLATFORMS).")
        return 2

    try:
        ci_text = fetch_ci_yaml(args.ci_url)
        our_workflow_text = read_our_workflow(args.our_workflow)
        drift = check_conformance(ci_text, expected_platforms, our_workflow_text)
    except ConformanceError as exc:
        logger.error("Build-conformance check failed (fail closed): %s", exc)
        return 2

    if not drift:
        logger.info(
            "Build-conformance OK: build-push-action matches the mirrored "
            "contract (platforms=%s).",
            ",".join(expected_platforms),
        )
        return 0

    logger.error("Build-conformance drift detected:")
    for message in drift:
        logger.error("  - %s", message)
    logger.error("%s", _RESYNC_NOTE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
