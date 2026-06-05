from __future__ import annotations

import json

from scripts.ai import runtime as agent_runtime


def test_run_agent_applies_profile_and_writes_hashed_evidence(tmp_path, monkeypatch) -> None:
    calls = {}

    def fake_run_claude_code(prompt, **kwargs):
        calls["prompt"] = prompt
        calls.update(kwargs)
        return "secret stdout", "secret stderr", 0

    monkeypatch.setattr(agent_runtime, "run_claude_code", fake_run_claude_code)
    monkeypatch.delenv("CI_AGENT_CLAUDE_MODEL", raising=False)

    result = agent_runtime.run_agent(
        "conflict_resolve_edit_only",
        "review this",
        cwd="/tmp/repo",
        evidence_dir=tmp_path,
    )

    assert result.returncode == 0
    assert calls["allowed_tools"] == "Read,Edit,MultiEdit,Grep,Glob,Bash"
    assert calls["disallowed_tools"] == "Write"
    assert "GITHUB_TOKEN" not in calls["env_allowlist"]
    assert calls["timeout"] == agent_runtime.AGENT_PROFILES["conflict_resolve_edit_only"].timeout
    assert calls["effort"] == "max"

    evidence_files = list(tmp_path.glob("*.json"))
    assert len(evidence_files) == 1
    evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
    assert evidence["profile"]["name"] == "conflict_resolve_edit_only"
    assert "stdout" not in evidence["result"]
    assert "stderr" not in evidence["result"]
    assert "secret stdout" not in evidence_files[0].read_text(encoding="utf-8")


def test_run_agent_writes_default_github_actions_evidence(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("CI_AGENT_EVIDENCE_DIR", raising=False)
    monkeypatch.setattr(
        agent_runtime,
        "run_claude_code",
        lambda *_args, **_kwargs: ("stdout", "", 0),
    )

    agent_runtime.run_agent("conflict_resolve_edit_only", "summarize", cwd=str(tmp_path))

    evidence_files = list((tmp_path / "agent-evidence").glob("*.json"))
    assert len(evidence_files) == 1


def test_validation_repair_profile_denies_shell_and_write_tools() -> None:
    profile = agent_runtime.AGENT_PROFILES["validation_repair_edit_only"]

    assert profile.allowed_tools == "Read,Edit,MultiEdit,Grep,Glob"
    assert profile.disallowed_tools == "Bash,Write"


def test_fuzzer_profile_is_readonly() -> None:
    profile = agent_runtime.AGENT_PROFILES["fuzzer_analysis_readonly"]
    assert profile.writes_allowed is False
    assert "Edit" not in profile.allowed_tools
    assert "Bash" not in profile.allowed_tools
    assert "Read" in profile.allowed_tools
