"""Tests for the apply/run/review fix-feedback loop.

The loop is the orchestration core. These tests inject fakes for apply, run,
review, and reset, and patch the shared patch builder, so we exercise the
control flow deterministically: success only when test-passed AND
review-approved, retry-on-feedback, refusal on an empty patch, and worktree
reset on every non-success exit.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from scripts.ci_fix import review as review_mod
from scripts.ci_fix.models import FixPath, FixProposal, ReviewVerdict, RunResult
from scripts.ci_fix.review import review_fix, run_fix_loop


def patch_build_approved(func):
    """Patch the shared build_approved_patch the review loop calls directly."""
    return patch.object(review_mod, "build_approved_patch", func)


def _proposal(path: FixPath = FixPath.AUTHOR) -> FixProposal:
    return FixProposal(
        path=path,
        failing_check="t",
        root_cause="rc",
        reasoning="why",
        confidence=0.9,
        build_command="make",
        verify_command="./runtest --single x",
    )


def _passed() -> RunResult:
    return RunResult(ran=True, passed=True, exit_code=0, command="c", output_tail="ok")


def _failed() -> RunResult:
    return RunResult(ran=True, passed=False, exit_code=1, command="c", output_tail="boom")


def _approved() -> ReviewVerdict:
    return ReviewVerdict(approved=True, reasoning="looks good")


def _rejected() -> ReviewVerdict:
    return ReviewVerdict(approved=False, reasoning="weakens assertion")


def _reproduce_then_pass():
    """Default run order: baseline fails, then build/verify calls pass."""
    results = iter([_failed()])

    def fake(*_a, **_k):
        return next(results, _passed())

    return fake


def _loop(*, path: FixPath = FixPath.AUTHOR, patch: str = "the diff", **overrides):
    """run_fix_loop with safe fakes; override individual collaborators per test.

    Patches the shared ``build_approved_patch`` (the review loop calls it
    directly) to return ``patch`` without needing a real git repo. The default
    command fake makes the baseline reproduce fail, then lets the fix verify.
    """
    defaults = dict(
        apply_func=lambda *a, **k: (True, ("test.tcl",)),
        run_command=_reproduce_then_pass(),
        review_func=lambda *a, **k: _approved(),
        reset_func=MagicMock(),
    )
    defaults.update(overrides)
    with patch_build_approved(lambda *a, **k: patch):
        return run_fix_loop("/repo", _proposal(path), **defaults)


def test_success_requires_pass_and_approval():
    result = _loop()
    assert result.success is True
    assert result.attempts == 1
    assert result.changed_paths == ("test.tcl",)


def test_refuse_proposal_short_circuits():
    result = _loop(path=FixPath.REFUSE, apply_func=lambda *a, **k: (False, ()))
    assert result.success is False
    assert "not applied" in result.detail


def test_retries_when_verify_fails_then_passes():
    runs = [_failed(), _passed(), _failed(), _passed(), _passed(), _passed()]
    result = _loop(max_attempts=3, run_command=lambda *a, **k: runs.pop(0))
    assert result.success is True
    assert result.attempts == 2


def test_retries_when_review_rejects_then_approves():
    reviews = [_rejected(), _approved()]
    result = _loop(max_attempts=3, review_func=lambda *a, **k: reviews.pop(0))
    assert result.success is True
    assert result.attempts == 2


def test_gives_up_after_max_attempts():
    result = _loop(max_attempts=2, review_func=lambda *a, **k: _rejected())
    assert result.success is False
    assert result.attempts == 2
    assert "review rejected" in result.detail


def test_unrunnable_command_hands_off_with_patch():
    """A verify command that cannot run here is not a failed fix: the loop hands
    off the authored patch for a human rather than refusing as if it failed."""
    unrunnable = RunResult(ran=False, passed=False, exit_code=-1, command="c", output_tail="no jsonschema")
    result = _loop(run_command=lambda *a, **k: unrunnable)
    assert result.success is False
    assert result.handoff is True
    assert result.handoff_patch == "the diff"
    assert "could not verify" in result.detail


def test_missing_dependency_failure_hands_off():
    """A ran-and-failed verify caused by a missing dependency (the job's setup
    was not replayed) is a handoff, not a refusal: the build never judged the
    fix. This is the common jsonschema/uses:-setup case."""
    missing_dep = RunResult(
        ran=True, passed=False, exit_code=1, command="c",
        output_tail="Traceback...\nModuleNotFoundError: No module named 'jsonschema'",
    )
    result = _loop(run_command=lambda *a, **k: missing_dep)
    assert result.success is False
    assert result.handoff is True
    assert result.handoff_patch == "the diff"


def test_genuine_test_failure_does_not_hand_off():
    """A ran-and-failed verify with no dependency signature is a real fix
    failure: retry then refuse, never hand off."""
    result = _loop(max_attempts=1, run_command=lambda *a, **k: _failed())
    assert result.success is False
    assert result.handoff is False
    assert "check still failing" in result.detail


def test_rejected_review_is_not_handed_off():
    """When verification cannot run but the skeptic rejects the patch, it must
    not be handed off: handoff is gated on review approval, not patch presence."""
    unrunnable = RunResult(ran=False, passed=False, exit_code=-1, command="c", output_tail="cannot run")
    result = _loop(run_command=lambda *a, **k: unrunnable, review_func=lambda *a, **k: _rejected())
    assert result.handoff is False
    assert result.handoff_patch == ""
    assert result.review == _rejected()
    assert "review rejected" in result.detail


def test_dependency_signatures_do_not_match_generic_failures():
    """The dep-signature heuristic must not swallow ordinary failures: a generic
    'not found' message or a bad-include compile error is a real fix failure,
    not a missing-dependency handoff."""
    from scripts.ci_fix.review import looks_like_missing_dependency
    assert looks_like_missing_dependency("Error: not found") is False
    assert looks_like_missing_dependency("assertion failed: key not found") is False
    assert looks_like_missing_dependency("fatal error: wrongheader.h: No such file") is False
    # but real toolchain/dependency signals still match
    assert looks_like_missing_dependency("ModuleNotFoundError: No module named 'x'") is True
    assert looks_like_missing_dependency("./run: line 3: pytest: command not found") is True
    assert looks_like_missing_dependency("sh: 1: cmake: not found") is True


def test_worktree_reset_on_failure():
    """Every non-success exit must reset the worktree to HEAD."""
    reset = MagicMock()
    _loop(max_attempts=1, review_func=lambda *a, **k: _rejected(), reset_func=reset)
    # One reset at loop start + one on the failure exit.
    assert reset.call_count >= 2


def test_zero_max_attempts_is_clamped():
    """A pathological max_attempts=0 must not raise; it runs at least once."""
    result = _loop(max_attempts=0)
    assert result.success is True
    assert result.attempts == 1


def test_feedback_passed_to_apply_on_retry():
    seen_feedback = []

    def fake_apply(repo, proposal, *, feedback=""):
        seen_feedback.append(feedback)
        return True, ("test.tcl",)

    runs = [_failed(), _passed(), _failed(), _passed(), _passed(), _passed()]
    _loop(max_attempts=2, apply_func=fake_apply, run_command=lambda *a, **k: runs.pop(0))
    assert seen_feedback[0] == ""           # first attempt: no feedback
    assert "did not make the check pass" in seen_feedback[1]


def _stream_result(obj: dict) -> str:
    return json.dumps({"type": "result", "subtype": "success", "result": json.dumps(obj)})


def test_review_fix_parses_approval(monkeypatch):
    monkeypatch.setattr(
        review_mod, "run_agent",
        MagicMock(return_value=MagicMock(
            stdout=_stream_result({"approved": True, "reasoning": "minimal and correct"}),
            stderr="", returncode=0,
        )),
    )
    verdict = review_fix("/repo", _proposal(), "some diff")
    assert verdict.approved is True
    assert "minimal" in verdict.reasoning


def test_review_fix_rejects_on_agent_failure(monkeypatch):
    monkeypatch.setattr(
        review_mod, "run_agent",
        MagicMock(return_value=MagicMock(stdout="", stderr="", returncode=1)),
    )
    verdict = review_fix("/repo", _proposal(), "diff")
    assert verdict.approved is False


def test_review_fix_rejects_when_no_verdict(monkeypatch):
    monkeypatch.setattr(
        review_mod, "run_agent",
        MagicMock(return_value=MagicMock(stdout="no json", stderr="", returncode=0)),
    )
    verdict = review_fix("/repo", _proposal(), "diff")
    assert verdict.approved is False


def test_review_fix_requires_strict_true(monkeypatch):
    """A truthy non-bool (e.g. the string "yes") must not count as approval."""
    monkeypatch.setattr(
        review_mod, "run_agent",
        MagicMock(return_value=MagicMock(
            stdout=_stream_result({"approved": "yes", "reasoning": "ambiguous"}),
            stderr="", returncode=0,
        )),
    )
    verdict = review_fix("/repo", _proposal(), "diff")
    assert verdict.approved is False


def test_empty_verify_command_refuses():
    """No verify command means the fix can't be verified - fail closed."""
    proposal = FixProposal(
        path=FixPath.AUTHOR, failing_check="t", root_cause="rc", reasoning="why",
        confidence=0.9, build_command="make", verify_command="",
    )
    result = run_fix_loop(
        "/repo", proposal,
        apply_func=lambda *a, **k: (True, ("test.tcl",)),
        run_command=lambda *a, **k: _passed(),
        review_func=lambda *a, **k: _approved(),
        reset_func=MagicMock(),
    )
    assert result.success is False
    assert "no command to verify" in result.detail


