from pathlib import Path

import yaml

from scripts.ai.runtime import AGENT_PROFILES

_POLL = Path(".github/workflows/release-notes-review-poll.yml")
_HANDLER = Path(".github/workflows/release-notes-review.yml")


def _workflow(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _step(workflow: dict, job: str, name: str) -> dict:
    return next(
        step
        for step in workflow["jobs"][job]["steps"]
        if step.get("name") == name
    )


def test_hourly_poller_reuses_one_sustained_runner_and_token() -> None:
    workflow = _workflow(_POLL)
    job = workflow["jobs"]["poll"]
    token = _step(workflow, "poll", "Generate App token")
    run = _step(workflow, "poll", "Poll and dispatch")

    assert workflow["on"]["schedule"] == [{"cron": "0 * * * *"}]
    assert job["timeout-minutes"] == "65"
    assert job["concurrency"]["group"] == "release-notes-review-poll"
    assert token["with"]["repositories"].splitlines() == [
        "valkey",
        "valkey-search",
        "valkey-json",
        "valkey-bloom",
        "valkey-ci-agent",
    ]
    assert {
        key: value
        for key, value in token["with"].items()
        if key.startswith("permission-")
    } == {
        "permission-actions": "write",
        "permission-members": "read",
        "permission-pull-requests": "write",
        "permission-metadata": "read",
    }
    assert "'300'" in run["env"]["RELEASE_NOTES_REVIEW_POLL_INTERVAL_SECONDS"]
    assert "'3300'" in run["env"]["RELEASE_NOTES_REVIEW_POLL_DURATION_SECONDS"]
    assert run["run"] == "python3 -m scripts.release_notes.review_poll"


def test_handler_has_only_pr_identity_inputs_and_scoped_write_token() -> None:
    workflow = _workflow(_HANDLER)
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    job = workflow["jobs"]["address"]
    token = _step(workflow, "address", "Generate target token")
    run = _step(workflow, "address", "Address review comments")

    assert list(inputs) == ["repo", "pr", "head_sha", "status_comment_id"]
    assert "github.event.sender.id == 284117741" in job["if"]
    assert job["concurrency"]["group"] == (
        "release-notes-review-${{ inputs.repo }}-${{ inputs.pr }}"
    )
    assert job["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    assert token["with"]["repositories"] == "${{ inputs.repo }}"
    assert {
        key: value
        for key, value in token["with"].items()
        if key.startswith("permission-")
    } == {
        "permission-members": "read",
        "permission-contents": "write",
        "permission-pull-requests": "write",
        "permission-metadata": "read",
    }
    assert run["run"] == "python3 -m scripts.release_notes.review_handler"


def test_review_editor_uses_one_edit_only_ai_profile() -> None:
    profile = AGENT_PROFILES["release_notes_review_edit_only"]

    assert profile.writes_allowed is True
    assert profile.allowed_tools == "Read,Edit,MultiEdit,Grep,Glob"
    assert profile.disallowed_tools == "Bash,Write"
    assert "release_notes_review_readonly" not in AGENT_PROFILES
