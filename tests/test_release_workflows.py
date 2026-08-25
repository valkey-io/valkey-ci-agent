from pathlib import Path

import yaml


def _load(name: str) -> dict:
    path = Path(".github/workflows") / name
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_prepare_opens_dashboard_and_notes_pr_in_parallel() -> None:
    workflow = _load("release-prepare.yml")
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["dry_run"]["default"] == "false"
    assert list(workflow["jobs"]) == ["authorize-start", "derive", "cut-notes", "tracker"]
    assert inputs["initiator"]["required"] == "true"
    assert workflow["jobs"]["cut-notes"]["with"]["release_owner"] == "${{ inputs.initiator }}"
    assert workflow["jobs"]["derive"]["needs"] == "authorize-start"
    assert "VALKEY_RELEASE_START_ACTOR" in str(workflow["jobs"]["authorize-start"])
    assert workflow["jobs"]["derive"]["environment"] == "release-control"
    assert workflow["jobs"]["cut-notes"]["uses"] == "./.github/workflows/release-notes-cut.yml"
    assert workflow["jobs"]["cut-notes"]["secrets"] == "inherit"
    assert workflow["jobs"]["tracker"]["needs"] == "derive"
    assert workflow["jobs"]["tracker"]["environment"] == "release-control"
    assert "permission-issues" in str(workflow["jobs"]["tracker"])


def test_publish_waits_for_qualification_before_protected_write() -> None:
    workflow = _load("release-publish.yml")
    jobs = workflow["jobs"]
    assert list(jobs) == ["validate", "qualify", "approval-plan", "publish", "onboard-backports"]
    assert jobs["qualify"]["needs"] == "validate"
    assert jobs["publish"]["needs"] == ["validate", "qualify", "approval-plan"]
    assert "automation_sha" in str(jobs["approval-plan"])
    assert jobs["publish"]["environment"] == "release"
    assert "VALKEY_RELEASE_PUBLISH_APP_PRIVATE_KEY" in str(jobs["publish"])
    assert "VALKEY_RELEASE_PUBLISH_APP_PRIVATE_KEY" not in str(jobs["validate"])
    assert "VALKEY_RELEASE_PUBLISH_APP_PRIVATE_KEY" not in str(jobs["qualify"])
    assert "TRIGGERING_ACTOR" in str(jobs["publish"])
    assert '"$APPROVER" != "$TRIGGERING_ACTOR"' in str(jobs["publish"])
    assert "release must disable admin bypass" in str(jobs["publish"])
    assert 'if has("can_admins_bypass") then .can_admins_bypass else true end' in str(jobs["publish"])
    assert ".can_admins_bypass // true" not in str(jobs["publish"])
    assert jobs["onboard-backports"]["continue-on-error"] == "true"


def test_publish_qualification_is_exact_and_synchronous() -> None:
    job = _load("release-publish.yml")["jobs"]["qualify"]
    assert job["uses"] == "valkey-io/valkey-release-automation/.github/workflows/qualify-release.yml@main"
    assert job["with"]["version"] == "${{ needs.validate.outputs.version }}"
    assert job["with"]["source_sha"] == "${{ needs.validate.outputs.sha }}"
    assert "automation_repo" not in job["with"]
    assert "automation_ref" not in job["with"]
    assert "github.run_id" in job["with"]["request_id"]
    assert "steps" not in job
    assert "VALKEY_RELEASE_CONTROL_APP_PRIVATE_KEY" not in str(job)


def test_no_controller_loop_workflows_remain() -> None:
    names = {path.name for path in Path(".github/workflows").glob("release-*.yml")}
    assert "release-reconcile.yml" not in names
    assert "release-adopt.yml" not in names
    assert "release-start.yml" not in names
    assert {"release-prepare.yml", "release-progress.yml", "release-publish.yml"} <= names


def test_progress_watcher_is_narrow_and_serialized() -> None:
    workflow = _load("release-progress.yml")
    assert workflow["on"]["schedule"][0]["cron"] == "0 * * * *"
    assert "workflow_run" not in workflow["on"]
    assert workflow["permissions"] == {"actions": "write", "contents": "read"}
    assert workflow["concurrency"]["group"] == "release-progress"
    job = workflow["jobs"]["sync"]
    assert job["environment"] == "release-control"
    assert job["timeout-minutes"] == "70"
    assert "scripts.release.tracker sync" in str(job)
    assert "--poll-interval-seconds" in str(job)
    assert "'300'" in str(job) and "'3300'" in str(job)
    assert "VALKEY_RELEASE_PUBLISH_APP_PRIVATE_KEY" not in str(job)
    steps = {step.get("id"): step for step in job["steps"] if step.get("id")}
    assert steps["target-token"]["with"]["repositories"] == "valkey"
    assert steps["target-token"]["with"]["permission-issues"] == "write"
    assert steps["automation-token"]["with"]["repositories"] == "valkey-release-automation"
    assert "permission-issues" not in steps["automation-token"]["with"]
    assert "AUTOMATION_GITHUB_TOKEN" in str(job)
