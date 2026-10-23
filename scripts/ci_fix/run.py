"""Run one CI fix as three jobs: plan, fix, publish.

Every front door runs its fix the same way, through ``ci-fix-run.yml``:
a Test Failure Detector issue (:mod:`scripts.ci_fix.issues`), a maintainer's
command on a pull request (:mod:`scripts.ci_fix.main`), and a bot-owned pull
request's red head (:mod:`scripts.backport.ci_followup`).

1. ``plan``: the front door validates its request and binds one exact failed
   job (a :class:`FixPlan`); this module resolves that job from the workflow
   that ran it, if it can host the fix (:mod:`scripts.ci_fix.ci_job`).
2. ``fix``: :func:`scripts.ci_fix.engine.prepare_fix`, inside that job after
   its own setup and build (``scripts/ci_fix/job.sh``), or on the bot's own
   runner. It holds no GitHub token.
3. ``publish``: the candidate is published as its target says, and the
   front door reports it (a draft PR and an issue comment, or a push or a suggestion and
   a PR comment), also when the fix job failed or never finished.

The plan, the resolved job and the fix's outcome pass between the jobs as
JSON (:mod:`scripts.ci_fix.state`).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from github import Auth, Github, GithubException

from scripts.backport import ci_followup
from scripts.ci_fix import issues
from scripts.ci_fix import main as pr_command
from scripts.ci_fix.ci_job import JobSpec, Unsupported, resolve, setup_script, workflow_source
from scripts.ci_fix.engine import prepare_fix
from scripts.ci_fix.models import (
    FixOutcome,
    FixPlan,
    FixResult,
    FixTask,
    FrontDoor,
    IssuePublication,
    ResultKind,
    TargetKind,
)
from scripts.ci_fix.publish import publish_candidate
from scripts.ci_fix.state import load, save
from scripts.common.git_auth import GitAuth
from scripts.common.identity import APP_LOGIN
from scripts.common.workflow_artifacts import ArtifactClient

logger = logging.getLogger(__name__)

PLAN_FILE = "plan.json"
JOB_FILE = "job.json"
SETUP_FILE = "setup.sh"
OUTCOME_FILE = "outcome.json"
# How a detector issue's fix is published (models.IssuePublication): a
# repository variable, so that the draft PRs can be turned on without a change.
ISSUE_PUBLICATION_ENV = "CI_FIX_ISSUES_PUBLISH"

Planner = Callable[..., "FixPlan | dict[str, Any]"]
Reporter = Callable[..., "dict[str, Any]"]

# Each front door: how it plans a request, and how the result is reported.
# The candidate is published as its target says (scripts/ci_fix/publish.py).
_FRONT_DOORS: dict[FrontDoor, tuple[Planner, Reporter]] = {
    FrontDoor.ISSUE: (issues.plan_request, issues.report_result),
    FrontDoor.PR: (pr_command.plan_request, pr_command.report_result),
    FrontDoor.FOLLOW_UP: (ci_followup.plan_request, ci_followup.report_result),
}


def plan(
    gh: Any,
    artifact_client: Any,
    front_door: FrontDoor,
    request: dict[str, Any],
    *,
    bot_login: str,
    out: Path,
) -> dict[str, str]:
    """Plan one request; the workflow's outputs.

    ``mode`` is ``job`` (the failed job hosts the fix: its runner, container,
    timeout and checkouts follow, and its spec and setup script are written
    next to the plan), ``local`` (it cannot), or ``done`` (the front door's
    result was final: a skip, or a refusal it already reported, with its
    ``action``).
    """
    planner, reporter = _FRONT_DOORS[front_door]
    planned = planner(gh, artifact_client, request, bot_login=bot_login)
    if not isinstance(planned, FixPlan):
        print(json.dumps(planned, indent=2))
        return {"mode": "done", "action": str(planned.get("action") or "")}
    try:
        return _plan_job(gh, artifact_client, planned, out)
    except Exception as exc:  # noqa: BLE001 - a planned request always gets a reported result
        logger.exception("Planning the fix failed")
        # The publish job does not run without a plan: report here, so the
        # front door records the failed attempt instead of saying nothing.
        result = FixResult(
            kind=ResultKind.FAILED, summary=f"The CI-fix attempt failed while planning: {exc}", task=planned.task,
        )
        print(json.dumps(reporter(gh, planned, result), indent=2))
        return {"mode": "done", "action": ResultKind.FAILED.value}


def _plan_job(gh: Any, artifact_client: Any, planned: FixPlan, out: Path) -> dict[str, str]:
    """Save ``planned`` with its job log, and how its fix runs: in the failed job, or not."""
    failure = planned.task.failure
    job_log = str(artifact_client.download_job_log(failure.repo_full_name, failure.job_id) or "")
    fix_plan = replace(planned, job_log=job_log)
    save(out / PLAN_FILE, fix_plan)
    spec = job_for(gh, fix_plan.task)
    if spec is None:
        return {"mode": "local"}
    save(out / JOB_FILE, spec)
    (out / SETUP_FILE).write_text(setup_script(spec), encoding="utf-8")
    checkout = spec.checkout
    return {
        "mode": "job",
        "job": spec.name,
        "runs_on": spec.runs_on,
        "container": spec.container,
        "timeout_minutes": str(spec.timeout_minutes),
        "use_repo": fix_plan.task.target.repo_full_name,
        "use_git_ref": checked_out(fix_plan.task),
        "extra_repository": checkout.repository if checkout else "",
        "extra_ref": checkout.ref if checkout else "",
        "extra_path": checkout.path if checkout else "",
    }


def checked_out(task: FixTask) -> str:
    """The commit the fix is prepared on: the target's current head, else the failed one."""
    return task.target.expected_sha or task.failure.head_sha


