"""Shared build/test command runner for backport validation."""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)


def run_build_commands(repo_dir: str, commands: list[str]) -> tuple[bool, str]:
    """Run validation commands sequentially.

    Returns (success, output). If commands is empty, returns (True, "") —
    build validation is skipped when no commands are configured.
    """
    if not commands:
        return True, ""
    for command in commands:
        logger.info("Running backport validation command: %s", command)
        result = subprocess.run(
            command,
            cwd=repo_dir,
            shell=True,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if result.returncode != 0:
            output = "\n".join(
                part for part in [result.stdout[-2000:], result.stderr[-2000:]]
                if part
            ).strip()
            return False, output or f"`{command}` failed with exit code {result.returncode}"
    return True, ""
