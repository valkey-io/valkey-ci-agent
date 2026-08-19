"""Semantic guards for .github/workflows/cve-scan.yml.

Parses the workflow with yaml and asserts the security-relevant invariants
that keep the automatic CVE rebuild fail-closed: the rebuild job only runs on
the canonical repo's main branch with a verified-fixable, non-empty version
list and dry-run suppressed; it dispatches through the protected
``cve-rebuild-dispatch`` environment; the scan job mints no App token and holds
no write permissions; the manual dry_run input defaults to True; no
AUTOMATION_PAT is referenced; and every external action is SHA-pinned.

It also asserts the rebuild job verifies the downstream build (not just the
dispatch): the dispatch step records a UTC timestamp, a polling step locates
the triggered valkey-container run, ``gh run watch --exit-status`` waits for
it so a failed build fails the job, and the job's ``timeout-minutes`` is
generous enough (>= 90) to outlast a multi-arch build.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_WORKFLOW = Path(".github/workflows/cve-scan.yml")

_USE_RE = re.compile(r"uses:\s*([^@\s]+)@([^#\s]+)")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


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


def test_rebuild_job_if_has_all_guards() -> None:
    """The rebuild job's `if` fails closed on repo, ref, verified-fixable, versions, and dry-run."""
    workflow = _load_workflow()
    if_expr = workflow["jobs"]["rebuild"]["if"]

    assert "github.repository == 'valkey-io/valkey-ci-agent'" in if_expr
    assert "github.ref == 'refs/heads/main'" in if_expr
    # Verified-fixable + non-empty version list required.
    assert "needs.scan.outputs.fixable == 'true'" in if_expr
    assert "needs.scan.outputs.versions != ''" in if_expr
    # Dry-run suppression: a dry-run manual dispatch must not dispatch a rebuild.
    assert "github.event.inputs.dry_run != 'true'" in if_expr
    assert "github.event_name != 'workflow_dispatch'" in if_expr


def test_rebuild_job_uses_protected_environment() -> None:
    """The rebuild job runs in the credential-scoped cve-rebuild-dispatch environment."""
    workflow = _load_workflow()
    assert workflow["jobs"]["rebuild"]["environment"] == "cve-rebuild-dispatch"


def test_no_automation_pat_referenced() -> None:
    """AUTOMATION_PAT must not appear anywhere; credentials come from the App token only."""
    assert "AUTOMATION_PAT" not in _raw_text()


def test_scan_job_has_no_app_token_step() -> None:
    """The scan job mints no GitHub App token (only the rebuild job may)."""
    workflow = _load_workflow()
    scan_steps = workflow["jobs"]["scan"]["steps"]
    uses = [step.get("uses", "") for step in scan_steps]
    assert not any("create-github-app-token" in u for u in uses)


def test_scan_job_has_no_write_permissions() -> None:
    """Neither the scan job nor the workflow default grants issues/actions write."""
    workflow = _load_workflow()

    workflow_perms = workflow.get("permissions", {})
    if isinstance(workflow_perms, dict):
        assert workflow_perms.get("issues") != "write"
        assert workflow_perms.get("actions") != "write"

    scan_perms = workflow["jobs"]["scan"].get("permissions", {})
    if isinstance(scan_perms, dict):
        assert scan_perms.get("issues") != "write"
        assert scan_perms.get("actions") != "write"


