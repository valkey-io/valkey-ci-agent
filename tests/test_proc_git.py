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


def test_git_failure_message_carries_stderr_and_subcommand(tmp_path):
    import pytest

    with pytest.raises(subprocess.CalledProcessError) as raised:
        git_output(str(tmp_path), "rev-parse", "--verify", "no-such-ref")
    message = str(raised.value)
    # The locked-config prefix is dropped so the subcommand is readable...
    assert raised.value.cmd[:2] == ["git", "rev-parse"]
    # ...and git's own reason survives into the message an operator sees.
    assert message.startswith("git rev-parse exited ")
    assert "no-such-ref" in message or "not a git repository" in message


def test_git_subcommand_skips_config_flags():
    from scripts.common.proc import git_subcommand

    assert git_subcommand(["git", "-c", "core.hooksPath=/dev/null", "--literal-pathspecs",
                           "push", "--force-with-lease", "origin"]) == "push"
    assert git_subcommand("git clone --branch 9.1 url dir") == "clone"


def test_is_ancestor_separates_no_from_unknown(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    git("commit", "-q", "--allow-empty", "-m", "first")
    first = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", "second")
    second = git("rev-parse", "HEAD")

    assert proc.is_ancestor(str(tmp_path), first, second) is True
    assert proc.is_ancestor(str(tmp_path), second, first) is False
    # A missing object is a broken question, not a negative answer.
    assert proc.is_ancestor(str(tmp_path), "0" * 40, second) is None


# --- repo- and user-controlled git config must never execute -------------------------

import os  # noqa: E402

from scripts.common.proc import build_approved_patch, worktree_changed_paths  # noqa: E402


def _repo_with_change(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    (repo / "f.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    (repo / "f.txt").write_text("changed\n")
    return repo


def _marker_script(tmp_path, name):
    marker = tmp_path / f"{name}.ran"
    script = tmp_path / f"{name}.sh"
    script.write_text(f"#!/bin/sh\necho ran > {marker}\ncat \"$1\" 2>/dev/null\n")
    script.chmod(0o755)
    return script, marker


def test_a_planted_fsmonitor_never_runs(tmp_path):
    repo = _repo_with_change(tmp_path)
    script, marker = _marker_script(tmp_path, "fsmonitor")
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", str(script)], check=True)

    changed = worktree_changed_paths(str(repo), strict=True)
    build_approved_patch(str(repo), changed, strict=True)

    assert changed == ("f.txt",)
    assert not marker.exists()


def test_a_planted_textconv_driver_never_shapes_the_patch(tmp_path):
    repo = _repo_with_change(tmp_path)
    script, marker = _marker_script(tmp_path, "textconv")
    (repo / ".gitattributes").write_text("*.txt diff=evil\n")
    subprocess.run(["git", "-C", str(repo), "config", "diff.evil.textconv", str(script)], check=True)

    patch = build_approved_patch(str(repo), ("f.txt",), strict=True)

    assert "+changed" in patch
    assert not marker.exists()


def test_user_global_config_is_ignored(tmp_path, monkeypatch):
    """~/.gitconfig is writable by the same user that runs untrusted tests.

    A global clean filter runs on every diff of a modified file, and nothing on
    the git command line turns it off: only ignoring the global config does.
    """
    repo = _repo_with_change(tmp_path)
    script, marker = _marker_script(tmp_path, "filter")
    attributes = tmp_path / "attributes"
    attributes.write_text("* filter=evil\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        f"[core]\n\tattributesFile = {attributes}\n[filter \"evil\"]\n\tclean = {script}\n"
    )
    monkeypatch.setenv("HOME", str(home))

    assert worktree_changed_paths(str(repo), strict=True) == ("f.txt",)
    build_approved_patch(str(repo), ("f.txt",), strict=True)
    assert not marker.exists()


def test_a_non_utf8_path_survives_discovery_patching_and_the_staged_check(tmp_path):
    """A path may be any bytes; it must come back from git exactly as written."""
    import os
    import subprocess

    import pytest

    from scripts.ci_fix.push import _apply_patch, _staged_paths
    from scripts.common.proc import build_approved_patch, worktree_changed_paths

    def git(cwd, *args):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                       capture_output=True)

    work, clean = tmp_path / "work", tmp_path / "clean"
    for repo in (work, clean):
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "seed").write_text("x\n")
        git(repo, "add", "seed")
        git(repo, "commit", "-qm", "seed")
    raw = os.fsencode(str(work)) + b"/caf\xe9.tcl"
    try:
        with open(raw, "wb") as handle:
            handle.write(b"set x 1\n")
    except OSError:
        pytest.skip("the local filesystem rejects non-UTF-8 path bytes")
    changed = worktree_changed_paths(str(work), strict=True)
    assert [os.fsencode(path) for path in changed] == [b"caf\xe9.tcl"]
    patch = build_approved_patch(str(work), changed, strict=True)
    _apply_patch(str(clean), patch)
    assert _staged_paths(str(clean)) == changed