def test_noop_verify_command_refuses():
    """A command with no build/test signal must not gate a push."""
    proposal = FixProposal(
        path=FixPath.AUTHOR, failing_check="t", root_cause="rc", reasoning="why",
        confidence=0.9, build_command="", verify_command="true && echo done",
    )
    ran = MagicMock()
    result = run_fix_loop(
        "/repo", proposal,
        apply_func=lambda *a, **k: (True, ("test.tcl",)),
        run_command=ran,
        review_func=lambda *a, **k: _approved(),
        reset_func=MagicMock(),
    )
    assert result.success is False
    assert "no build or test signal" in result.detail
    ran.assert_not_called()


def test_is_noop_command_catches_short_circuit_and_trailing():
    """The exit-determining statement governs: masked failures are no-ops."""
    from scripts.ci_fix.review import _is_noop_command
    assert _is_noop_command("make || true") is True
    assert _is_noop_command("make; true") is True
    assert _is_noop_command("./runtest && true") is True
    assert _is_noop_command("make test || :") is True
    assert _is_noop_command("make test | tee test.log") is True
    assert _is_noop_command("true") is True
    # Real commands whose failure can surface are not no-ops.
    assert _is_noop_command("set -o pipefail; make test | tee test.log") is False
    assert _is_noop_command("make && ./runtest --single x") is False
    assert _is_noop_command("./runtest --single x") is False
    assert _is_noop_command("cc -o x x.c && ./x") is False