def test_workflow_dispatch_dry_run_defaults_true() -> None:
    """The manual dry_run input defaults to True so an operator must opt in to a real rebuild."""
    workflow = _load_workflow()
    dry_run = _on_section(workflow)["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run["default"] is True


def test_all_external_actions_are_sha_pinned() -> None:
    """Every non-local `uses:` is pinned to a 40-hex commit SHA."""
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


def _rebuild_steps() -> list[dict]:
    """Return the rebuild job's steps."""
    return _load_workflow()["jobs"]["rebuild"]["steps"]


def _step_run_text() -> str:
    """Concatenate every rebuild step's `run` script for substring assertions."""
    return "\n".join(step.get("run", "") for step in _rebuild_steps())


def test_rebuild_dispatch_step_records_timestamp() -> None:
    """The dispatch step captures a UTC timestamp before dispatching and exposes it as an output."""
    dispatch = next(s for s in _rebuild_steps() if s.get("id") == "dispatch")
    run = dispatch["run"]
    # A UTC timestamp is captured ...
    assert "date -u" in run
    assert "DISPATCH_TS=" in run
    # ... and exported so the locating step can bound its search.
    assert "dispatch_ts=" in run
    # Captured before the dispatch call, not after.
    assert run.index("DISPATCH_TS=") < run.index("gh workflow run")


def test_rebuild_has_polling_locate_step() -> None:
    """A step locates the triggered valkey-container run created at/after the dispatch timestamp."""
    locate = next((s for s in _rebuild_steps() if s.get("id") == "locate"), None)
    assert locate is not None, "expected a 'locate' step that finds the triggered run"
    run = locate["run"]
    assert "gh run list" in run
    assert "--created" in run
    assert "DISPATCH_TS" in run
    # Oldest-in-window selection so an unrelated later dispatch is not grabbed.
    assert "sort_by(.createdAt)" in run
    # Fail-loud when the run cannot be located (non-zero exit).
    assert "exit 1" in run


def test_rebuild_waits_with_run_watch_exit_status() -> None:
    """The rebuild job waits for the run via `gh run watch --exit-status` so a failed build fails the job."""
    watch = next((s for s in _rebuild_steps() if s.get("id") == "watch"), None)
    assert watch is not None, "expected a 'watch' step that waits for the run"
    run = watch["run"]
    assert "gh run watch" in run
    assert "--exit-status" in run


def test_rebuild_reports_run_url() -> None:
    """The summary and Slack notification surface the located run URL, not just the dispatch."""
    summary = next(s for s in _rebuild_steps() if s.get("name") == "Job summary")
    assert "RUN_URL" in summary["run"]
    # The run URL reaches Slack through the compose step, which feeds the Slack
    # message body via steps.slack.outputs.text.
    compose = next(s for s in _rebuild_steps() if s.get("id") == "slack")
    assert "steps.locate.outputs.run_url" in yaml.safe_dump(compose)
    slack = next(s for s in _rebuild_steps() if "notify-slack-action" in s.get("uses", ""))
    assert "steps.slack.outputs.text" in yaml.safe_dump(slack)


def test_rebuild_timeout_outlasts_multiarch_build() -> None:
    """The rebuild timeout exceeds observed ci.yml full-matrix builds (130 to 150 min)."""
    timeout = _load_workflow()["jobs"]["rebuild"]["timeout-minutes"]
    assert isinstance(timeout, int)
    assert timeout >= 200
    assert timeout <= 360


def _compose_step() -> dict:
    """Return the 'Compose Slack notification' step (id 'slack')."""
    return next(s for s in _rebuild_steps() if s.get("id") == "slack")


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


def _rebuild_step(step_id: str) -> dict:
    """Return the rebuild step with the given id."""
    return next(s for s in _rebuild_steps() if s.get("id") == step_id)


def test_no_workflow_level_concurrency() -> None:
    """Concurrency is declared per job, not workflow-level (fix 4): a workflow-level
    group would cover the rebuild job and let a newer scan cancel the watcher."""
    workflow = _load_workflow()
    assert "concurrency" not in workflow


def test_scan_job_concurrency_cancels_in_progress() -> None:
    """The scan job cancels superseded runs (a redo is cheap)."""
    concurrency = _load_workflow()["jobs"]["scan"]["concurrency"]
    assert concurrency["group"] == "cve-scan-scan-${{ github.ref }}"
    assert concurrency["cancel-in-progress"] is True


def test_rebuild_job_concurrency_does_not_cancel_in_progress() -> None:
    """The rebuild job must NOT cancel in-progress: a newer scan cannot kill a
    container build already dispatched and being watched (fix 4)."""
    concurrency = _load_workflow()["jobs"]["rebuild"]["concurrency"]
    assert concurrency["group"] == "cve-scan-rebuild-${{ github.ref }}"
    assert concurrency["cancel-in-progress"] is False


def test_scan_and_rebuild_concurrency_groups_differ() -> None:
    """Distinct groups keep the scan and rebuild jobs independent."""
    workflow = _load_workflow()
    scan_group = workflow["jobs"]["scan"]["concurrency"]["group"]
    rebuild_group = workflow["jobs"]["rebuild"]["concurrency"]["group"]
    assert scan_group != rebuild_group


def test_dispatch_step_uses_app_token() -> None:
    """The dispatch step (the sole step needing actions:write on another repo)
    still uses the short-lived App token (fix 1)."""
    dispatch = _rebuild_step("dispatch")
    assert dispatch["env"]["GH_TOKEN"] == "${{ steps.token.outputs.token }}"


def test_locate_watch_conclusion_use_default_github_token() -> None:
    """locate/watch/conclusion use GITHUB_TOKEN, not the 1h App token (fix 1):
    valkey-container is public and GITHUB_TOKEN outlives a 130-150 min build."""
    for step_id in ("locate", "watch", "conclusion"):
        gh_token = _rebuild_step(step_id)["env"]["GH_TOKEN"]
        assert gh_token == "${{ secrets.GITHUB_TOKEN }}"
        assert "steps.token.outputs.token" not in gh_token


def test_dispatch_step_captures_bot_login() -> None:
    """The dispatch step exposes the bot login as an output for actor filtering (fix 3)."""
    run = _rebuild_step("dispatch")["run"]
    assert "gh api user --jq .login" in run
    assert "bot_login=" in run


def test_locate_step_filters_by_actor() -> None:
    """The locate step narrows the run window to the dispatching bot via --user (fix 3)."""
    locate = _rebuild_step("locate")
    run = locate["run"]
    assert "--user" in run
    # The bot login flows in from the dispatch step's output.
    assert locate["env"]["BOT_LOGIN"] == "${{ steps.dispatch.outputs.bot_login }}"
