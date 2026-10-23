"""Wrapper around the Claude Code CLI."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from scripts.common.isolation import as_user, handed_to, kill_all
from scripts.common.logging_utils import log_group
from scripts.common.proc import filter_env

logger = logging.getLogger(__name__)

_DEFAULT_CLAUDE_MODEL = "fable"
_DEFAULT_BEDROCK_FABLE_MODEL = "us.anthropic.claude-fable-5"
_DEFAULT_BEDROCK_OPUS_MODEL = "us.anthropic.claude-opus-4-8"
_CLAUDE_MODEL_ENV = "CI_AGENT_CLAUDE_MODEL"
_BEDROCK_FABLE_MODEL_ENV = "CI_AGENT_CLAUDE_BEDROCK_FABLE_MODEL"
_BEDROCK_OPUS_MODEL_ENV = "CI_AGENT_CLAUDE_BEDROCK_OPUS_MODEL"
_DEFAULT_TIMEOUT_SECONDS = 60 * 60
_PASSTHROUGH_ENV_VARS = {
    "PATH",
    "HOME",
    "TMPDIR",
    "TMP",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
}
DEFAULT_CLAUDE_ENV_ALLOWLIST = tuple(sorted(_PASSTHROUGH_ENV_VARS))


def run_claude_code(
    prompt: str,
    *,
    cwd: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
    model: str | None = _DEFAULT_CLAUDE_MODEL,
    effort: str | None = "max",
    max_turns: int = 200,
    allowed_tools: str = "Read,Edit,MultiEdit,Write,Bash,Glob,Grep",
    disallowed_tools: str | None = None,
    env_allowlist: tuple[str, ...] | None = None,
    confined: bool = False,
    extra_dirs: tuple[str, ...] = (),
    run_as: str = "",
) -> tuple[str, str, int]:
    """Run claude CLI and return (stdout, stderr, exit_code).

    Requires ``claude`` on PATH and Bedrock credentials in the
    environment (CLAUDE_CODE_USE_BEDROCK=1 + AWS creds).

    ``confined`` keeps the CLI's own working-directory boundary instead of
    bypassing all permission checks: tools may read (and, under
    ``acceptEdits``, edit) only ``cwd`` and ``extra_dirs``, and a request for
    anything else - another directory, ``/proc``, a symlink out of the tree - is
    refused. Use it whenever the prompt or the files carry untrusted content.
    A confined profile cannot use Bash, which would stall on an approval, or
    Glob, whose absolute patterns are not boundary-checked.
    """
    env = _build_claude_env(env_allowlist)
    env["CLAUDE_CODE_USE_BEDROCK"] = "1"
    # Resolve once here so the env-var override (CI_AGENT_CLAUDE_MODEL)
    # always wins, regardless of whether the caller pre-resolved.
    # runtime.run_agent intentionally calls _resolve_claude_model too so
    # it can capture the resolved value in the audit record - the two
    # calls are idempotent by design (override wins each time).
    resolved_model = _resolve_claude_model(model)
    env["ANTHROPIC_DEFAULT_FABLE_MODEL"] = _resolve_bedrock_fable_model()
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = _resolve_bedrock_opus_model()
    if "AWS_REGION" not in env and "AWS_DEFAULT_REGION" not in env:
        env["AWS_REGION"] = "us-east-1"
    elif "AWS_REGION" not in env and "AWS_DEFAULT_REGION" in env:
        env["AWS_REGION"] = env["AWS_DEFAULT_REGION"]
    elif "AWS_DEFAULT_REGION" not in env and "AWS_REGION" in env:
        env["AWS_DEFAULT_REGION"] = env["AWS_REGION"]

    cmd = [
        "claude", "--print",
        "--max-turns", str(max_turns),
        "--tools", allowed_tools,
        # Three layers, each doing distinct work: --tools is the set of tools
        # that exist, --disallowedTools (below) hard-denies specific ones, and
        # the permission mode decides what an existing tool may touch.
        *_permission_args(allowed_tools, confined, extra_dirs),
        # cwd is an untrusted checkout. --safe-mode disables all project
        # customizations (CLAUDE.md, hooks, plugins, skills) so a malicious
        # repo cannot execute code in our credentialed subprocess.
        # --strict-mcp-config additionally blocks project-defined MCP servers.
        "--safe-mode",
        "--strict-mcp-config",
        "--output-format", "stream-json",
        "--verbose",
    ]
    denied = (
        _default_disallowed_tools(allowed_tools)
        if disallowed_tools is None
        else disallowed_tools
    )
    if denied:
        cmd.extend(["--disallowedTools", denied])
    if resolved_model:
        cmd.extend(["--model", resolved_model])
    if effort:
        cmd.extend(["--effort", effort])

    logger.info("Running Claude Code in %s (timeout %ds)", cwd or ".", timeout)
    started = time.monotonic()
    # The event stream is long; fold it so the lines around it stay readable.
    with log_group("Claude Code output"):
        logger.debug("Prompt starts: %s", " ".join(prompt[:200].split()))
        if run_as:
            # The checkout is untrusted and Bash can run anything in it: run
            # the CLI as an account that cannot read this process's
            # environment, where the job's credentials live.
            if not cwd:
                raise ValueError("an isolated run needs a working directory")
            with handed_to(run_as, Path(cwd), readable=tuple(Path(d) for d in extra_dirs)) as home:
                stdout, stderr, returncode = _run_streaming(
                    as_user(run_as, env, cmd, home=home), prompt, cwd=cwd, env={}, timeout=timeout,
                    run_as=run_as,
                )
        else:
            stdout, stderr, returncode = _run_streaming(cmd, prompt, cwd=cwd, env=env, timeout=timeout)
    elapsed = time.monotonic() - started
    if stderr.startswith("timeout"):
        logger.error("Claude Code timed out after %ds.", timeout)
    elif returncode == 127 and stderr == "claude not found":
        logger.error("claude CLI not found on PATH.")
    else:
        logger.log(logging.INFO if returncode == 0 else logging.WARNING,
                   "Claude Code exited %d after %.0fs (%d chars of output).",
                   returncode, elapsed, len(stdout))
    return stdout, stderr, returncode


def _run_streaming(
    cmd: list[str], prompt: str, *, cwd: str | None, env: dict[str, str], timeout: int,
    run_as: str = "",
) -> tuple[str, str, int]:
    stdout_parts: list[str] = []
    process = None
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
            env=env,
            bufsize=1,
            # Its own process group, so a timeout also kills what the CLI
            # spawned (the `claude` launcher execs a separate binary).
            start_new_session=True,
        )

        def _read_stdout() -> None:
            if process.stdout is None:
                return
            for line in process.stdout:
                stdout_parts.append(line)
                _log_stream_event(line)

        def _write_stdin() -> None:
            # A prompt larger than the pipe buffer blocks until the CLI reads
            # it; writing here keeps the timeout below armed meanwhile.
            if process.stdin is None:
                return
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        reader = threading.Thread(target=_read_stdout, daemon=True)
        reader.start()
        writer = threading.Thread(target=_write_stdin, daemon=True)
        writer.start()

        returncode = process.wait(timeout=timeout)
        writer.join(timeout=5)
        reader.join(timeout=5)
        stdout = "".join(stdout_parts)
        return stdout, "", returncode
    except subprocess.TimeoutExpired:
        if process is not None:
            _kill_process_group(process, run_as)
        # Let the reader thread flush buffered output before we read it.
        reader.join(timeout=5)
        stdout = "".join(stdout_parts)
        return stdout, f"timeout after {timeout}s", 1
    except FileNotFoundError:
        return "", "claude not found", 127


def _kill_process_group(process: Any, run_as: str = "") -> None:
    """Kill *process* and everything it spawned; fall back to the process alone.

    A CLI run as ``run_as`` sits under root's sudo, which this process may
    not signal: everything that account runs is killed instead.
    """
    if run_as:
        kill_all(run_as)
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
        return
    except (AttributeError, TypeError, ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.kill()
    except (ProcessLookupError, PermissionError):
        pass


def _permission_args(allowed_tools: str, confined: bool, extra_dirs: tuple[str, ...]) -> list[str]:
    """CLI permission flags for one run.

    Unconfined runs skip all permission checks (the approval prompt would
    otherwise block every write in headless --print mode); their checkouts are
    trusted code. Confined runs keep the working-directory boundary: reads, and
    edits under acceptEdits, are allowed only inside cwd and ``extra_dirs``.
    """
    if not confined:
        return ["--dangerously-skip-permissions"]
    tools = {token.split("(", 1)[0] for token in re.split(r"[\s,]+", allowed_tools) if token}
    if "Bash" in tools:
        raise ValueError("a confined agent profile cannot allow Bash")
    # Glob checks its path argument but not an absolute pattern, so it lists
    # file names anywhere; Grep (files_with_matches) lists inside the boundary.
    if "Glob" in tools:
        raise ValueError("a confined agent profile cannot allow Glob")
    edits = any(tool in allowed_tools for tool in ("Edit", "MultiEdit"))
    # No settings files at all: a checkout's .claude/settings.json (or one a
    # test planted in ~/.claude) could add directories or allow rules that
    # lift the boundary. Model, region and credentials come from flags and env.
    args = ["--permission-mode", "acceptEdits" if edits else "default", "--setting-sources", ""]
    for directory in extra_dirs:
        args += ["--add-dir", directory]
    return args


def _build_claude_env(env_allowlist: tuple[str, ...] | None = None) -> dict[str, str]:
    """Return the minimal environment Claude Code needs for Bedrock.

    GitHub tokens and other workflow secrets are intentionally not inherited.
    Tool-using prompts may contain untrusted PR or artifact content, so the
    subprocess gets only process/runtime basics plus AWS credentials required
    by the Bedrock provider.
    """
    allowed = set(env_allowlist or DEFAULT_CLAUDE_ENV_ALLOWLIST)
    env = filter_env(tuple(allowed))
    env["CLAUDE_CODE_USE_BEDROCK"] = "1"
    env["ANTHROPIC_DEFAULT_FABLE_MODEL"] = _resolve_bedrock_fable_model()
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = _resolve_bedrock_opus_model()
    return env


def _resolve_claude_model(model: str | None) -> str | None:
    """Resolve the Claude Code model alias, honoring operator override."""
    override = os.environ.get(_CLAUDE_MODEL_ENV, "").strip()
    if override:
        return override
    return model or _DEFAULT_CLAUDE_MODEL


def _resolve_bedrock_fable_model() -> str:
    """Resolve the Bedrock Fable model/inference profile used by Claude Code."""
    return (
        os.environ.get(_BEDROCK_FABLE_MODEL_ENV, "").strip()
        or _DEFAULT_BEDROCK_FABLE_MODEL
    )


def _resolve_bedrock_opus_model() -> str:
    """Resolve the Bedrock Opus model/inference profile used by Claude Code."""
    return (
        os.environ.get(_BEDROCK_OPUS_MODEL_ENV, "").strip()
        or _DEFAULT_BEDROCK_OPUS_MODEL
    )


def _default_disallowed_tools(allowed_tools: str) -> str:
    """Deny dangerous tools unless the profile explicitly allowed them."""
    allowed = {
        token.split("(", 1)[0]
        for token in re.split(r"[\s,]+", allowed_tools.strip())
        if token
    }
    return ",".join(tool for tool in ("Bash", "Write") if tool not in allowed)


def _log_stream_event(raw_line: str) -> None:
    raw_line = raw_line.strip()
    if not raw_line:
        return
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        logger.info("Claude stream: %s", _truncate(raw_line, 500))
        return

    summary = _summarize_stream_event(event)
    if summary:
        logger.info("Claude stream: %s", summary)
    else:
        logger.debug("Claude stream event: %s", _truncate(raw_line, 1000))


def _summarize_stream_event(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or event.get("event") or "")
    subtype = str(event.get("subtype") or "")

    if event_type == "system":
        session_id = event.get("session_id") or event.get("sessionId") or ""
        model = event.get("model") or ""
        cwd = event.get("cwd") or ""
        parts = ["system"]
        if subtype:
            parts.append(subtype)
        if model:
            parts.append(f"model={model}")
        if session_id:
            parts.append(f"session={session_id}")
        if cwd:
            parts.append(f"cwd={cwd}")
        return " ".join(parts)

    if event_type == "assistant":
        message = event.get("message")
        if not isinstance(message, dict):
            return "assistant event"
        content = message.get("content")
        summaries: list[str] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text = str(block.get("text") or "").strip()
                    if text:
                        summaries.append(f"text={_truncate(text, 240)}")
                elif block_type == "tool_use":
                    name = str(block.get("name") or "tool")
                    summaries.append(f"tool={name} {_summarize_tool_input(block.get('input'))}")
        return "assistant " + "; ".join(summaries) if summaries else "assistant event"

    if event_type == "user":
        message = event.get("message")
        if not isinstance(message, dict):
            return "user event"
        content = message.get("content")
        if isinstance(content, list):
            result_count = sum(
                1 for block in content
                if isinstance(block, dict) and block.get("type") == "tool_result"
            )
            if result_count:
                return f"tool_result count={result_count}"
        return "user event"

    if event_type == "result":
        duration = event.get("duration_ms")
        cost = event.get("total_cost_usd")
        turns = event.get("num_turns")
        result = str(event.get("result") or "").strip()
        parts = ["result"]
        if subtype:
            parts.append(subtype)
        if turns is not None:
            parts.append(f"turns={turns}")
        if duration is not None:
            parts.append(f"duration_ms={duration}")
        if cost is not None:
            parts.append(f"cost_usd={cost}")
        if result:
            parts.append(f"text={_truncate(result, 300)}")
        return " ".join(parts)

    return f"{event_type or 'unknown'} event"


def _summarize_tool_input(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("file_path", "path", "pattern", "command"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return f"{key}={_truncate(value, 180)}"
    return _truncate(json.dumps(tool_input, sort_keys=True, default=str), 180)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"
