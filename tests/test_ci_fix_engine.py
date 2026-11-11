from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.ci_fix import engine as engine_mod
from scripts.ci_fix.agent import AgentTimeout
from scripts.ci_fix.ci_job import JobCommands
from scripts.ci_fix.models import (
    CandidateAction,
    Failure,
    FixCandidate,
    FixContext,
    FixPolicy,
    FixResult,
    FixTarget,
    FixTask,
    ResultKind,
    TargetKind,
)
from scripts.ci_fix.verification import TestRun, VerifyPlan
from tests.git_helpers import git


@pytest.fixture(autouse=True)
def _approving_review_and_no_tests(monkeypatch):
    """Keep edit-loop tests focused on the edit loop.

    Every failure gets a plan whose build and test pass, unless a test plans
    its own. The real review and test runner start agents and processes;
    these tests use their own scripts directly instead.
    """
    monkeypatch.setattr(engine_mod, "review_candidate", lambda *a, **k: (True, "looks right"))
    monkeypatch.setattr(engine_mod, "isolated_user", lambda: "")
    monkeypatch.setattr(
        engine_mod, "plan_for", lambda repo_dir, tests, **_kwargs: VerifyPlan(build="true", test="true", tests=tests),
    )


_LOG = "exact failed job log"
_REAL_PLAN_FOR = engine_mod.plan_for


def test_a_c_unit_tests_file_is_located_in_the_checkout(tmp_path):
    (tmp_path / "src" / "unit").mkdir(parents=True)
    (tmp_path / "src" / "unit" / "test_crc64.cpp").write_text("", encoding="utf-8")
    tests = (FailedTest("Crc64Test.TestCrc64", "test_crc64.cpp"), FailedTest("x", "tests/unit/expire.tcl"),
             FailedTest("Gone.Test", "test_gone.cpp"))

    assert engine_mod.locate_unit_tests(str(tmp_path), tests) == (
        FailedTest("Crc64Test.TestCrc64", "src/unit/test_crc64.cpp"), tests[1], tests[2],
    )


def test_a_sentinel_test_is_searched_for_by_name_though_nothing_runs_it(tmp_path):
    """Its name finds the upstream fix (#5274); with no file, no plan runs it and no history is read."""
    log = "09:24:21> SENTINEL SIMULATE-FAILURE valkey-cli hint: FAILED: caught an error in the test\n"
    tests = engine_mod.failed_tests(_task("a" * 40), log)

    assert tests == (FailedTest("SENTINEL SIMULATE-FAILURE valkey-cli hint", ""),)
    assert _REAL_PLAN_FOR(str(tmp_path), tests, scheduled=False) is None
    # A runtest failure in the same log is the one to fix.
    both = "[err]: INCR fails in tests/unit/type/incr.tcl\n" + log
    assert engine_mod.failed_tests(_task("a" * 40), both) == (FailedTest("INCR fails", "tests/unit/type/incr.tcl"),)


def _source_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "value.txt").write_text("before\n", encoding="utf-8")
    git(repo, "add", "value.txt")
    git(repo, "commit", "-m", "initial")
    return repo, git(repo, "rev-parse", "HEAD")


def _task(
    head_sha: str,
    *,
    kind: TargetKind = TargetKind.UPDATE_PR,
    job_id: int = 99,
) -> FixTask:
    failure = Failure(
        repo_full_name="owner/repo",
        run_id=42,
        head_sha=head_sha,
        job_id=job_id,
        job_name="tests",
        base_branch="main",
        pr_number=17,
    )
    target = FixTarget(
        kind=kind,
        repo_full_name="owner/repo",
        branch="agent/ci-fix/17",
        expected_sha=head_sha,
    )
    return FixTask(failure, target, FixPolicy.GENERAL)


def _install_clone(monkeypatch, source: Path):
    calls = []

    def fake_clone(repo_full_name, destination, sha, *, pull_number=0):
        calls.append((repo_full_name, sha, pull_number))
        shutil.copytree(source, destination)
        git(destination, "checkout", "--detach", sha)
        # Like a real clone: the default branch as origin/HEAD. A test's
        # "upstream" branch stands for it when there is one.
        default = "upstream" if git(destination, "branch", "--list", "upstream") else sha
        git(destination, "update-ref", "refs/remotes/origin/main", default)
        git(destination, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        return True

    monkeypatch.setattr(engine_mod, "shallow_clone_at_sha", fake_clone)
    return calls


def test_the_budget_fits_the_aws_session_and_leaves_a_round_after_verification() -> None:
    assert engine_mod.PREPARE_BUDGET_S < engine_mod.AWS_SESSION_S
    assert engine_mod.PREPARE_BUDGET_S - engine_mod._VERIFY_RESERVE_S >= engine_mod._MIN_ROUND_S


def test_prepare_fix_binds_exact_job_and_builds_binary_patch(tmp_path, monkeypatch) -> None:
    source, head_sha = _source_repo(tmp_path)
    clone_calls = _install_clone(monkeypatch, source)
    seen = {}

    def agent(task, repo_dir, job_log, **_kwargs):
        seen["task"] = task
        seen["job_log"] = job_log
        (Path(repo_dir) / "value.txt").write_text("after\n", encoding="utf-8")
        (Path(repo_dir) / "asset.bin").write_bytes(b"\x00\x01\x02new binary\xff")
        return FixCandidate(
            action=CandidateAction.PATCH,
            summary="Prepared the fix",
            failing_check="tests",
            root_cause="The expected value is stale.",
            reasoning="Update the value and binary fixture.",
            validation="focused check",
            changed_paths=("asset.bin", "value.txt"),
        )

    task = _task(head_sha)
    candidate = engine_mod.prepare_fix(task, _LOG, agent_func=agent)

    assert clone_calls == [("owner/repo", head_sha, 17)]
    assert seen == {"task": task, "job_log": "exact failed job log"}
    assert candidate.action is CandidateAction.PATCH
    assert candidate.changed_paths == ("asset.bin", "value.txt")
    assert "diff --git a/value.txt b/value.txt" in candidate.patch
    assert "diff --git a/asset.bin b/asset.bin" in candidate.patch
    assert "GIT binary patch" in candidate.patch


def test_prepare_fix_refuses_without_exact_log_or_edits(tmp_path, monkeypatch) -> None:
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    task = _task(head_sha)

    no_log = engine_mod.prepare_fix(task, "", agent_func=lambda *args, **kwargs: None)
    assert no_log.action is CandidateAction.REFUSE
    assert "exact failed-job log is unavailable" in no_log.summary

    no_edits = engine_mod.prepare_fix(
        task,
        _LOG,
        agent_func=lambda *args, **kwargs: FixCandidate(
            action=CandidateAction.PATCH,
            failing_check="tests",
            root_cause="cause",
            reasoning="fix",
            changed_paths=("value.txt",),
        ),
    )
    assert no_edits.action is CandidateAction.REFUSE
    assert "made no working-tree changes" in no_edits.summary


def test_prepare_fix_rejects_agent_commit(tmp_path, monkeypatch) -> None:
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)

    def committing_agent(task, repo_dir, job_log, **_kwargs):
        repo = Path(repo_dir)
        (repo / "value.txt").write_text("committed\n", encoding="utf-8")
        git(repo, "add", "value.txt")
        git(repo, "commit", "-m", "agent must not commit")
        return FixCandidate(action=CandidateAction.PATCH)

    candidate = engine_mod.prepare_fix(
        _task(head_sha),
        _LOG,
        agent_func=committing_agent,
    )

    assert candidate.action is CandidateAction.REFUSE
    assert "created a commit" in candidate.summary


def test_prepare_fix_sends_a_mixed_port_and_edits_back(tmp_path, monkeypatch) -> None:
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    feedback = []

    def mixed_agent(task, repo_dir, job_log, **kwargs):
        feedback.append(kwargs["feedback"])
        (Path(repo_dir) / "value.txt").write_text("edited\n", encoding="utf-8")
        return FixCandidate(
            action=CandidateAction.PORT,
            failing_check="tests",
            root_cause="The branch lacks the upstream repair.",
            reasoning="Port the merged repair.",
            port_commit="b" * 40,
            validation="not authoritative",
        )

    candidate = engine_mod.prepare_fix(
        _task(head_sha),
        _LOG,
        agent_func=mixed_agent,
    )

    assert candidate.action is CandidateAction.REFUSE
    assert "mixed an upstream port" in feedback[1]
    assert "still holds your previous edits" in feedback[1]


def test_a_mistake_in_what_the_agent_hands_back_goes_back_to_it(tmp_path, monkeypatch) -> None:
    """A fix with no root cause is for the agent to complete, not a final refusal."""
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    feedback = []

    def agent(task, repo_dir, job_log, **kwargs):
        feedback.append(kwargs["feedback"])
        (Path(repo_dir) / "value.txt").write_text("fixed\n", encoding="utf-8")
        # The failing check it left out is the job's, and a field it left empty does not cost a round.
        return FixCandidate(action=CandidateAction.PATCH, root_cause="", reasoning="fix")

    candidate = engine_mod.prepare_fix(
        _task(head_sha), _LOG, agent_func=agent, review_func=lambda *a, **k: (True, "fixes it"),
    )

    assert candidate.action is CandidateAction.PATCH and candidate.failing_check == "tests"
    assert feedback == [""]


