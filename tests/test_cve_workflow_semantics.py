"""Semantic guards for .github/workflows/cve-scan.yml.

Parses the workflow with yaml and asserts the behavior-level invariants of the
verification-instead-of-prediction architecture: the scan job emits candidates
(fixable/versions/targets) with no write access and no App token; a verify job
builds the candidate image itself (one platform per finding, mirroring
valkey-container's build with no registry credentials) and gates the rebuild
job; and the rebuild job dispatches valkey-container's plain ci.yml only on
proof, correlating the downstream run by an EXACT run name (not a timestamp
window or actor filter) and waiting for its real outcome.

These assert behavior, not just wiring: the drift check must precede the build,
the build must be push-less/load-only/single-platform with no ``context:``
override, the verify job must carry no registry secret, and the locate step
must match on the correlation run name while using none of the deleted
``--created`` / ``--user`` / ``gh api user`` heuristics.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/cve-scan.yml"

_USE_RE = re.compile(r"uses:\s*([^@\s]+)@([^#\s]+)")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# valkey-container ci.yml build pins the verify job must mirror (SHA -> action).
# Kept here so the semantics test asserts the exact documented SHAs; the runtime
# drift check reads them from both YAML files instead of restating them.
_VALKEY_CONTAINER_PINS = {
    "docker/build-push-action": "f9f3042f7e2789586610d6e8b85c8f03e5195baf",
    "docker/setup-qemu-action": "06116385d9baf250c9f4dcb4858b16962ea869c3",
    "docker/setup-buildx-action": "d7f5e7f509e45cec5c76c4d5afdd7de93d0b3df5",
}


def _load_workflow() -> dict:
    """Parse the CVE scan workflow YAML into a dict."""
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _raw_text() -> str:
    """Return the raw workflow file text."""
    return _WORKFLOW.read_text(encoding="utf-8")


def _on_section(workflow: dict) -> dict:
    """Return the workflow's trigger section.

    PyYAML parses the bare ``on:`` key as the boolean ``True`` (YAML 1.1), so
    fall back to that when the string key is absent.
    """
    if "on" in workflow:
        return workflow["on"]
    return workflow[True]


def _job_steps(job: str) -> list[dict]:
    """Return the steps of the named job."""
    return _load_workflow()["jobs"][job]["steps"]


def _rebuild_steps() -> list[dict]:
    """Return the rebuild job's steps."""
    return _job_steps("rebuild")


def _verify_steps() -> list[dict]:
    """Return the verify job's steps."""
    return _job_steps("verify")


def _rebuild_step(step_id: str) -> dict:
    """Return the rebuild step with the given id."""
    return next(s for s in _rebuild_steps() if s.get("id") == step_id)


def _verify_build_step() -> dict:
    """Return the verify job's docker build step."""
    return next(s for s in _verify_steps() if "build-push-action" in s.get("uses", ""))


# --------------------------------------------------------------------------- #
# Triggers and pinning
# --------------------------------------------------------------------------- #


