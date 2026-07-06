"""Credential-boundary regression tests for the shared git wrappers.

The verification command runs untrusted PR code in the worktree before the
pipeline calls ``git_output`` (``reset --hard``, ``clean``, ``diff``) to undo
its changes. That code can plant repo-local git config (a diff/filter driver,
``core.sshCommand``) which git would later execute. These tests prove the
parent's credentials are never in scope when that happens.
"""

import subprocess

from scripts.common import proc
from scripts.common.proc import git_output, run_git


def _captured_env(monkeypatch):
    seen = {}
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        seen.setdefault("env", kwargs.get("env"))
        # Run for real so return-code/output behavior is unchanged.
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(proc.subprocess, "run", fake_run)
    return seen


def test_git_output_runs_with_scrubbed_env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(str(repo), "init", "-q")
    (repo / "f.txt").write_text("hi\n")
    run_git(str(repo), "add", "f.txt")
    run_git(str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "init")

    # Untrusted code mutates repo-local config (would run on next git op).
    run_git(str(repo), "config", "core.sshCommand", "leak $TARGET_TOKEN")

    # A secret is present in the parent environment.
    monkeypatch.setenv("TARGET_TOKEN", "super-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")

    seen = _captured_env(monkeypatch)
    git_output(str(repo), "reset", "--hard", "HEAD")

    env = seen["env"]
    assert env is not None, "git_output must pass an explicit scrubbed env"
    assert "TARGET_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_git_output_diff_still_works_scrubbed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(str(repo), "init", "-q")
    (repo / "f.txt").write_text("a\n")
    run_git(str(repo), "add", "f.txt")
    run_git(str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "init")
    (repo / "f.txt").write_text("b\n")

    monkeypatch.setenv("TARGET_TOKEN", "super-secret")
    out = git_output(str(repo), "diff", "--name-only")
    assert "f.txt" in out


def test_build_approved_patch_treats_paths_literally(tmp_path):
    """A file named with pathspec magic must not broaden the approved patch.

    Without ``--literal-pathspecs``, ``git diff HEAD -- ':(glob)*'`` would
    expand to all tracked changes, leaking an unapproved edit into the patch
    that gets reviewed and pushed.
    """
    from scripts.common.proc import build_approved_patch

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(str(repo), "init", "-q")
    (repo / "approved.txt").write_text("v1\n")
    (repo / "secret.txt").write_text("orig\n")
    run_git(str(repo), "add", "approved.txt", "secret.txt")
    run_git(str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "init")

    # Two tracked edits: only approved.txt is in the approved set.
    (repo / "approved.txt").write_text("v2\n")
    (repo / "secret.txt").write_text("leaked\n")
    # An untracked file whose name is pathspec magic.
    (repo / ":(glob)*").write_text("x\n")

    patch = build_approved_patch(str(repo), (":(glob)*", "approved.txt"))
    assert "approved.txt" in patch
    # The unrelated tracked edit must not be pulled in by the magic pathspec.
    assert "secret.txt" not in patch
    assert "leaked" not in patch


def test_reset_worktree_removes_ignored_build_artifacts(tmp_path):
    from scripts.ci_fix.review import reset_worktree

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(str(repo), "init", "-q")
    (repo / ".gitignore").write_text("*.o\nbuild/\n")
    (repo / "f.c").write_text("int main(){return 0;}\n")
    run_git(str(repo), "add", ".gitignore", "f.c")
    run_git(
        str(repo),
        "-c", "user.email=t@t",
        "-c", "user.name=t",
        "commit", "-qm", "init",
    )

    (repo / "f.o").write_text("object")
    (repo / "build").mkdir()
    (repo / "build" / "server").write_text("binary")
    (repo / "scratch.txt").write_text("untracked")

    reset_worktree(str(repo))

    assert not (repo / "f.o").exists()
    assert not (repo / "build").exists()
    assert not (repo / "scratch.txt").exists()
    assert (repo / "f.c").exists()
