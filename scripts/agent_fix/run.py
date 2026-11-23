"""Run one fix as three jobs: plan, fix, publish; a sweep's, as five.

Every front door runs its fix the same way, through ``agent-fix-run.yml``:
a Test Failure Detector issue (:mod:`scripts.agent_fix.issues`), a maintainer's
command on a pull request (:mod:`scripts.agent_fix.main`), a bot-owned pull
request's red head (:mod:`scripts.backport.ci_followup`), and an issue a
committer labelled for a change (:mod:`scripts.agent_fix.labelled`).

1. ``plan``: the front door validates its request and binds one exact failed
   job (a :class:`FixPlan`); this module resolves that job from the workflow
   that ran it, if it can host the fix (:mod:`scripts.agent_fix.ci_job`). A
   labelled issue has no failed job: its change is made in the target
   repository's pull request job, resolved the same way.
2. ``fix``: :func:`scripts.agent_fix.engine.prepare_fix`, or for a labelled
   issue :func:`scripts.agent_fix.engine.prepare_change`, inside that job after
   its own setup and build (``scripts/agent_fix/job.sh``), or on the bot's own
   runner. It holds no GitHub token.
3. ``publish``: the candidate is published as its target says. A published
   fix or a refusal is reported on the target PR or issue; a failure stays in
   this repository's workflow logs and retry state.

A backport sweep's fix of the bot's own, for a test the default branch has
too, is made for the default branch before it is published:
``plan-upstream`` (:func:`plan_upstream`) resolves the default branch's own
job of the same name, and a second ``fix`` job, that one (or the bot's
runner, when it cannot host a fix), makes the same fix there
(:func:`scripts.agent_fix.engine.prepare_upstream`). Its outcome is the one
published.

The plan, the resolved job and the fix's outcome pass between the jobs as
JSON (:mod:`scripts.agent_fix.state`).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from github import Auth, Github, GithubException

from scripts.agent_fix import issues, labelled, upstream
from scripts.agent_fix import main as pr_command
from scripts.agent_fix.ci_job import JobSpec, Unsupported, resolve, setup_script, workflow_source
from scripts.agent_fix.engine import (
    failed_tests,
    prepare_change,
    prepare_fix,
    prepare_upstream,
    upstream_task,
)
from scripts.agent_fix.models import (
    CandidateAction,
    FixCandidate,
    FixOutcome,
    FixPlan,
    FixResult,
    FixTask,
    FrontDoor,
    IssuePublication,
    ResultKind,
    TargetKind,
)
from scripts.agent_fix.policy import allows_ci_changes, push_repo
from scripts.agent_fix.publish import publish_candidate
from scripts.agent_fix.state import decode, load, save
from scripts.backport import ci_followup
from scripts.common.git_auth import GitAuth
from scripts.common.identity import APP_LOGIN
from scripts.common.workflow_artifacts import ArtifactClient

logger = logging.getLogger(__name__)

PLAN_FILE = "plan.json"
JOB_FILE = "job.json"
SETUP_FILE = "setup.sh"
OUTCOME_FILE = "outcome.json"
# In the plan of a fix for the default branch: the release branch's fix it is made from.
RELEASE_FILE = "release.json"
# And the default branch's commit it is made on.
SHA_FILE = "sha"
_PRIOR_PLAN_ARTIFACTS = 20
# How a detector issue's fix is published (models.IssuePublication): a
# repository variable, so that the draft PRs can be turned on without a change.
ISSUE_PUBLICATION_ENV = "AGENT_FIX_CI_ISSUES_PUBLISH"

Planner = Callable[..., "FixPlan | dict[str, Any]"]
Reporter = Callable[..., "dict[str, Any]"]

# Each front door: how it plans a request, and how the result is reported.
# The candidate is published as its target says (scripts/agent_fix/publish.py).
_FRONT_DOORS: dict[FrontDoor, tuple[Planner, Reporter]] = {
    FrontDoor.ISSUE: (issues.plan_request, issues.report_result),
    FrontDoor.PR: (pr_command.plan_request, pr_command.report_result),
    FrontDoor.FOLLOW_UP: (ci_followup.plan_request, ci_followup.report_result),
    FrontDoor.LABEL: (labelled.plan_request, labelled.report_result),
}


def plan(
    gh: Any,
    artifact_client: Any,
    front_door: FrontDoor,
    request: dict[str, Any],
    *,
    bot_login: str,
    out: Path,
    prior_plans: tuple[FixPlan, ...] = (),
) -> dict[str, str]:
    """Plan one request; the workflow's outputs.

    ``mode`` is ``job`` (the failed job hosts the fix: its runner, container,
    timeout and checkouts follow, and its spec and setup script are written
    next to the plan), ``local`` (it cannot), or ``done`` (the front door's
    result was final: a skip, or a refusal it already reported, with its
    ``action``).
    """
    planner, reporter = _FRONT_DOORS[front_door]
    planner_options: dict[str, Any] = {"bot_login": bot_login}
    if front_door is FrontDoor.FOLLOW_UP:
        planner_options["prior_plans"] = prior_plans
    planned = planner(gh, artifact_client, request, **planner_options)
    if not isinstance(planned, FixPlan):
        print(json.dumps(planned, indent=2))
        return {"mode": "done", "action": str(planned.get("action") or "")}
    # This artifact is the retry ledger for silent follow-up failures. Save it
    # before log download/job resolution, then replace it below with the bound
    # plan that contains the job log.
    save(out / PLAN_FILE, planned)
    try:
        return _plan_job(gh, artifact_client, planned, out)
    except Exception as exc:  # noqa: BLE001 - a planned request always gets a reported result
        logger.exception("Planning the fix failed")
        # The publish job does not run without a plan: report here, so the
        # front door records the failed attempt instead of saying nothing.
        result = FixResult(
            kind=ResultKind.FAILED, summary=f"The agent-fix attempt failed while planning: {exc}", task=planned.task,
        )
        print(json.dumps(reporter(gh, planned, result), indent=2))
        return {"mode": "done", "action": ResultKind.FAILED.value}


def _plan_job(gh: Any, artifact_client: Any, planned: FixPlan, out: Path) -> dict[str, str]:
    """Save ``planned`` with its job log, and how its fix runs: in the failed job, or not."""
    failure = planned.task.failure
    # A labelled issue has no failed job: its evidence is made in the fix job.
    job_log = "" if planned.task.request is not None else str(
        artifact_client.download_job_log(failure.repo_full_name, failure.job_id) or ""
    )
    fix_plan = replace(planned, job_log=job_log)
    if fix_plan.task.target.upstream_branch:
        # Read from the log, as the engine reads them: the fix job has no GitHub token.
        open_fixes = upstream.find_open_fixes(gh, fix_plan.task, failed_tests(fix_plan.task, job_log))
        fix_plan = replace(fix_plan, task=replace(fix_plan.task, open_fixes=open_fixes))
    save(out / PLAN_FILE, fix_plan)
    return {
        **_job_outputs(gh, fix_plan.task, out),
        # Publish asks for workflows:write only when the push may change .github/.
        "ci_changes": "true" if allows_ci_changes(fix_plan.task) else "",
        # A sweep's fix may be made for the default branch too (plan_upstream).
        "upstream": "true" if fix_plan.task.target.upstream_branch else "",
    }


def _job_outputs(gh: Any, task: FixTask, out: Path) -> dict[str, str]:
    """Where ``task``'s fix runs: its job, saved in ``out`` with its setup, or the bot's runner."""
    spec = job_for(gh, task)
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
        # The repository the change is for, not the fork its branch may be pushed to.
        "use_repo": task.target.base_repo_full_name or task.target.repo_full_name,
        "use_git_ref": checked_out(task),
        "extra_repository": checkout.repository if checkout else "",
        "extra_ref": checkout.ref if checkout else "",
        "extra_path": checkout.path if checkout else "",
    }


def plan_upstream(gh: Any, plan_dir: Path, outcome_dir: Path, out: Path, result_dir: Path) -> dict[str, str]:
    """Plan making a sweep's fix for the default branch in that branch's own job, or pass its outcome on.

    The fix is the bot's own patch, run on the release branch, for a test the
    default branch has too. The default branch's job is the one named like
    the failed job, in the same workflow file at the branch's tip: its setup
    installs and builds what the branch needs, and it runs the test the way
    the branch's CI does. Then ``out`` holds that job's plan, the release
    branch's fix (``RELEASE_FILE``) and the branch's tip (``SHA_FILE``), and
    ``mode`` is ``job``, or ``local`` when the job cannot host a fix, which
    is then made on the bot's runner. Otherwise the outcome for publish, the
    release branch's as it is, is in ``result_dir``.
    """
    fix_plan = load(plan_dir / PLAN_FILE, FixPlan)
    source = outcome_dir / OUTCOME_FILE
    if not source.is_file():
        # Publish reports a fix job that ended without a result.
        return {"mode": ""}
    result_dir.mkdir(parents=True, exist_ok=True)
    try:
        outcome = load(source, FixOutcome)
    except (OSError, ValueError, TypeError, KeyError):
        # Publish reports it as unreadable.
        shutil.copyfile(source, result_dir / OUTCOME_FILE)
        return {"mode": ""}
    candidate, task = outcome.candidate, fix_plan.task
    branch = task.target.upstream_branch
    tests = (candidate.failing_tests or failed_tests(task, fix_plan.job_log)) if candidate else ()
    if not (
        candidate and branch and tests and candidate.action is CandidateAction.PATCH
        and not candidate.adapts and not candidate.not_run
    ):
        save(result_dir / OUTCOME_FILE, outcome)
        return {"mode": ""}
    repo = gh.get_repo(task.failure.repo_full_name)
    sha = repo.get_branch(branch).commit.sha
    if not all(_has_file(repo, test.file, sha) for test in tests if test.file):
        logger.info("%s does not have the failing test: the fix is %s's own", branch, task.failure.base_branch)
        save(result_dir / OUTCOME_FILE, outcome)
        return {"mode": ""}
    outputs = _job_outputs(gh, upstream_task(task, sha), out)
    save(out / PLAN_FILE, fix_plan)
    save(out / RELEASE_FILE, candidate)
    (out / SHA_FILE).write_text(sha, encoding="utf-8")
    return outputs


def _has_file(repo: Any, path: str, sha: str) -> bool:
    try:
        repo.get_contents(path, ref=sha)
    except GithubException as exc:
        if exc.status == 404:
            return False
        raise
    return True


def checked_out(task: FixTask) -> str:
    """The commit the fix is prepared on: the target's current head, else the failed one."""
    return task.target.expected_sha or task.failure.head_sha


