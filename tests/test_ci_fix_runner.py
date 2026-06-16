"""Tests for the sanitized verification command-runner.

These guard the trust anchor: a passing verdict comes only from a real
subprocess exit code, the environment is scrubbed, and the working directory
cannot escape the repo clone.
"""

from __future__ import annotations

import os

from scripts.ci_fix.runner import run_verification_command


def test_passed_derived_from_exit_zero(tmp_path):
    result = run_verification_command(str(tmp_path), "true")
    assert result.ran is True
    assert result.passed is True
    assert result.exit_code == 0


def test_failed_derived_from_nonzero_exit(tmp_path):
    result = run_verification_command(str(tmp_path), "exit 7")
    assert result.ran is True
    assert result.passed is False
    assert result.exit_code == 7


def test_empty_command_does_not_run(tmp_path):
    result = run_verification_command(str(tmp_path), "   ")
    assert result.ran is False
    assert result.passed is False


def test_missing_repo_dir_does_not_run(tmp_path):
    missing = tmp_path / "nope"
    result = run_verification_command(str(missing), "true")
    assert result.ran is False
    assert result.passed is False


def test_workdir_escape_is_rejected(tmp_path):
    result = run_verification_command(str(tmp_path), "true", workdir="../..")
    assert result.ran is False
    assert "escapes" in result.output_tail


def test_workdir_inside_repo_is_allowed(tmp_path):
    (tmp_path / "src").mkdir()
    result = run_verification_command(str(tmp_path), "true", workdir="src")
    assert result.ran is True
    assert result.passed is True


def test_timeout_marks_not_passed(tmp_path):
    result = run_verification_command(str(tmp_path), "sleep 5", timeout=1)
    assert result.ran is True
    assert result.passed is False
    assert result.timed_out is True


def test_environment_is_scrubbed(tmp_path, monkeypatch):
    """Tokens of any name in the parent env must not reach the command."""
    monkeypatch.setenv("GITHUB_TOKEN", "secret1")
    monkeypatch.setenv("GH_TOKEN", "secret2")
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "secret3")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    out = tmp_path / "leak.txt"
    result = run_verification_command(
        str(tmp_path),
        f'echo "[${{GITHUB_TOKEN}}][${{GH_TOKEN}}][${{ACTIONS_RUNTIME_TOKEN}}]" > {out}',
    )
    assert result.passed is True
    assert out.read_text().strip() == "[][][]"


def test_output_tail_truncates_large_output(tmp_path):
    result = run_verification_command(str(tmp_path), "yes x | head -c 100000")
    assert result.passed is True
    assert "[truncated]" in result.output_tail
    assert len(result.output_tail) < 100000


def test_aws_credentials_never_reach_command(tmp_path, monkeypatch):
    """The verification command must not see AWS/Bedrock credentials (P0)."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "shhh")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "tok")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    out = tmp_path / "aws.txt"
    result = run_verification_command(
        str(tmp_path),
        f'echo "[${{AWS_ACCESS_KEY_ID}}][${{AWS_SECRET_ACCESS_KEY}}][${{AWS_SESSION_TOKEN}}]" > {out}',
    )
    assert result.passed is True
    assert out.read_text().strip() == "[][][]"


def test_output_tail_is_captured(tmp_path):
    result = run_verification_command(str(tmp_path), "echo hello-from-test")
    assert "hello-from-test" in result.output_tail


def test_chained_command_runs_via_shell(tmp_path):
    result = run_verification_command(str(tmp_path), "true && echo ok && exit 0")
    assert result.passed is True
    assert "ok" in result.output_tail


def test_docker_image_wraps_command(tmp_path, monkeypatch):
    """When a container image is given, the command runs via docker run."""
    from scripts.ci_fix import runner as runner_mod

    captured = {}

    def fake_run_capped(command, cwd, env, timeout):
        captured["command"] = command
        return True, 0, "ok", False

    monkeypatch.setattr(runner_mod, "_run_capped", fake_run_capped)
    result = run_verification_command(
        str(tmp_path), "make test", container_image="almalinux:8",
    )
    assert result.passed is True
    assert captured["command"].startswith("docker run --rm")
    assert "almalinux:8" in captured["command"]
    assert "make test" in captured["command"]
    # The user-facing command stays the plain one, not the docker wrapper.
    assert result.command == "make test"