def test_oversized_patch_refuses():
    """A patch larger than the review cap must fail closed, not push unreviewed."""
    from scripts.ci_fix.review import MAX_REVIEWABLE_PATCH_CHARS

    big = "+" * (MAX_REVIEWABLE_PATCH_CHARS + 1)
    review_called = MagicMock()
    with patch_build_approved(lambda *a, **k: big):
        result = run_fix_loop(
            "/repo", _proposal(),
            apply_func=lambda *a, **k: (True, ("test.tcl",)),
            run_command=_reproduce_then_pass(),
            review_func=review_called,
            reset_func=MagicMock(),
        )
    assert result.success is False
    assert "too large" in result.detail
    review_called.assert_not_called()


def test_empty_patch_refuses_instead_of_approving():
    """If the approved paths produce no patch, the loop must refuse, not approve.

    A passing test with no actual change (or a vanished edit) must never reach
    a push. The review loop inspects the exact patch a push would apply, so an
    empty patch fails the attempt.
    """
    from scripts.common.proc import EmptyPatch

    def empty_patch(*a, **k):
        raise EmptyPatch("approved paths produced an empty patch")

    review_called = MagicMock()
    with patch_build_approved(empty_patch):
        result = run_fix_loop(
            "/repo", _proposal(),
            apply_func=lambda *a, **k: (True, ("test.tcl",)),
            run_command=_reproduce_then_pass(),
            review_func=review_called,
            reset_func=MagicMock(),
        )

    assert result.success is False
    assert "no change" in result.detail
    review_called.assert_not_called()


# --- reproduce-before-fix and repeated verification ---

def _calls_recorder():
    calls = []

    def make(results):
        seq = list(results)

        def fake(_repo, command, **_k):
            calls.append(command)
            return seq.pop(0)

        return fake

    return calls, make


def test_reproduce_green_refuses_as_flaky():
    apply_called = MagicMock()
    result = _loop(
        run_command=lambda *a, **k: _passed(),
        apply_func=apply_called,
    )
    assert result.success is False
    assert "did not reproduce" in result.detail
    apply_called.assert_not_called()


def test_verify_runs_k_times_and_builds_once():
    calls, make = _calls_recorder()
    run = make([_failed(), _passed(), _passed(), _passed(), _passed()])
    result = _loop(verify_runs=3, run_command=run)
    assert result.success is True
    assert calls[0] == "make && ./runtest --single x"
    assert calls[1] == "make"
    assert calls[2:] == ["./runtest --single x"] * 3


def test_one_of_k_verify_runs_fails_retries():
    calls, make = _calls_recorder()
    run = make([_failed(), _passed(), _passed(), _failed()])
    result = _loop(verify_runs=2, max_attempts=1, run_command=run)
    assert result.success is False
    assert "still failing" in result.detail
    assert calls == [
        "make && ./runtest --single x",
        "make",
        "./runtest --single x",
        "./runtest --single x",
    ]