def job_for(gh: Any, task: FixTask) -> JobSpec | None:
    """The failed job, from its workflow file at the checked-out commit, or None when it cannot host the fix.

    The setup replays in that tree, so it is that tree's workflow: a detector
    issue's fix is prepared on the branch tip, not the older commit the
    scheduled run tested. A run of every release branch is the exception: its
    jobs are the default branch's, which a release branch's own workflow may
    not have (``Failure.workflow_sha``). A suggestion is never run, so it
    needs no job. Nor does the head of a pull request the bot opened for a
    labelled issue: a job's setup and build run as the workflow user, and
    that tree holds a change nobody has reviewed yet, made from an issue
    anyone can write. It is built on the bot's runner, by the isolated
    account alone.
    """
    failure = task.failure
    workflow = failure.source.value if failure.source else failure.workflow
    if task.target.kind is TargetKind.SUGGEST or not workflow:
        return None
    if task.target.kind is TargetKind.UPDATE_PR and labelled.is_change_branch(task.target.branch):
        return None
    if failure.scheduled or not failure.pr_number:
        ref = f"refs/heads/{failure.base_branch}"
    else:
        ref = f"refs/pull/{failure.pr_number}/merge"
    try:
        sha = checked_out(task)
        source = workflow_source(gh, failure.repo_full_name, workflow, failure.workflow_sha or sha)
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
    # What a job that is stopped mid-fix leaves behind: an attempt that started.
    save(out / OUTCOME_FILE, FixOutcome(error="the fix job stopped before the fix finished", started=True))
    try:
        job = load(plan_dir / JOB_FILE, JobSpec).commands if (plan_dir / JOB_FILE).exists() else None
        if (plan_dir / RELEASE_FILE).exists():
            release = load(plan_dir / RELEASE_FILE, FixCandidate)
            sha = (plan_dir / SHA_FILE).read_text(encoding="utf-8").strip()
            candidate = prepare_upstream(fix_plan.task, fix_plan.job_log, release, sha=sha, repo_dir=repo_dir, job=job)
        elif fix_plan.task.request is not None:
            candidate = prepare_change(fix_plan.task, repo_dir=repo_dir, job=job)
        else:
            candidate = prepare_fix(fix_plan.task, fix_plan.job_log, repo_dir=repo_dir, job=job)
        outcome = FixOutcome(candidate=candidate, started=True)
    except Exception as exc:  # noqa: BLE001 - the publish job reports every outcome
        logger.exception("Agent-fix failed")
        outcome = FixOutcome(error=str(exc) or type(exc).__name__, started=True)
    save(out / OUTCOME_FILE, outcome)
    return outcome


