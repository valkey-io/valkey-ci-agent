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
    slack = next(s for s in _rebuild_steps() if "notify-slack-action" in s.get("uses", ""))
    assert "steps.locate.outputs.run_url" in yaml.safe_dump(slack)


def test_rebuild_timeout_outlasts_multiarch_build() -> None:
    """The rebuild timeout exceeds observed ci.yml full-matrix builds (130 to 150 min)."""
    timeout = _load_workflow()["jobs"]["rebuild"]["timeout-minutes"]
    assert isinstance(timeout, int)
    assert timeout >= 200
    assert timeout <= 360
