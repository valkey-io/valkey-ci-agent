"""Fail-closed required-check validation for one exact candidate commit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scripts.common.github_client import retry_github_call
from scripts.release.models import ReleasePolicy


@dataclass(frozen=True)
class CandidateCheck:
    name: str
    status: str
    conclusion: str | None
    url: str

    @property
    def passed(self) -> bool:
        return self.status == "completed" and self.conclusion == "success"


@dataclass(frozen=True)
class CandidateCI:
    workflow_url: str
    workflow_status: str
    workflow_conclusion: str | None
    suite_id: int | None
    checks: tuple[CandidateCheck, ...]

    @property
    def passed_count(self) -> int:
        return sum(check.passed for check in self.checks)

    @property
    def ready(self) -> bool:
        return (
            self.workflow_status == "completed"
            and bool(self.suite_id)
            and all(check.passed for check in self.checks)
        )

    @property
    def state(self) -> str:
        if self.workflow_status == "unavailable":
            return "unavailable"
        if self.workflow_status == "missing":
            return "missing"
        if self.workflow_status != "completed":
            return "running"
        if self.ready:
            return "passed"
        if self.workflow_conclusion not in {None, "success"} or any(
            check.status == "completed" and check.conclusion not in {None, "success"}
            for check in self.checks
        ):
            return "failed"
        return "blocked"


def evaluate_candidate_ci(repo: Any, policy: ReleasePolicy, sha: str) -> CandidateCI:
    """Return the latest configured workflow and required-check state for *sha*."""
    workflow_runs = retry_github_call(
        lambda: list(repo.get_workflow_runs(head_sha=sha)),
        retries=2,
        description=f"list workflow runs on {sha[:12]}",
    )
    matching_runs = [run for run in workflow_runs if (run.path or "").rsplit("/", 1)[-1] == policy.checks_workflow]
    if not matching_runs:
        return CandidateCI(
            workflow_url="",
            workflow_status="missing",
            workflow_conclusion=None,
            suite_id=None,
            checks=tuple(
                CandidateCheck(name=name, status="missing", conclusion=None, url="")
                for name in policy.required_checks
            ),
        )

    latest_workflow = max(matching_runs, key=_workflow_order)
    suite_id = latest_workflow.check_suite_id
    latest: dict[str, Any] = {}
    if suite_id:
        commit = retry_github_call(
            lambda: repo.get_commit(sha),
            retries=2,
            description=f"get candidate {sha[:12]}",
        )
        check_runs = retry_github_call(
            lambda: list(commit.get_check_runs()),
            retries=2,
            description=f"list checks on {sha[:12]}",
        )
        for run in check_runs:
            suite = (getattr(run, "_rawData", {}) or {}).get("check_suite") or {}
            if suite.get("id") != suite_id:
                continue
            current = latest.get(run.name)
            if current is None or _order(run) > _order(current):
                latest[run.name] = run

    checks = tuple(
        CandidateCheck(
            name=name,
            status=getattr(latest.get(name), "status", "missing"),
            conclusion=getattr(latest.get(name), "conclusion", None),
            url=getattr(latest.get(name), "html_url", "") or "",
        )
        for name in policy.required_checks
    )
    return CandidateCI(
        workflow_url=getattr(latest_workflow, "html_url", "") or "",
        workflow_status=latest_workflow.status,
        workflow_conclusion=getattr(latest_workflow, "conclusion", None),
        suite_id=suite_id,
        checks=checks,
    )


def require_green_checks(repo: Any, policy: ReleasePolicy, sha: str) -> None:
    """Require every named check to pass in the configured workflow on *sha*."""
    candidate_ci = evaluate_candidate_ci(repo, policy, sha)
    if candidate_ci.workflow_status == "missing":
        raise ValueError(f"no {policy.checks_workflow} run exists on candidate {sha[:12]}")
    if candidate_ci.workflow_status != "completed":
        raise ValueError(
            f"latest {policy.checks_workflow} run on {sha[:12]} is {candidate_ci.workflow_status}; "
            "wait for it to complete"
        )
    if not candidate_ci.suite_id:
        raise ValueError(f"latest {policy.checks_workflow} run on {sha[:12]} has no check suite")

    blockers = [
        f"{check.name} ({check.status if check.status != 'completed' else check.conclusion or 'no conclusion'})"
        for check in candidate_ci.checks
        if not check.passed
    ]
    if blockers:
        raise ValueError(f"required candidate CI is not green on {sha[:12]}: {', '.join(blockers)}")


def _order(run: Any) -> tuple[bool, Any, int]:
    started = getattr(run, "started_at", None)
    return started is not None, started, getattr(run, "id", 0) or 0


def _workflow_order(run: Any) -> tuple[float, int]:
    created = getattr(run, "created_at", None)
    timestamp = created.timestamp() if created is not None else 0.0
    return timestamp, getattr(run, "id", 0) or 0
