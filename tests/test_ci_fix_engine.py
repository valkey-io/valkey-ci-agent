from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.ci_fix import engine as engine_mod
from scripts.ci_fix.models import (
    CandidateAction,
    Failure,
    FixCandidate,
    FixPolicy,
    FixResult,
    FixTarget,
    FixTask,
    ResultKind,
    TargetKind,
)
from scripts.ci_fix.reproduce import TestRun
from tests.git_helpers import git


@pytest.fixture(autouse=True)
def _approving_review_and_no_tests(monkeypatch):
    """The real review and test runner start agents and processes; these tests are about the rest.

    Their test commands are the tests' own scripts, run directly: inside
    Actions the engine would otherwise insist on the isolated account, which
    tests/test_isolation.py drives on purpose.
    """
    monkeypatch.setattr(engine_mod, "review_candidate", lambda *a, **k: (True, "looks right"))
    monkeypatch.setattr(engine_mod, "isolated_user", lambda: "")


_LOG = "exact failed job log"


def test_a_c_unit_tests_file_is_located_in_the_checkout(tmp_path):
    (tmp_path / "src" / "unit").mkdir(parents=True)
    (tmp_path / "src" / "unit" / "test_crc64.cpp").write_text("", encoding="utf-8")
    tests = (FailedTest("Crc64Test.TestCrc64", "test_crc64.cpp"), FailedTest("x", "tests/unit/expire.tcl"),
             FailedTest("Gone.Test", "test_gone.cpp"))

    assert engine_mod.locate_unit_tests(str(tmp_path), tests) == (
        FailedTest("Crc64Test.TestCrc64", "src/unit/test_crc64.cpp"), tests[1], tests[2],
    )


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


def test_the_budget_fits_the_aws_session_and_leaves_a_round_after_reproduction() -> None:
    assert engine_mod.PREPARE_BUDGET_S < engine_mod.AWS_SESSION_S
    after_reproduction = engine_mod.PREPARE_BUDGET_S - engine_mod.REPRO_BUDGET_S
    assert after_reproduction - engine_mod._VERIFY_RESERVE_S >= engine_mod._MIN_ROUND_S


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
    """A path it forgot to declare is for the agent to correct, not a final refusal."""
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    declared = iter([(), ("value.txt",)])
    feedback = []

    def agent(task, repo_dir, job_log, **kwargs):
        feedback.append(kwargs["feedback"])
        (Path(repo_dir) / "value.txt").write_text("fixed\n", encoding="utf-8")
        return FixCandidate(
            action=CandidateAction.PATCH, failing_check="tests", root_cause="cause", reasoning="fix",
            changed_paths=next(declared),
        )

    candidate = engine_mod.prepare_fix(
        _task(head_sha), _LOG, agent_func=agent, review_func=lambda *a, **k: (True, "fixes it"),
    )

    assert candidate.action is CandidateAction.PATCH
    assert "did not declare its changed paths" in feedback[1]


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

    def other_issues_fixed(_task, candidate, repo_dir, *_args, **_kwargs):
        tree = subprocess.run(["git", "-C", repo_dir, "diff", "HEAD"], capture_output=True, text=True).stdout
        seen.append(("+fixed" in candidate.patch, "+fixed" in tree))
        return (), ()

    monkeypatch.setattr(engine_mod, "other_issues_fixed", other_issues_fixed)
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
        return replace(_port_agent(commit)(task, repo_dir, job_log), test_command="check")

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


def test_prepare_fix_refuses_undeclared_incidental_paths(tmp_path, monkeypatch) -> None:
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)

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
        _task(head_sha),
        _LOG,
        agent_func=agent,
    )

    assert candidate.action is CandidateAction.REFUSE
    assert "did not match the paths declared" in candidate.summary


