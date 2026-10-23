"""One CI fix as three jobs, whichever front door asked for it."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import GithubException

from scripts.ci_fix import run
from scripts.ci_fix.ci_job import JobCommands, JobSpec, setup_script
from scripts.ci_fix.models import (
    CandidateAction,
    Failure,
    FixCandidate,
    FixOutcome,
    FixPlan,
    FixPolicy,
    FixResult,
    FixTarget,
    FixTask,
    FrontDoor,
    IssuePublication,
    ResultKind,
    TargetKind,
)
from scripts.ci_fix.state import load, save
from scripts.common.valkey_ci import Source

_BOT = "valkeyrie-ops[bot]"
_HEAD = "b" * 40
_CANDIDATE = FixCandidate(action=CandidateAction.PATCH, patch="diff", changed_paths=("src/a.c",))


def _pr_plan(job_name="test-ubuntu-latest") -> FixPlan:
    """A PR command's plan: the PR's CI run, on a bot-owned branch into unstable."""
    task = FixTask(
        Failure(repo_full_name="valkey-io/valkey", run_id=100, head_sha=_HEAD, job_id=22, job_name=job_name,
                base_branch="unstable", pr_number=42, workflow="ci"),
        FixTarget(kind=TargetKind.UPDATE_PR, repo_full_name="valkey-io/valkey", branch="agent/ci-fix/x",
                  expected_sha=_HEAD),
        FixPolicy.GENERAL,
    )
    return FixPlan(front_door=FrontDoor.PR, task=task, comment_id=7)


@pytest.fixture
def front_door(monkeypatch):
    """The PR front door, its planner, publisher and reporter replaced by records."""
    seen = SimpleNamespace(planned=None, reported=[], published=[])

    def planner(_gh, _artifacts, request, *, bot_login):
        return seen.planned

    def reporter(_gh, plan, result):
        seen.reported.append(result)
        return {"action": result.kind.value}

    monkeypatch.setitem(run._FRONT_DOORS, FrontDoor.PR, (planner, reporter))
    return seen


_WORKFLOWS = Path(__file__).parent / "fixtures" / "valkey_workflows"


@pytest.fixture
def workflows(monkeypatch):
    """Valkey's workflow files, as the plan reads them at the failed commit."""
    read = []

    def workflow_source(_gh, repository, workflow, ref):
        read.append((repository, workflow, ref))
        return (_WORKFLOWS / f"{workflow}.yml").read_text(encoding="utf-8")

    monkeypatch.setattr(run, "workflow_source", workflow_source)
    return read


def _plan(tmp_path, planned):
    artifacts = SimpleNamespace(download_job_log=lambda _repo, _job: "[err]: TTL expiration in tests/integration/rdb.tcl")
    return run.plan(MagicMock(), artifacts, FrontDoor.PR, {}, bot_login=_BOT, out=tmp_path)


def test_a_failure_its_job_can_host_is_fixed_in_that_job(front_door, workflows, tmp_path):
    front_door.planned = _pr_plan()

    outputs = _plan(tmp_path, front_door.planned)

    assert outputs == {
        "mode": "job", "job": "test-ubuntu-latest", "runs_on": "ubuntu-latest", "container": "",
        "timeout_minutes": "180", "use_repo": "valkey-io/valkey", "use_git_ref": _HEAD,
        "extra_repository": "ianlancetaylor/libbacktrace", "extra_ref": "b9e40069c0b47a722286b94eb5231f7f05c08713",
        "extra_path": "libbacktrace",
    }
    # The PR's run used the workflow at its head.
    assert workflows == [("valkey-io/valkey", "ci", _HEAD)]
    # Everything the later jobs need survives the handoff, the job log included.
    assert load(tmp_path / run.PLAN_FILE, FixPlan) == replace(
        front_door.planned, job_log="[err]: TTL expiration in tests/integration/rdb.tcl",
    )
    spec = load(tmp_path / run.JOB_FILE, JobSpec)
    assert spec.commands.runtest == "./runtest --verbose --tags -slow --dump-logs"
    assert (tmp_path / run.SETUP_FILE).read_text(encoding="utf-8") == setup_script(spec)


