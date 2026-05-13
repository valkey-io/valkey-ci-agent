"""Central capability profiles for tool-using AI subprocesses."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from scripts.ai.claude_code import (
    DEFAULT_CLAUDE_ENV_ALLOWLIST,
    _resolve_claude_model,
    run_claude_code,
)

AgentProfileName = Literal[
    "conflict_resolve_edit_only",
]


@dataclass(frozen=True)
class AgentProfile:
    """Execution contract for one kind of AI task."""

    name: AgentProfileName
    allowed_tools: str
    timeout: int
    effort: str | None = "high"
    max_turns: int = 200
    writes_allowed: bool = False
    output_schema: str = "text"
    failure_policy: str = "fail-closed"
    env_allowlist: tuple[str, ...] = DEFAULT_CLAUDE_ENV_ALLOWLIST


@dataclass(frozen=True)
class AgentRunResult:
    """Result and audit metadata for one AI subprocess call."""

    profile: AgentProfileName
    stdout: str
    stderr: str
    returncode: int
    prompt_sha256: str
    cwd: str
    allowed_tools: str
    model: str
    started_at: str
    finished_at: str


AGENT_PROFILES: dict[AgentProfileName, AgentProfile] = {
    "conflict_resolve_edit_only": AgentProfile(
        name="conflict_resolve_edit_only",
        allowed_tools="Read,Edit,MultiEdit,Grep,Glob,Bash",
        timeout=3600,
        effort="max",
        max_turns=240,
        writes_allowed=True,
        output_schema="edited-files",
    ),
}


def get_agent_profile(name: AgentProfileName) -> AgentProfile:
    """Return the immutable profile for ``name``."""
    return AGENT_PROFILES[name]


def run_agent(
    profile_name: AgentProfileName,
    prompt: str,
    *,
    cwd: str | None = None,
    timeout: int | None = None,
    model: str | None = None,
    evidence_dir: str | Path | None = None,
) -> AgentRunResult:
    """Run Claude Code under a named capability profile.

    The profile controls tool permissions, timeout, effort, and audit labels.
    Optional evidence is written after the process exits so generated files in
    the working tree cannot influence the prompt that just ran.
    """
    profile = get_agent_profile(profile_name)
    started_at = datetime.now(timezone.utc).isoformat()
    resolved_model = _resolve_claude_model(model)
    stdout, stderr, rc = run_claude_code(
        prompt,
        cwd=cwd,
        timeout=timeout if timeout is not None else profile.timeout,
        model=resolved_model,
        effort=profile.effort,
        max_turns=profile.max_turns,
        allowed_tools=profile.allowed_tools,
        env_allowlist=profile.env_allowlist,
    )
    finished_at = datetime.now(timezone.utc).isoformat()
    result = AgentRunResult(
        profile=profile_name,
        stdout=stdout,
        stderr=stderr,
        returncode=rc,
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        cwd=str(cwd or ""),
        allowed_tools=profile.allowed_tools,
        model=resolved_model or "",
        started_at=started_at,
        finished_at=finished_at,
    )
    _write_evidence(result, profile, evidence_dir)
    return result


def _write_evidence(
    result: AgentRunResult,
    profile: AgentProfile,
    evidence_dir: str | Path | None,
) -> None:
    configured_dir = evidence_dir or os.environ.get("CI_AGENT_EVIDENCE_DIR", "")
    if not configured_dir and os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        configured_dir = "agent-evidence"
    if not configured_dir:
        return
    target_dir = Path(configured_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        evidence = {
            "profile": asdict(profile),
            "result": {
                key: value
                for key, value in asdict(result).items()
                if key not in {"stdout", "stderr"}
            },
            "stdout_sha256": hashlib.sha256(
                result.stdout.encode("utf-8")
            ).hexdigest(),
            "stderr_sha256": hashlib.sha256(
                result.stderr.encode("utf-8")
            ).hexdigest(),
        }
        path = target_dir / (
            f"{result.started_at.replace(':', '').replace('+', 'Z')}-"
            f"{result.profile}-{result.prompt_sha256[:12]}.json"
        )
        path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        return