def test_prepare_fix_bounds_a_large_job_log_but_keeps_its_tail(
    tmp_path,
    monkeypatch,
) -> None:
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    monkeypatch.setattr(engine_mod, "MAX_JOB_LOG_CHARS", 100)
    monkeypatch.setattr(engine_mod, "JOB_LOG_HEAD_CHARS", 20)
    seen = {}

    def agent(task, repo_dir, job_log, **_kwargs):
        seen["log"] = job_log
        return FixCandidate(action=CandidateAction.REFUSE, summary="no fix")

    candidate = engine_mod.prepare_fix(
        _task(head_sha),
        "start-" + "x" * 200 + "-final-error",
        agent_func=agent,
    )

    assert candidate.action is CandidateAction.REFUSE
    assert seen["log"].startswith("start-")
    assert "truncated" in seen["log"]
    assert seen["log"].endswith("-final-error")


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

def _value_agent(*values, command="test \"$(cat value.txt)\" = right", failure_type=None):
    """An agent writing each of ``values`` in turn, recording the feedback it got."""
    from scripts.ci_fix.models import FailureType

    rounds = iter(values)
    feedback = []

    def agent(task, repo_dir, job_log, *, feedback="", **_kwargs):  # noqa: ARG001
        agent.feedback.append(feedback)
        (Path(repo_dir) / "value.txt").write_text(f"{next(rounds)}\n", encoding="utf-8")
        return FixCandidate(
            action=CandidateAction.PATCH, failing_check="value check", root_cause="stale value",
            reasoning="set the right value", changed_paths=("value.txt",), test_command=command,
            failure_type=failure_type or FailureType.DETERMINISTIC,
        )

    agent.feedback = feedback
    return agent


def _prepare(tmp_path, monkeypatch, agent, *, kind=TargetKind.UPDATE_PR, review=None, test=None):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    return engine_mod.prepare_fix(
        _task(head_sha, kind=kind), _LOG, agent_func=agent,
        review_func=review or (lambda *a, **k: (True, "fixes the stale value")),
        test_func=test or engine_mod.run_test_command,
    )


def test_a_fix_whose_test_fails_without_it_and_passes_with_it_is_accepted(tmp_path, monkeypatch):
    agent = _value_agent("right")
    candidate = _prepare(tmp_path, monkeypatch, agent)
    assert candidate.action is CandidateAction.PATCH
    assert agent.feedback == [""]
    assert "failed without the patch (on run 1) and passed 2 time(s) with it" in candidate.verification
    assert "Review: fixes the stale value" in candidate.verification


def test_a_fix_whose_test_still_fails_goes_back_with_the_output(tmp_path, monkeypatch):
    agent = _value_agent("almost", "right")
    candidate = _prepare(tmp_path, monkeypatch, agent)
    assert candidate.action is CandidateAction.PATCH
    assert "With your change" in agent.feedback[1] and "failed on run 1 of 2" in agent.feedback[1]
    assert "Accepted on attempt 2 of 3." in candidate.verification
    assert "+right" in candidate.patch and "almost" not in candidate.patch


def test_a_test_that_passes_without_the_fix_does_not_count(tmp_path, monkeypatch):
    agent = _value_agent("right", "right", "right", command="true")
    candidate = _prepare(tmp_path, monkeypatch, agent)
    assert candidate.action is CandidateAction.REFUSE
    assert "passes 2 time(s) without your change" in agent.feedback[1]
    assert "No candidate passed its test and review in 3 attempts" in candidate.summary


def test_a_rejected_review_goes_back_to_the_agent(tmp_path, monkeypatch):
    verdicts = iter([(False, "skips the assertion"), (True, "fixes the cause")])
    agent = _value_agent("right", "right")
    candidate = _prepare(tmp_path, monkeypatch, agent, review=lambda *a, **k: next(verdicts))
    assert candidate.action is CandidateAction.PATCH
    assert agent.feedback[1].startswith("A reviewer rejected the patch: skips the assertion")
    assert agent.feedback[1].endswith("The working tree still holds your previous edits; revise them.")