def _upstream_commit(source: Path, head_sha: str, content: str = "fixed\n") -> str:
    """A commit on another branch of ``source``, as a merged upstream fix would be."""
    git(source, "checkout", "-q", "-b", "upstream")
    (source / "value.txt").write_text(content, encoding="utf-8")
    git(source, "commit", "-qam", "Fix the value (#1)")
    commit = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "-q", head_sha)
    return commit


def _port_agent(*commits):
    rounds = iter(commits)

    def agent(task, repo_dir, job_log, *, feedback="", **_kwargs):  # noqa: ARG001
        agent.feedback.append(feedback)
        return FixCandidate(
            action=CandidateAction.PORT,
            failing_check="tests",
            root_cause="The branch lacks the upstream repair.",
            reasoning="Port the merged repair.",
            port_commit=next(rounds),
            validation="model claimed success",
        )

    agent.feedback = []
    return agent


def test_a_port_that_does_not_apply_comes_back_with_its_diff(tmp_path, monkeypatch) -> None:
    """The agent cannot run git: without the diff it cannot adapt the commit it found."""
    source, base_sha = _source_repo(tmp_path)
    commit = _upstream_commit(source, base_sha)
    (source / "value.txt").write_text("changed on this branch\n", encoding="utf-8")
    git(source, "commit", "-qam", "Diverge")
    head_sha = git(source, "rev-parse", "HEAD")
    _install_clone(monkeypatch, source)
    agent = _port_agent(commit, commit, commit)

    engine_mod.prepare_fix(_task(head_sha), _LOG, agent_func=agent)

    assert "Adapt it as a `patch` if it is the right fix" in agent.feedback[1]
    assert "Its diff:" in agent.feedback[1] and "-before\n+fixed" in agent.feedback[1]


def test_prepare_fix_refuses_a_port_without_a_full_sha(tmp_path, monkeypatch) -> None:
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)

    candidate = engine_mod.prepare_fix(
        _task(head_sha),
        _LOG,
        agent_func=_port_agent("abc123", "abc123", "abc123"),
        review_func=lambda *a, **k: (True, "unused"),
    )

    assert candidate.action is CandidateAction.REFUSE
    assert "not a full commit SHA" in candidate.summary


def test_prepare_fix_accepts_clean_full_sha_port_without_validation(tmp_path, monkeypatch) -> None:
    source, head_sha = _source_repo(tmp_path)
    commit = _upstream_commit(source, head_sha)
    _install_clone(monkeypatch, source)
    reviewed = []

    candidate = engine_mod.prepare_fix(
        _task(head_sha),
        _LOG,
        agent_func=_port_agent(commit),
        review_func=lambda candidate, *a, **k: reviewed.append(candidate.patch) or (True, "fixes it"),
    )

    assert candidate.action is CandidateAction.PORT
    assert candidate.port_commit == commit
    assert candidate.patch == ""
    assert candidate.validation == ""
    # The port was cherry-picked into the checkout for its review.
    assert "+fixed" in reviewed[0]
    assert "Review: fixes it" in candidate.verification


def test_other_issues_are_checked_with_a_ports_change_still_in_the_tree(tmp_path, monkeypatch) -> None:
    """A port's cherry-pick is undone after its round: the check must come first, with its patch."""
    source, head_sha = _source_repo(tmp_path)
    commit = _upstream_commit(source, head_sha)
    _install_clone(monkeypatch, source)
    seen = []

    def other_issues_touched(_task, candidate, repo_dir, *_args, **_kwargs):
        tree = subprocess.run(["git", "-C", repo_dir, "diff", "HEAD"], capture_output=True, text=True).stdout
        seen.append(("+fixed" in candidate.patch, "+fixed" in tree))
        return ()

    monkeypatch.setattr(engine_mod, "other_issues_touched", other_issues_touched)
    engine_mod.prepare_fix(
        _task(head_sha), _LOG, agent_func=_port_agent(commit), review_func=lambda *a, **k: (True, "ok"),
    )

    assert seen == [(True, True)]


def test_a_port_that_does_not_apply_goes_back_to_the_agent(tmp_path, monkeypatch) -> None:
    source, head_sha = _source_repo(tmp_path)
    commit = _upstream_commit(source, head_sha)
    _install_clone(monkeypatch, source)
    agent = _port_agent("b" * 40, commit)

    candidate = engine_mod.prepare_fix(_task(head_sha), _LOG, agent_func=agent)

    assert candidate.action is CandidateAction.PORT
    assert candidate.port_commit == commit
    assert agent.feedback[1].startswith(f"Refusing to port {'b' * 12}:")


def test_a_port_is_tested_and_the_checkout_restored(tmp_path, monkeypatch) -> None:
    source, head_sha = _source_repo(tmp_path)
    commit = _upstream_commit(source, head_sha)
    _install_clone(monkeypatch, source)
    seen = []

    def test(repo_dir, command, timeout):
        seen.append((Path(repo_dir) / "value.txt").read_text())
        return TestRun(passed=True, output="ok")

    def agent(task, repo_dir, job_log, **_kwargs):
        agent.trees.append((Path(repo_dir) / "value.txt").read_text())
        return _port_agent(commit)(task, repo_dir, job_log)

    agent.trees = []
    monkeypatch.setattr(engine_mod, "_verify", lambda candidate, repo_dir, *a: seen.append(
        (Path(repo_dir) / "value.txt").read_text()) or "it still fails")
    candidate = engine_mod.prepare_fix(_task(head_sha), _LOG, agent_func=agent, test_func=test)

    assert candidate.action is CandidateAction.REFUSE
    # Each round tested the ported content, and each next round started clean.
    assert seen == ["fixed\n"] * engine_mod.MAX_ROUNDS
    assert agent.trees == ["before\n"] * engine_mod.MAX_ROUNDS


def test_prepare_fix_caps_comment_sized_patch(tmp_path, monkeypatch) -> None:
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    suggestion = engine_mod.target_capabilities(TargetKind.SUGGEST)
    monkeypatch.setattr(
        engine_mod,
        "target_capabilities",
        lambda _kind: replace(suggestion, patch_limit=100),
    )

    def large_agent(task, repo_dir, job_log, **_kwargs):
        (Path(repo_dir) / "value.txt").write_text("x" * 1000, encoding="utf-8")
        return FixCandidate(
            action=CandidateAction.PATCH,
            failing_check="tests",
            root_cause="cause",
            reasoning="fix",
            changed_paths=("value.txt",),
        )

    candidate = engine_mod.prepare_fix(
        _task(head_sha, kind=TargetKind.SUGGEST),
        _LOG,
        agent_func=large_agent,
    )

    assert candidate.action is CandidateAction.REFUSE
    assert "too large" in candidate.summary
    assert "100 characters" in candidate.summary


def test_prepare_fix_rejects_an_action_disabled_by_target_capabilities(
    tmp_path,
    monkeypatch,
) -> None:
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    capabilities = engine_mod.target_capabilities(TargetKind.UPDATE_PR)
    monkeypatch.setattr(
        engine_mod,
        "target_capabilities",
        lambda _kind: replace(
            capabilities,
            allowed_actions=frozenset(
                {CandidateAction.PORT, CandidateAction.REFUSE}
            ),
        ),
    )

    def patch_agent(task, repo_dir, job_log, **_kwargs):
        (Path(repo_dir) / "value.txt").write_text("after\n", encoding="utf-8")
        return FixCandidate(
            action=CandidateAction.PATCH,
            failing_check="tests",
            root_cause="cause",
            reasoning="fix",
            changed_paths=("value.txt",),
        )

    candidate = engine_mod.prepare_fix(
        _task(head_sha),
        _LOG,
        agent_func=patch_agent,
    )

    assert candidate.action is CandidateAction.REFUSE
    assert "patch action is not supported" in candidate.summary


def test_a_file_the_agent_changed_and_did_not_list_is_pointed_out_not_refused(tmp_path, monkeypatch) -> None:
    """The tree is the patch. A list that does not match it used to cost a whole round, of up to two hours."""
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    reviewed = []

    def agent(task, repo_dir, job_log, **_kwargs):
        (Path(repo_dir) / "value.txt").write_text("fixed\n", encoding="utf-8")
        (Path(repo_dir) / "generated.txt").write_text("incidental\n", encoding="utf-8")
        return FixCandidate(
            action=CandidateAction.PATCH,
            failing_check="tests",
            root_cause="cause",
            reasoning="fix",
            changed_paths=("value.txt",),
        )

    candidate = engine_mod.prepare_fix(
        _task(head_sha), _LOG, agent_func=agent,
        review_func=lambda candidate, *a, **k: reviewed.append(candidate.verification) or (True, "both belong"),
    )

    assert candidate.action is CandidateAction.PATCH
    assert candidate.changed_paths == ("generated.txt", "value.txt") and "+incidental" in candidate.patch
    # The reviewer, and the report, are told which file was not listed.
    note = "The patch also changes `generated.txt`, which its author did not list"
    assert note in reviewed[0] and note in candidate.verification


