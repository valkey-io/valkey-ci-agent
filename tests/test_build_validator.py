"""Tests for shared build validation."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import patch

from scripts.common.build_validator import run_build_commands


def test_run_build_commands_scrubs_credentials_and_actions_control_files(
    tmp_path, monkeypatch,
) -> None:
    secrets = {
        "AWS_ACCESS_KEY_ID": "AKIA-secret",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "AWS_SESSION_TOKEN": "session",
        "TARGET_TOKEN": "github-token",
        "GITHUB_ENV": "/tmp/github-env",
        "GITHUB_PATH": "/tmp/github-path",
        "GITHUB_OUTPUT": "/tmp/github-output",
        "ACTIONS_RUNTIME_TOKEN": "runtime-token",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    completed = subprocess.CompletedProcess(
        args="make", returncode=0, stdout="ok", stderr="",
    )
    with patch(
        "scripts.common.build_validator.subprocess.run",
        return_value=completed,
    ) as run:
        ok, _ = run_build_commands(str(tmp_path), ["make"])

    assert ok is True
    child_env = run.call_args.kwargs["env"]
    assert all(name not in child_env for name in secrets)
    if os.environ.get("PATH"):
        assert child_env["PATH"] == os.environ["PATH"]


def test_run_build_commands_returns_failure_on_timeout(tmp_path) -> None:
    with patch(
        "scripts.common.build_validator.subprocess.run",
        side_effect=subprocess.TimeoutExpired(
            cmd="make",
            timeout=1800,
            output="partial stdout",
            stderr="partial stderr",
        ),
    ):
        ok, output = run_build_commands(str(tmp_path), ["make"])

    assert ok is False
    assert "partial stdout" in output
    assert "partial stderr" in output


def test_run_build_commands_gives_req_res_validator_90_minutes(tmp_path) -> None:
    completed = subprocess.CompletedProcess(
        args="command", returncode=0, stdout="", stderr="",
    )
    with patch(
        "scripts.common.build_validator.subprocess.run",
        return_value=completed,
    ) as run:
        ok, _ = run_build_commands(
            str(tmp_path),
            [
                "make",
                "./utils/req-res-log-validator.py "
                "--verbose --fail-missing-reply-schemas",
            ],
        )

    assert ok is True
    assert [call.kwargs["timeout"] for call in run.call_args_list] == [1800, 5400]


def test_run_build_commands_skips_empty_command_list(tmp_path) -> None:
    ok, output = run_build_commands(str(tmp_path), [])

    assert ok is True
    assert output == ""


def test_run_build_commands_writes_full_log_on_failure(tmp_path) -> None:
    """When log_path is set, the full untruncated stdout/stderr must be persisted."""
    long_stdout = "warning line\n" * 5000  # ~60kB, well past the 2000-char tail
    real_error = "src/foo.c:42: error: undefined reference to 'old_helper'"
    full_stdout = real_error + "\n" + long_stdout

    completed = subprocess.CompletedProcess(
        args="make",
        returncode=1,
        stdout=full_stdout,
        stderr="link failed",
    )
    log_path = tmp_path / "validation.log"
    with patch(
        "scripts.common.build_validator.subprocess.run",
        return_value=completed,
    ):
        ok, summary = run_build_commands(
            str(tmp_path), ["make"], log_path=str(log_path),
        )

    assert ok is False
    # Summary stays short (existing tail behavior)
    assert "warning line" in summary
    # Full log file must contain the actual error that the tail dropped
    log_text = log_path.read_text()
    assert real_error in log_text
    assert "$ make" in log_text
    assert "# exit code: 1" in log_text
    assert "link failed" in log_text


def test_run_build_commands_overwrites_existing_log(tmp_path) -> None:
    """Each call should refresh the log file so stale runs aren't presented as current."""
    log_path = tmp_path / "validation.log"
    log_path.write_text("stale content from previous run")

    completed = subprocess.CompletedProcess(
        args="make", returncode=0, stdout="ok", stderr="",
    )
    with patch(
        "scripts.common.build_validator.subprocess.run",
        return_value=completed,
    ):
        run_build_commands(str(tmp_path), ["make"], log_path=str(log_path))

    text = log_path.read_text()
    assert "stale content" not in text
    assert "$ make" in text


def test_run_build_commands_logs_each_outcome_and_skipped_commands(tmp_path, caplog) -> None:
    import logging

    with caplog.at_level(logging.INFO, logger="scripts.common.build_validator"):
        ok, _ = run_build_commands(str(tmp_path), ["true", "exit 4", "echo never"])

    assert ok is False
    messages = [r.getMessage() for r in caplog.records]
    assert any("1/3 passed" in m for m in messages)
    assert any(
        "2/3 failed with exit code 4" in m and "1 remaining command(s) not run" in m
        for m in messages
    )
    assert not any("echo never" in m for m in messages)


def test_run_build_commands_says_when_there_is_nothing_to_run(tmp_path, caplog) -> None:
    import logging

    # Routine (Valkey has no setup commands), so INFO, not a warning.
    with caplog.at_level(logging.INFO, logger="scripts.common.build_validator"):
        assert run_build_commands(str(tmp_path), []) == (True, "")
    record = next(r for r in caplog.records if "nothing to run" in r.getMessage())
    assert record.levelname == "INFO"
