from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from scripts.ci_fix import sandbox


def test_setup_runs_in_the_sandbox_without_workflow_credentials(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    setup = tmp_path / "setup.sh"
    setup.write_text("true\n", encoding="utf-8")
    seen = {}

    @contextmanager
    def handed_to(user, root):
        seen["handoff"] = (user, root)
        yield str(tmp_path / "home")

    def as_user(user, env, argv, *, home, cwd):
        seen["invocation"] = (user, env, argv, home, cwd)
        return SimpleNamespace(argv=("isolated",), env={"SAFE": "1"})

    def run(argv, *, cwd, env, check):
        seen["run"] = (argv, cwd, env, check)
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("TARGET_TOKEN", "secret")
    monkeypatch.setattr(sandbox, "handed_to", handed_to)
    monkeypatch.setattr(sandbox, "as_user", as_user)
    monkeypatch.setattr(sandbox.subprocess, "run", run)

    assert sandbox.run_setup(repo, setup, "cifix123") == 0
    user, env, argv, home, cwd = seen["invocation"]
    assert user == "cifix123"
    assert argv == ["sh", str(setup)]
    assert cwd == repo
    assert env["GITHUB_WORKSPACE"] == str(repo)
    assert env["RUNNER_TEMP"] == home
    assert "TARGET_TOKEN" not in env
    assert seen["run"] == (["isolated"], None, {"SAFE": "1"}, False)
