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


def _loop(*, path: FixPath = FixPath.AUTHOR, patch: str = "the diff", **overrides):
    """run_fix_loop with safe fakes; override individual collaborators per test.

    Patches the shared ``build_approved_patch`` (the review loop calls it
    directly) to return ``patch`` without needing a real git repo. Pass
    ``patch=""`` is not allowed - use ``patch_raises=EmptyPatch`` semantics by
    patching directly in the test.
    """
    defaults = dict(
        apply_func=lambda *a, **k: (True, ("test.tcl",)),
        run_command=lambda *a, **k: _passed(),
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


def test_retries_when_test_fails_then_passes():
    runs = [_failed(), _passed()]
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


def test_unrunnable_command_breaks_loop():
    unrunnable = RunResult(ran=False, passed=False, exit_code=-1, command="c", output_tail="no cwd")
    result = _loop(run_command=lambda *a, **k: unrunnable)
    assert result.success is False
    assert "could not run" in result.detail


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

    runs = [_failed(), _passed()]
    _loop(max_attempts=2, apply_func=fake_apply, run_command=lambda *a, **k: runs.pop(0))
    assert seen_feedback[0] == ""           # first attempt: no feedback
    assert "did not make the test pass" in seen_feedback[1]


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
    assert _is_noop_command("true") is True
    # Real commands whose failure can surface are not no-ops.
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
            run_command=lambda *a, **k: _passed(),
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
            run_command=lambda *a, **k: _passed(),
            review_func=review_called,
            reset_func=MagicMock(),
        )

    assert result.success is False
    assert "no change" in result.detail
    review_called.assert_not_called()


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