def test_a_flaky_fix_must_pass_ten_times(tmp_path, monkeypatch):
    from scripts.ci_fix.models import FailureType

    runs = []

    def test(repo_dir, command, timeout):
        runs.append((Path(repo_dir) / "value.txt").read_text())
        return engine_mod.run_test_command(repo_dir, command, timeout)

    candidate = _prepare(tmp_path, monkeypatch, _value_agent("right", failure_type=FailureType.FLAKY), test=test)
    assert candidate.action is CandidateAction.PATCH
    assert runs == ["before\n"] + ["right\n"] * engine_mod.FLAKY_TEST_RUNS
    assert f"passed {engine_mod.FLAKY_TEST_RUNS} time(s)" in candidate.verification


def test_a_suggestion_is_reviewed_but_its_code_is_never_run(tmp_path, monkeypatch):
    test = []
    candidate = _prepare(tmp_path, monkeypatch, _value_agent("right"), kind=TargetKind.SUGGEST,
                         test=lambda *a: test.append(a) or TestRun(True, ""))
    assert candidate.action is CandidateAction.PATCH
    assert test == []


def _agent_command_check(monkeypatch, baseline, patched, *, tests=()):
    """``_test_feedback`` with the unpatched runs giving ``baseline`` and the patched ones ``patched``."""
    from contextlib import contextmanager

    state = {"patched": True}

    @contextmanager
    def without_patch(_repo_dir, _patch):
        state["patched"] = False
        try:
            yield
        finally:
            state["patched"] = True

    monkeypatch.setattr(engine_mod, "_without_patch", without_patch)
    candidate = FixCandidate(action=CandidateAction.PATCH, test_command="./runtest --single unit/value")
    checks = []
    feedback = engine_mod._test_feedback(
        candidate, "repo", lambda *_a: patched if state["patched"] else baseline,
        engine_mod.time.monotonic() + 3600, checks, tests,
    )
    return feedback, checks


def test_an_agent_command_killed_for_time_is_not_a_reproduction(monkeypatch):
    feedback, checks = _agent_command_check(
        monkeypatch, TestRun(False, "timed out after 60s", timed_out=True), TestRun(True, ""),
    )
    assert "did not finish" in feedback and checks == []


def test_an_agent_command_must_run_the_failing_test_with_the_patch(monkeypatch):
    test = FailedTest("value is right", "tests/unit/value.tcl")
    failing = TestRun(False, "", "[err]: value is right in tests/unit/value.tcl")
    renamed = TestRun(True, "", "[ok]: value is renamed (1 ms)")
    feedback, checks = _agent_command_check(monkeypatch, failing, renamed, tests=(test,))
    assert "without running the failing test" in feedback and checks == []

    ran = TestRun(True, "", "[ok]: value is right (1 ms)")
    feedback, checks = _agent_command_check(monkeypatch, failing, ran, tests=(test,))
    assert feedback == "" and checks


def test_an_agent_command_that_is_not_runtest_is_judged_by_its_exit_status(monkeypatch):
    """A gtest or other command names no runtest test; its exit status is the result."""
    test = FailedTest("value is right", "tests/unit/value.tcl")
    feedback, checks = _agent_command_check(
        monkeypatch, TestRun(False, "[  FAILED  ] ValueTest.Right"), TestRun(True, "[  PASSED  ] 1 test"),
        tests=(test,),
    )
    assert feedback == "" and checks


def test_without_a_test_command_the_report_says_so(tmp_path, monkeypatch):
    candidate = _prepare(tmp_path, monkeypatch, _value_agent("right", command=""))
    assert "No test command could run this check" in candidate.verification


def test_no_new_round_starts_without_time_to_finish_it(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "PREPARE_BUDGET_S", 60)
    agent = _value_agent("almost", "right")
    # A failed attempt, which a follow-up retries, not a refusal that retires the job.
    with pytest.raises(RuntimeError, match=r"^Ran out of time after 1 attempt\(s\)\."):
        _prepare(tmp_path, monkeypatch, agent)
    assert len(agent.feedback) == 1


