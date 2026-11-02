from __future__ import annotations

import os

from scripts.ci_fix import sandbox


def test_setup_runs_in_the_checkout_without_workflow_credentials(tmp_path, monkeypatch):
    """The job's own setup needs root (sudo apt-get, sudo make install), so it runs as the workflow user."""
    repo = tmp_path / "repo"
    repo.mkdir()
    setup = tmp_path / "setup.sh"
    report = tmp_path / "report"
    setup.write_text(
        f'printf "%s\\n" "$(pwd -P)" "$GITHUB_WORKSPACE" "$RUNNER_TEMP" '
        f'"${{TARGET_TOKEN:-none}}" "$(id -u)" > {report}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("TARGET_TOKEN", "secret")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "agent-temp"))

    assert sandbox.run_setup(repo, setup) == 0

    cwd, workspace, runner_temp, token, uid = report.read_text(encoding="utf-8").splitlines()
    assert cwd == workspace == str(repo.resolve())
    assert runner_temp.startswith(str(tmp_path.resolve() / "ci-fix-setup-"))
    assert token == "none"
    assert uid == str(os.getuid())


def test_a_failed_setup_returns_its_status(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    setup = tmp_path / "setup.sh"
    setup.write_text("exit 7\n", encoding="utf-8")

    assert sandbox.run_setup(repo, setup) == 7