def test_workflow_dispatch_dry_run_defaults_true() -> None:
    """The manual dry_run input defaults to True so an operator must opt in to a real rebuild."""
    workflow = _load_workflow()
    dry_run = _on_section(workflow)["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run["default"] is True


def test_all_external_actions_are_sha_pinned() -> None:
    """Every non-local `uses:` is pinned to a 40-hex commit SHA; local ./ actions are exempt."""
    offenders = []
    for line_no, line in enumerate(_raw_text().splitlines(), start=1):
        match = _USE_RE.search(line)
        if not match:
            continue
        action, ref = match.groups()
        if action.startswith("./"):
            continue
        if not _SHA_RE.fullmatch(ref):
            offenders.append(f"{_WORKFLOW}:{line_no}: {action}@{ref}")

    assert offenders == []


def test_no_automation_pat_referenced() -> None:
    """AUTOMATION_PAT must not appear anywhere; credentials come from the App token only."""
    assert "AUTOMATION_PAT" not in _raw_text()


# --------------------------------------------------------------------------- #
# Scan job: candidates only, least privilege
# --------------------------------------------------------------------------- #


def test_scan_job_emits_fixable_versions_targets() -> None:
    """The scan job exposes the candidate outputs the verify/rebuild jobs consume."""
    outputs = _load_workflow()["jobs"]["scan"]["outputs"]
    assert "fixable" in outputs
    assert "versions" in outputs
    assert "targets" in outputs


def test_scan_job_has_no_app_token_step() -> None:
    """The scan job mints no GitHub App token (only the rebuild job may)."""
    uses = [step.get("uses", "") for step in _job_steps("scan")]
    assert not any("create-github-app-token" in u for u in uses)


def test_scan_job_has_no_write_permissions() -> None:
    """Neither the scan job nor the workflow default grants issues/actions write.

    Handles both the mapping form and the scalar form: a scalar ``write-all``
    grants issues and actions write, so it must fail this test rather than slip
    through (an ``isinstance(dict)`` guard would skip the exact config the test
    exists to reject).
    """
    def _assert_no_write(perms: object) -> None:
        if isinstance(perms, str):
            assert perms != "write-all"
        elif isinstance(perms, dict):
            assert perms.get("issues") != "write"
            assert perms.get("actions") != "write"

    workflow = _load_workflow()
    _assert_no_write(workflow.get("permissions", {}))
    _assert_no_write(workflow["jobs"]["scan"].get("permissions", {}))


# --------------------------------------------------------------------------- #
# Verify job: build-and-scan the real artifact, gate the rebuild
# --------------------------------------------------------------------------- #


def test_verify_job_exists_and_gates_rebuild_via_collect() -> None:
    """verify exists; collect needs verify; rebuild needs [scan, collect].

    A failed verification propagates through collect (which fails closed on any
    errored entry) and blocks dispatch, so rebuild depends on collect, NOT on
    verify directly."""
    workflow = _load_workflow()
    assert "verify" in workflow["jobs"], "expected a 'verify' job"
    assert "collect" in workflow["jobs"], "expected a 'collect' job"

    collect_needs = workflow["jobs"]["collect"]["needs"]
    collect_needs = [collect_needs] if isinstance(collect_needs, str) else collect_needs
    assert "verify" in collect_needs

    rebuild_needs = workflow["jobs"]["rebuild"]["needs"]
    rebuild_needs = [rebuild_needs] if isinstance(rebuild_needs, str) else rebuild_needs
    assert "scan" in rebuild_needs
    assert "collect" in rebuild_needs
    assert "verify" not in rebuild_needs


def test_verify_job_if_has_all_guards() -> None:
    """The verify job fails closed on repo, ref, verified-fixable, versions, and dry-run."""
    if_expr = _load_workflow()["jobs"]["verify"]["if"]
    assert "github.repository == 'valkey-io/valkey-ci-agent'" in if_expr
    assert "github.ref == 'refs/heads/main'" in if_expr
    assert "needs.scan.outputs.fixable == 'true'" in if_expr
    assert "needs.scan.outputs.versions != ''" in if_expr
    assert "github.event.inputs.dry_run != 'true'" in if_expr
    assert "github.event_name != 'workflow_dispatch'" in if_expr


def test_verify_matrix_is_fed_from_scan_via_fromjson() -> None:
    """The verify matrix entries come from the scan job's targets-derived output via fromJSON."""
    strategy = _load_workflow()["jobs"]["verify"]["strategy"]
    assert strategy["matrix"]["include"] == "${{ fromJSON(needs.scan.outputs.matrix) }}"


def test_verify_matrix_parallelism_is_bounded() -> None:
    """Matrix parallelism is capped but raised above the old 2 for the wider,
    every-affected-architecture matrix (up to 5 lines x 2 variants x 4 platforms
    = 40 entries). Assert the relationship (> the previous 2), not a brittle
    exact value, since the invariant is "bounded but higher than before"."""
    strategy = _load_workflow()["jobs"]["verify"]["strategy"]
    assert isinstance(strategy["max-parallel"], int)
    # Bounded (a wide finding set never runs unbounded heavy builds) ...
    assert strategy["max-parallel"] >= 1
    # ... but raised above the old cap of 2 to keep wall clock sane at 40 entries.
    assert strategy["max-parallel"] > 2


def test_verify_matrix_does_not_fail_fast() -> None:
    """fail-fast stays false so every affected architecture's outcome is reported,
    not cancelled on the first failure (the rebuild is gated on ALL entries anyway)."""
    strategy = _load_workflow()["jobs"]["verify"]["strategy"]
    assert strategy["fail-fast"] is False


def test_verify_timeout_covers_emulated_source_compile() -> None:
    """The verify job timeout is raised above the previous 60 to cover a QEMU-emulated
    source compile (arm/v7, ppc64le). Assert the relationship (> 60, under GitHub's 360
    ceiling), not a brittle exact value, since the bound is an unvalidated estimate."""
    timeout = _load_workflow()["jobs"]["verify"]["timeout-minutes"]
    assert isinstance(timeout, int)
    assert timeout > 60
    assert timeout <= 360


def _all_drift_check_steps() -> list[tuple[str, dict]]:
    """Return (job_name, step) for every step that runs the build_conformance drift check."""
    workflow = _load_workflow()
    hits: list[tuple[str, dict]] = []
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            if "build_conformance" in str(step.get("run", "")):
                hits.append((job_name, step))
    return hits


def test_drift_check_runs_exactly_once_in_whole_workflow() -> None:
    """The build-conformance drift check appears EXACTLY ONCE across all jobs.

    The bug: it used to be a step inside the verify job, which fans out to the
    matrix, so it ran once per leg (up to 40 redundant ci.yml fetches, each an
    independent chance for a transient failure to kill a leg and silently drop a
    platform). Assert the behavior (runs once), not where it lives.
    """
    assert len(_all_drift_check_steps()) == 1


def test_drift_check_not_inside_verify_job() -> None:
    """The drift check is NOT a step of the verify job (the job that fans out per platform)."""
    assert all(
        "build_conformance" not in str(s.get("run", "")) for s in _verify_steps()
    )


def test_drift_check_job_gates_verify_via_needs() -> None:
    """The job that runs the drift check gates verify: verify `needs` it (assert the needs
    chain), and it is a distinct job, so one check blocks every build leg on drift."""
    hits = _all_drift_check_steps()
    assert len(hits) == 1
    drift_job = hits[0][0]
    assert drift_job != "verify"
    verify_needs = _load_workflow()["jobs"]["verify"]["needs"]
    verify_needs = [verify_needs] if isinstance(verify_needs, str) else verify_needs
    assert drift_job in verify_needs


def test_drift_check_is_fail_closed_and_gated_like_verify() -> None:
    """The drift-check step is fail-closed (`set -euo pipefail`, so drift or a fetch error
    fails the job) and its job runs only when a real build is imminent (same repo/ref/fixable/
    dry-run guards as verify), so a transient ci.yml fetch never fails a routine scan."""
    hits = _all_drift_check_steps()
    assert len(hits) == 1
    drift_job, step = hits[0]
    assert "set -euo pipefail" in step["run"]
    if_expr = _load_workflow()["jobs"][drift_job]["if"]
    assert "github.repository == 'valkey-io/valkey-ci-agent'" in if_expr
    assert "github.ref == 'refs/heads/main'" in if_expr
    assert "needs.scan.outputs.fixable == 'true'" in if_expr
    assert "github.event.inputs.dry_run != 'true'" in if_expr


def test_drift_check_has_no_platform_literal() -> None:
    """The drift-check step passes NO platform literal: the expected list is
    single-sourced from scripts/cve_scan/config.py via build_conformance's
    default. The old CVE_VERIFY_PLATFORMS env and the 4-platform literal are gone
    from the whole workflow, so the repo holds exactly one copy of the list."""
    hits = _all_drift_check_steps()
    assert len(hits) == 1
    _, step = hits[0]
    run = step["run"]
    assert "--platforms" not in run
    assert "CVE_VERIFY_PLATFORMS" not in run
    text = _raw_text()
    assert "CVE_VERIFY_PLATFORMS" not in text
    assert "linux/amd64,linux/arm64,linux/arm/v7,linux/ppc64le" not in text


def test_verify_build_action_pins_match_valkey_container() -> None:
    """The verify job's build-push / setup-qemu / setup-buildx pins equal the
    documented valkey-container ci.yml SHAs, so the verification build uses the
    same BuildKit and QEMU as the publishing build (drift check enforces it)."""
    verify = _load_workflow()["jobs"]["verify"]
    pins: dict[str, str] = {}
    for step in verify["steps"]:
        uses = step.get("uses", "")
        for action in _VALKEY_CONTAINER_PINS:
            if uses.startswith(f"{action}@"):
                pins[action] = uses.split("@", 1)[1].split()[0]
    assert pins == _VALKEY_CONTAINER_PINS


def test_scan_job_qemu_pin_left_independent_of_verify() -> None:
    """The scan job's QEMU pin is intentionally NOT forced to valkey-container's
    build SHA: it serves a different purpose (multi-arch scanning of published
    images, not building), so it may differ while staying SHA-pinned."""
    scan = _load_workflow()["jobs"]["scan"]
    scan_qemu = next(
        s["uses"] for s in scan["steps"] if "setup-qemu-action" in s.get("uses", "")
    )
    assert "setup-qemu-action@" in scan_qemu
    assert _VALKEY_CONTAINER_PINS["docker/setup-qemu-action"] not in scan_qemu


def test_verify_build_step_is_local_single_platform() -> None:
    """The candidate build pushes nothing, loads locally, has no context override,
    disables provenance, and builds exactly one platform."""
    with_block = _verify_build_step()["with"]
    assert with_block["push"] is False
    assert with_block["load"] is True
    assert with_block["provenance"] is False
    # No context override: mirror valkey-container's build (context = repo root).
    assert "context" not in with_block
    # Exactly one platform per matrix entry (the matrix value, no comma-list).
    platforms = str(with_block["platforms"])
    assert platforms == "${{ matrix.platform }}"
    assert "," not in platforms


def test_verify_build_step_mirrors_dockerfile_path() -> None:
    """The build points at ./<line>/<variant>/Dockerfile, mirroring valkey-container exactly."""
    file_ref = _verify_build_step()["with"]["file"]
    assert file_ref == "./${{ matrix.line }}/${{ matrix.variant }}/Dockerfile"


def test_verify_job_has_no_registry_credentials() -> None:
    """The candidate never leaves the runner: no registry login and no secret in the verify job."""
    dumped = yaml.safe_dump(_load_workflow()["jobs"]["verify"])
    assert "login-action" not in dumped
    assert "password" not in dumped
    assert "username" not in dumped
    # The verify job authenticates to nothing: no ${{ secrets.* }} references.
    assert "secrets." not in dumped


def test_verify_job_runs_candidate_verification() -> None:
    """The verify job proves the built artifact via scripts.cve_scan.verify_candidate."""
    steps = _verify_steps()
    assert any("verify_candidate" in s.get("run", "") for s in steps)


# --------------------------------------------------------------------------- #
# Rebuild job: dispatch on proof, correlate by exact run name
# --------------------------------------------------------------------------- #


def test_rebuild_job_if_has_all_guards() -> None:
    """The rebuild job's `if` fails closed on repo, ref, and dry-run, and gates on COLLECT's
    verified outputs (not the scan's raw candidate set)."""
    if_expr = _load_workflow()["jobs"]["rebuild"]["if"]
    assert "github.repository == 'valkey-io/valkey-ci-agent'" in if_expr
    assert "github.ref == 'refs/heads/main'" in if_expr
    assert "needs.collect.outputs.fixable == 'true'" in if_expr
    assert "needs.collect.outputs.verified_versions != ''" in if_expr
    assert "github.event.inputs.dry_run != 'true'" in if_expr
    assert "github.event_name != 'workflow_dispatch'" in if_expr


def test_rebuild_job_uses_protected_environment() -> None:
    """The rebuild job runs in the credential-scoped cve-rebuild-dispatch environment."""
    assert _load_workflow()["jobs"]["rebuild"]["environment"] == "cve-rebuild-dispatch"


def test_dispatch_step_uses_app_token() -> None:
    """The dispatch step (the sole step needing actions:write on another repo) uses the App token."""
    assert _rebuild_step("dispatch")["env"]["GH_TOKEN"] == "${{ steps.token.outputs.token }}"


def test_dispatch_passes_version_and_correlation_id() -> None:
    """The dispatch passes BOTH the version list and a correlation_id to ci.yml."""
    run = _rebuild_step("dispatch")["run"]
    assert 'gh workflow run ci.yml' in run
    assert '--field "version=' in run
    assert '--field "correlation_id=' in run


def test_correlation_id_is_run_id_and_attempt() -> None:
    """The correlation_id is github.run_id + github.run_attempt (unique, deterministic per run)."""
    run = _rebuild_step("dispatch")["run"]
    assert "${{ github.run_id }}-${{ github.run_attempt }}" in run


def test_locate_matches_correlation_run_name_not_heuristics() -> None:
    """The locate step matches the exact correlation run name and drops the deleted heuristics."""
    locate = _rebuild_step("locate")
    run = locate["run"]
    # Correlation-name match, restricted to ci.yml on mainline.
    assert "gh run list" in run
    assert "--workflow ci.yml" in run
    assert "displayTitle" in run
    assert locate["env"]["RUN_NAME"] == "${{ steps.dispatch.outputs.run_name }}"
    # The deleted timestamp-window / actor-filter / gh-api-user heuristics are gone.
    assert "--created" not in run
    assert "--user" not in run
    assert "gh api user" not in run
    assert "sort_by(.createdAt)" not in run
    # Fail loud when the run cannot be located (non-zero exit).
    assert "exit 1" in run


def test_dispatch_run_name_matches_correlation_id() -> None:
    """The run name the locate step matches is 'CVE rebuild <correlation_id>'."""
    run = _rebuild_step("dispatch")["run"]
    assert "run_name=CVE rebuild ${CORRELATION_ID}" in run


def test_rebuild_waits_with_run_watch_exit_status() -> None:
    """The rebuild job waits via `gh run watch --exit-status` so a failed build fails the job."""
    run = _rebuild_step("watch")["run"]
    assert "gh run watch" in run
    assert "--exit-status" in run


def test_locate_watch_conclusion_use_default_github_token() -> None:
    """locate/watch/conclusion use GITHUB_TOKEN, not the 1h App token: valkey-container is
    public and GITHUB_TOKEN outlives a 130-150 min build."""
    for step_id in ("locate", "watch", "conclusion"):
        gh_token = _rebuild_step(step_id)["env"]["GH_TOKEN"]
        assert gh_token == "${{ secrets.GITHUB_TOKEN }}"
        assert "steps.token.outputs.token" not in gh_token


def test_rebuild_reports_run_url() -> None:
    """The summary and Slack notification surface the located run URL, not just the dispatch."""
    summary = next(s for s in _rebuild_steps() if s.get("name") == "Job summary")
    assert "RUN_URL" in summary["run"]
    compose = _rebuild_step("slack")
    assert "steps.locate.outputs.run_url" in yaml.safe_dump(compose)
    slack = next(s for s in _rebuild_steps() if "notify-slack-action" in s.get("uses", ""))
    assert "steps.slack.outputs.text" in yaml.safe_dump(slack)


def test_rebuild_timeout_outlasts_multiarch_build() -> None:
    """The rebuild timeout exceeds observed ci.yml full-matrix builds (130 to 150 min)."""
    timeout = _load_workflow()["jobs"]["rebuild"]["timeout-minutes"]
    assert isinstance(timeout, int)
    assert 200 <= timeout <= 360


def test_watch_step_timeout_leaves_reporting_headroom() -> None:
    """The watch step has its own timeout below the job timeout, with >= 10 min headroom, so a
    watcher timeout is a STEP failure that still lets the always() reporting steps run."""
    workflow = _load_workflow()
    job_timeout = workflow["jobs"]["rebuild"]["timeout-minutes"]
    step_timeout = _rebuild_step("watch")["timeout-minutes"]
    assert isinstance(step_timeout, int)
    assert step_timeout < job_timeout
    assert job_timeout - step_timeout >= 10


# --------------------------------------------------------------------------- #
# Slack reporting derived from the real downstream outcome
# --------------------------------------------------------------------------- #


def _compose_step() -> dict:
    """Return the 'Compose Slack notification' step (id 'slack')."""
    return _rebuild_step("slack")


def _slack_step() -> dict:
    """Return the notify-slack-action step."""
    return next(s for s in _rebuild_steps() if "notify-slack-action" in s.get("uses", ""))


def test_compose_step_precedes_slack_step() -> None:
    """A compose step (id 'slack') exists and runs before the Slack notify step."""
    steps = _rebuild_steps()
    compose_idx = next(i for i, s in enumerate(steps) if s.get("id") == "slack")
    slack_idx = next(i for i, s in enumerate(steps) if "notify-slack-action" in s.get("uses", ""))
    assert compose_idx < slack_idx


def test_compose_step_reads_downstream_conclusion() -> None:
    """The compose step derives its outputs from the downstream build conclusion."""
    assert "steps.conclusion.outputs.conclusion" in yaml.safe_dump(_compose_step())


def test_slack_status_uses_compose_output_not_job_status() -> None:
    """The Slack step's status comes from the compose step, never job.status."""
    slack = _slack_step()
    assert slack["with"]["status"] == "${{ steps.slack.outputs.status }}"
    assert "job.status" not in yaml.safe_dump(slack)


def test_slack_message_uses_compose_text() -> None:
    """The Slack message body is the composed single-line text output."""
    assert _slack_step()["with"]["message_format"] == "${{ steps.slack.outputs.text }}"


def test_slack_action_pin_and_secret_unchanged() -> None:
    """The Slack action pin and webhook secret name are preserved."""
    slack = _slack_step()
    assert slack["uses"] == "ravsamhq/notify-slack-action@042f29088bb3bdbda5b4ff7b4818466a277fa8f7"
    assert "SLACK_NOTIFICATIONS_WEBHOOK_URL" in yaml.safe_dump(slack["env"])


def test_slack_step_continue_on_error() -> None:
    """A missing webhook must never fail the rebuild job."""
    assert _slack_step()["continue-on-error"] is True


def test_single_slack_step_lives_in_rebuild_job() -> None:
    """The workflow's only Slack step is in the rebuild job."""
    workflow = _load_workflow()
    for job_name, job in workflow["jobs"].items():
        slack_steps = [s for s in job["steps"] if "notify-slack-action" in s.get("uses", "")]
        if job_name == "rebuild":
            assert len(slack_steps) == 1
        else:
            assert slack_steps == []


# --------------------------------------------------------------------------- #
# Concurrency asymmetry (scan cancels; rebuild does not)
# --------------------------------------------------------------------------- #


def test_no_workflow_level_concurrency() -> None:
    """Concurrency is declared per job, not workflow-level: a workflow-level group would cover
    the rebuild job and let a newer scan cancel the watcher."""
    assert "concurrency" not in _load_workflow()


def test_scan_job_concurrency_cancels_in_progress() -> None:
    """The scan job cancels superseded runs (a redo is cheap)."""
    concurrency = _load_workflow()["jobs"]["scan"]["concurrency"]
    assert concurrency["group"] == "cve-scan-scan-${{ github.ref }}"
    assert concurrency["cancel-in-progress"] is True


def test_rebuild_job_concurrency_does_not_cancel_in_progress() -> None:
    """The rebuild job must NOT cancel in-progress: a newer scan cannot kill a container build
    already dispatched and being watched."""
    concurrency = _load_workflow()["jobs"]["rebuild"]["concurrency"]
    assert concurrency["group"] == "cve-scan-rebuild-${{ github.ref }}"
    assert concurrency["cancel-in-progress"] is False


def test_scan_and_rebuild_concurrency_groups_differ() -> None:
    """Distinct groups keep the scan and rebuild jobs independent."""
    workflow = _load_workflow()
    scan_group = workflow["jobs"]["scan"]["concurrency"]["group"]
    rebuild_group = workflow["jobs"]["rebuild"]["concurrency"]["group"]
    assert scan_group != rebuild_group


# --------------------------------------------------------------------------- #
# Per-entry markers: record without failing on survivors, fail closed on error
# --------------------------------------------------------------------------- #


def _verify_step(step_id: str) -> dict:
    """Return the verify step with the given id."""
    return next(s for s in _verify_steps() if s.get("id") == step_id)


def _collect_steps() -> list[dict]:
    """Return the collect job's steps."""
    return _job_steps("collect")


def test_verify_records_outcome_without_failing_on_survivors() -> None:
    """The record step maps verify_candidate's exit code to an outcome and does NOT abort on
    exit 1 (survivors): it uses `set -uo pipefail`, never `set -e`, and records survivors so
    a surviving CVE on one line never fails the entry."""
    run = _verify_step("record")["run"]
    # No `set -e`: a survivor (exit 1) is recorded, not a step failure.
    assert "set -uo pipefail" in run
    assert "set -euo pipefail" not in run
    assert "set -e " not in run
    # Exit-code mapping: 0 verified, 1 survivors, anything else error.
    assert "0) outcome=verified" in run
    assert "1) outcome=survivors" in run
    assert "*) outcome=error" in run


def test_verify_exit_code_2_fails_entry_loudly() -> None:
    """A fail-closed error (verify_candidate exit 2, mapped to outcome=error, or a failed
    build) fails the matrix entry via a dedicated step; survivors (exit 1) do NOT, because the
    step keys strictly on outcome == 'error'."""
    fail_step = next(
        s for s in _verify_steps() if s.get("name") == "Fail entry on verification error"
    )
    assert "steps.record.outputs.outcome == 'error'" in fail_step["if"]
    # It must not fire on survivors.
    assert "survivors" not in fail_step["if"]
    assert "exit 1" in fail_step["run"]


def test_failed_build_is_recorded_as_error() -> None:
    """A failed or skipped candidate build is treated as a fail-closed error, never silently
    skipped: the record step keys on the build step's outcome."""
    run = _verify_step("record")["run"]
    assert 'BUILD_OUTCOME' in run
    assert '"$BUILD_OUTCOME" != "success"' in run
    assert "rc=2" in run


def test_verify_marker_uploaded_before_fail_step() -> None:
    """The marker upload runs before the fail step (and with always()), so an errored entry
    still contributes its marker to collect instead of vanishing."""
    steps = _verify_steps()
    upload_idx = next(
        i for i, s in enumerate(steps) if "upload-artifact" in s.get("uses", "")
    )
    fail_idx = next(
        i
        for i, s in enumerate(steps)
        if s.get("name") == "Fail entry on verification error"
    )
    assert upload_idx < fail_idx
    upload = steps[upload_idx]
    assert upload["if"] == "always()"
    # Per-entry unique artifact name so parallel matrix entries never collide.
    assert upload["with"]["name"] == (
        "cve-verify-marker-${{ matrix.line }}-${{ matrix.variant }}-"
        "${{ steps.record.outputs.slug }}"
    )
    assert upload["with"]["if-no-files-found"] == "error"


def test_marker_artifact_actions_are_sha_pinned() -> None:
    """The upload/download-artifact actions the marker flow adds are present and SHA-pinned."""
    text = _raw_text()
    for line in text.splitlines():
        match = _USE_RE.search(line)
        if not match:
            continue
        action, ref = match.groups()
        if action in ("actions/upload-artifact", "actions/download-artifact"):
            assert _SHA_RE.fullmatch(ref), f"{action}@{ref} is not SHA-pinned"
    assert "actions/upload-artifact@" in text
    assert "actions/download-artifact@" in text


# --------------------------------------------------------------------------- #
# Collect job: aggregate markers into the dispatchable line set
# --------------------------------------------------------------------------- #


def test_collect_job_exists_and_needs_verify() -> None:
    """A collect job exists and depends on verify (and scan)."""
    workflow = _load_workflow()
    assert "collect" in workflow["jobs"]
    needs = workflow["jobs"]["collect"]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert "verify" in needs
    assert "scan" in needs


def test_collect_runs_even_when_a_verify_entry_failed() -> None:
    """collect must still run when a verify entry errored (failing the verify job), so it can
    surface the error and fail closed rather than be skipped by the failed `needs`. It carries
    the same scan guards so a clean week skips it entirely."""
    if_expr = _load_workflow()["jobs"]["collect"]["if"]
    assert "!cancelled()" in if_expr
    assert "needs.scan.outputs.fixable == 'true'" in if_expr
    assert "github.repository == 'valkey-io/valkey-ci-agent'" in if_expr
    assert "github.ref == 'refs/heads/main'" in if_expr
    assert "github.event.inputs.dry_run != 'true'" in if_expr


def test_collect_outputs_verified_versions_and_fixable() -> None:
    """collect exposes verified_versions, fixable, and the per-architecture arch_report."""
    outputs = _load_workflow()["jobs"]["collect"]["outputs"]
    assert outputs["verified_versions"] == "${{ steps.aggregate.outputs.verified_versions }}"
    assert outputs["fixable"] == "${{ steps.aggregate.outputs.fixable }}"
    assert outputs["arch_report"] == "${{ steps.aggregate.outputs.arch_report }}"


def test_collect_downloads_markers_and_aggregates() -> None:
    """collect downloads every per-entry marker and runs the aggregation CLI on them."""
    steps = _collect_steps()
    download = next(s for s in steps if "download-artifact" in s.get("uses", ""))
    assert download["with"]["pattern"] == "cve-verify-marker-*"
    assert download["with"]["merge-multiple"] is True
    aggregate = next(s for s in steps if s.get("id") == "aggregate")
    assert "collect_verification" in aggregate["run"]
    assert "--markers-dir" in aggregate["run"]


def test_collect_receives_expected_matrix() -> None:
    """collect reconciles markers against the scan's expected matrix: the aggregate step is
    handed needs.scan.outputs.matrix via --expected-matrix, so a leg that died without
    uploading a marker is caught as missing (fail closed) instead of silently dropped."""
    aggregate = next(s for s in _collect_steps() if s.get("id") == "aggregate")
    assert aggregate["env"]["EXPECTED_MATRIX"] == "${{ needs.scan.outputs.matrix }}"
    assert "--expected-matrix" in aggregate["run"]
    # collect needs scan so needs.scan.outputs.matrix resolves.
    needs = _load_workflow()["jobs"]["collect"]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert "scan" in needs


# --------------------------------------------------------------------------- #
# Rebuild dispatches the VERIFIED set, not the scan's raw candidate list
# --------------------------------------------------------------------------- #


def test_dispatch_uses_collect_verified_versions_not_scan_versions() -> None:
    """The dispatch sends collect's verified_versions, and does NOT fall back to the scan's
    raw candidate list (the latent all-or-nothing mismatch this change fixes)."""
    dispatch = _rebuild_step("dispatch")
    assert dispatch["env"]["VERSIONS"] == "${{ needs.collect.outputs.verified_versions }}"
    assert "needs.scan.outputs.versions" not in yaml.safe_dump(dispatch)


def test_rebuild_reports_skipped_lines() -> None:
    """The summary and Slack composition state which lines were skipped (scan minus verified)
    because verification did not prove them fixed."""
    summary = next(s for s in _rebuild_steps() if s.get("name") == "Job summary")
    assert "SKIPPED" in summary["run"]
    assert "Skipped" in summary["run"]
    compose = _rebuild_step("slack")
    assert "SKIPPED" in compose["run"]


# --------------------------------------------------------------------------- #
# Per-architecture reporting: the dispatched set is any-architecture, so both
# the summary and the (single-line) Slack message must state, per dispatched
# line, which architectures were proven fixed and which remain vulnerable.
# --------------------------------------------------------------------------- #


def test_collect_arch_report_feeds_rebuild_summary_and_slack() -> None:
    """The rebuild summary and Slack steps consume collect's arch_report output."""
    summary = next(s for s in _rebuild_steps() if s.get("name") == "Job summary")
    compose = _rebuild_step("slack")
    assert summary["env"]["ARCH_REPORT"] == "${{ needs.collect.outputs.arch_report }}"
    assert compose["env"]["ARCH_REPORT"] == "${{ needs.collect.outputs.arch_report }}"


def test_rebuild_summary_renders_per_architecture_status() -> None:
    """The Job summary renders each dispatched line's proven-fixed and still-vulnerable
    architectures from arch_report (jq over the machine-readable report)."""
    summary = next(s for s in _rebuild_steps() if s.get("name") == "Job summary")
    run = summary["run"]
    assert "ARCH_REPORT" in run
    assert "jq" in run
    assert "proven fixed on" in run
    assert "still vulnerable" in run


def test_rebuild_summary_makes_no_blanket_fix_claim() -> None:
    """The summary must not claim a dispatched line is fully fixed: the old
    '(proven fixable by our own candidate build)' blanket wording is gone."""
    summary = next(s for s in _rebuild_steps() if s.get("name") == "Job summary")
    assert "proven fixable by our own candidate build" not in summary["run"]


def test_slack_text_includes_per_architecture_note() -> None:
    """The composed Slack text carries a per-architecture note derived from arch_report."""
    compose = _rebuild_step("slack")
    run = compose["run"]
    assert "ARCH_REPORT" in run
    assert "jq" in run
    assert "ARCH_NOTE" in run
    assert "Per-arch:" in run


def test_slack_text_stays_single_line() -> None:
    """The Slack text output is assembled to stay single-line: the per-arch note is joined
    with ' | ' (never a newline), because it is passed via GITHUB_OUTPUT."""
    compose = _rebuild_step("slack")
    run = compose["run"]
    # The jq that builds the per-arch note joins entries on ' | ', not newlines.
    assert 'join(" | ")' in run
    # The text output is a single echo line into GITHUB_OUTPUT.
    assert 'echo "text=${TEXT}"' in run


def test_verify_job_skips_on_empty_matrix() -> None:
    """An empty matrix must skip the verify job, not fail it with an opaque error.

    `strategy.matrix.include: fromJSON(...)` errors out when handed `[]`, which
    would surface as a confusing GitHub matrix error rather than a clean skip.
    """
    condition = _load_workflow()["jobs"]["verify"]["if"]
    assert "needs.scan.outputs.matrix != '[]'" in condition