def job_for(gh: Any, task: FixTask) -> JobSpec | None:
    """The failed job, from its workflow file at the checked-out commit, or None when it cannot host the fix.

    The setup replays in that tree, so it is that tree's workflow: a detector
    issue's fix is prepared on the branch tip, not the older commit the
    scheduled run tested. A suggestion is never run, so it needs no job.
    """
    failure = task.failure
    workflow = failure.source.value if failure.source else failure.workflow
    if task.target.kind is TargetKind.SUGGEST or not workflow:
        return None
    if failure.scheduled or not failure.pr_number:
        ref = f"refs/heads/{failure.base_branch}"
    else:
        ref = f"refs/pull/{failure.pr_number}/merge"
    try:
        sha = checked_out(task)
        source = workflow_source(gh, failure.repo_full_name, workflow, sha)
        return resolve(source, failure.job_name, scheduled=failure.scheduled,
                       repository=failure.repo_full_name, sha=sha, ref=ref)
    except (Unsupported, GithubException) as exc:
        logger.info("%s cannot host the fix, which is prepared on the bot's runner: %s", failure.job_name, exc)
        return None


def fix(plan_dir: Path, out: Path, *, repo_dir: str = "") -> FixOutcome:
    """Prepare the planned fix, in the failed job's checkout ``repo_dir`` or a clone.

    In the job, it is built and tested with the job's own commands.
    """
    fix_plan = load(plan_dir / PLAN_FILE, FixPlan)
    try:
        job = load(plan_dir / JOB_FILE, JobSpec).commands if (plan_dir / JOB_FILE).exists() else None
        candidate = prepare_fix(fix_plan.task, fix_plan.job_log, repo_dir=repo_dir, job=job)
        outcome = FixOutcome(candidate=candidate)
    except Exception as exc:  # noqa: BLE001 - the publish job reports every outcome
        logger.exception("CI-fix failed")
        outcome = FixOutcome(error=str(exc) or type(exc).__name__)
    save(out / OUTCOME_FILE, outcome)
    return outcome


