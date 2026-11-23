"""One fix as three jobs, whichever front door asked for it."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import GithubException

from scripts.agent_fix import run
from scripts.agent_fix.ci_job import JOB_MINUTES, JobCommands, JobSpec, setup_script
from scripts.agent_fix.models import (
    CandidateAction,
    FailedTest,
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
    IssueRequest,
    ResultKind,
    TargetKind,
)
from scripts.agent_fix.state import encode, load, save
from scripts.common.valkey_ci import Source
from scripts.common.workflow_artifacts import WorkflowArtifact

_BOT = "valkeyrie-ops[bot]"
_HEAD = "b" * 40
_CANDIDATE = FixCandidate(action=CandidateAction.PATCH, patch="diff", changed_paths=("src/a.c",))


def _pr_plan(job_name="test-ubuntu-latest") -> FixPlan:
    """A PR command's plan: the PR's CI run, on a bot-owned branch into unstable."""
    task = FixTask(
        Failure(repo_full_name="valkey-io/valkey", run_id=100, head_sha=_HEAD, job_id=22, job_name=job_name,
                base_branch="unstable", pr_number=42, workflow="ci"),
        FixTarget(kind=TargetKind.UPDATE_PR, repo_full_name="valkey-io/valkey", branch="agent/fix/x",
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
        "timeout_minutes": str(JOB_MINUTES), "use_repo": "valkey-io/valkey", "use_git_ref": _HEAD,
        "extra_repository": "ianlancetaylor/libbacktrace", "extra_ref": "b9e40069c0b47a722286b94eb5231f7f05c08713",
        "extra_path": "libbacktrace", "ci_changes": "", "upstream": "",
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


def test_a_release_branchs_weekly_job_is_resolved_from_the_workflow_that_ran_it(front_door, workflows, tmp_path):
    """Weekly runs the default branch's daily.yml on each release branch: 8.0's own has no test-ubuntu-tls-io-threads."""
    plan = _pr_plan("test-sanitizer-address (clang)")
    failure = replace(plan.task.failure, source=Source.DAILY, workflow="weekly", workflow_sha="f" * 40)
    front_door.planned = replace(plan, task=replace(plan.task, failure=failure))

    outputs = _plan(tmp_path, front_door.planned)

    assert workflows == [("valkey-io/valkey", "daily", "f" * 40)]
    # The fix is still prepared on the commit it is pushed to.
    assert outputs["job"] == "test-sanitizer-address (clang)" and outputs["use_git_ref"] == _HEAD


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

    assert _plan(tmp_path, front_door.planned) == {"mode": "local", "ci_changes": "", "upstream": ""}
    assert (tmp_path / run.PLAN_FILE).exists()
    assert not (tmp_path / run.JOB_FILE).exists()


def test_a_workflow_that_cannot_be_read_falls_back_to_the_bots_runner(front_door, monkeypatch, tmp_path):
    front_door.planned = _pr_plan()

    def unreadable(*_args):
        raise GithubException(404, "Not Found", None)

    monkeypatch.setattr(run, "workflow_source", unreadable)

    assert _plan(tmp_path, front_door.planned) == {"mode": "local", "ci_changes": "", "upstream": ""}


@pytest.mark.parametrize(("branch", "policy", "kind", "expected"), [
    ("agent/backport/sweep/9.0", FixPolicy.BACKPORT, TargetKind.UPDATE_PR, "true"),
    ("agent/fix/x", FixPolicy.GENERAL, TargetKind.UPDATE_PR, ""),
    ("agent/backport/sweep/9.0", FixPolicy.GENERAL, TargetKind.UPDATE_PR, ""),
    ("agent/backport/sweep/9.0", FixPolicy.BACKPORT, TargetKind.SUGGEST, ""),
])
def test_only_a_bot_owned_backport_branch_asks_publish_for_workflows(
    front_door, workflows, tmp_path, branch, policy, kind, expected,
):
    plan = _pr_plan()
    target = replace(plan.task.target, branch=branch, kind=kind)
    front_door.planned = replace(plan, task=replace(plan.task, target=target, policy=policy))

    assert _plan(tmp_path, front_door.planned)["ci_changes"] == expected


def test_a_suggestion_is_never_run_in_a_job(front_door, workflows, tmp_path):
    plan = _pr_plan()
    front_door.planned = replace(plan, task=replace(plan.task, target=replace(plan.task.target,
                                                                             kind=TargetKind.SUGGEST)))

    assert _plan(tmp_path, front_door.planned) == {"mode": "local", "ci_changes": "", "upstream": ""}
    assert workflows == []


_UNSTABLE = "e" * 40
_SHARD_TEST = FailedTest("Replica keeps its shard_id", "tests/unit/cluster/reset.tcl")


def _sweep_plan(job_name="test-sanitizer-address (clang)") -> FixPlan:
    """A sweep PR's failure on 8.0, whose fix may be made for unstable too."""
    task = FixTask(
        Failure(repo_full_name="valkey-io/valkey", run_id=100, head_sha=_HEAD, job_id=22, job_name=job_name,
                base_branch="8.0", pr_number=5077, source=Source.DAILY, workflow="daily", workflow_sha="f" * 40),
        FixTarget(kind=TargetKind.UPDATE_PR, repo_full_name="valkey-io/valkey", base_repo_full_name="valkey-io/valkey",
                  branch="agent/backport/sweep/8.0", expected_sha=_HEAD, fix_branch="agent/backport/ci-sweep/8.0",
                  upstream_branch="unstable"),
        FixPolicy.BACKPORT,
    )
    return FixPlan(front_door=FrontDoor.FOLLOW_UP, task=task, job_log="the log")


