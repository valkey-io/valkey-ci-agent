"""Tests for the edit-only fix application step."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from scripts.ci_fix import apply as apply_mod
from scripts.ci_fix.apply import apply_fix
from scripts.ci_fix.models import FixPath, FixProposal


def _proposal(path: FixPath = FixPath.AUTHOR) -> FixProposal:
    return FixProposal(
        path=path, failing_check="t", root_cause="rc", reasoning="why",
        confidence=0.9, build_command="make", verify_command="./runtest --single x",
    )


def test_refuse_proposal_never_calls_agent(monkeypatch):
    agent = MagicMock()
    monkeypatch.setattr(apply_mod, "run_agent", agent)
    ok, changed = apply_fix("/repo", _proposal(FixPath.REFUSE))
    assert ok is False
    assert changed == ()
    agent.assert_not_called()


def test_agent_failure_returns_not_applied(monkeypatch):
    monkeypatch.setattr(apply_mod, "run_agent",
                        MagicMock(return_value=MagicMock(returncode=1, stdout="", stderr="boom")))
    monkeypatch.setattr(apply_mod, "worktree_changed_paths", lambda _r: ("test.tcl",))
    ok, changed = apply_fix("/repo", _proposal())
    assert ok is False
    assert changed == ()


def test_no_edits_treated_as_refusal(monkeypatch):
    """The agent ran cleanly but declined to edit (e.g. fix would weaken assertion)."""
    monkeypatch.setattr(apply_mod, "run_agent",
                        MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr="")))
    monkeypatch.setattr(apply_mod, "worktree_changed_paths", lambda _r: ())
    ok, changed = apply_fix("/repo", _proposal())
    assert ok is False
    assert changed == ()


def test_successful_edit_returns_changed_paths(monkeypatch):
    monkeypatch.setattr(apply_mod, "run_agent",
                        MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr="")))
    monkeypatch.setattr(apply_mod, "worktree_changed_paths",
                        lambda _r: ("tests/integration/corrupt-dump.tcl",))
    ok, changed = apply_fix("/repo", _proposal())
    assert ok is True
    assert changed == ("tests/integration/corrupt-dump.tcl",)


def test_feedback_included_in_prompt(monkeypatch):
    captured = {}

    def fake_run_agent(profile, prompt, **kwargs):
        captured["prompt"] = prompt
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(apply_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(apply_mod, "worktree_changed_paths", lambda _r: ("t",))
    apply_fix("/repo", _proposal(), feedback="the test still failed at line 42")
    assert "still failed at line 42" in captured["prompt"]
    assert "rejected" in captured["prompt"].lower()


def test_apply_fix_declines_port_path(monkeypatch):
    """PORT is cherry-picked in the pipeline with its original authorship, so
    apply_fix (the authored-fix editor) must not act on a PORT proposal."""
    agent = MagicMock()
    monkeypatch.setattr(apply_mod, "run_agent", agent)
    ok, changed = apply_fix("/repo", _proposal(FixPath.PORT))
    assert ok is False
    assert changed == ()
    agent.assert_not_called()