def test_prepare_fix_gives_the_agent_the_whole_job_log(
    tmp_path,
    monkeypatch,
) -> None:
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    seen = {}
    log = "start-" + "x" * 1_000_000 + "-final-error"

    def agent(task, repo_dir, job_log, **_kwargs):
        seen["log"] = job_log
        return FixCandidate(action=CandidateAction.REFUSE, summary="no fix")

    candidate = engine_mod.prepare_fix(_task(head_sha), log, agent_func=agent)

    assert candidate.action is CandidateAction.REFUSE
    # The failing test's own output is often in the middle of a long log.
    assert seen["log"] == log


def test_draft_issue_uses_current_target_sha_with_exact_old_run_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    source, current_sha = _source_repo(tmp_path)
    clone_calls = _install_clone(monkeypatch, source)
    task = _task(current_sha, kind=TargetKind.DRAFT_PR)
    task = replace(
        task,
        failure=replace(task.failure, head_sha="f" * 40),
    )

    candidate = engine_mod.prepare_fix(
        task,
        _LOG,
        agent_func=lambda *args, **kwargs: FixCandidate(
            action=CandidateAction.REFUSE,
            summary="already fixed on the current branch",
        ),
    )

    assert candidate.action is CandidateAction.REFUSE
    assert clone_calls == [("owner/repo", current_sha, 17)]


# --- the feedback loop: test without and with the patch, review, retry -------------

def _value_agent(*values, failure_type=None):
    """An agent writing each of ``values`` in turn, recording the feedback it got."""
    from scripts.ci_fix.models import FailureType

    rounds = iter(values)
    feedback = []

    def agent(task, repo_dir, job_log, *, feedback="", **_kwargs):  # noqa: ARG001
        agent.feedback.append(feedback)
        (Path(repo_dir) / "value.txt").write_text(f"{next(rounds)}\n", encoding="utf-8")
        return FixCandidate(
            action=CandidateAction.PATCH, failing_check="value check", root_cause="stale value",
            reasoning="set the right value", changed_paths=("value.txt",),
            failure_type=failure_type or FailureType.DETERMINISTIC,
        )

    agent.feedback = feedback
    return agent


def _prepare(tmp_path, monkeypatch, agent, *, kind=TargetKind.UPDATE_PR, review=None, test=None):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    _with_plan(monkeypatch)
    return engine_mod.prepare_fix(
        _task(head_sha, kind=kind), _LOG, agent_func=agent,
        review_func=review or (lambda *a, **k: (True, "fixes the stale value")),
        test_func=test or engine_mod.run_test_command,
    )


def test_a_fix_whose_test_passes_with_it_is_accepted(tmp_path, monkeypatch):
    agent = _value_agent("right")
    candidate = _prepare(tmp_path, monkeypatch, agent)
    assert candidate.action is CandidateAction.PATCH
    assert agent.feedback == [""]
    assert "passed 40 loop(s)" in candidate.verification
    assert "Review: fixes the stale value" in candidate.verification


def test_a_fix_whose_test_still_fails_goes_back_with_the_output(tmp_path, monkeypatch):
    agent = _value_agent("almost", "right")
    candidate = _prepare(tmp_path, monkeypatch, agent)
    assert candidate.action is CandidateAction.PATCH
    assert "With your change" in agent.feedback[1] and "loops=1` could not run the tests" in agent.feedback[1]
    assert "Accepted on attempt 2 of 3." in candidate.verification
    assert "+right" in candidate.patch and "almost" not in candidate.patch


def test_the_next_round_is_told_what_the_last_one_concluded(tmp_path, monkeypatch):
    """Each session starts without the last one's reading of the code."""
    agent = _value_agent("almost", "right")
    candidate = _prepare(tmp_path, monkeypatch, agent)
    assert candidate.action is CandidateAction.PATCH
    assert "Your previous session concluded" in agent.feedback[1]
    assert "- Root cause: stale value\n- Change: set the right value" in agent.feedback[1]
    assert agent.feedback[1].endswith("The working tree still holds your previous edits; revise them.")


def test_a_rejected_review_goes_back_to_the_agent(tmp_path, monkeypatch):
    verdicts = iter([(False, "skips the assertion"), (True, "fixes the cause")])
    agent = _value_agent("right", "right")
    candidate = _prepare(tmp_path, monkeypatch, agent, review=lambda *a, **k: next(verdicts))
    assert candidate.action is CandidateAction.PATCH
    assert agent.feedback[1].startswith("A reviewer rejected the patch: skips the assertion")
    assert agent.feedback[1].endswith("The working tree still holds your previous edits; revise them.")


def test_a_suggestion_is_reviewed_but_its_code_is_never_run(tmp_path, monkeypatch):
    test = []
    candidate = _prepare(tmp_path, monkeypatch, _value_agent("right"), kind=TargetKind.SUGGEST,
                         test=lambda *a: test.append(a) or TestRun(True, ""))
    assert candidate.action is CandidateAction.PATCH
    assert test == []


def test_no_new_round_starts_without_time_to_finish_it(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "PREPARE_BUDGET_S", 60)
    agent = _value_agent("almost", "right")
    # A failed attempt, which a follow-up retries, not a refusal that retires the job.
    with pytest.raises(RuntimeError, match=r"^Ran out of time after 1 attempt\(s\)\."):
        _prepare(tmp_path, monkeypatch, agent)
    assert len(agent.feedback) == 1


# --- existing fixes, and the command that verifies the patch -----------------

from scripts.ci_fix.models import FailedTest  # noqa: E402
from scripts.ci_fix.prior_fixes import PriorFix  # noqa: E402
from scripts.ci_fix.verification import LOOPS, VerifyPlan  # noqa: E402

_LOG_WITH_FAILURE = (
    "2026-10-15T01:33:46.2219673Z [err]: value is right in tests/unit/value.tcl\n"
    "Expected 'before' to be equal to 'right'\n"
)


def _with_plan(monkeypatch, seen=None):
    def plan_for(repo_dir, tests, *, scheduled, job=None):
        if seen is not None:
            seen.update(tests=tests, scheduled=scheduled, job=job)
        # The loop count rides along as an argument, not a comment: under load
        # the whole command is one line, where a comment would hide its end.
        notes = ("The failed job could not host the fix, so a plain build stands in for it.",) if job is None else ()
        return VerifyPlan(
            build="true",
            test=f'test "$(head -1 value.txt)" = right && : loops={LOOPS}',
            notes=notes,
            tests=tests,
        )

    monkeypatch.setattr(engine_mod, "plan_for", plan_for)


def test_a_failure_nothing_can_run_gets_a_fix_that_is_reviewed_and_said_not_to_be_run(tmp_path, monkeypatch):
    """A job that failed outside any test, with nothing upstream to port (#5285, #5306, #5217).

    People fixed each with a few lines read from the log. The bot may propose
    one, but never as tested: it is reviewed, and published as not run.
    """
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    monkeypatch.setattr(engine_mod, "plan_for", lambda *_args, **_kwargs: None)
    agent, reviewed, timeouts = _value_agent("right"), [], []

    def recording_agent(*args, timeout, **kwargs):
        timeouts.append(timeout)
        return agent(*args, **kwargs)

    def review(candidate, *_args, **_kwargs):
        reviewed.append(candidate.verification)
        return True, "reads right"

    candidate = engine_mod.prepare_fix(
        _task(head_sha), _LOG, agent_func=recording_agent, review_func=review,
        test_func=lambda *_args: pytest.fail("nothing is run"),
    )

    assert candidate.action is CandidateAction.PATCH and candidate.not_run
    assert engine_mod._NOT_RUN in candidate.verification and engine_mod._NOT_RUN in reviewed[0]
    assert "passed" not in candidate.verification and "Review: reads right" in candidate.verification
    # The review still gets its time after the agent.
    assert timeouts[0] <= engine_mod.PREPARE_BUDGET_S - engine_mod._REVIEW_RESERVE_S


def test_a_backport_branch_never_gets_a_fix_that_was_not_run(tmp_path, monkeypatch):
    """There only an upstream change is accepted, and with nothing to port there is no fix."""
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    monkeypatch.setattr(engine_mod, "plan_for", lambda *_args, **_kwargs: None)

    candidate = engine_mod.prepare_fix(
        _backport(_task(head_sha)), _LOG, agent_func=lambda *a, **k: pytest.fail("no edit"),
    )

    assert candidate.action is CandidateAction.REFUSE and not candidate.not_run
    assert "could not be checked" in candidate.summary


