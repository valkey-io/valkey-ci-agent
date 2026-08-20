"""High-value architecture guards for the CVE workflow."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_PATH = Path(__file__).parents[1] / ".github/workflows/cve-scan.yml"
_USE = re.compile(r"uses:\s*([^@\s]+)@([^#\s]+)")


def _workflow() -> dict:
    return yaml.safe_load(_PATH.read_text(encoding="utf-8"))


def _job(name: str) -> dict:
    return _workflow()["jobs"][name]


def _step(job: str, *, step_id: str | None = None, name: str | None = None) -> dict:
    return next(
        item
        for item in _job(job)["steps"]
        if (step_id is None or item.get("id") == step_id) and (name is None or item.get("name") == name)
    )


def test_safe_trigger_permissions_and_pins() -> None:
    workflow = _workflow()
    on = workflow.get("on", workflow[True])
    assert on["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    assert workflow["permissions"] == {"contents": "read"}
    assert "AUTOMATION_PAT" not in _PATH.read_text(encoding="utf-8")
    for action, ref in _USE.findall(_PATH.read_text(encoding="utf-8")):
        if not action.startswith("./"):
            assert re.fullmatch(r"[0-9a-f]{40}", ref), f"unpinned {action}@{ref}"


def test_scan_emits_one_plan_contract() -> None:
    outputs = _job("scan")["outputs"]
    assert set(outputs) == {"plan"}
    assert outputs["plan"] == "${{ steps.scan.outputs.plan }}"
    assert not any("create-github-app-token" in step.get("uses", "") for step in _job("scan")["steps"])


def test_verify_consumes_plan_as_matrix_with_leg_cves() -> None:
    verify = _job("verify")
    assert verify["strategy"]["matrix"]["include"] == "${{ fromJSON(needs.scan.outputs.plan) }}"
    assert verify["strategy"]["fail-fast"] is False
    assert verify["strategy"]["max-parallel"] > 1
    assert "needs.scan.outputs.plan != '[]'" in verify["if"]
    record = _step("verify", step_id="record")
    assert record["env"]["CVES_JSON"] == "${{ toJSON(matrix.cves) }}"
    assert "--cves-json" in record["run"]
    assert "--targets" not in record["run"]


def test_verify_uses_shared_local_only_single_platform_build() -> None:
    verify = _job("verify")
    checkout = _step("verify", name="Checkout valkey-container")
    assert checkout["with"]["repository"] == "valkey-io/valkey-container"
    assert checkout["with"]["path"] == "container"
    build = next(step for step in verify["steps"] if step.get("uses") == "./container/.github/actions/build-image")
    inputs = build["with"]
    assert inputs["context"] == "./container"
    assert inputs["dockerfile"] == "./container/${{ matrix.line }}/${{ matrix.variant }}/Dockerfile"
    assert inputs["platforms"] == "${{ matrix.platform }}"
    assert inputs["push"] is False
    assert inputs["load"] is True
    dumped = yaml.safe_dump(verify)
    assert "secrets." not in dumped
    assert "login-action" not in dumped


def test_verify_records_every_outcome_with_unique_marker() -> None:
    steps = _job("verify")["steps"]
    record = _step("verify", step_id="record")
    run = record["run"]
    assert "set -uo pipefail" in run
    assert "0) outcome=verified" in run
    assert "1) outcome=survivors" in run
    assert "*) outcome=error" in run
    assert "markers/${LINE}-${VARIANT}-${SLUG}.json" in run

    upload_index = next(i for i, item in enumerate(steps) if "upload-artifact" in item.get("uses", ""))
    fail_index = next(i for i, item in enumerate(steps) if item.get("name") == "Fail entry on verification error")
    upload = steps[upload_index]
    assert upload_index < fail_index
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "markers/*.json"
    assert "steps.record.outputs.slug" in upload["with"]["name"]
    assert "steps.record.outputs.outcome == 'error'" in steps[fail_index]["if"]


def test_collect_reconciles_markers_against_same_plan() -> None:
    collect = _job("collect")
    assert set(collect["needs"]) == {"scan", "verify"}
    assert "!cancelled()" in collect["if"]
    download = next(step for step in collect["steps"] if "download-artifact" in step.get("uses", ""))
    assert download["with"]["pattern"] == "cve-verify-marker-*"
    assert download["with"]["merge-multiple"] is True
    aggregate = _step("collect", step_id="aggregate")
    assert aggregate["env"]["PLAN"] == "${{ needs.scan.outputs.plan }}"
    assert "--plan" in aggregate["run"]
    assert set(collect["outputs"]) == {"verified_versions", "arch_report"}


def test_verify_concurrency_does_not_collide_between_matrix_legs() -> None:
    concurrency = _job("verify")["concurrency"]
    group = concurrency["group"]
    assert "${{ matrix.line }}" in group
    assert "${{ matrix.variant }}" in group
    assert "${{ matrix.platform }}" in group
    assert concurrency["cancel-in-progress"] is False


def test_repo_branch_and_dry_run_guard_all_mutating_paths() -> None:
    for name in ("verify", "collect", "rebuild"):
        condition = _job(name)["if"]
        assert "github.repository == 'valkey-io/valkey-ci-agent'" in condition
        assert "github.ref == 'refs/heads/main'" in condition
        assert "github.event.inputs.dry_run != 'true'" in condition
    rebuild = _job("rebuild")
    assert "needs.collect.outputs.verified_versions != ''" in rebuild["if"]
    assert rebuild["environment"] == "cve-rebuild-dispatch"


def test_dispatch_credential_is_scoped_to_dispatch_step() -> None:
    token = _step("rebuild", step_id="token")
    assert token["with"]["repositories"] == "valkey-container"
    assert token["with"]["permission-actions"] == "write"
    dispatch = _step("rebuild", step_id="dispatch")
    assert dispatch["env"]["GH_TOKEN"] == "${{ steps.token.outputs.token }}"
    for step_id in ("locate", "watch", "conclusion"):
        assert _step("rebuild", step_id=step_id)["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"


def test_dispatch_is_correlated_and_real_result_is_waited_for() -> None:
    dispatch = _step("rebuild", step_id="dispatch")["run"]
    assert "${{ github.run_id }}-${{ github.run_attempt }}" in dispatch
    assert "gh workflow run ci.yml" in dispatch
    assert '--field "version=${VERSIONS}"' in dispatch
    assert '--field "correlation_id=${CORRELATION_ID}"' in dispatch

    locate = _step("rebuild", step_id="locate")["run"]
    assert "displayTitle == $n" in locate
    assert "--workflow ci.yml" in locate
    assert "--branch mainline" in locate
    assert "--created" not in locate
    assert "--exit-status" in _step("rebuild", step_id="watch")["run"]
    assert "gh run view" in _step("rebuild", step_id="conclusion")["run"]


def test_reports_use_collects_per_architecture_result() -> None:
    report = _step("rebuild", step_id="report")
    assert report["env"]["ARCH_REPORT"] == "${{ needs.collect.outputs.arch_report }}"
    workflow = yaml.safe_dump(_workflow())
    assert "needs.scan.outputs.versions" not in workflow
    assert "SCAN_VERSIONS" not in report["env"]
    assert ".dispatched == false" in report["run"]
    assert "proven fixed on" in report["run"]
    assert "still vulnerable" in report["run"]
    assert "Per-arch:" in report["run"]
    assert 'join(" | ")' in report["run"]
    notify = _step("rebuild", name="Notify Slack about rebuild")
    assert notify["with"]["status"] == "${{ steps.report.outputs.status }}"


def test_rebuild_dispatches_only_collects_verified_versions() -> None:
    rebuild = _job("rebuild")
    assert set(rebuild["needs"]) == {"scan", "collect"}
    dispatch = _step("rebuild", step_id="dispatch")
    assert dispatch["env"]["VERSIONS"] == "${{ needs.collect.outputs.verified_versions }}"
    assert "needs.scan.outputs.versions" not in yaml.safe_dump(dispatch)