# --- existing fixes and reproduction before the agent starts -----------------

from scripts.ci_fix.models import FailedTest  # noqa: E402
from scripts.ci_fix.prior_fixes import PriorFix  # noqa: E402
from scripts.ci_fix.reproduce import LOOPS, ReproPlan  # noqa: E402

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
        return ReproPlan(build="true", test=f'test "$(head -1 value.txt)" = right && : loops={LOOPS}', tests=tests)

    monkeypatch.setattr(engine_mod, "plan_for", plan_for)


def _runs_value_check(results):
    """A test runner that evaluates the plan command, recording each run."""
    def run(repo_dir, command, timeout):
        outcome = engine_mod.run_test_command(repo_dir, command, timeout)
        report = (
            "[ok]: value is right (1 ms)" if outcome.passed else "[err]: value is right in tests/unit/value.tcl"
        )
        loops = command.split("loops=", 1)[-1].split()[0]
        results.append(((Path(repo_dir) / "value.txt").read_text().splitlines()[0], loops))
        return TestRun(outcome.passed, outcome.output, report)

    return run


def test_the_failure_is_reproduced_first_and_the_fix_verified_with_the_same_command(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    seen, runs = {}, []
    _with_plan(monkeypatch, seen)
    agent = _value_agent("right", command="")
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
    # Reproduced on the unfixed code before the agent ran; verified with the fix.
    # Reproduced twice on the unfixed code (so not a lucky flake), verified with the fix.
    assert runs == [("before", "1"), ("before", "1"), ("right", "10")]
    assert contexts[0].reproduction.startswith("Reproduced on every run")
    assert contexts[0].verified_by_code
    assert "Reproduced on every run" in candidate.verification
    assert "the reproduction passed 10 loop(s)" in candidate.verification


def test_a_detector_issue_for_a_c_unit_test_is_reproduced_with_gtest_and_the_fix_verified(tmp_path, monkeypatch):
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
    runs, contexts = [], []

    def gtest_runner(repo_dir, command, timeout):
        """A unit-test binary whose verdict is value.txt's content."""
        assert "gtest_parallel.py ./valkey-unit-gtests --timeout_per_test 300 --repeat=" in command
        assert "--gtest_filter=Crc64Test.TestCrc64)" in command  # a PR's run: no --accurate
        value = (Path(repo_dir) / "value.txt").read_text().splitlines()[0]
        runs.append((value, command.split("--repeat=", 1)[1].split()[0]))
        if value == "right":
            return TestRun(True, "", "[1/1] Crc64Test.TestCrc64 (1 ms)")
        return TestRun(False, "", "src/unit/test_crc64.cpp:29: Failure\n[  FAILED  ] Crc64Test.TestCrc64 (0 ms)")

    agent = _value_agent("right", command="")

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
    assert runs == [("before", "1"), ("before", "1"), ("right", "10")]
    assert contexts[0].reproduction.startswith("Reproduced on every run with `make")
    assert "--repeat=1 --gtest_filter=Crc64Test.TestCrc64)`" in contexts[0].reproduction
    assert "the reproduction passed 10 loop(s)" in candidate.verification


def test_a_flake_that_failed_both_plain_runs_is_still_verified_as_a_flake(tmp_path, monkeypatch):
    from scripts.ci_fix.models import FailureType

    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    runs = []
    _with_plan(monkeypatch)
    task = _task(head_sha)
    task = replace(task, failure=replace(task.failure, failed_tests=(FailedTest("value is right", "tests/unit/value.tcl"),)))
    candidate = engine_mod.prepare_fix(
        task, _LOG_WITH_FAILURE, agent_func=_value_agent("right", command="", failure_type=FailureType.FLAKY),
        test_func=_runs_value_check(runs),
    )

    assert candidate.action is CandidateAction.PATCH
    # Both plain runs failed, but the agent judged it flaky: a flake's loops.
    assert runs == [("before", "1"), ("before", "1"), ("right", "40")]
    assert "the reproduction passed 40 loop(s)" in candidate.verification


def test_a_fix_that_does_not_stop_the_reproduction_goes_back(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    _with_plan(monkeypatch)
    agent = _value_agent("almost", "right", command="")

    candidate = engine_mod.prepare_fix(
        _task(head_sha), _LOG_WITH_FAILURE, agent_func=agent,
        test_func=_runs_value_check([]),
    )

    assert candidate.action is CandidateAction.PATCH
    assert "the reproduction still fails within 10 loop(s)" in agent.feedback[1]


def test_a_failure_that_does_not_reproduce_is_reported_as_unproven(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    (source / "value.txt").write_text("right\n", encoding="utf-8")
    git(source, "commit", "-qam", "already right")
    head_sha = git(source, "rev-parse", "HEAD")
    _install_clone(monkeypatch, source)
    _with_plan(monkeypatch)
    runs = []

    candidate = engine_mod.prepare_fix(
        _task(head_sha), _LOG_WITH_FAILURE,
        agent_func=_value_agent("right\nwith a sturdier wait", command=""),
        test_func=_runs_value_check(runs),
    )

    assert candidate.action is CandidateAction.PATCH
    assert [loops for _value, loops in runs] == ["1", "20", "20", "40"]
    assert "Did not reproduce" in candidate.verification
    assert "does not show the fix works" in candidate.verification


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
    calls = iter([
        TestRun(False, "", "[err]: value is right in tests/unit/value.tcl"),  # reproduction,
        TestRun(False, "", "[err]: value is right in tests/unit/value.tcl"),  # confirmed
        TestRun(True, "", ""),  # with the fix: nothing ran
        TestRun(True, "", "[ok]: value is right (1 ms)"),
    ])
    agent = _value_agent("skip it", "right", command="")

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
    calls = iter([
        TestRun(False, "", "[err]: value is right in tests/unit/value.tcl"),
        TestRun(False, "", "[err]: value is right in tests/unit/value.tcl"),
        TestRun(False, "", "[exception]: Executing test client: broken pipe"),
        TestRun(True, "", "[ok]: value is right (1 ms)"),
    ])
    agent = _value_agent("broken", "right", command="")

    engine_mod.prepare_fix(
        _task(head_sha), _LOG_WITH_FAILURE, agent_func=agent,
        test_func=lambda *a: next(calls),
    )

    assert "could not build or run the tests" in agent.feedback[1]
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
    monkeypatch.setattr(engine_mod, "as_user", lambda user, env, argv, home: ["sh", "-c", "exec sleep 2"])
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


def test_when_code_cannot_reproduce_the_agents_own_reproduction_proves_the_fix(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    (source / "value.txt").write_text("right\n", encoding="utf-8")
    git(source, "commit", "-qam", "plan test passes")
    head_sha = git(source, "rev-parse", "HEAD")
    _install_clone(monkeypatch, source)
    _with_plan(monkeypatch)
    # The agent's command widens the race: it fails until the second line exists.
    agent = _value_agent("right\nfixed", command="test \"$(wc -l < value.txt)\" -ge 2")

    candidate = engine_mod.prepare_fix(
        _task(head_sha), _LOG_WITH_FAILURE, agent_func=agent,
        test_func=_runs_value_check([]),
    )

    assert candidate.action is CandidateAction.PATCH
    assert "Did not reproduce" in candidate.verification
    assert "failed without the patch (on run 1)" in candidate.verification


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
    first = _value_agent("almost")

    def agent(task, repo_dir, job_log, **kwargs):
        if kwargs["feedback"]:
            raise RuntimeError("CI-fix agent failed with exit code 1")
        return first(task, repo_dir, job_log, **kwargs)

    with pytest.raises(RuntimeError, match="^The agent failed on attempt 2") as raised:
        engine_mod.prepare_fix(_task(head_sha), _LOG, agent_func=agent)

    assert "With your change" in str(raised.value)


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


def test_a_detector_issue_reproduces_only_its_own_test(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    seen = {}
    _with_plan(monkeypatch, seen)
    own = FailedTest("value is right", "tests/unit/value.tcl")
    log = _LOG_WITH_FAILURE + "[err]: another flaky test in tests/unit/other.tcl\n"
    task = _task(head_sha)
    task = replace(task, failure=replace(task.failure, failed_tests=(own,), issue_number=7))

    engine_mod.prepare_fix(task, log, agent_func=_value_agent("right", command=""),
                           test_func=_runs_value_check([]))

    assert seen["tests"] == (own,)


def test_a_verification_that_runs_out_of_time_is_a_failed_attempt(tmp_path, monkeypatch):
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    _with_plan(monkeypatch)
    calls = iter([
        TestRun(False, "", "[err]: value is right in tests/unit/value.tcl"),
        TestRun(False, "", "[err]: value is right in tests/unit/value.tcl"),
        TestRun(False, "timed out after 60s", timed_out=True),
        TestRun(True, "", "[ok]: value is right (1 ms)"),
    ])
    agent = _value_agent("right", "right", command="")

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

    from scripts.ci_fix.reproduce import TestRun

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


def test_another_issue_is_claimed_only_after_its_own_test_is_verified(tmp_path, monkeypatch):
    candidate = _prepare_with_others(tmp_path, monkeypatch, [(11, "B")])
    assert candidate.action is CandidateAction.PATCH
    assert (candidate.also_fixes, candidate.also_touches) == ((11,), ())
    assert "Also fixes issue #11: `B` failed without the patch and passed with it." in candidate.verification


def test_an_issue_whose_test_the_patch_does_not_fix_is_only_mentioned(tmp_path, monkeypatch):
    # C is not in the file, so its reproduction never fails: nothing is shown.
    candidate = _prepare_with_others(tmp_path, monkeypatch, [(12, "C")])
    assert (candidate.also_fixes, candidate.also_touches) == ((), (12,))
    assert "The patch changes `C`, issue `#12`'s test, but it was not shown to fix it." in candidate.verification
    # Unlinked: a bare #12 would let this PR keep #12 from a fix of its own.
    assert "#12" not in candidate.verification.replace("`#12`", "")


def test_an_issue_in_another_file_is_not_considered(tmp_path, monkeypatch):
    from scripts.ci_fix.models import FailedTest, OtherIssue

    source, head_sha = _two_test_repo(tmp_path)
    _install_clone(monkeypatch, source)
    task = replace(
        _task(head_sha, kind=TargetKind.DRAFT_PR),
        other_issues=(OtherIssue(number=13, test=FailedTest("Z", "tests/unit/other.tcl")),),
    )
    task = replace(task, failure=replace(task.failure, failed_tests=(FailedTest("A", "tests/unit/t.tcl"),)))
    candidate = engine_mod.prepare_fix(
        task, _LOG, agent_func=_fix_both_agent(),
        review_func=lambda *a, **k: (True, "ok"), test_func=_only_tests_pass,
    )
    assert (candidate.also_fixes, candidate.also_touches) == ((), ())


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


def test_a_test_command_that_rewrites_the_patched_file_does_not_get_its_patch_published(tmp_path, monkeypatch):
    """The tree is the candidate: a command that edits it during verification changes the candidate."""
    source, head_sha = _source_repo(tmp_path)
    _install_clone(monkeypatch, source)
    feedback_seen = []

    def agent(task, repo_dir, job_log, feedback="", **_kwargs):  # noqa: ARG001
        feedback_seen.append(feedback)
        (Path(repo_dir) / "value.txt").write_text("bad\n", encoding="utf-8")
        return FixCandidate(
            action=CandidateAction.PATCH, failing_check="value", root_cause="wrong value",
            reasoning="set it", changed_paths=("value.txt",), test_command="check",
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
