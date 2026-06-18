"""Tests for the AI diagnose step.

The parsing is what we own deterministically: the agent's structured output
becomes a typed ``FixProposal``, an unrecognized path collapses to REFUSE, and
agent failures raise. The NAN-payload backport failure (PRs #3988/#3989) is the
end-to-end shape we expect for an "author" proposal.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from scripts.ci_fix import diagnose as diagnose_mod
from scripts.ci_fix.diagnose import diagnose_failure, write_logs_to_workspace
from scripts.ci_fix.models import FixPath
from scripts.ci_fix.port_discovery import PortCandidate


def _stream_json_result(obj: dict) -> str:
    """Wrap a payload the way Claude Code stream-json emits a final result."""
    return "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "result", "subtype": "success", "result": json.dumps(obj)}),
    ])


def _mock_agent(monkeypatch, stdout: str, returncode: int = 0) -> None:
    monkeypatch.setattr(
        diagnose_mod, "run_agent",
        MagicMock(return_value=MagicMock(stdout=stdout, stderr="", returncode=returncode)),
    )


def test_author_proposal_nan_payload(monkeypatch):
    payload = {
        "path": "author",
        "failing_check": "corrupt payload: zset listpack with NAN score",
        "root_cause": "RESTORE payload embeds RDB version 80; on this branch "
                      "RDB_VERSION is 11, so RESTORE rejects it before the NAN check.",
        "reasoning": "Test scaffolding: set the payload's RDB version byte to the "
                     "branch RDB_VERSION and zero the checksum. Assertion unchanged.",
        "confidence": 0.9,
        "build_command": "make -j4",
        "verify_command": "./runtest --single integration/corrupt-dump --dump-logs",
        "workdir": "",
        "unstable_fix_commit": "",
        "other_failing_checks": [],
    }
    _mock_agent(monkeypatch, _stream_json_result(payload))

    proposal = diagnose_failure("/tmp/ci.log", "/tmp/repo")
    assert proposal.path is FixPath.AUTHOR
    assert "NAN score" in proposal.failing_check
    assert proposal.verify_command.startswith("./runtest")
    assert proposal.confidence == 0.9


def test_port_proposal_carries_commit(monkeypatch):
    payload = {
        "path": "port",
        "failing_check": "some test",
        "root_cause": "already fixed upstream",
        "reasoning": "clean cherry-pick",
        "confidence": 0.8,
        "build_command": "make",
        "verify_command": "./runtest --single unit/x",
        "unstable_fix_commit": "abc123",
    }
    _mock_agent(monkeypatch, _stream_json_result(payload))

    proposal = diagnose_failure("/tmp/ci.log", "/tmp/repo")
    assert proposal.path is FixPath.PORT
    assert proposal.unstable_fix_commit == "abc123"


def test_refuse_proposal(monkeypatch):
    payload = {
        "path": "refuse",
        "failing_check": "flaky timing test",
        "root_cause": "intermittent timing dependency",
        "reasoning": "genuinely flaky; no safe deterministic fix",
        "confidence": 0.2,
    }
    _mock_agent(monkeypatch, _stream_json_result(payload))

    proposal = diagnose_failure("/tmp/ci.log", "/tmp/repo")
    assert proposal.path is FixPath.REFUSE
    assert proposal.build_command == ""


def test_unknown_path_collapses_to_refuse(monkeypatch):
    payload = {"path": "yolo", "failing_check": "t", "confidence": 0.99}
    _mock_agent(monkeypatch, _stream_json_result(payload))

    proposal = diagnose_failure("/tmp/ci.log", "/tmp/repo")
    assert proposal.path is FixPath.REFUSE


def test_author_without_essentials_collapses_to_refuse(monkeypatch):
    # An "author" proposal with no test name / root cause cannot be acted on.
    payload = {"path": "author", "failing_check": "", "root_cause": "", "confidence": 0.9}
    _mock_agent(monkeypatch, _stream_json_result(payload))
    assert diagnose_failure("/tmp/ci.log", "/tmp/repo").path is FixPath.REFUSE


def test_confidence_clamped(monkeypatch):
    payload = {"path": "refuse", "confidence": 5.0}
    _mock_agent(monkeypatch, _stream_json_result(payload))
    assert diagnose_failure("/tmp/ci.log", "/tmp/repo").confidence == 1.0


def test_agent_failure_raises(monkeypatch):
    _mock_agent(monkeypatch, "", returncode=1)
    with pytest.raises(RuntimeError, match="diagnosis agent failed"):
        diagnose_failure("/tmp/ci.log", "/tmp/repo")


def test_no_json_raises(monkeypatch):
    _mock_agent(monkeypatch, "no json here at all")
    with pytest.raises(ValueError, match="no diagnosis JSON"):
        diagnose_failure("/tmp/ci.log", "/tmp/repo")


def test_plain_json_without_stream_wrapper(monkeypatch):
    """The agent may emit a bare JSON object, not wrapped in stream-json."""
    payload = {"path": "refuse", "failing_check": "t", "confidence": 0.1}
    _mock_agent(monkeypatch, json.dumps(payload))
    assert diagnose_failure("/tmp/ci.log", "/tmp/repo").path is FixPath.REFUSE


def test_hint_is_included_in_prompt(monkeypatch):
    captured = {}

    def fake_run_agent(profile, prompt, **kwargs):
        captured["prompt"] = prompt
        return MagicMock(
            stdout=_stream_json_result({"path": "refuse", "confidence": 0.0}),
            stderr="", returncode=0,
        )

    monkeypatch.setattr(diagnose_mod, "run_agent", fake_run_agent)
    diagnose_failure("/tmp/ci.log", "/tmp/repo", hint="look at the valgrind timeout")
    assert "valgrind timeout" in captured["prompt"]
    assert "Maintainer hint" in captured["prompt"]


def test_port_candidates_are_included_in_prompt(monkeypatch):
    captured = {}

    def fake_run_agent(profile, prompt, **kwargs):
        captured["prompt"] = prompt
        return MagicMock(
            stdout=_stream_json_result({"path": "refuse", "confidence": 0.0}),
            stderr="", returncode=0,
        )

    monkeypatch.setattr(diagnose_mod, "run_agent", fake_run_agent)
    diagnose_failure(
        "/tmp/ci.log", "/tmp/repo",
        port_candidates=(
            PortCandidate(
                sha="9f374e15848d7b070cdd58a071a741c0a59a6c75",
                subject="Skips the internal clients from logresreq checks (#3154)",
                paths=("src/logreqres.c",),
            ),
        ),
    )
    assert "Default-branch candidate fixes" in captured["prompt"]
    assert "9f374e15848d" in captured["prompt"]
    assert "src/logreqres.c" in captured["prompt"]


def test_long_hint_is_truncated(monkeypatch):
    captured = {}

    def fake_run_agent(profile, prompt, **kwargs):
        captured["prompt"] = prompt
        return MagicMock(
            stdout=_stream_json_result({"path": "refuse", "confidence": 0.0}),
            stderr="", returncode=0,
        )

    monkeypatch.setattr(diagnose_mod, "run_agent", fake_run_agent)
    long_hint = "Z" * 2000
    diagnose_failure("/tmp/ci.log", "/tmp/repo", hint=long_hint)
    # The 2000-char hint is capped at the 500-char limit ("Z" appears nowhere
    # else in the prompt template).
    assert captured["prompt"].count("Z") == 500


def test_write_logs_to_workspace_keeps_files_separate(tmp_path):
    logs = {
        "test/2_test.txt": b"[err]: NAN score",
        "build/1_build.txt": b"make output",
    }
    logs_dir = write_logs_to_workspace(logs, tmp_path)
    # Each step is its own file (no giant concatenated blob), path separators
    # flattened so the layout is one level deep.
    written = sorted(p.name for p in logs_dir.iterdir())
    assert written == ["build__1_build.txt", "test__2_test.txt"]
    assert (logs_dir / "test__2_test.txt").read_bytes() == b"[err]: NAN score"


def test_refuse_proposal_clears_actionable_fields(monkeypatch):
    # A REFUSE proposal must not carry build/verify/commit data.
    payload = {"path": "refuse", "failing_check": "t", "root_cause": "real bug",
               "build_command": "make", "verify_command": "./runtest", "workdir": "src",
               "unstable_fix_commit": "abc123"}
    _mock_agent(monkeypatch, _stream_json_result(payload))
    p = diagnose_failure("/tmp/ci.log", "/tmp/repo")
    assert p.path is FixPath.REFUSE
    assert p.build_command == "" and p.verify_command == ""
    assert p.workdir == "" and p.unstable_fix_commit == "" and p.failing_job_hint == ""


def test_max_turns_exhaustion_refuses_gracefully(monkeypatch):
    """A diagnosis that runs out of turns must refuse, not raise, and carry its
    partial findings into the reason for the PR comment."""
    import json as _json

    from scripts.ci_fix.models import FixPath

    stream = "\n".join([
        _json.dumps({"type": "assistant", "text": "Tracing the reqres desync in the validator."}),
        _json.dumps({"type": "result", "subtype": "error_max_turns",
                     "result": "Found a parser desync but ran out of turns."}),
    ])
    _mock_agent(monkeypatch, stream, returncode=1)
    proposal = diagnose_failure("/logs", "/repo")
    assert proposal.path is FixPath.REFUSE
    assert "investigation budget" in proposal.reasoning
    assert "parser desync" in proposal.reasoning  # partial findings surfaced


def test_genuine_agent_failure_still_raises(monkeypatch):
    """A nonzero exit without the turn-exhaustion marker is a real failure."""
    _mock_agent(monkeypatch, "some crash output", returncode=1)
    try:
        diagnose_failure("/logs", "/repo")
        raised = False
    except RuntimeError:
        raised = True
    assert raised