_RELEASE_FIX = FixCandidate(
    action=CandidateAction.PATCH, patch="diff", changed_paths=(_SHARD_TEST.file,), failing_tests=(_SHARD_TEST,),
)


def test_a_sweeps_fix_is_planned_for_unstable_after_its_own(front_door, workflows, tmp_path):
    front_door.planned = _sweep_plan()

    assert _plan(tmp_path, front_door.planned)["upstream"] == "true"


def _unstable_repo(*, files=(_SHARD_TEST.file,)):
    repo = MagicMock()
    repo.get_branch.return_value.commit.sha = _UNSTABLE

    def get_contents(path, ref):
        assert ref == _UNSTABLE
        if path not in files:
            raise GithubException(404, "Not Found", None)

    repo.get_contents.side_effect = get_contents
    gh = MagicMock()
    gh.get_repo.return_value = repo
    return gh


def _plan_upstream(tmp_path, gh, candidate=_RELEASE_FIX, plan=None):
    save(tmp_path / "plan" / run.PLAN_FILE, plan or _sweep_plan())
    save(tmp_path / "fix" / run.OUTCOME_FILE, FixOutcome(candidate=candidate, started=True))
    return run.plan_upstream(gh, tmp_path / "plan", tmp_path / "fix", tmp_path / "out", tmp_path / "result")


def test_a_sweeps_fix_is_made_for_unstable_in_unstables_own_job(workflows, tmp_path):
    outputs = _plan_upstream(tmp_path, _unstable_repo())

    assert outputs["mode"] == "job" and outputs["job"] == "test-sanitizer-address (clang)"
    assert outputs["use_git_ref"] == _UNSTABLE
    # Unstable's own workflow at its tip, not the one the release branch ran.
    assert workflows == [("valkey-io/valkey", "daily", _UNSTABLE)]
    spec = load(tmp_path / "out" / run.JOB_FILE, JobSpec)
    assert (tmp_path / "out" / run.SETUP_FILE).read_text(encoding="utf-8") == setup_script(spec)
    assert load(tmp_path / "out" / run.PLAN_FILE, FixPlan) == _sweep_plan()
    assert load(tmp_path / "out" / run.RELEASE_FILE, FixCandidate) == _RELEASE_FIX
    assert (tmp_path / "out" / run.SHA_FILE).read_text(encoding="utf-8") == _UNSTABLE
    # Its outcome comes from that job.
    assert not (tmp_path / "result" / run.OUTCOME_FILE).exists()


def test_a_fix_for_a_test_unstable_lacks_is_published_as_it_is(workflows, tmp_path):
    outputs = _plan_upstream(tmp_path, _unstable_repo(files=()))

    assert outputs == {"mode": ""} and workflows == []
    assert load(tmp_path / "result" / run.OUTCOME_FILE, FixOutcome) == FixOutcome(candidate=_RELEASE_FIX, started=True)