def test_empty_build_runs_verify_command_k_times():
    proposal = FixProposal(
        path=FixPath.AUTHOR, failing_check="long enough check",
        root_cause="rc", reasoning="w", confidence=0.9,
        build_command="", verify_command="make && ./runtest x",
    )
    calls, make = _calls_recorder()
    run = make([_failed(), _passed(), _passed()])
    with patch_build_approved(lambda *a, **k: "diff"):
        result = run_fix_loop(
            "/repo", proposal, verify_runs=2,
            apply_func=lambda *a, **k: (True, ("f",)),
            run_command=run,
            review_func=lambda *a, **k: _approved(),
            reset_func=MagicMock(),
        )
    assert result.success is True
    assert calls == ["make && ./runtest x", "make && ./runtest x", "make && ./runtest x"]


def test_baseline_unrunnable_hands_off_even_if_post_fix_passes():
    """A green post-fix run without a failing baseline is not enough to push."""
    runs = [RunResult(False, False, -1, "c", "no cwd"), _passed(), _passed(), _passed()]
    result = _loop(run_command=lambda *a, **k: runs.pop(0))
    assert result.success is False
    assert result.handoff is True
    assert "baseline" in result.detail


def test_reproduced_the_named_failure_matching():
    from scripts.ci_fix.review import reproduced_the_named_failure

    prop = FixProposal(
        path=FixPath.AUTHOR, failing_check="zset listpack with NAN score",
        root_cause="rc", reasoning="w", confidence=0.9,
        build_command="make", verify_command="./runtest x",
    )
    hit = RunResult(
        ran=True, passed=False, exit_code=1, command="c",
        output_tail="[err]: zset listpack with NAN score in tests/x.tcl",
    )
    miss = RunResult(
        ran=True, passed=False, exit_code=1, command="c",
        output_tail="ld: symbol not found",
    )
    assert reproduced_the_named_failure(prop, hit) is True
    assert reproduced_the_named_failure(prop, miss) is False

    short = FixProposal(
        path=FixPath.AUTHOR, failing_check="io",
        root_cause="rc", reasoning="w", confidence=0.9,
        build_command="m", verify_command="v",
    )
    assert reproduced_the_named_failure(short, RunResult(True, False, 1, "c", "io error")) is False


# --- direct tests for the shared helpers ---

def test_precheck_command_refuses_empty_and_noop():
    from scripts.ci_fix.review import precheck_command
    empty = FixProposal(path=FixPath.AUTHOR, failing_check="t", root_cause="rc",
                        reasoning="w", confidence=0.9, build_command="", verify_command="")
    assert "no command to verify" in precheck_command(empty)
    noop = FixProposal(path=FixPath.AUTHOR, failing_check="t", root_cause="rc",
                       reasoning="w", confidence=0.9, build_command="", verify_command="make || true")
    assert "no build or test signal" in precheck_command(noop)
    ok = FixProposal(path=FixPath.AUTHOR, failing_check="t", root_cause="rc",
                     reasoning="w", confidence=0.9, build_command="make", verify_command="./runtest x")
    assert precheck_command(ok) == ""


def test_build_and_review_patch_empty_oversized_rejected_ok(monkeypatch):
    from scripts.ci_fix.review import (
        MAX_REVIEWABLE_PATCH_CHARS,
        build_and_review_patch,
    )
    from scripts.common.proc import EmptyPatch

    # empty
    monkeypatch.setattr(review_mod, "build_approved_patch",
                        lambda *a, **k: (_ for _ in ()).throw(EmptyPatch("x")))
    r = build_and_review_patch("/repo", ("f",), _proposal(), review_func=lambda *a, **k: _approved())
    assert r.ok is False and "no change" in r.detail

    # oversized
    monkeypatch.setattr(review_mod, "build_approved_patch",
                        lambda *a, **k: "+" * (MAX_REVIEWABLE_PATCH_CHARS + 1))
    r = build_and_review_patch("/repo", ("f",), _proposal(), review_func=lambda *a, **k: _approved())
    assert r.ok is False and "too large" in r.detail

    # rejected
    monkeypatch.setattr(review_mod, "build_approved_patch", lambda *a, **k: "small diff")
    r = build_and_review_patch("/repo", ("f",), _proposal(), review_func=lambda *a, **k: _rejected())
    assert r.ok is False and r.review is not None and "rejected" in r.detail

    # ok
    r = build_and_review_patch("/repo", ("f",), _proposal(), review_func=lambda *a, **k: _approved())
    assert r.ok is True and r.patch == "small diff" and r.review.approved is True