def _runs_value_check(results):
    """A test runner that evaluates the plan command, recording each run."""
    def run(repo_dir, command, timeout):
        outcome = engine_mod.run_test_command(repo_dir, command, timeout)
        report = (
            "[ok]: value is right (1 ms)" if outcome.passed else "[err]: value is right in tests/unit/value.tcl"
        )
        if "loops=" in command:  # not the build, which runs first and alone
            loops = command.split("loops=", 1)[-1].split()[0]
            results.append(((Path(repo_dir) / "value.txt").read_text().splitlines()[0], loops))
        return TestRun(outcome.passed, outcome.output, report)

    return run


def test_the_fix_is_verified_with_the_jobs_command(tmp_path, monkeypatch):
    """CI's failure is the evidence: the unfixed code is never run, only the patched one."""
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    seen, runs = {}, []
    _with_plan(monkeypatch, seen)
    agent = _value_agent("right")
    contexts = []

    def recording_agent(*args, **kwargs):
        contexts.append(kwargs["context"])
        return agent(*args, **kwargs)

    task = _task(head_sha)
    task = replace(task, failure=replace(task.failure, failed_tests=(FailedTest("value is right", "tests/unit/value.tcl"),)))
    candidate = engine_mod.prepare_fix(
        task, _LOG_WITH_FAILURE, agent_func=recording_agent,
        test_func=_runs_value_check(runs),
    )

    assert candidate.action is CandidateAction.PATCH
    assert seen["tests"] == (FailedTest("value is right", "tests/unit/value.tcl"),)
    assert runs == [("right", "1"), ("right", "40")]  # only with the fix: once, then looped
    assert contexts[0].verification.startswith("Code checks your change with")
    assert contexts[0].verified_by_code
    assert "passed 40 loop(s)" in candidate.verification


def test_a_detector_issue_for_a_c_unit_test_is_verified_with_gtest(tmp_path, monkeypatch):
    """The whole path: the issue's test, located in the checkout, repeated by name with the real planner."""
    source, head_sha = _source_repo(tmp_path)
    (source / "src" / "unit").mkdir(parents=True)
    (source / "src" / "unit" / "Makefile").write_text("test-unit:\n", encoding="utf-8")
    (source / "src" / "unit" / "test_crc64.cpp").write_text("TEST(Crc64Test, TestCrc64) {}\n", encoding="utf-8")
    (source / "deps" / "gtest-parallel").mkdir(parents=True)
    (source / "deps" / "gtest-parallel" / "gtest_parallel.py").write_text("", encoding="utf-8")
    git(source, "add", "src", "deps")
    git(source, "commit", "-m", "unit test")
    head_sha = git(source, "rev-parse", "HEAD")
    _install_clone(monkeypatch, source)
    monkeypatch.setattr(engine_mod, "plan_for", _REAL_PLAN_FOR)
    runs, contexts = [], []

    def gtest_runner(repo_dir, command, timeout):
        """A unit-test binary whose verdict is value.txt's content."""
        if "gtest_parallel.py" not in command:
            assert command.startswith("make ")  # the build, which runs first and alone
            return TestRun(True, "")
        assert "gtest_parallel.py ./valkey-unit-gtests --timeout_per_test 300 --repeat=" in command
        assert "--gtest_filter=Crc64Test.TestCrc64)" in command  # a PR's run: no --accurate
        value = (Path(repo_dir) / "value.txt").read_text().splitlines()[0]
        runs.append((value, command.split("--repeat=", 1)[1].split()[0]))
        if value == "right":
            return TestRun(True, "", "[1/1] Crc64Test.TestCrc64 (1 ms)")
        return TestRun(False, "", "src/unit/test_crc64.cpp:29: Failure\n[  FAILED  ] Crc64Test.TestCrc64 (0 ms)")

    agent = _value_agent("right")

    def recording_agent(*args, **kwargs):
        contexts.append(kwargs["context"])
        return agent(*args, **kwargs)

    task = _task(head_sha, kind=TargetKind.DRAFT_PR)
    task = replace(task, failure=replace(
        task.failure, issue_number=7, pr_number=0,
        failed_tests=(FailedTest("Crc64Test.TestCrc64", "test_crc64.cpp"),),
    ))
    candidate = engine_mod.prepare_fix(task, _LOG, agent_func=recording_agent, test_func=gtest_runner)

    assert candidate.action is CandidateAction.PATCH
    assert runs == [("right", "1"), ("right", "40")]
    assert "--repeat=40 --gtest_filter=Crc64Test.TestCrc64)`" in contexts[0].verification
    assert "passed 40 loop(s)" in candidate.verification


def test_a_flaky_fix_is_verified_with_a_flakes_loops(tmp_path, monkeypatch):
    from scripts.ci_fix.models import FailureType

    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    runs = []
    _with_plan(monkeypatch)
    task = _task(head_sha)
    task = replace(task, failure=replace(task.failure, failed_tests=(FailedTest("value is right", "tests/unit/value.tcl"),)))
    candidate = engine_mod.prepare_fix(
        task, _LOG_WITH_FAILURE, agent_func=_value_agent("right", failure_type=FailureType.FLAKY),
        test_func=_runs_value_check(runs),
    )

    assert candidate.action is CandidateAction.PATCH
    assert runs == [("right", "1"), ("right", "40")]