@pytest.mark.parametrize("candidate", [
    None, replace(_RELEASE_FIX, adapts="c" * 40), replace(_RELEASE_FIX, not_run=True),
    replace(_RELEASE_FIX, action=CandidateAction.PORT), FixCandidate(action=CandidateAction.REFUSE),
])
def test_only_a_run_fix_of_the_bots_own_is_made_for_unstable(workflows, tmp_path, candidate):
    gh = _unstable_repo()

    assert _plan_upstream(tmp_path, gh, candidate) == {"mode": ""}
    assert load(tmp_path / "result" / run.OUTCOME_FILE, FixOutcome).candidate == candidate
    gh.get_repo.assert_not_called()


def test_a_fix_whose_job_has_no_result_is_left_to_publish(tmp_path):
    save(tmp_path / "plan" / run.PLAN_FILE, _sweep_plan())

    assert run.plan_upstream(MagicMock(), tmp_path / "plan", tmp_path / "fix", tmp_path / "out", tmp_path / "result") \
        == {"mode": ""}
    assert not (tmp_path / "result").exists()


def test_without_a_job_on_unstable_to_host_it_the_fix_is_made_on_the_bots_runner(workflows, tmp_path):
    outputs = _plan_upstream(tmp_path, _unstable_repo(), plan=_sweep_plan("test-no-workflow-has-this-job"))

    assert outputs == {"mode": "local"}
    assert not (tmp_path / "out" / run.JOB_FILE).exists()
    assert load(tmp_path / "out" / run.RELEASE_FILE, FixCandidate) == _RELEASE_FIX
    assert (tmp_path / "out" / run.SHA_FILE).read_text(encoding="utf-8") == _UNSTABLE
    assert not (tmp_path / "result" / run.OUTCOME_FILE).exists()


def test_the_fix_job_for_unstable_makes_the_release_branchs_fix_there(monkeypatch, tmp_path):
    save(tmp_path / run.PLAN_FILE, _sweep_plan())
    save(tmp_path / run.RELEASE_FILE, _RELEASE_FIX)
    (tmp_path / run.SHA_FILE).write_text(_UNSTABLE, encoding="utf-8")
    made = replace(_RELEASE_FIX, upstream_refusal="no")
    seen = {}

    def prepare_upstream(task, job_log, release, **kwargs):
        seen.update(task=task, log=job_log, release=release, **kwargs)
        return made

    monkeypatch.setattr(run, "prepare_upstream", prepare_upstream)
    monkeypatch.setattr(run, "prepare_fix", lambda *_a, **_k: pytest.fail("the release branch's fix is made"))

    outcome = run.fix(tmp_path, tmp_path / "out", repo_dir="/job")

    assert seen == {"task": _sweep_plan().task, "log": "the log", "release": _RELEASE_FIX, "sha": _UNSTABLE,
                    "repo_dir": "/job", "job": None}
    assert outcome == FixOutcome(candidate=made, started=True)


def test_a_crash_after_the_plan_is_reported_as_a_failed_attempt(front_door, tmp_path):
    """The saved plan records the silent attempt even when planning later fails."""
    front_door.planned = _pr_plan()

    def unreadable(_repo, _job):
        raise OSError("the log download kept failing")

    artifacts = SimpleNamespace(download_job_log=unreadable)
    outputs = run.plan(MagicMock(), artifacts, FrontDoor.PR, {}, bot_login=_BOT, out=tmp_path)

    assert outputs == {"mode": "done", "action": "failed"}
    (result,) = front_door.reported
    assert result.kind is ResultKind.FAILED and "the log download kept failing" in result.summary
    assert load(tmp_path / run.PLAN_FILE, FixPlan) == front_door.planned


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
    assert load(tmp_path / "out" / run.OUTCOME_FILE, FixOutcome) == outcome
    assert outcome == FixOutcome(candidate=_CANDIDATE, started=True)