def record_failure(out: Path, error: str) -> FixOutcome:
    """Save a failure that happened before the fix engine could write one."""
    outcome = FixOutcome(error=error)
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
        outcome = FixOutcome(error=f"the fix job's result could not be read ({exc}; see {run_url})", started=True)
    reporter = _FRONT_DOORS[fix_plan.front_door][1]
    if outcome.candidate is None:
        if not outcome.started:
            # The workflow uploads the plan again, for the runs that count this one's attempts.
            save(plan_dir / PLAN_FILE, replace(fix_plan, never_started=True))
            _write_outputs({"never_started": "true"})
        result = FixResult(
            kind=ResultKind.FAILED, summary=f"The agent-fix attempt failed: {outcome.error}", task=fix_plan.task,
        )
    else:
        try:
            result = _published(gh, fix_plan, outcome.candidate, git_env=git_env)
        except Exception as exc:  # noqa: BLE001 - every request gets a reported result
            logger.exception("Agent-fix publication failed")
            result = FixResult(kind=ResultKind.FAILED, summary=f"The agent-fix attempt failed: {exc}",
                               task=fix_plan.task, candidate=outcome.candidate)
    return reporter(gh, fix_plan, result)


def _published(gh: Any, fix_plan: FixPlan, candidate: FixCandidate, *, git_env: dict[str, str]) -> FixResult:
    """Publish ``candidate`` as its target says, after what only its front door can recheck."""
    task, request = fix_plan.task, fix_plan.task.request
    bot_login = f"{APP_LOGIN}[bot]"
    # Only an issue's fix reads the variable: a bad value cannot stop a PR's.
    mode = IssuePublication.DRAFT_PR
    if task.target.kind is TargetKind.DRAFT_PR and request is None:
        mode = issue_publication()
    elif task.target.kind is TargetKind.DRAFT_PR and request is not None and not request.replay:
        mode = labelled.publication()
        if mode is IssuePublication.DRAFT_PR and candidate.action is not CandidateAction.REFUSE:
            # As many are open as may be, since this one was dispatched: its change is posted on the issue.
            held = labelled.hold_reason(gh.get_repo(task.failure.repo_full_name), bot_login)
            if held:
                logger.info("No pull request is opened: %s", held)
                mode = IssuePublication.COMMENT
    return publish_candidate(gh, task, candidate, git_env=git_env, issue_publication=mode)