def test_a_scheduled_failure_is_resolved_from_its_scheduled_workflow(front_door, workflows, tmp_path):
    plan = _pr_plan("test-sanitizer-address (clang)")
    failure = replace(plan.task.failure, source=Source.DAILY, pr_number=0, workflow="")
    front_door.planned = replace(plan, task=replace(plan.task, failure=failure))

    assert _plan(tmp_path, front_door.planned)["job"] == "test-sanitizer-address (clang)"
    assert workflows == [("valkey-io/valkey", "daily", _HEAD)]
    assert load(tmp_path / run.JOB_FILE, JobSpec).env == {"CC": "clang"}


def test_an_issue_fix_replays_the_job_from_the_tip_it_checks_out(front_door, workflows, tmp_path):
    """The scheduled run tested an older commit; its setup must match the tree it runs in."""
    tip = "c" * 40
    plan = _pr_plan("test-sanitizer-address (clang)")
    failure = replace(plan.task.failure, source=Source.DAILY, pr_number=0, workflow="")
    target = replace(plan.task.target, kind=TargetKind.DRAFT_PR, expected_sha=tip)
    front_door.planned = replace(plan, task=replace(plan.task, failure=failure, target=target))

    outputs = _plan(tmp_path, front_door.planned)

    assert outputs["use_git_ref"] == tip
    assert workflows == [("valkey-io/valkey", "daily", tip)]


def test_a_failure_its_job_cannot_host_is_fixed_on_the_bots_runner(front_door, workflows, tmp_path):
    front_door.planned = _pr_plan("test-no-workflow-has-this-job")

    assert _plan(tmp_path, front_door.planned) == {"mode": "local"}
    assert (tmp_path / run.PLAN_FILE).exists()
    assert not (tmp_path / run.JOB_FILE).exists()


def test_a_workflow_that_cannot_be_read_falls_back_to_the_bots_runner(front_door, monkeypatch, tmp_path):
    front_door.planned = _pr_plan()

    def unreadable(*_args):
        raise GithubException(404, "Not Found", None)

    monkeypatch.setattr(run, "workflow_source", unreadable)

    assert _plan(tmp_path, front_door.planned) == {"mode": "local"}


def test_a_suggestion_is_never_run_in_a_job(front_door, workflows, tmp_path):
    plan = _pr_plan()
    front_door.planned = replace(plan, task=replace(plan.task, target=replace(plan.task.target,
                                                                             kind=TargetKind.SUGGEST)))

    assert _plan(tmp_path, front_door.planned) == {"mode": "local"}
    assert workflows == []


def test_a_crash_after_the_plan_is_reported_as_a_failed_attempt(front_door, tmp_path):
    """The publish job needs a plan to run: without this, nothing would be said."""
    front_door.planned = _pr_plan()

    def unreadable(_repo, _job):
        raise OSError("the log download kept failing")

    artifacts = SimpleNamespace(download_job_log=unreadable)
    outputs = run.plan(MagicMock(), artifacts, FrontDoor.PR, {}, bot_login=_BOT, out=tmp_path)

    assert outputs == {"mode": "done", "action": "failed"}
    (result,) = front_door.reported
    assert result.kind is ResultKind.FAILED and "the log download kept failing" in result.summary


@pytest.mark.parametrize(("final", "outputs"), [
    ({"action": "skipped", "reason": "x"}, {"mode": "done", "action": "skipped"}),
    ({"action": "failed"}, {"mode": "done", "action": "failed"}),
])
def test_a_final_answer_ends_the_run_at_the_plan(front_door, tmp_path, final, outputs):
    front_door.planned = final

    assert _plan(tmp_path, final) == outputs
    assert not (tmp_path / run.PLAN_FILE).exists()