def test_a_fix_that_does_not_stop_the_failure_goes_back(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    _with_plan(monkeypatch)
    agent = _value_agent("almost", "right")

    candidate = engine_mod.prepare_fix(
        _task(head_sha), _LOG_WITH_FAILURE, agent_func=agent,
        test_func=_runs_value_check([]),
    )

    assert candidate.action is CandidateAction.PATCH
    assert "the failing test still fails within 1 loop(s)" in agent.feedback[1]


def test_existing_fixes_and_the_test_guide_reach_the_agent_and_the_report(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    searched = {}

    def find(repo_dir, tests, *, tested_sha, issue_number, paths):
        searched.update(tests=tests, tested_sha=tested_sha, issue_number=issue_number, paths=paths)
        return (PriorFix(sha="c" * 40, subject="Deflake value test (#9)", date="2026-10-01",
                         reasons=("names the test `value is right`",), score=5),)

    monkeypatch.setattr(engine_mod, "find_prior_fixes", find)
    monkeypatch.setattr(engine_mod, "render_prior_fixes", lambda repo_dir, fixes: "full detail")
    contexts = []

    def agent(task, repo_dir, job_log, **kwargs):
        contexts.append(kwargs["context"])
        return FixCandidate(action=CandidateAction.REFUSE, summary="Already fixed upstream.")

    task = _task(head_sha)
    task = replace(
        task,
        failure=replace(task.failure, repo_full_name="valkey-io/valkey", issue_number=12),
        target=replace(task.target, repo_full_name="valkey-io/valkey"),
    )
    candidate = engine_mod.prepare_fix(task, _LOG_WITH_FAILURE, agent_func=agent)

    assert searched == {
        "tests": (FailedTest("value is right", "tests/unit/value.tcl"),),
        "tested_sha": head_sha,
        "issue_number": 12,
        "paths": (),
    }
    context = contexts[0]
    assert "Deflake value test (#9)" in context.prior_fixes and context.prior_fixes_detail == "full detail"
    assert "tests/README.md" in context.guide  # points the agent at the checkout's own docs
    assert candidate.action is CandidateAction.REFUSE
    assert "Existing fixes found" in candidate.verification


def test_a_fix_that_skips_the_failing_test_is_sent_back(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    _with_plan(monkeypatch)
    built, ran = TestRun(True, ""), TestRun(True, "", "[ok]: value is right (1 ms)")
    calls = iter([
        built, TestRun(True, "", ""),  # with the fix: nothing ran
        built, ran, ran,
    ])
    agent = _value_agent("skip it", "right")

    candidate = engine_mod.prepare_fix(
        _task(head_sha), _LOG_WITH_FAILURE, agent_func=agent,
        test_func=lambda *a: next(calls),
    )

    assert candidate.action is CandidateAction.PATCH
    assert "passed without running the failing test" in agent.feedback[1]


def test_a_harness_error_with_the_fix_is_not_reported_as_the_same_failure(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    _with_plan(monkeypatch)
    built, ran = TestRun(True, ""), TestRun(True, "", "[ok]: value is right (1 ms)")
    calls = iter([
        built, TestRun(False, "", "[exception]: Executing test client: broken pipe"),
        built, ran, ran,
    ])
    agent = _value_agent("broken", "right")

    engine_mod.prepare_fix(
        _task(head_sha), _LOG_WITH_FAILURE, agent_func=agent,
        test_func=lambda *a: next(calls),
    )

    assert "could not run the tests" in agent.feedback[1]
    assert "still fails" not in agent.feedback[1]


def test_switching_from_a_patch_to_a_port_discards_the_patch(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    commit = _upstream_commit(source, head_sha)
    _install_clone(monkeypatch, source)
    patch_then_port = iter([_value_agent("almost"), _port_agent(commit)])
    # Round 1's patch fails its own test, so only the port reaches review.
    verdicts = iter([(True, "the upstream fix")])

    def agent(*args, **kwargs):
        return next(patch_then_port)(*args, **kwargs)

    candidate = engine_mod.prepare_fix(
        _task(head_sha), _LOG, agent_func=agent,
        review_func=lambda *a, **k: next(verdicts),
        test_func=lambda repo_dir, command, timeout: TestRun("almost" not in (Path(repo_dir) / "value.txt").read_text(), ""),
    )

    assert candidate.action is CandidateAction.PORT
    assert candidate.port_commit == commit


def test_a_timed_out_command_leaves_no_process_behind(tmp_path):
    marker = tmp_path / "still-running"
    result = engine_mod.run_test_command(
        str(tmp_path), f"(sleep 3; touch {marker}) & sleep 30", timeout=1,
    )

    assert not result.passed and "timed out" in result.output
    import time

    time.sleep(3.5)
    assert not marker.exists()


def test_a_timed_out_command_run_as_the_account_is_stopped_through_it(tmp_path, monkeypatch):
    """The command sits under root's sudo or runuser, which the runner may not signal."""
    from contextlib import contextmanager

    @contextmanager
    def handed_to(user, root):
        yield str(tmp_path)

    def no_signal(*_args):
        raise PermissionError("Operation not permitted")

    killed = []
    monkeypatch.setattr(engine_mod, "isolated_user", lambda: "ci-fix-agent")
    monkeypatch.setattr(engine_mod, "handed_to", handed_to)
    monkeypatch.setattr(
        engine_mod,
        "as_user",
        lambda user, env, argv, home, cwd: SimpleNamespace(
            argv=["sh", "-c", "exec sleep 2"],
            env={},
        ),
    )
    monkeypatch.setattr(engine_mod.os, "killpg", no_signal)
    monkeypatch.setattr(engine_mod, "kill_all", killed.append)

    result = engine_mod.run_test_command(str(tmp_path), "make test", timeout=1)

    assert result.timed_out and not result.passed
    assert killed == ["ci-fix-agent"]


def test_the_agent_leaves_time_to_verify_and_review(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    timeouts = []

    def agent(task, repo_dir, job_log, *, timeout, **_kwargs):
        timeouts.append(timeout)
        return FixCandidate(action=CandidateAction.REFUSE, summary="no")

    engine_mod.prepare_fix(_task(head_sha), _LOG, agent_func=agent)

    assert timeouts[0] <= engine_mod.PREPARE_BUDGET_S - engine_mod._VERIFY_RESERVE_S
    # Nor may one session use the whole budget: a candidate that fails its test gets another round.
    assert timeouts[0] == engine_mod._MAX_AGENT_ROUND_S
    assert engine_mod.PREPARE_BUDGET_S - engine_mod._MAX_AGENT_ROUND_S - engine_mod._VERIFY_RESERVE_S \
        >= engine_mod._MIN_ROUND_S


def test_the_review_reads_what_the_checks_found(tmp_path, monkeypatch):
    reviewed = []

    def review(candidate, *_args, **_kwargs):
        reviewed.append(candidate.verification)
        return True, "ok"

    _prepare(tmp_path, monkeypatch, _value_agent("right"), review=review)

    assert "passed 40 loop(s)" in reviewed[0]


# --- where a change already merged upstream comes first ------------------------

def _backport(task: FixTask) -> FixTask:
    return replace(task, policy=FixPolicy.BACKPORT)


def _adapting_agent(adapts: str, path: str = "value.txt", text: str = "fixed\nkept for this branch\n"):
    """An agent whose patch is the upstream commit's line ("fixed") and one of its own, unless ``text`` says otherwise."""
    def agent(task, repo_dir, job_log, *, feedback="", context=None, **_kwargs):  # noqa: ARG001
        agent.feedback.append(feedback)
        agent.contexts.append(context)
        (Path(repo_dir) / path).write_text(text, encoding="utf-8")
        return FixCandidate(
            action=CandidateAction.PATCH, failing_check="value check", root_cause="the branch lacks the fix",
            reasoning="adapt the upstream fix", changed_paths=(path,), adapts=adapts,
        )

    agent.feedback, agent.contexts = [], []
    return agent


def test_a_backport_branch_takes_a_fix_of_its_own_where_none_is_upstream(tmp_path, monkeypatch):
    """A release branch's flake is fixed where it fails: 16 of 17 fixes people pushed there were hand-written.

    The report says the default branch does not have the fix.
    """
    source, head_sha = _source_repo(tmp_path)
    _upstream_commit(source, head_sha)
    _install_clone(monkeypatch, source)
    agent = _adapting_agent("")

    candidate = engine_mod.prepare_fix(_backport(_task(head_sha)), _LOG, agent_func=agent)

    assert agent.contexts[0].upstream_first and agent.feedback == [""]
    assert candidate.action is CandidateAction.PATCH and candidate.adapts == ""
    assert "the default branch does not have it" in candidate.verification


def test_a_backport_branch_takes_a_patch_that_adapts_a_merged_upstream_commit(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    commit = _upstream_commit(source, head_sha)
    _install_clone(monkeypatch, source)

    candidate = engine_mod.prepare_fix(_backport(_task(head_sha)), _LOG, agent_func=_adapting_agent(commit))

    assert candidate.action is CandidateAction.PATCH
    assert candidate.adapts == commit and "+kept for this branch" in candidate.patch
    assert "the default branch does not have it" not in candidate.verification


def test_an_adaptation_may_write_other_lines_where_its_commit_writes_its_own(tmp_path, monkeypatch):
    """It replaces what the commit replaces: one that a reviewer accepted shared 12% of its added lines.

    Upstream asserted a listpack encoding within 16 bytes; the release branch has a hashtable there
    and three allocations, so its adaptation asserted that within 48, under a comment of its own.
    """
    source, head_sha = _source_repo(tmp_path)
    commit = _upstream_commit(source, head_sha)
    _install_clone(monkeypatch, source)
    agent = _adapting_agent(commit, text="fixed the way this branch needs\n# and says why\n")

    candidate = engine_mod.prepare_fix(_backport(_task(head_sha)), _LOG, agent_func=agent)

    assert candidate.action is CandidateAction.PATCH and candidate.adapts == commit


@pytest.mark.parametrize(("adapts", "path", "text"), [
    # Every hand-written fix pushed to a backport branch changed a file that unported upstream commits
    # change too: naming one of them adapts nothing unless the patch is its change.
    ("upstream", "value.txt", "before\na fix of its own\nwith a longer wait\n"),
    ("upstream", "value.txt", "before\nfixed\nand two lines\nof its own\n"),  # a third of it is the commit's
    ("upstream", "other.txt", "fixed\n"),  # the commit changes value.txt
    ("head", "value.txt", "fixed\n"),  # already on this branch
    ("b" * 40, "value.txt", "fixed\n"),  # not merged upstream
])
def test_a_patch_is_reported_as_adapting_a_commit_only_when_it_is_that_commits_change(
    tmp_path, monkeypatch, adapts, path, text,
):
    """Otherwise it is published as what it is: a fix of the branch's own."""
    source, head_sha = _source_repo(tmp_path)
    commit = _upstream_commit(source, head_sha)
    _install_clone(monkeypatch, source)
    agent = _adapting_agent({"upstream": commit, "head": head_sha}.get(adapts, adapts), path, text)

    candidate = engine_mod.prepare_fix(_backport(_task(head_sha)), _LOG, agent_func=agent)

    assert candidate.action is CandidateAction.PATCH and candidate.adapts == ""
    assert "the default branch does not have it" in candidate.verification


def test_a_backport_that_broke_the_build_may_be_repaired_by_hand(tmp_path, monkeypatch):
    """#5128's backport used bool without <stdbool.h>: the fix adapts the backport, and no upstream commit is it."""
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    monkeypatch.setattr(engine_mod, "plan_for", _REAL_PLAN_FOR)
    agent = _adapting_agent("")

    candidate = engine_mod.prepare_fix(
        _backport(_task(head_sha)), "make: *** [Makefile:6: all] Error 2", agent_func=agent,
        job=JobCommands(build="true", runtest="./runtest"),
    )

    assert not agent.contexts[0].upstream_first
    assert candidate.action is CandidateAction.PATCH and candidate.adapts == ""


def test_a_failure_no_command_can_run_takes_an_upstream_change_first_and_is_only_built(tmp_path, monkeypatch):
    """A sanitizer leak or a sentinel test: nothing here runs it, but its fix may be upstream (#5274)."""
    source, head_sha = _source_repo(tmp_path)
    commit = _upstream_commit(source, head_sha)
    _install_clone(monkeypatch, source)
    monkeypatch.setattr(engine_mod, "plan_for", lambda *_args, **_kwargs: None)
    contexts, commands = [], []

    def agent(task, repo_dir, job_log, *, context=None, **kwargs):
        contexts.append(context)
        return _port_agent(commit)(task, repo_dir, job_log, **kwargs)

    candidate = engine_mod.prepare_fix(
        _task(head_sha), _LOG, agent_func=agent,
        test_func=lambda _repo, command, _timeout: commands.append(command) or TestRun(True, ""),
    )

    assert contexts[0].upstream_first
    assert "No command here can run the failing check" in contexts[0].verification
    assert candidate.action is CandidateAction.PORT and candidate.port_commit == commit
    assert commands == ['make -j"$(getconf _NPROCESSORS_ONLN)"']
    assert "builds." in candidate.verification

    # A fix written here is built and reviewed too, and reported as not upstream and not run.
    own = engine_mod.prepare_fix(_task(head_sha), _LOG, agent_func=_adapting_agent(""),
                                 test_func=lambda *_args: TestRun(True, ""))
    assert own.action is CandidateAction.PATCH and own.adapts == ""
    assert "the default branch does not have it" in own.verification
    assert "code only checks that the change builds" in own.verification


def test_a_failure_with_nothing_to_search_for_does_not_claim_a_search(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    contexts = []

    def agent(task, repo_dir, job_log, **kwargs):
        contexts.append(kwargs["context"])
        return FixCandidate(action=CandidateAction.REFUSE, summary="no")

    candidate = engine_mod.prepare_fix(_task(head_sha), "Process completed with exit code 2.",
                                       agent_func=agent)

    assert not contexts[0].prior_fixes_searched
    assert "No merged change" not in candidate.verification


def test_a_port_not_merged_on_the_default_branch_goes_back_before_testing(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    git(source, "checkout", "-q", "-b", "side")
    (source / "value.txt").write_text("unmerged\n", encoding="utf-8")
    git(source, "commit", "-qam", "Unmerged change")
    unmerged = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "-q", head_sha)
    commit = _upstream_commit(source, head_sha)
    _install_clone(monkeypatch, source)
    agent = _port_agent(unmerged, commit)

    candidate = engine_mod.prepare_fix(_task(head_sha), _LOG, agent_func=agent)

    assert candidate.port_commit == commit
    assert "it is not merged into origin/main" in agent.feedback[1]
    assert "reset to the working commit" in agent.feedback[1]


def test_a_later_round_whose_agent_fails_is_a_failed_attempt_not_a_refusal(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    _with_plan(monkeypatch)
    first = _value_agent("almost")

    def agent(task, repo_dir, job_log, **kwargs):
        if kwargs["feedback"]:
            raise RuntimeError("CI-fix agent failed with exit code 1")
        return first(task, repo_dir, job_log, **kwargs)

    with pytest.raises(RuntimeError, match="^The agent failed on attempt 2") as raised:
        engine_mod.prepare_fix(_task(head_sha), _LOG, agent_func=agent)

    assert "With your change" in str(raised.value)


@pytest.mark.parametrize("rounds", [1, 2])
def test_an_agent_that_runs_out_its_session_refuses_without_a_retry(tmp_path, monkeypatch, rounds):
    """A second session reads what the first read and times out the same way (#5224)."""
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    _with_plan(monkeypatch)
    first = _value_agent("almost")
    calls = []

    def agent(task, repo_dir, job_log, **kwargs):
        calls.append(kwargs["feedback"])
        if len(calls) == rounds:
            (Path(repo_dir) / "partial.txt").write_text("unfinished\n", encoding="utf-8")
            raise AgentTimeout("CI-fix agent failed with exit code 1: timeout after 1200s")
        return first(task, repo_dir, job_log, **kwargs)

    candidate = engine_mod.prepare_fix(_task(head_sha), _LOG, agent_func=agent)

    assert len(calls) == rounds
    assert candidate.action is CandidateAction.REFUSE
    assert "whole session without finding an evidence-backed fix" in candidate.summary
    assert ("Earlier round: With your change" in candidate.verification) is (rounds == 2)


def test_the_review_is_bounded_by_the_deadline(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    seen = []

    engine_mod.prepare_fix(
        _task(head_sha), _LOG, agent_func=_value_agent("right"),
        review_func=lambda *a, timeout, **k: seen.append(timeout) or (True, "ok"),
    )

    assert 0 < seen[0] <= engine_mod.PREPARE_BUDGET_S


def test_ignored_files_the_agent_left_are_removed_before_verification(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    (source / ".gitignore").write_text("*.o\n", encoding="utf-8")
    git(source, "add", ".gitignore")
    git(source, "commit", "-qm", "ignore build output")
    head_sha = git(source, "rev-parse", "HEAD")
    _install_clone(monkeypatch, source)
    seen = []

    def agent(task, repo_dir, job_log, **kwargs):
        (Path(repo_dir) / "server.o").write_text("prebuilt to skip the rebuild")
        return _value_agent("right")(task, repo_dir, job_log, **kwargs)

    def test(repo_dir, command, timeout):
        seen.append((Path(repo_dir) / "server.o").exists())
        return engine_mod.run_test_command(repo_dir, command, timeout)

    engine_mod.prepare_fix(_task(head_sha), _LOG, agent_func=agent, test_func=test)

    assert seen and not any(seen)


def test_a_merge_commit_port_is_refused_before_a_round_is_spent_on_it(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    git(source, "checkout", "-q", "-b", "upstream")
    git(source, "checkout", "-q", "-b", "topic")
    (source / "other.txt").write_text("x\n", encoding="utf-8")
    git(source, "add", "other.txt")
    git(source, "commit", "-qm", "topic change")
    git(source, "checkout", "-q", "upstream")
    (source / "third.txt").write_text("y\n", encoding="utf-8")
    git(source, "add", "third.txt")
    git(source, "commit", "-qm", "upstream change")
    git(source, "merge", "-q", "--no-ff", "-m", "Merge topic", "topic")
    merge = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "-q", head_sha)
    _install_clone(monkeypatch, source)
    agent = _port_agent(merge, merge, merge)

    candidate = engine_mod.prepare_fix(_task(head_sha), _LOG, agent_func=agent)

    assert candidate.action is CandidateAction.REFUSE
    assert "merge commits require an explicit mainline parent" in agent.feedback[1]


def test_a_detector_issue_verifies_only_its_own_test(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    seen = {}
    _with_plan(monkeypatch, seen)
    own = FailedTest("value is right", "tests/unit/value.tcl")
    log = _LOG_WITH_FAILURE + "[err]: another flaky test in tests/unit/other.tcl\n"
    task = _task(head_sha)
    task = replace(task, failure=replace(task.failure, failed_tests=(own,), issue_number=7))

    engine_mod.prepare_fix(task, log, agent_func=_value_agent("right"),
                           test_func=_runs_value_check([]))

    assert seen["tests"] == (own,)


def test_a_verification_that_runs_out_of_time_is_a_failed_attempt(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    _with_plan(monkeypatch)
    calls = iter([
        TestRun(False, "timed out after 60s", timed_out=True),
        TestRun(True, "", "[ok]: value is right (1 ms)"),
    ])
    agent = _value_agent("right", "right")

    # Nothing is known about the change: a failed attempt, retried, not feedback.
    with pytest.raises(RuntimeError, match="did not finish in the time left"):
        engine_mod.prepare_fix(_task(head_sha), _LOG_WITH_FAILURE, agent_func=agent,
                               test_func=lambda *a: next(calls))
    assert len(agent.feedback) == 1


# --- other detector issues the same patch fixes -----------------------------------

def _two_test_repo(tmp_path: Path) -> tuple[Path, str]:
    """A checkout whose one test file holds two failing tests, as Valkey's often do."""
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "runtest").write_text("#!/bin/sh\n")
    (repo / "runtest").chmod(0o755)
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "unit" / "t.tcl").write_text("A=bad\nB=bad\n", encoding="utf-8")
    git(repo, "init")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "initial")
    return repo, git(repo, "rev-parse", "HEAD")


def _fix_both_agent(repo_dir_file="tests/unit/t.tcl"):
    def agent(task, repo_dir, job_log, **_kwargs):  # noqa: ARG001
        (Path(repo_dir) / repo_dir_file).write_text("A=good\nB=good\n", encoding="utf-8")
        return FixCandidate(
            action=CandidateAction.PATCH, failing_check="A", root_cause="A is bad",
            reasoning="fix both", changed_paths=(repo_dir_file,),
        )
    return agent


def _only_tests_pass(repo_dir, command, _timeout=None):
    """Pass when every test the command selects has a good line in the tree."""
    import re as _re

    from scripts.ci_fix.verification import TestRun

    names = _re.findall(r"--only (?:'([^']*)'|(\S+))", command)
    names = [quoted or bare for quoted, bare in names]
    text = (Path(repo_dir) / "tests" / "unit" / "t.tcl").read_text()
    lines, passed = [], True
    for name in names:
        good = f"{name}=good" in text
        passed = passed and good
        # runtest prints a pass as "[ok]: <name> (N ms)" and a failure as
        # "[err]: <name> in <file>".
        lines.append(f"[ok]: {name} (1 ms)" if good else f"[err]: {name} in tests/unit/t.tcl")
    report = "\n".join(lines)
    return TestRun(passed=passed, output=report, report=report)


def _prepare_with_others(tmp_path, monkeypatch, others):
    from scripts.ci_fix.models import FailedTest, OtherIssue

    source, head_sha = _two_test_repo(tmp_path)
    _install_clone(monkeypatch, source)
    monkeypatch.setattr(engine_mod, "plan_for", _REAL_PLAN_FOR)
    task = replace(
        _task(head_sha, kind=TargetKind.DRAFT_PR),
        other_issues=tuple(OtherIssue(number=n, test=FailedTest(t, "tests/unit/t.tcl")) for n, t in others),
    )
    task = replace(task, failure=replace(task.failure, failed_tests=(FailedTest("A", "tests/unit/t.tcl"),),
                                         issue_number=10))
    return engine_mod.prepare_fix(
        task, _LOG, agent_func=_fix_both_agent(),
        review_func=lambda *a, **k: (True, "fixes both"),
        test_func=_only_tests_pass,
    )


def test_another_issue_whose_test_passes_with_the_patch_is_touched_not_claimed(tmp_path, monkeypatch):
    candidate = _prepare_with_others(tmp_path, monkeypatch, [(11, "B")])
    assert candidate.action is CandidateAction.PATCH
    assert candidate.also_touches == (11,)
    assert "The patch changes `B`, issue `#11`'s test, which passed 40 loop(s) with it." in candidate.verification


def test_an_issue_whose_test_does_not_pass_with_the_patch_is_only_mentioned(tmp_path, monkeypatch):
    # C is not in the file, so its test never runs.
    candidate = _prepare_with_others(tmp_path, monkeypatch, [(12, "C")])
    assert candidate.also_touches == (12,)
    assert "The patch changes `C`, issue `#12`'s test, which did not pass with it." in candidate.verification
    # Unlinked: a bare #12 would let this PR keep #12 from a fix of its own.
    assert "#12" not in candidate.verification.replace("`#12`", "")


def test_an_issue_in_another_file_is_not_considered(tmp_path, monkeypatch):
    from scripts.ci_fix.models import FailedTest, OtherIssue

    source, head_sha = _two_test_repo(tmp_path)
    _install_clone(monkeypatch, source)
    monkeypatch.setattr(engine_mod, "plan_for", _REAL_PLAN_FOR)
    task = replace(
        _task(head_sha, kind=TargetKind.DRAFT_PR),
        other_issues=(OtherIssue(number=13, test=FailedTest("Z", "tests/unit/other.tcl")),),
    )
    task = replace(task, failure=replace(task.failure, failed_tests=(FailedTest("A", "tests/unit/t.tcl"),)))
    candidate = engine_mod.prepare_fix(
        task, _LOG, agent_func=_fix_both_agent(),
        review_func=lambda *a, **k: (True, "ok"), test_func=_only_tests_pass,
    )
    assert candidate.also_touches == ()


# --- inside the failed job: its checkout ----------------------------------------

def _job_checkout(tmp_path: Path, source: Path) -> Path:
    """The job's checkout after its setup: a build product, and a dependency checked out into it."""
    (source / ".gitignore").write_text("*.o\n", encoding="utf-8")
    git(source, "add", ".gitignore")
    git(source, "commit", "-qm", "ignore build output")
    job = tmp_path / "job"
    git(tmp_path, "clone", "-q", str(source), str(job))
    (job / "server.o").write_text("built by the job", encoding="utf-8")
    (job / "util.o").write_text("built by the job", encoding="utf-8")
    (job / "libbacktrace").mkdir()
    (job / "libbacktrace" / "configure").write_text("#!/bin/sh\n", encoding="utf-8")
    return job


def test_in_a_jobs_checkout_only_what_the_agent_left_is_removed_before_verification(tmp_path, monkeypatch):
    source, _ = _source_repo(tmp_path)
    job = _job_checkout(tmp_path, source)
    head_sha = git(job, "rev-parse", "HEAD")
    monkeypatch.setattr(engine_mod, "shallow_clone_at_sha", lambda *a, **k: pytest.fail("no clone"))
    seen = []

    def agent(task, repo_dir, job_log, **kwargs):
        (Path(repo_dir) / "server.o").write_text("prebuilt to skip the rebuild", encoding="utf-8")
        (Path(repo_dir) / "new.o").write_text("left behind", encoding="utf-8")
        return _value_agent("right")(task, repo_dir, job_log, **kwargs)

    def test(repo_dir, command, timeout):
        seen.append(sorted(path.name for path in Path(repo_dir).glob("*.o")))
        return engine_mod.run_test_command(repo_dir, command, timeout)

    candidate = engine_mod.prepare_fix(
        _task(head_sha), _LOG, agent_func=agent, test_func=test, repo_dir=str(job),
    )

    assert candidate.action is CandidateAction.PATCH
    assert candidate.changed_paths == ("value.txt",)  # not the job's libbacktrace
    assert seen and all(names == ["util.o"] for names in seen)
    assert (job / "libbacktrace" / "configure").exists()


def test_a_build_break_no_test_names_is_fixed_and_verified_by_the_jobs_build(tmp_path, monkeypatch):
    """#5077: the job failed in make. The job's build is the check."""
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    monkeypatch.setattr(engine_mod, "plan_for", _REAL_PLAN_FOR)
    build = 'test "$(cat value.txt)" = right || { echo "value.c:1:5: error: unknown type name bool"; exit 2; }'
    seen = []

    def agent(task, repo_dir, job_log, **kwargs):
        seen.append(kwargs["context"])
        return _value_agent("right")(task, repo_dir, job_log, **kwargs)

    candidate = engine_mod.prepare_fix(
        _task(head_sha), "make: *** [Makefile:6: all] Error 2", agent_func=agent,
        job=JobCommands(build=build, runtest="./runtest"),
    )

    assert candidate.action is CandidateAction.PATCH
    assert f"`{build}`: the build must pass." in seen[0].verification
    assert f"With the fix, `{build}` builds." in candidate.verification


def test_a_job_that_did_not_break_in_make_gets_no_build_plan(tmp_path, monkeypatch):
    """A reply-schema validator failure names no test either, but its build passed (#5306)."""
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    monkeypatch.setattr(engine_mod, "plan_for", _REAL_PLAN_FOR)

    contexts = []

    def agent(task, repo_dir, job_log, *, context=None, **_kwargs):  # noqa: ARG001
        contexts.append(context)
        return FixCandidate(action=CandidateAction.REFUSE, summary="The validator is not a code fix.")

    candidate = engine_mod.prepare_fix(
        _task(head_sha), "make[1]: [Makefile:849: distclean] Error 2 (ignored)\nJSON schema validation error",
        agent_func=agent, job=JobCommands(build="false", runtest="./runtest"),
        test_func=lambda *_args: pytest.fail("the job's build is not the check"),
    )

    # No build plan: the fix would be reviewed, not run, and the agent may still decline.
    assert contexts[0].reviewed_only and not contexts[0].verified_by_code
    assert candidate.action is CandidateAction.REFUSE and not candidate.not_run


def _chain():
    test = FailedTest("t", "tests/unit/a.tcl")
    whole = VerifyPlan(build="make", test="whole", tests=(test,), single_run=True, attributed=True)
    isolated = VerifyPlan(build="make", test="isolated", tests=(test,), fallback=whole, attributed=True)
    return VerifyPlan(build="make", test="only", tests=(test,), fallback=isolated)


def test_a_test_that_fails_under_only_is_verified_in_its_own_block(tmp_path):
    """#5073: with --only the setup is skipped and the test always fails; in its block it passes."""
    seen, checks = [], []
    results = {"make": TestRun(True, ""),
               "only": TestRun(False, "", "[err]: t in tests/unit/a.tcl"),
               "isolated": TestRun(True, "", "[ok]: t (1 ms)")}

    def run(_repo, command, _timeout):
        name = command.split(" && ")[-1].split()[0]
        seen.append(name)
        return results[name]

    feedback = engine_mod._verify_with_plan(_chain(), str(tmp_path), run, time.monotonic() + 600, checks)

    # The build, each plan once until one passes, then that one looped.
    assert feedback == "" and seen == ["make", "only", "isolated", "isolated"]
    assert "`make && isolated` passed 40 loop(s)" in checks[0]


def test_a_test_that_passes_once_and_fails_in_its_loops_is_not_passed_by_a_fallback(tmp_path):
    """A fallback is for a test that cannot run alone, not for one that still fails now and then."""
    seen = []

    def run(_repo, command, _timeout):
        seen.append(command)
        if command == "make" or "--loops 1" in command:
            return TestRun(True, "", "[ok]: t (1 ms)")
        return TestRun(False, "boom", "[err]: t in tests/unit/a.tcl")

    test = FailedTest("t", "tests/unit/a.tcl")
    whole = VerifyPlan(build="make", test="whole", tests=(test,), single_run=True, attributed=True)
    plan = VerifyPlan(build="make", test=f"only --loops {LOOPS}", tests=(test,), fallback=whole)

    feedback = engine_mod._verify_with_plan(plan, str(tmp_path), run, time.monotonic() + 7200, [])

    assert seen == ["make", "make && only --loops 1", "make && only --loops 40"]
    assert "still fails within 40 loop(s): `make && only --loops 40`" in feedback


def test_a_slow_test_gets_the_loops_that_fit(tmp_path):
    """One 79s loop cannot run 40 times in the time kept for verification (run 42480180080)."""
    seen, checks = [], []

    def run(_repo, command, _timeout):
        seen.append(command)
        return TestRun(True, "", "[ok]: t (1 ms)", seconds=79.0)

    plan = VerifyPlan(build="make", test=f"only --loops {LOOPS}", tests=(FailedTest("t", "tests/unit/a.tcl"),))

    assert engine_mod._verify_with_plan(plan, str(tmp_path), run, time.monotonic() + 7200, checks) == ""
    assert seen[-1] == "make && only --loops 34"
    assert "passed 34 loop(s)" in checks[0] and checks[1] == "One loop takes 79s, so 34 loop(s) ran, not 40."

    # With no time for a second run, the one that passed is the check.
    seen.clear(), checks.clear()
    assert engine_mod._verify_with_plan(plan, str(tmp_path), run, time.monotonic() + 100, checks) == ""
    assert seen == ["make", "make && only --loops 1"] and "passed 1 loop(s)" in checks[0]


def test_a_change_that_does_not_build_is_told_so(tmp_path):
    seen = []

    def run(_repo, command, _timeout):
        seen.append(command)
        return TestRun(False, "value.c:1:5: error: unknown type name bool")

    plan = VerifyPlan(build="make", test="only", tests=(FailedTest("t", "tests/unit/a.tcl"),))
    feedback = engine_mod._verify_with_plan(plan, str(tmp_path), run, time.monotonic() + 600, [])

    assert seen == ["make"]
    assert "`make` does not build" in feedback and "unknown type name bool" in feedback


def test_a_fix_under_deps_makes_the_build_rebuild_them(tmp_path):
    """#5330: a fix in deps/libvalkey was verified against the library built without it."""
    (tmp_path / "src").mkdir()
    settings = tmp_path / "src" / ".make-settings"
    git(tmp_path, "init")
    plan = VerifyPlan(build="true", test="true", build_only=True)
    run = lambda *_args: TestRun(True, "")  # noqa: E731

    settings.write_text("PREV_FINAL_CFLAGS=-O2\n", encoding="utf-8")
    in_src = FixCandidate(action=CandidateAction.PATCH, patch="--- a/src/a.c\n+++ b/src/a.c\n@@ -1 +1 @@\n-a\n+b\n")
    engine_mod._verify(in_src, str(tmp_path), plan, run, time.monotonic() + 600, [])
    assert settings.exists()

    in_deps = replace(in_src, patch=in_src.patch.replace("src/a.c", "deps/libvalkey/src/net.c"))
    engine_mod._verify(in_deps, str(tmp_path), plan, run, time.monotonic() + 600, [])
    assert not settings.exists()


def test_a_patch_that_fails_every_way_gets_the_last_runs_feedback(tmp_path):
    seen = []

    def run(_repo, command, _timeout):
        seen.append(command.split(" && ")[-1].split()[0])
        return TestRun(True, "") if command == "make" else TestRun(False, "boom", "[err]: t in tests/unit/a.tcl")

    feedback = engine_mod._verify_with_plan(_chain(), str(tmp_path), run, time.monotonic() + 600, [])

    assert seen == ["make", "only", "isolated", "whole"]
    assert "still fails within 1 loop(s): `make && whole`" in feedback


def test_a_jobs_checkout_at_another_commit_is_refused(tmp_path):
    source, _ = _source_repo(tmp_path)
    job = _job_checkout(tmp_path, source)

    with pytest.raises(RuntimeError, match="checked out"):
        with engine_mod.checkout(_task("a" * 40), str(job)):
            pass


_TCL = """\
start_server {} {
    test {first value} {
        assert_equal 1 [r get a]
    }

    test {second value} {
        assert_equal 2 [r get b]
    }
}
"""


def _changed(before: str, after: str) -> dict:
    import difflib

    diff = difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                "a/tests/unit/t.tcl", "b/tests/unit/t.tcl")
    return engine_mod.changed_lines("".join(diff))


def test_an_issue_counts_as_touched_only_when_its_own_test_block_changed(tmp_path):
    from scripts.ci_fix.models import FailedTest

    after = _TCL.replace("assert_equal 1", "assert_equal 10")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "t.tcl").write_text(after)
    changed = _changed(_TCL, after)

    assert engine_mod._changes_test(str(tmp_path), changed, FailedTest("first value", "tests/unit/t.tcl"))
    # The sibling three lines away is untouched, though inside the hunk's context.
    assert not engine_mod._changes_test(str(tmp_path), changed, FailedTest("second value", "tests/unit/t.tcl"))
    assert not engine_mod._changes_test(str(tmp_path), changed, FailedTest("first value", "tests/unit/other.tcl"))


def test_a_deleted_line_still_marks_the_test_it_was_in(tmp_path):
    from scripts.ci_fix.models import FailedTest

    after = _TCL.replace("        assert_equal 2 [r get b]\n", "")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "t.tcl").write_text(after)

    assert engine_mod._changes_test(str(tmp_path), _changed(_TCL, after), FailedTest("second value", "tests/unit/t.tcl"))


def test_a_test_whose_name_is_built_at_run_time_counts_as_touched_by_any_change_to_its_file(tmp_path):
    from scripts.ci_fix.models import FailedTest

    source = _TCL + 'foreach cmd {incr decr} {\n    test "$cmd operation" {\n    }\n}\n'
    after = source.replace("assert_equal 1", "assert_equal 10")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "t.tcl").write_text(after)

    assert engine_mod._changes_test(str(tmp_path), _changed(source, after), FailedTest("incr operation", "tests/unit/t.tcl"))


def test_a_test_that_rewrites_the_patched_file_does_not_get_its_patch_published(tmp_path, monkeypatch):
    """The tree is the candidate: a build or test that edits it during verification changes the candidate."""
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    feedback_seen = []

    def agent(task, repo_dir, job_log, feedback="", **_kwargs):  # noqa: ARG001
        feedback_seen.append(feedback)
        (Path(repo_dir) / "value.txt").write_text("bad\n", encoding="utf-8")
        return FixCandidate(
            action=CandidateAction.PATCH, failing_check="value", root_cause="wrong value",
            reasoning="set it", changed_paths=("value.txt",),
        )

    def test(repo_dir, command, timeout):  # noqa: ARG001
        value = Path(repo_dir) / "value.txt"
        if value.read_text() == "before\n":
            return TestRun(passed=False, output="[err]: value in tests/unit/value.tcl")
        value.write_text("right\n", encoding="utf-8")  # the command "fixes" the file itself
        return TestRun(passed=True, output="ok")

    candidate = engine_mod.prepare_fix(
        _task(head_sha), _LOG, agent_func=agent, review_func=lambda *a, **k: (True, "ok"), test_func=test,
    )

    assert candidate.action is CandidateAction.REFUSE
    assert "changed tracked files while the candidate was being verified" in candidate.summary
    assert all(engine_mod._TREE_CHANGED in text for text in feedback_seen[1:])


def test_a_fallback_copy_a_killed_run_left_never_reaches_the_patch(tmp_path):
    source, _ = _source_repo(tmp_path)
    engine_mod._ignore_generated_copies(str(source))
    engine_mod._ignore_generated_copies(str(source))  # idempotent
    (source / "tests" / "unit").mkdir(parents=True)
    (source / "tests" / "unit" / "a.ci-fix-isolated.tcl").write_text("left by a killed run", encoding="utf-8")

    assert engine_mod.tree_patch(str(source)) == ""
    exclude = (source / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.count("tests/**/*.ci-fix-isolated.tcl") == 1


def test_verification_may_use_the_rest_of_the_deadline_not_one_commands_default():
    """#5301 and 9.2's 40-loop checks died at 1800s with hours of the session left."""
    assert engine_mod._timeout(time.monotonic() + 3 * 3600) > engine_mod.TEST_TIMEOUT_S
    assert engine_mod._timeout(time.monotonic() - 5) == 60
