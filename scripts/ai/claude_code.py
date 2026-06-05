"""Wrapper around the Claude Code CLI."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_CLAUDE_MODEL = "opus"
_DEFAULT_BEDROCK_OPUS_MODEL = "us.anthropic.claude-opus-4-8"
_CLAUDE_MODEL_ENV = "CI_AGENT_CLAUDE_MODEL"
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
) -> tuple[str, str, int]:
    """Run claude CLI and return (stdout, stderr, exit_code).

    Requires ``claude`` on PATH and Bedrock credentials in the
    environment (CLAUDE_CODE_USE_BEDROCK=1 + AWS creds).
    """
    env = _build_claude_env(env_allowlist)
    env["CLAUDE_CODE_USE_BEDROCK"] = "1"
    # Resolve once here so the env-var override (CI_AGENT_CLAUDE_MODEL)
    # always wins, regardless of whether the caller pre-resolved.
    # runtime.run_agent intentionally calls _resolve_claude_model too so
    # it can capture the resolved value in the audit record — the two
    # calls are idempotent by design (override wins each time).
    resolved_model = _resolve_claude_model(model)
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
        # --dangerously-skip-permissions drops the interactive approval prompt
        # that otherwise blocks every write in headless --print mode. The env
        # is already hardened (GitHub tokens stripped, AWS-only) and runs in
        # throwaway checkouts.
        "--dangerously-skip-permissions",
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

    logger.info("Running claude: cwd=%s, timeout=%d, prompt=%s…", cwd, timeout, prompt[:120])
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
        )

        def _read_stdout() -> None:
            if process.stdout is None:
                return
            for line in process.stdout:
                stdout_parts.append(line)
                _log_stream_event(line)

        reader = threading.Thread(target=_read_stdout, daemon=True)
        reader.start()
        if process.stdin is not None:
            process.stdin.write(prompt)
            process.stdin.close()

        returncode = process.wait(timeout=timeout)
        reader.join(timeout=5)
        stdout = "".join(stdout_parts)
        logger.info("Claude exited %d (%d chars stdout).", returncode, len(stdout))
        return stdout, "", returncode
    except subprocess.TimeoutExpired:
        if process is not None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        # Let the reader thread flush buffered output before we read it.
        reader.join(timeout=5)
        stdout = "".join(stdout_parts)
        logger.error("Claude timed out after %ds.", timeout)
        return stdout, f"timeout after {timeout}s", 1
    except FileNotFoundError:
        logger.error("claude CLI not found on PATH.")
        return "", "claude not found", 127


def _build_claude_env(env_allowlist: tuple[str, ...] | None = None) -> dict[str, str]:
    """Return the minimal environment Claude Code needs for Bedrock.

    GitHub tokens and other workflow secrets are intentionally not inherited.
    Tool-using prompts may contain untrusted PR or artifact content, so the
    subprocess gets only process/runtime basics plus AWS credentials required
    by the Bedrock provider.
    """
    allowed = set(env_allowlist or DEFAULT_CLAUDE_ENV_ALLOWLIST)
    env = {
        name: value
        for name, value in os.environ.items()
        if name in allowed and value
    }
    env["CLAUDE_CODE_USE_BEDROCK"] = "1"
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = _resolve_bedrock_opus_model()
    return env


def _resolve_claude_model(model: str | None) -> str | None:
    """Resolve the Claude Code model alias, honoring operator override."""
    override = os.environ.get(_CLAUDE_MODEL_ENV, "").strip()
    if override:
        return override
    return model or _DEFAULT_CLAUDE_MODEL


def _resolve_bedrock_opus_model() -> str:
    """Resolve the Bedrock Opus model/inference profile used by Claude Code."""
    return os.environ.get(_BEDROCK_OPUS_MODEL_ENV, "").strip() or _DEFAULT_BEDROCK_OPUS_MODEL


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