def test_the_fix_job_prepares_from_the_plan_alone_with_the_jobs_own_commands(monkeypatch, tmp_path):
    save(tmp_path / run.PLAN_FILE, replace(_pr_plan(), job_log="the log"))
    commands = JobCommands(build="make", runtest="./runtest --dump-logs")
    save(tmp_path / run.JOB_FILE, JobSpec(
        job_id="j", name="j", runs_on="ubuntu-latest", container="", timeout_minutes=10, env={"CC": "gcc"},
        steps=(), checkout=None, commands=commands,
    ))
    seen = {}

    def prepare_fix(task, job_log, **kwargs):
        seen.update(task=task, log=job_log, **kwargs)
        return _CANDIDATE

    monkeypatch.setattr(run, "prepare_fix", prepare_fix)

    outcome = run.fix(tmp_path, tmp_path / "out", repo_dir="/job")

    assert seen == {"task": _pr_plan().task, "log": "the log", "repo_dir": "/job", "job": commands}
    assert load(tmp_path / "out" / run.OUTCOME_FILE, FixOutcome) == outcome == FixOutcome(candidate=_CANDIDATE)


def _publish(tmp_path, outcome_dir):
    return run.publish_and_report(
        MagicMock(), tmp_path, outcome_dir, git_env={}, run_url="https://run",
    )


def test_a_fix_that_crashed_or_never_finished_is_reported_as_failed(front_door, monkeypatch, tmp_path):
    save(tmp_path / run.PLAN_FILE, _pr_plan())
    monkeypatch.setattr(run, "prepare_fix", MagicMock(side_effect=RuntimeError("agent died")))
    run.fix(tmp_path, tmp_path / "out")

    _publish(tmp_path, tmp_path / "out")
    _publish(tmp_path, tmp_path / "nothing")

    crashed, missing = front_door.reported
    assert crashed.kind is ResultKind.FAILED and "agent died" in crashed.summary
    assert missing.kind is ResultKind.FAILED and "https://run" in missing.summary


def test_a_result_that_cannot_be_read_is_reported_as_failed(front_door, tmp_path):
    save(tmp_path / run.PLAN_FILE, _pr_plan())
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / run.OUTCOME_FILE).write_text('{"candidate": {"not": "a candidate"', encoding="utf-8")

    _publish(tmp_path, tmp_path / "out")

    (unreadable,) = front_door.reported
    assert unreadable.kind is ResultKind.FAILED and "could not be read" in unreadable.summary


def _issue_plan() -> FixPlan:
    """A detector issue's plan: the same task, published as a draft PR or a comment."""
    plan = _pr_plan()
    return replace(plan, task=replace(plan.task, target=replace(plan.task.target, kind=TargetKind.DRAFT_PR)))


@pytest.mark.parametrize(("value", "mode"), [
    ("", IssuePublication.COMMENT), ("comment", IssuePublication.COMMENT), ("draft-pr", IssuePublication.DRAFT_PR),
])
def test_how_an_issue_fix_is_published_is_the_repository_variables_choice(
    front_door, monkeypatch, tmp_path, value, mode,
):
    save(tmp_path / run.PLAN_FILE, _issue_plan())
    save(tmp_path / run.OUTCOME_FILE, FixOutcome(candidate=_CANDIDATE))
    monkeypatch.setenv(run.ISSUE_PUBLICATION_ENV, value)
    seen = {}

    def publish(_gh, task, candidate, *, git_env, issue_publication):
        seen["mode"] = issue_publication
        return FixResult(kind=ResultKind.SUGGESTED, summary="s", task=task)

    monkeypatch.setattr(run, "publish_candidate", publish)

    assert _publish(tmp_path, tmp_path) == {"action": "suggested"}
    assert seen["mode"] is mode


def test_an_unknown_publication_mode_is_a_reported_failure_not_a_guess(front_door, monkeypatch, tmp_path):
    save(tmp_path / run.PLAN_FILE, _issue_plan())
    save(tmp_path / run.OUTCOME_FILE, FixOutcome(candidate=_CANDIDATE))
    monkeypatch.setenv(run.ISSUE_PUBLICATION_ENV, "pr")
    monkeypatch.setattr(run, "publish_candidate", lambda *a, **k: pytest.fail("nothing is published"))

    _publish(tmp_path, tmp_path)

    (failed,) = front_door.reported
    assert failed.kind is ResultKind.FAILED and "CI_FIX_ISSUES_PUBLISH must be one of comment, draft-pr" in failed.summary


