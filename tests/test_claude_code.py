from __future__ import annotations

import io
import logging
import subprocess
import time

from scripts.ai import claude_code


class _RecordingStdin(io.StringIO):
    def close(self):
        pass


class _FakeProcess:
    def __init__(
        self,
        cmd,
        *,
        stdout_text: str = "",
        returncode: int = 0,
        timeout: bool = False,
        **kwargs,
    ):
        self.cmd = cmd
        self.kwargs = kwargs
        self.stdin = _RecordingStdin()
        self.stdout = io.StringIO(stdout_text)
        self.returncode = returncode
        self.timeout = timeout
        self.killed = False

    def wait(self, timeout=None):
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd=self.cmd, timeout=timeout)
        return self.returncode

    def kill(self):
        self.killed = True


def test_run_claude_code_streams_json_and_uses_bedrock_env(monkeypatch, caplog):
    captured = {}
    stream = (
        '{"type":"system","subtype":"init","session_id":"abc","model":"fable","cwd":"/tmp/checkout"}\n'
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"src/a.c"}}]}}\n'
        '{"type":"result","subtype":"success","num_turns":2,"duration_ms":123,"total_cost_usd":0.01,"result":"done"}\n'
    )

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        captured["process"] = _FakeProcess(cmd, stdout_text=stream, **kwargs)
        return captured["process"]

    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("CI_AGENT_CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("CI_AGENT_CLAUDE_BEDROCK_FABLE_MODEL", raising=False)
    monkeypatch.delenv("CI_AGENT_CLAUDE_BEDROCK_OPUS_MODEL", raising=False)
    monkeypatch.setattr(claude_code.subprocess, "Popen", fake_popen)

    with caplog.at_level(logging.INFO, logger="scripts.ai.claude_code"):
        stdout, stderr, rc = claude_code.run_claude_code(
            "fix this", cwd="/tmp/checkout"
        )

    assert stdout == stream
    assert stderr == ""
    assert rc == 0
    assert captured["process"].stdin.getvalue() == "fix this"
    assert captured["cmd"][:4] == ["claude", "--print", "--max-turns", "200"]
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "fable"
    assert captured["cmd"][captured["cmd"].index("--effort") + 1] == "max"
    assert (
        captured["cmd"][captured["cmd"].index("--output-format") + 1] == "stream-json"
    )
    assert "--verbose" in captured["cmd"]
    tools = captured["cmd"][captured["cmd"].index("--tools") + 1]
    assert "Edit" in tools
    assert "MultiEdit" in tools
    assert "--dangerously-skip-permissions" in captured["cmd"]
    assert "--strict-mcp-config" in captured["cmd"]
    assert "--disallowedTools" not in captured["cmd"]
    assert captured["kwargs"]["cwd"] == "/tmp/checkout"
    assert captured["kwargs"]["env"]["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert (
        captured["kwargs"]["env"]["ANTHROPIC_DEFAULT_FABLE_MODEL"]
        == "us.anthropic.claude-fable-5"
    )
    assert (
        captured["kwargs"]["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"]
        == "us.anthropic.claude-opus-4-8"
    )
    assert captured["kwargs"]["env"]["AWS_REGION"] == "us-east-1"
    assert (
        "Claude stream: system init model=fable session=abc cwd=/tmp/checkout"
        in caplog.text
    )
    assert "Claude stream: assistant tool=Read file_path=src/a.c" in caplog.text
    assert (
        "Claude stream: result success turns=2 duration_ms=123 cost_usd=0.01 text=done"
        in caplog.text
    )


def test_run_claude_code_preserves_existing_region_and_model(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return _FakeProcess(
            cmd, stdout_text='{"type":"result","result":"ok"}\n', **kwargs
        )

    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.delenv("CI_AGENT_CLAUDE_MODEL", raising=False)
    monkeypatch.setattr(claude_code.subprocess, "Popen", fake_popen)

    stdout, stderr, rc = claude_code.run_claude_code("prompt", model="model-id")

    assert (stdout, stderr, rc) == ('{"type":"result","result":"ok"}\n', "", 0)
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "model-id"
    assert captured["env"]["AWS_REGION"] == "us-west-2"


def test_run_claude_code_does_not_inherit_github_tokens(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _FakeProcess(
            cmd, stdout_text='{"type":"result","result":"ok"}\n', **kwargs
        )

    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("GH_TOKEN", "gh-secret")
    monkeypatch.setenv("BACKPORT_GITHUB_TOKEN", "backport-secret")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setattr(claude_code.subprocess, "Popen", fake_popen)

    stdout, stderr, rc = claude_code.run_claude_code("prompt")

    assert (stdout, stderr, rc) == ('{"type":"result","result":"ok"}\n', "", 0)
    assert captured["env"]["AWS_REGION"] == "us-west-2"
    assert "GITHUB_TOKEN" not in captured["env"]
    assert "GH_TOKEN" not in captured["env"]
    assert "BACKPORT_GITHUB_TOKEN" not in captured["env"]


def test_run_claude_code_denies_bash_and_write_when_not_allowed(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProcess(
            cmd, stdout_text='{"type":"result","result":"ok"}\n', **kwargs
        )

    monkeypatch.setattr(claude_code.subprocess, "Popen", fake_popen)

    stdout, stderr, rc = claude_code.run_claude_code(
        "prompt",
        allowed_tools="Read,Edit,MultiEdit,Grep,Glob",
    )

    assert (stdout, stderr, rc) == ('{"type":"result","result":"ok"}\n', "", 0)
    assert (
        captured["cmd"][captured["cmd"].index("--tools") + 1]
        == "Read,Edit,MultiEdit,Grep,Glob"
    )
    assert "--dangerously-skip-permissions" in captured["cmd"]
    assert (
        captured["cmd"][captured["cmd"].index("--disallowedTools") + 1] == "Bash,Write"
    )


def test_run_claude_code_respects_explicit_empty_disallowed_tools(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProcess(
            cmd, stdout_text='{"type":"result","result":"ok"}\n', **kwargs
        )

    monkeypatch.setattr(claude_code.subprocess, "Popen", fake_popen)

    stdout, stderr, rc = claude_code.run_claude_code(
        "prompt",
        allowed_tools="Read,Edit,MultiEdit,Grep,Glob",
        disallowed_tools="",
    )

    assert (stdout, stderr, rc) == ('{"type":"result","result":"ok"}\n', "", 0)
    assert "--disallowedTools" not in captured["cmd"]


def test_run_claude_code_honors_model_env_overrides(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return _FakeProcess(
            cmd, stdout_text='{"type":"result","result":"ok"}\n', **kwargs
        )

    monkeypatch.setenv("CI_AGENT_CLAUDE_MODEL", "custom-opus")
    monkeypatch.setenv(
        "CI_AGENT_CLAUDE_BEDROCK_FABLE_MODEL",
        "global.anthropic.claude-fable-5",
    )
    monkeypatch.setenv(
        "CI_AGENT_CLAUDE_BEDROCK_OPUS_MODEL",
        "global.anthropic.claude-opus-4-7",
    )
    monkeypatch.setattr(claude_code.subprocess, "Popen", fake_popen)

    stdout, stderr, rc = claude_code.run_claude_code("prompt", model="ignored")

    assert (stdout, stderr, rc) == ('{"type":"result","result":"ok"}\n', "", 0)
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "custom-opus"
    assert captured["env"]["ANTHROPIC_DEFAULT_FABLE_MODEL"] == (
        "global.anthropic.claude-fable-5"
    )
    assert captured["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == (
        "global.anthropic.claude-opus-4-7"
    )


def test_run_claude_code_reports_timeout(monkeypatch):
    fake_processes = []

    def fake_popen(cmd, **kwargs):
        process = _FakeProcess(
            cmd,
            stdout_text='{"type":"assistant","message":{"content":[]}}\n',
            timeout=True,
            **kwargs,
        )
        fake_processes.append(process)
        return process

    monkeypatch.setattr(claude_code.subprocess, "Popen", fake_popen)

    stdout, stderr, rc = claude_code.run_claude_code("prompt", timeout=3)

    assert stdout == '{"type":"assistant","message":{"content":[]}}\n'
    assert stderr == "timeout after 3s"
    assert rc == 1
    assert fake_processes[0].killed is True


def test_an_isolated_cli_that_times_out_is_stopped_through_its_account(monkeypatch):
    """The CLI sits under root's sudo, which the runner may not signal."""
    process = _FakeProcess(["sudo"], timeout=True)

    def no_signal(*_args):
        raise PermissionError("Operation not permitted")

    killed = []
    monkeypatch.setattr(claude_code.subprocess, "Popen", lambda cmd, **_kwargs: process)
    monkeypatch.setattr(claude_code.os, "killpg", no_signal)
    monkeypatch.setattr(process, "kill", no_signal)
    monkeypatch.setattr(claude_code, "kill_all", killed.append)

    stdout, stderr, rc = claude_code._run_streaming(
        ["sudo"], "prompt", cwd=None, env={}, timeout=3, run_as="ci-fix-agent",
    )

    assert (stderr, rc) == ("timeout after 3s", 1)
    assert killed == ["ci-fix-agent"]


def test_a_process_that_may_not_be_signalled_does_not_turn_a_timeout_into_a_crash(monkeypatch):
    def no_signal(*_args):
        raise PermissionError("Operation not permitted")

    process = _FakeProcess(["cli"])
    monkeypatch.setattr(claude_code.os, "killpg", no_signal)
    monkeypatch.setattr(process, "kill", no_signal)

    claude_code._kill_process_group(process)


def test_run_claude_code_reports_missing_cli(monkeypatch):
    def fake_popen(_cmd, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(claude_code.subprocess, "Popen", fake_popen)

    assert claude_code.run_claude_code("prompt") == ("", "claude not found", 127)


def test_timeout_fires_even_when_the_cli_never_reads_its_prompt(monkeypatch):
    # A prompt larger than the pipe buffer, to a process that never reads
    # stdin and spawns a child: the call must still time out and kill both.
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        return real_popen(["sh", "-c", "sleep 30 & sleep 30"], **kwargs)

    monkeypatch.setattr(claude_code.subprocess, "Popen", fake_popen)
    started = time.monotonic()
    stdout, stderr, rc = claude_code.run_claude_code("x" * 1_000_000, timeout=2)
    assert stderr == "timeout after 2s" and rc == 1
    assert time.monotonic() - started < 20


# --- confined runs: the CLI's working-directory boundary is kept ---------------------

def _captured_cmd(monkeypatch, **kwargs):
    from scripts.ai import claude_code

    captured = {}

    class _Proc:
        stdout = iter(())
        stdin = io.StringIO()

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **_kw):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(claude_code.subprocess, "Popen", fake_popen)
    claude_code.run_claude_code("p", cwd="/repo", **kwargs)
    return captured["cmd"]


def test_a_confined_read_only_run_keeps_permission_checks(monkeypatch):
    cmd = _captured_cmd(monkeypatch, allowed_tools="Read,Grep", confined=True, extra_dirs=("/work/logs",))
    assert "--dangerously-skip-permissions" not in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "default"
    assert cmd[cmd.index("--add-dir") + 1] == "/work/logs"
    assert "--allowedTools" not in cmd  # a blanket allow would lift the path boundary
    # A checkout's or user's settings file could add directories or allow rules.
    assert cmd[cmd.index("--setting-sources") + 1] == ""


def test_a_confined_edit_run_accepts_edits_only_inside(monkeypatch):
    cmd = _captured_cmd(monkeypatch, allowed_tools="Read,Edit,MultiEdit,Grep", confined=True)
    assert "--dangerously-skip-permissions" not in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--add-dir" not in cmd


def test_a_confined_profile_cannot_allow_bash(monkeypatch):
    import pytest

    with pytest.raises(ValueError, match="cannot allow Bash"):
        _captured_cmd(monkeypatch, allowed_tools="Read,Bash", confined=True)


def test_a_confined_profile_cannot_allow_glob(monkeypatch):
    """Glob's absolute patterns are not boundary-checked, so it would list names anywhere."""
    import pytest

    with pytest.raises(ValueError, match="cannot allow Glob"):
        _captured_cmd(monkeypatch, allowed_tools="Read,Grep,Glob", confined=True)


def test_unconfined_runs_keep_their_existing_flags(monkeypatch):
    cmd = _captured_cmd(monkeypatch, allowed_tools="Read,Edit,Bash")
    assert "--dangerously-skip-permissions" in cmd
    assert "--permission-mode" not in cmd