def test_a_fix_job_stopped_mid_fix_leaves_an_attempt_that_started(monkeypatch, tmp_path):
    save(tmp_path / run.PLAN_FILE, _pr_plan())
    monkeypatch.setattr(run, "prepare_fix", MagicMock(side_effect=KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        run.fix(tmp_path, tmp_path / "out")

    left = load(tmp_path / "out" / run.OUTCOME_FILE, FixOutcome)
    assert left.started and left.candidate is None and "stopped before the fix finished" in left.error


def test_a_failure_before_the_fix_engine_is_recorded(tmp_path):
    outcome = run.record_failure(tmp_path / "out", "setup replay failed")

    assert outcome == FixOutcome(error="setup replay failed")
    assert load(tmp_path / "out" / run.OUTCOME_FILE, FixOutcome) == outcome


def test_a_setup_replay_failure_records_its_phase_and_run(tmp_path):
    script = Path("scripts/agent_fix/job.sh").read_text(encoding="utf-8")
    writer = script[script.index("write_failure_outcome() {"):script.index("\nrestore_agent() {")]
    setup_job = script[script.index("setup_job() {"):script.index("\nrun_fix() {")]
    agent = tmp_path / "agent"
    agent.mkdir()
    program = f"""
set -u
read_state() {{ :; }}
python() {{
  if [[ "$*" == *scripts.agent_fix.sandbox* ]]; then
    return 23
  fi
  return 127
}}
{writer}
{setup_job}
mode=job
user="$(id -un)"
uid="$(id -u)"
agent="$TEST_AGENT"
repo="$TEST_AGENT"
sandbox="$TEST_AGENT"
out="$TEST_OUT"
set +e
setup_job
status=$?
printf '%s\\n' "$status"
"""
    result = subprocess.run(
        ["bash", "-c", program],
        env={
            **os.environ,
            "TEST_AGENT": str(agent),
            "TEST_OUT": str(tmp_path / "out"),
            "GITHUB_SERVER_URL": "https://github.example",
            "GITHUB_REPOSITORY": "valkey-io/valkey-ci-agent",
            "GITHUB_RUN_ID": "41394055294",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "23"
    recorded = json.loads((tmp_path / "out" / run.OUTCOME_FILE).read_text(encoding="utf-8"))
    assert "setup and build replay failed before the fix agent ran" in recorded["error"]
    assert (
        "https://github.example/valkey-io/valkey-ci-agent/actions/runs/41394055294"
        in recorded["error"]
    )


def test_agent_setup_failure_records_a_result_without_python_or_a_staged_checkout(tmp_path):
    script = Path("scripts/agent_fix/job.sh").read_text(encoding="utf-8")
    writer = script[script.index("write_failure_outcome() {"):script.index("\nrestore_agent() {")]
    ensure = script[script.index("ensure_result() {"):script.index("\ncleanup() {")]
    program = f"""
set -u
{writer}
{ensure}
out="$TEST_OUT"
GITHUB_SERVER_URL="https://github.example"
GITHUB_REPOSITORY="valkey-io/valkey-ci-agent"
GITHUB_RUN_ID="41643802266"
ensure_result
"""

    result = subprocess.run(
        ["bash", "-c", program],
        env={**os.environ, "TEST_OUT": str(tmp_path / "out")},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    recorded = json.loads((tmp_path / "out" / run.OUTCOME_FILE).read_text(encoding="utf-8"))
    assert "fix job stopped before the fix engine wrote a result" in recorded["error"]
    assert "https://github.example/valkey-io/valkey-ci-agent/actions/runs/41643802266" in recorded["error"]


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


def test_only_an_attempt_whose_engine_never_started_is_marked_on_its_plan(
    front_door, monkeypatch, tmp_path,
):
    """A runner that could not install or build says nothing about the failure: the plan records that."""
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    save(tmp_path / run.PLAN_FILE, _pr_plan())
    monkeypatch.setattr(run, "prepare_fix", MagicMock(side_effect=RuntimeError("agent died")))
    run.fix(tmp_path, tmp_path / "crashed")
    run.record_failure(tmp_path / "setup", "the failed job's setup and build replay failed")

    _publish(tmp_path, tmp_path / "crashed")
    assert not load(tmp_path / run.PLAN_FILE, FixPlan).never_started and not outputs.exists()

    for never_started in ("setup", "nothing"):
        save(tmp_path / run.PLAN_FILE, _pr_plan())
        _publish(tmp_path, tmp_path / never_started)
        assert load(tmp_path / run.PLAN_FILE, FixPlan) == replace(_pr_plan(), never_started=True)
    assert outputs.read_text(encoding="utf-8") == "never_started=true\n" * 2
    assert all(result.kind is ResultKind.FAILED for result in front_door.reported)


def test_a_result_that_cannot_be_read_is_reported_as_failed(front_door, tmp_path):
    save(tmp_path / run.PLAN_FILE, _pr_plan())
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / run.OUTCOME_FILE).write_text('{"candidate": {"not": "a candidate"', encoding="utf-8")

    _publish(tmp_path, tmp_path / "out")

    (unreadable,) = front_door.reported
    assert unreadable.kind is ResultKind.FAILED and "could not be read" in unreadable.summary
    assert not load(tmp_path / run.PLAN_FILE, FixPlan).never_started


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
    assert failed.kind is ResultKind.FAILED and "AGENT_FIX_CI_ISSUES_PUBLISH must be one of comment, draft-pr" in failed.summary


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


@pytest.mark.parametrize(("action", "exit_code"), [("failed", 1), ("refused", 0)])
def test_the_cli_fails_only_for_failed_publication(monkeypatch, action, exit_code):
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
    monkeypatch.setattr(run, "publish_and_report", lambda *_args, **_kwargs: {"action": action})

    assert run.main(["publish", "--plan", "plan", "--outcome", "outcome"]) == exit_code


def test_prior_followup_plans_load_from_agent_artifacts() -> None:
    plan = replace(_pr_plan(), front_door=FrontDoor.FOLLOW_UP)
    client = MagicMock()
    client.list_named_artifacts.return_value = [
        WorkflowArtifact(1, "agent-fix-plan-x", 1, False, run_id=10),
        WorkflowArtifact(2, "agent-fix-plan-x", 1, True, run_id=9),
        WorkflowArtifact(3, "agent-fix-plan-x", 1, False, run_id=8),
        WorkflowArtifact(4, "agent-fix-plan-x", 1, False, run_id=7),
    ]
    client.download_artifact.side_effect = [
        {"plan.json": json.dumps(encode(plan)).encode()},
        {"plan.json": b"{bad"},
    ]

    loaded = run.load_prior_plans(
        client,
        "o/agent",
        "agent-fix-plan-x",
        current_run_id=10,
    )

    assert loaded == (plan,)
    client.list_named_artifacts.assert_called_once_with(
        "o/agent",
        "agent-fix-plan-x",
        max_artifacts=run._PRIOR_PLAN_ARTIFACTS,
    )
    assert client.download_artifact.call_count == 2


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

    from scripts.agent_fix import ci_job, models, verification

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
    assert {models.Failure, ci_job.JobSpec, verification.JobCommands} <= seen
    assert offenders == []


def test_the_run_page_says_whether_a_fix_was_published(tmp_path, monkeypatch):
    """A refused fix is a green run: the summary keeps it from reading as a fix."""
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    run.write_summary({"action": "refused", "summary": "No safe fix."})
    run.write_summary({"action": "suggested", "summary": "Patched the test."})

    text = summary.read_text(encoding="utf-8")
    assert "**refused** (no fix published)" in text and "No safe fix." in text
    assert "**suggested** (a fix was published)" in text


# --- a labelled issue: no failed job, a change made from a test written first -------

def _label_plan(**target) -> FixPlan:
    """A labelled issue's plan: on unstable's tip, hosted by Valkey's pull request job."""
    task = FixTask(
        Failure(repo_full_name="valkey-io/valkey", run_id=0, head_sha=_HEAD, job_id=0, job_name="test-ubuntu-latest",
                base_branch="unstable", issue_number=7, workflow="ci"),
        FixTarget(**{"kind": TargetKind.DRAFT_PR, "repo_full_name": "valkey-io/valkey", "branch": "agent/fix/issue-7-5",
                     "expected_sha": _HEAD, **target}),
        FixPolicy.GENERAL,
        request=IssueRequest(number=7, title="value is wrong", body="It says before."),
    )
    return FixPlan(front_door=FrontDoor.LABEL, task=task, attempt=1)


def test_a_labelled_issue_has_no_log_and_is_hosted_by_the_pull_request_job(monkeypatch, workflows, tmp_path):
    planned = _label_plan()
    monkeypatch.setitem(run._FRONT_DOORS, FrontDoor.LABEL, (lambda *_a, **_k: planned, MagicMock()))
    artifacts = SimpleNamespace(download_job_log=lambda *_a: pytest.fail("no job failed"))

    outputs = run.plan(MagicMock(), artifacts, FrontDoor.LABEL, {}, bot_login=_BOT, out=tmp_path)

    assert (outputs["mode"], outputs["job"], outputs["use_git_ref"]) == ("job", "test-ubuntu-latest", _HEAD)
    assert outputs["use_repo"] == "valkey-io/valkey"
    # The branch's own workflow, at the commit the change is made on.
    assert workflows == [("valkey-io/valkey", "ci", _HEAD)]
    assert load(tmp_path / run.PLAN_FILE, FixPlan) == planned


def test_the_change_is_made_on_the_targets_code_whichever_fork_its_branch_goes_to(monkeypatch, workflows, tmp_path):
    planned = _label_plan(repo_full_name="valkeyrie/valkey", base_repo_full_name="valkey-io/valkey")
    monkeypatch.setitem(run._FRONT_DOORS, FrontDoor.LABEL, (lambda *_a, **_k: planned, MagicMock()))

    outputs = run.plan(MagicMock(), SimpleNamespace(), FrontDoor.LABEL, {}, bot_login=_BOT, out=tmp_path)

    assert outputs["use_repo"] == "valkey-io/valkey"


def test_the_head_of_the_bots_own_change_pr_is_never_built_by_a_jobs_setup(workflows):
    """A job's setup and build run as the workflow user; that head is a change nobody has reviewed."""
    head = replace(_label_plan(kind=TargetKind.UPDATE_PR).task, request=None)
    made_again = _label_plan(kind=TargetKind.UPDATE_PR, replaces="c" * 40).task

    assert run.job_for(MagicMock(), head) is None
    # The change made again is made on the base branch's tip, which is trusted.
    assert run.job_for(MagicMock(), made_again).name == "test-ubuntu-latest"
    assert run.job_for(MagicMock(), _label_plan().task).name == "test-ubuntu-latest"
    # A detector issue's fix shares the prefix, not the rule: its failed job is replayed as before.
    detector = replace(head, target=replace(head.target, branch="agent/fix/ci-issue-7-100"))
    assert run.job_for(MagicMock(), detector).name == "test-ubuntu-latest"


def test_a_labelled_issues_change_is_prepared_from_a_test_not_a_log(monkeypatch, tmp_path):
    save(tmp_path / run.PLAN_FILE, _label_plan())
    seen = {}

    def prepare_change(task, *, repo_dir, job):
        seen.update(task=task, repo_dir=repo_dir, job=job)
        return _CANDIDATE

    monkeypatch.setattr(run, "prepare_change", prepare_change)
    monkeypatch.setattr(run, "prepare_fix", lambda *_a, **_k: pytest.fail("no job failed"))

    outcome = run.fix(tmp_path, tmp_path / "out", repo_dir="/checkout")

    assert outcome == FixOutcome(candidate=_CANDIDATE, started=True)
    assert seen == {"task": _label_plan().task, "repo_dir": "/checkout", "job": None}


@pytest.mark.parametrize(("value", "mode"), [("", IssuePublication.COMMENT), ("draft-pr", IssuePublication.DRAFT_PR)])
def test_how_a_labelled_issues_change_is_published_is_its_own_variables_choice(monkeypatch, tmp_path, value, mode):
    save(tmp_path / run.PLAN_FILE, _label_plan())
    save(tmp_path / run.OUTCOME_FILE, FixOutcome(candidate=_CANDIDATE))
    monkeypatch.setenv("AGENT_FIX_PUBLISH", value)
    # The detector's variable is for the detector's issues.
    monkeypatch.setenv(run.ISSUE_PUBLICATION_ENV, "draft-pr" if not value else "comment")
    monkeypatch.setitem(run._FRONT_DOORS, FrontDoor.LABEL, (MagicMock(), lambda _gh, _plan, result: result))
    seen = {}

    def publish(_gh, task, candidate, *, git_env, issue_publication):
        seen["mode"] = issue_publication
        return FixResult(kind=ResultKind.SUGGESTED, summary="s", task=task)

    monkeypatch.setattr(run, "publish_candidate", publish)
    monkeypatch.setattr(run.labelled, "hold_reason", lambda _repo, _bot: "")

    _publish(tmp_path, tmp_path)

    assert seen["mode"] is mode
    # As many pull requests are open as may be, since the run was dispatched: the change is posted instead.
    monkeypatch.setattr(run.labelled, "hold_reason", lambda _repo, _bot: "5 are open")
    _publish(tmp_path, tmp_path)
    assert seen["mode"] is IssuePublication.COMMENT


def test_a_review_round_is_prepared_on_the_pull_requests_head(monkeypatch, tmp_path):
    plan = _label_plan(kind=TargetKind.UPDATE_PR)
    reviewed = replace(plan, task=replace(plan.task, request=replace(plan.task.request, pr_number=9, review=("alice: rename it",))))
    save(tmp_path / run.PLAN_FILE, reviewed)
    monkeypatch.setattr(run, "prepare_review", lambda task, *, repo_dir, job: _CANDIDATE)
    monkeypatch.setattr(run, "prepare_change", lambda *_a, **_k: pytest.fail("the pull request has its test"))

    assert run.fix(tmp_path, tmp_path / "out").candidate == _CANDIDATE


def test_looking_after_a_pull_request_counts_its_failed_attempts_from_earlier_plans(monkeypatch, tmp_path):
    planner = MagicMock(return_value={"action": "skipped"})
    monkeypatch.setitem(run._FRONT_DOORS, FrontDoor.UPKEEP, (planner, MagicMock()))
    earlier = (replace(_pr_plan(), front_door=FrontDoor.UPKEEP),)

    run.plan(
        MagicMock(), None, FrontDoor.UPKEEP, {"repo": "valkey-io/valkey"}, bot_login="bot", out=tmp_path,
        prior_plans=earlier,
    )

    assert planner.call_args.kwargs["prior_plans"] == earlier


def test_a_pull_request_a_committer_took_over_during_the_run_is_not_pushed_to(monkeypatch, tmp_path):
    plan = _pr_plan()
    save(tmp_path / run.PLAN_FILE, replace(plan, front_door=FrontDoor.UPKEEP))
    save(tmp_path / run.OUTCOME_FILE, FixOutcome(candidate=_CANDIDATE))
    reported = []
    monkeypatch.setitem(run._FRONT_DOORS, FrontDoor.UPKEEP, (MagicMock(), lambda _gh, _plan, result: reported.append(result)))
    monkeypatch.setattr(run.upkeep, "taken_over_since", lambda _gh, _plan, _bot: "A committer has reviewed this pull request.")
    monkeypatch.setattr(run, "publish_candidate", lambda *_a, **_k: pytest.fail("nothing is pushed"))

    _publish(tmp_path, tmp_path)

    assert reported[0].kind is ResultKind.REFUSED and "A committer has reviewed" in reported[0].summary


@pytest.mark.parametrize(("pushed_to", "token"), [("valkeyrie/valkey", "fork-token"), ("valkey-io/valkey", "token")])
def test_a_push_to_the_bots_fork_uses_the_forks_token(monkeypatch, tmp_path, pushed_to, token):
    save(tmp_path / run.PLAN_FILE, _label_plan(repo_full_name=pushed_to))
    used = []

    class AuthContext:
        def __init__(self, token, **_kwargs):
            used.append(token)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def env(self):
            return {}

    monkeypatch.setenv("TARGET_TOKEN", "token")
    monkeypatch.setenv("PUSH_TOKEN", "fork-token")
    monkeypatch.setenv("AGENT_FIX_PUSH_REPO", "valkeyrie/valkey")
    monkeypatch.setattr(run, "Github", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(run, "GitAuth", AuthContext)
    monkeypatch.setattr(run, "publish_and_report", lambda *_args, **_kwargs: {"action": "opened"})

    assert run.main(["publish", "--plan", str(tmp_path), "--outcome", str(tmp_path)]) == 0
    assert used == [token]