def test_a_prs_fix_does_not_read_the_issue_publication_variable(front_door, monkeypatch, tmp_path):
    save(tmp_path / run.PLAN_FILE, _pr_plan())
    save(tmp_path / run.OUTCOME_FILE, FixOutcome(candidate=_CANDIDATE))
    monkeypatch.setenv(run.ISSUE_PUBLICATION_ENV, "pr")  # wrong, and irrelevant to a PR
    monkeypatch.setattr(run, "publish_candidate", lambda _gh, task, *a, **k: FixResult(
        kind=ResultKind.PUSHED, summary="p", task=task,
    ))

    assert _publish(tmp_path, tmp_path) == {"action": "pushed"}


def test_the_candidate_is_published_as_its_target_says(front_door, monkeypatch, tmp_path):
    save(tmp_path / run.PLAN_FILE, _pr_plan())
    save(tmp_path / run.OUTCOME_FILE, FixOutcome(candidate=_CANDIDATE))
    seen = {}

    def publish(_gh, task, candidate, *, git_env, issue_publication):
        seen.update(task=task, candidate=candidate)
        return FixResult(kind=ResultKind.OPENED, summary="opened", task=task)

    monkeypatch.setattr(run, "publish_candidate", publish)

    assert _publish(tmp_path, tmp_path) == {"action": "opened"}
    assert seen == {"task": _pr_plan().task, "candidate": _CANDIDATE}


def test_every_front_door_plans_publishes_and_reports():
    assert set(run._FRONT_DOORS) == set(FrontDoor)


def test_the_cli_fails_for_a_failed_publication(monkeypatch):
    class AuthContext:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def env(self):
            return {}

    monkeypatch.setenv("TARGET_TOKEN", "token")
    monkeypatch.setattr(run, "Github", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(run, "GitAuth", lambda *_args, **_kwargs: AuthContext())
    monkeypatch.setattr(run, "publish_and_report", lambda *_args, **_kwargs: {"action": "failed"})

    assert run.main(["publish", "--plan", "plan", "--outcome", "outcome"]) == 1


def test_a_publication_that_raises_is_reported_as_failed(front_door, monkeypatch, tmp_path):
    save(tmp_path / run.PLAN_FILE, _pr_plan())
    save(tmp_path / run.OUTCOME_FILE, FixOutcome(candidate=_CANDIDATE))
    monkeypatch.setattr(run, "publish_candidate", MagicMock(side_effect=RuntimeError("push failed")))

    _publish(tmp_path, tmp_path)

    (result,) = front_door.reported
    assert result.kind is ResultKind.FAILED and "push failed" in result.summary


def test_every_type_a_fix_job_decodes_resolves_on_the_oldest_python_the_job_may_have():
    """A job's container brings its own Python, as old as 3.9 (CentOS Stream 9),
    where ``X | None`` in an annotation cannot be evaluated."""
    import ast
    import dataclasses
    import typing

    from scripts.ci_fix import ci_job, models, reproduce

    def decoded(kind, seen):
        if kind in seen or not (isinstance(kind, type) and dataclasses.is_dataclass(kind)):
            return
        seen.add(kind)
        for hint in typing.get_type_hints(kind).values():
            for arg in (hint, *typing.get_args(hint)):
                for inner in (arg, *typing.get_args(arg)):
                    decoded(inner, seen)

    seen: set = set()
    for root in (models.FixPlan, models.FixOutcome, ci_job.JobSpec):
        decoded(root, seen)
    offenders = []
    for kind in seen:
        source = Path(__import__(kind.__module__, fromlist=["_"]).__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ClassDef) and node.name == kind.__name__:
                for item in node.body:
                    if isinstance(item, ast.AnnAssign) and "|" in ast.unparse(item.annotation):
                        offenders.append(f"{kind.__name__}.{ast.unparse(item.target)}")
    assert {models.Failure, ci_job.JobSpec, reproduce.JobCommands} <= seen
    assert offenders == []