def load_prior_plans(
    artifact_client: ArtifactClient,
    repo_full_name: str,
    artifact_name: str,
    *,
    current_run_id: int,
) -> tuple[FixPlan, ...]:
    """Load recent plans for one reusable-workflow key from agent artifacts."""
    plans: list[FixPlan] = []
    artifacts = artifact_client.list_named_artifacts(
        repo_full_name,
        artifact_name,
        max_artifacts=_PRIOR_PLAN_ARTIFACTS,
    )
    for artifact in artifacts:
        if artifact.expired or artifact.run_id == current_run_id:
            continue
        try:
            files = artifact_client.download_artifact(
                repo_full_name,
                artifact.artifact_id,
            )
            payload = next(
                data
                for path, data in files.items()
                if Path(path).name == PLAN_FILE
            )
            plans.append(decode(FixPlan, json.loads(payload)))
        except (StopIteration, UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "Ignoring unreadable prior plan artifact %s: %s",
                artifact.artifact_id,
                exc,
            )
    return tuple(plans)


def issue_publication() -> IssuePublication:
    """How a detector issue's fix is published, from the environment; a comment unless told otherwise."""
    return IssuePublication.from_env(ISSUE_PUBLICATION_ENV)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_ = commands.add_parser("plan", help="validate a request and plan its fix")
    plan_.add_argument("--front-door", required=True, choices=[door.value for door in FrontDoor])
    plan_.add_argument("--request", required=True, help="the front door's request, as JSON")
    plan_.add_argument("--out", required=True, type=Path)
    plan_.add_argument("--key", default="", help="this fix's workflow artifact key")
    upstream_ = commands.add_parser(
        "plan-upstream", help="plan making a sweep's fix for the default branch, in that branch's own job",
    )
    upstream_.add_argument("--plan", required=True, type=Path)
    upstream_.add_argument("--outcome", required=True, type=Path, help="the release branch's fix job's result")
    upstream_.add_argument("--out", required=True, type=Path)
    upstream_.add_argument("--result", required=True, type=Path, help="where the outcome for publish goes, if no job")
    fix_ = commands.add_parser("fix", help="prepare the planned fix; needs no GitHub token")
    fix_.add_argument("--plan", required=True, type=Path)
    fix_.add_argument("--out", required=True, type=Path)
    fix_.add_argument("--repo-dir", default="", help="the failed job's own checkout, if it hosts the fix")
    failed_ = commands.add_parser(
        "record-failure",
        help="record a fix-job failure that happened before the fix engine ran",
    )
    failed_.add_argument("--out", required=True, type=Path)
    failed_.add_argument("--error", required=True)
    publish_ = commands.add_parser("publish", help="publish the fix and report it")
    publish_.add_argument("--plan", required=True, type=Path)
    publish_.add_argument("--outcome", required=True, type=Path)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "fix":
        outcome = fix(args.plan, args.out, repo_dir=args.repo_dir)
        return 0 if outcome.candidate is not None else 1
    if args.command == "record-failure":
        record_failure(args.out, args.error)
        return 0

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
        prior_plans: tuple[FixPlan, ...] = ()
        agent_token = os.environ.get("AGENT_TOKEN", "")
        agent_repo = os.environ.get("GITHUB_REPOSITORY", "")
        if (
            FrontDoor(args.front_door) is FrontDoor.FOLLOW_UP
            and args.key
            and agent_token
            and agent_repo
        ):
            agent_gh = Github(auth=Auth.Token(agent_token))
            prior_plans = load_prior_plans(
                ArtifactClient(agent_gh, token=agent_token),
                agent_repo,
                f"agent-fix-plan-{args.key}",
                current_run_id=int(os.environ.get("GITHUB_RUN_ID", "0") or 0),
            )
        outputs = plan(
            gh, ArtifactClient(gh, token=token), FrontDoor(args.front_door), request,
            bot_login=bot_login, out=args.out, prior_plans=prior_plans,
        )
        _write_outputs(outputs)
        action = outputs.get("action", "")
        return 1 if action == ResultKind.FAILED.value else 0
    if args.command == "plan-upstream":
        _write_outputs(plan_upstream(gh, args.plan, args.outcome, args.out, args.result))
        return 0

    run_url = "{}/{}/actions/runs/{}".format(
        os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
        os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", ""),
    )
    # A labelled issue's branch may live in the bot's own fork, which has a token of its own.
    push_token = os.environ.get("PUSH_TOKEN", "")
    if push_token and load(args.plan / PLAN_FILE, FixPlan).task.target.repo_full_name != push_repo():
        push_token = ""
    with GitAuth(push_token or token, prefix="agent-fix-git-askpass-") as auth:
        result = publish_and_report(
            gh, args.plan, args.outcome, git_env=auth.env(), run_url=run_url,
        )
    print(json.dumps(result, indent=2))
    write_summary(result)
    return 1 if result.get("action") == ResultKind.FAILED.value else 0


def write_summary(result: dict[str, Any]) -> None:
    """One line on the run's page: what the fix did, so a refusal does not read as a fix."""
    path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    action = str(result.get("action") or "unknown")
    summary = " ".join(str(result.get("summary") or "").split())[:500]
    published = action in {kind.value for kind in ResultKind if kind.successful}
    lines = [f"### Agent fix: **{action}**{' (a fix was published)' if published else ' (no fix published)'}", ""]
    if summary:
        lines += [summary, ""]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


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