def publish_and_report(
    gh: Any,
    plan_dir: Path,
    outcome_dir: Path,
    *,
    git_env: dict[str, str],
    run_url: str,
) -> dict[str, Any]:
    """Publish the fix job's candidate, or the reason there is none, and report it."""
    fix_plan = load(plan_dir / PLAN_FILE, FixPlan)
    try:
        outcome = load(outcome_dir / OUTCOME_FILE, FixOutcome)
    except FileNotFoundError:
        outcome = FixOutcome(error=f"the fix job ended without a result (see {run_url})")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        outcome = FixOutcome(error=f"the fix job's result could not be read ({exc}; see {run_url})")
    reporter = _FRONT_DOORS[fix_plan.front_door][1]
    if outcome.candidate is None:
        result = FixResult(
            kind=ResultKind.FAILED, summary=f"The CI-fix attempt failed: {outcome.error}", task=fix_plan.task,
        )
    else:
        try:
            # Only an issue's fix reads the variable: a bad value cannot stop a PR's.
            mode = (issue_publication() if fix_plan.task.target.kind is TargetKind.DRAFT_PR
                    else IssuePublication.DRAFT_PR)
            result = publish_candidate(gh, fix_plan.task, outcome.candidate, git_env=git_env, issue_publication=mode)
        except Exception as exc:  # noqa: BLE001 - every request gets a reported result
            logger.exception("CI-fix publication failed")
            result = FixResult(kind=ResultKind.FAILED, summary=f"The CI-fix attempt failed: {exc}",
                               task=fix_plan.task, candidate=outcome.candidate)
    return reporter(gh, fix_plan, result)


def issue_publication() -> IssuePublication:
    """How a detector issue's fix is published, from the environment; a comment unless told otherwise."""
    value = os.environ.get(ISSUE_PUBLICATION_ENV, "").strip().lower()
    if not value:
        return IssuePublication.COMMENT
    try:
        return IssuePublication(value)
    except ValueError:
        choices = ", ".join(mode.value for mode in IssuePublication)
        raise ValueError(f"{ISSUE_PUBLICATION_ENV} must be one of {choices}, not {value!r}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_ = commands.add_parser("plan", help="validate a request and plan its fix")
    plan_.add_argument("--front-door", required=True, choices=[door.value for door in FrontDoor])
    plan_.add_argument("--request", required=True, help="the front door's request, as JSON")
    plan_.add_argument("--out", required=True, type=Path)
    fix_ = commands.add_parser("fix", help="prepare the planned fix; needs no GitHub token")
    fix_.add_argument("--plan", required=True, type=Path)
    fix_.add_argument("--out", required=True, type=Path)
    fix_.add_argument("--repo-dir", default="", help="the failed job's own checkout, if it hosts the fix")
    publish_ = commands.add_parser("publish", help="publish the fix and report it")
    publish_.add_argument("--plan", required=True, type=Path)
    publish_.add_argument("--outcome", required=True, type=Path)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "fix":
        outcome = fix(args.plan, args.out, repo_dir=args.repo_dir)
        return 0 if outcome.candidate is not None else 1

    token = os.environ.get("TARGET_TOKEN", "")
    if not token:
        parser.error("TARGET_TOKEN is required")
    gh = Github(auth=Auth.Token(token))
    bot_login = f"{APP_LOGIN}[bot]"
    if args.command == "plan":
        try:
            request = json.loads(args.request)
        except ValueError:
            parser.error("--request must be JSON")
        if not isinstance(request, dict):
            parser.error("--request must be a JSON object")
        outputs = plan(
            gh, ArtifactClient(gh, token=token), FrontDoor(args.front_door), request,
            bot_login=bot_login, out=args.out,
        )
        _write_outputs(outputs)
        return 1 if outputs.get("action") == ResultKind.FAILED.value else 0

    run_url = "{}/{}/actions/runs/{}".format(
        os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
        os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", ""),
    )
    with GitAuth(token, prefix="ci-fix-git-askpass-") as auth:
        result = publish_and_report(
            gh, args.plan, args.outcome, git_env=auth.env(), run_url=run_url,
        )
    print(json.dumps(result, indent=2))
    return 1 if result.get("action") == ResultKind.FAILED.value else 0


def _write_outputs(outputs: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(json.dumps(outputs, indent=2))
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


if __name__ == "__main__":
    raise SystemExit(main())
