"""Untrusted commands run as a separate account that cannot reach the job's secrets.

The tests that need a real account run where CI_FIX_TEST_AGENT_USER names one
this process can switch to, as root or through sudo (CI creates it); elsewhere
they skip.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ai import claude_code
from scripts.ci_fix import engine
from scripts.common import isolation

_USER = os.environ.get("CI_FIX_TEST_AGENT_USER", "")
needs_user = pytest.mark.skipif(
    not _USER or (os.geteuid() != 0 and (
        shutil.which("sudo") is None
        or subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0)),
    reason="CI_FIX_TEST_AGENT_USER names no account this test can switch to",
)


def test_inside_actions_an_account_is_required(monkeypatch):
    monkeypatch.delenv(isolation.USER_ENV, raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(RuntimeError, match="refusing to run untrusted commands as the workflow user"):
        isolation.isolated_user()
    monkeypatch.delenv("GITHUB_ACTIONS")
    assert isolation.isolated_user() == ""


def test_the_command_runs_with_its_own_home_and_git_allowed(monkeypatch):
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    monkeypatch.setattr(isolation.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(isolation.shutil, "which", lambda name: name)
    invocation = isolation.as_user(
        "v",
        {"PATH": "/usr/bin", "HOME": "/home/runner", "AWS_SESSION_TOKEN": "secret"},
        ["bash", "-c", "make"],
        home="/w/ci-fix-home-1",
    )
    assert invocation.argv[:9] == (
        "sudo",
        "-n",
        (
            "--preserve-env=AWS_SESSION_TOKEN,GIT_CONFIG_COUNT,GIT_CONFIG_KEY_0,"
            "GIT_CONFIG_VALUE_0,HOME,LOGNAME,PATH,TMP,TMPDIR,USER"
        ),
        "runuser",
        "--preserve-environment",
        "-u",
        "v",
        "--",
        "setpriv",
    )
    assert invocation.argv[-5:] == ("env", "TMPDIR=/w/ci-fix-home-1", "bash", "-c", "make")
    assert invocation.env["HOME"] == "/w/ci-fix-home-1"
    assert invocation.env["TMPDIR"] == "/w/ci-fix-home-1"
    assert invocation.env["GIT_CONFIG_VALUE_0"] == "*"
    assert invocation.env["AWS_SESSION_TOKEN"] == "secret"
    assert "HOME=/home/runner" not in invocation.argv
    assert "secret" not in invocation.argv


def test_root_switches_account_without_sudo_and_macos_through_sudo(monkeypatch):
    """A job's container runs as root and has no sudo; macOS has no runuser."""
    monkeypatch.setattr(isolation.os, "geteuid", lambda: 0)
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    monkeypatch.setattr(isolation.shutil, "which", lambda name: f"/usr/sbin/{name}")
    linux = isolation.as_user("v", {}, ["make"], home="/h")
    assert linux.argv[:6] == (
        "/usr/sbin/runuser", "--preserve-environment", "-u", "v", "--", "/usr/sbin/setpriv",
    )
    monkeypatch.setattr(isolation.os, "geteuid", lambda: 501)
    monkeypatch.setattr(isolation.sys, "platform", "darwin")
    macos = isolation.as_user("v", {}, ["make"], home="/h")
    assert macos.argv[:6] == (
        "/usr/sbin/sudo",
        "-n",
        (
            "--preserve-env=GIT_CONFIG_COUNT,GIT_CONFIG_KEY_0,GIT_CONFIG_VALUE_0,"
            "HOME,LOGNAME,TMP,TMPDIR,USER"
        ),
        "-u",
        "v",
        "--",
    )
    assert macos.argv[6] == "/usr/sbin/env"


def test_the_command_is_resolved_before_sudo_can_replace_path(monkeypatch):
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    monkeypatch.setattr(isolation.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(isolation.shutil, "which", lambda name: f"/chosen/{name}")

    invocation = isolation.as_user("v", {}, ["claude", "--print"], home="/h")

    assert invocation.argv[-2:] == ("/chosen/claude", "--print")


def test_the_command_enters_its_checkout_after_switching_accounts(monkeypatch):
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    monkeypatch.setattr(isolation.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(isolation.shutil, "which", lambda name: f"/chosen/{name}")

    invocation = isolation.as_user(
        "v",
        {},
        ["make", "test"],
        home="/h",
        cwd="/tmp/checkout with spaces",
    )

    assert invocation.argv[-9:] == (
        "/chosen/env",
        "TMPDIR=/h",
        "/chosen/sh",
        "-c",
        'cd "$1" && shift && exec "$@"',
        "ci-fix-cwd",
        "/tmp/checkout with spaces",
        "/chosen/make",
        "test",
    )


@needs_user
def test_the_scrubbed_environment_reaches_the_isolated_account(checkout):
    checkout.chmod(0o700)
    with isolation.handed_to(_USER, checkout) as home:
        invocation = isolation.as_user(
            _USER,
            {
                "CI_FIX_ENV_PROBE": "preserved",
                "GITHUB_WORKSPACE": str(checkout),
                "RUNNER_TEMP": home,
            },
            [
                "sh",
                "-c",
                'test "$CI_FIX_ENV_PROBE" = preserved'
                ' && test "$HOME" = "$1"'
                ' && test "$TMPDIR" = "$1"'
                ' && test "$USER" = "$2"'
                ' && test "$GITHUB_WORKSPACE" = "$3"'
                ' && test "$RUNNER_TEMP" = "$1"',
                "ci-fix-env",
                home,
                _USER,
                str(checkout),
            ],
            home=home,
            cwd=checkout,
        )
        result = subprocess.run(
            list(invocation.argv),
            cwd=None,
            env=invocation.env,
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 0, result.stderr


def test_file_cleanup_drops_roots_already_covered_by_an_ancestor(monkeypatch):
    calls = []
    monkeypatch.setattr(isolation, "_WRITABLE_ROOTS", ("/tmp", "/var/tmp"))
    monkeypatch.setattr(
        isolation.pwd,
        "getpwnam",
        lambda _user: SimpleNamespace(pw_dir="/tmp/valkey-ci-fix.123/home"),
    )
    monkeypatch.setattr(isolation.Path, "is_dir", lambda _path: True)
    monkeypatch.setattr(isolation, "_as_root", lambda *argv: calls.append(argv))

    isolation._remove_files_of("agent")

    assert calls == [("find", "/tmp", "/var/tmp", "-xdev", "-mindepth", "1", "-user", "agent", "-delete")]


def test_hard_link_cleanup_retries_when_cancelled_build_files_disappear(monkeypatch, tmp_path):
    calls = []
    results = iter(
        [
            OSError(
                f"find: '{tmp_path}/src/rax.c': No such file or directory\n"
                f"find: '{tmp_path}/src/ziplist.o': No such file or directory\n"
            ),
            None,
        ]
    )

    def as_root(*argv):
        calls.append(argv)
        result = next(results)
        if result is not None:
            raise result

    monkeypatch.setattr(
        isolation.subprocess,
        "run", lambda *_args, **_kwargs: pytest.fail("subprocess is wrapped by _as_root")
    )
    monkeypatch.setattr(isolation, "_as_root", as_root)
    monkeypatch.setattr(isolation.time, "sleep", lambda _seconds: None)

    isolation._remove_hard_links(tmp_path)

    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0][0] == "find"


def test_hard_link_cleanup_does_not_retry_other_find_errors(monkeypatch, tmp_path):
    calls = []

    def as_root(*argv):
        calls.append(argv)
        raise OSError("find: Permission denied")

    monkeypatch.setattr(isolation, "_as_root", as_root)

    with pytest.raises(OSError, match="Permission denied"):
        isolation._remove_hard_links(tmp_path)

    assert len(calls) == 1


@pytest.fixture
def checkout():
    """A checkout under a traversable directory, as the engine makes one."""
    work = Path(tempfile.mkdtemp(prefix="ci-fix-test-", dir="/tmp"))
    work.chmod(0o711)
    repo = work / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "file").write_text("x\n")
    yield repo
    subprocess.run([*isolation._root(), "rm", "-rf", str(work)], check=False)


@pytest.fixture
def credentialed():
    """A process of the workflow user whose environment holds a secret, as the job's does.

    ``/proc/<pid>/environ`` shows the environment a process started with, so
    a secret set in this process after it started would never appear there,
    and a broken isolation would go unnoticed.
    """
    holder = subprocess.Popen(["sleep", "60"], env={**os.environ, "TARGET_TOKEN": "ghs_secret"})
    environ = f"/proc/{holder.pid}/environ"
    assert b"ghs_secret" in Path(environ).read_bytes()  # the workflow user can read it
    yield environ
    holder.kill()
    holder.wait()


@needs_user
def test_a_test_command_cannot_read_the_parent_environment_or_change_git(checkout, monkeypatch, credentialed):
    monkeypatch.setenv(isolation.USER_ENV, _USER)
    checkout.chmod(0o700)
    head = (checkout / ".git" / "HEAD").read_text()
    run = engine.run_test_command(
        str(checkout),
        f"n=$(tr '\\0' '\\n' < {credentialed} 2>/dev/null | grep -c ghs_secret); echo \"seen=${{n:-0}}\"; "
        "echo \"home=$HOME tmp=$TMPDIR git=$GIT_CONFIG_VALUE_0\"; echo evil > .git/HEAD; echo left > /tmp/ci-fix-isolation-marker; echo ok > made; id -un",
    )
    assert "seen=0" in run.output and _USER in run.output
    # What the command is given does arrive: a sudo that drops the
    # environment (sudo-rs with -E) would pass the check above too.
    given = next(line for line in run.output.splitlines() if line.startswith("home="))
    home, tmp, git = (field.split("=", 1)[1] for field in given.split())
    assert "/ci-fix-home-" in home and tmp == home and git == "*"
    assert (checkout / ".git" / "HEAD").read_text() == head
    assert not Path("/tmp/ci-fix-isolation-marker").exists()
    assert (checkout / "made").stat().st_uid == os.getuid()  # handed back


@needs_user
def test_nothing_the_command_started_outlives_it(checkout, monkeypatch):
    monkeypatch.setenv(isolation.USER_ENV, _USER)
    marker = checkout / "late"
    engine.run_test_command(str(checkout), f"(setsid sleep 2; touch {marker}) >/dev/null 2>&1 &")
    time.sleep(3)
    assert not marker.exists()


@needs_user
def test_the_claude_cli_runs_as_the_account(checkout, monkeypatch, tmp_path, credentialed):
    """The real CLI path, with a fake ``claude`` that reports who it is and what it can read."""
    checkout.chmod(0o700)
    bin_dir = Path(tempfile.mkdtemp(prefix="ci-fix-bin-", dir="/tmp"))
    bin_dir.chmod(0o755)
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/bin/bash\ncat >/dev/null\n"
        f"seen=$(tr '\\0' '\\n' < {credentialed} 2>/dev/null | grep -c ghs_secret)\n"
        'echo "{\\"type\\":\\"result\\",\\"result\\":\\"user=$(id -un) seen=${seen:-0} '
        'bedrock=${CLAUDE_CODE_USE_BEDROCK:-} aws=${AWS_SESSION_TOKEN:-}\\"}"\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    # The CLI's own Bedrock credentials must arrive; the job's other secrets must not.
    monkeypatch.setenv("AWS_SESSION_TOKEN", "bedrock-token")
    stdout, _stderr, rc = claude_code.run_claude_code("p", cwd=str(checkout), run_as=_USER, timeout=60)
    shutil.rmtree(bin_dir, ignore_errors=True)
    assert rc == 0
    assert f"user={_USER} seen=0 bedrock=1 aws=bedrock-token" in stdout


def test_a_replaced_git_directory_is_never_handed_back(monkeypatch):
    """The account owns the checkout's directory, so it can swap .git for one of its own."""
    monkeypatch.setattr(isolation, "_as_root", lambda *argv: None)
    me = isolation.pwd.getpwuid(os.getuid())
    monkeypatch.setattr(isolation.pwd, "getpwnam", lambda user: me)
    monkeypatch.setattr(isolation, "kill_all", lambda user: None)
    monkeypatch.setattr(isolation, "_remove_files_of", lambda user: None)
    work = Path(tempfile.mkdtemp(prefix="ci-fix-test-", dir="/tmp"))
    work.chmod(0o711)
    repo = work / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(isolation.tempfile, "mkdtemp", lambda **_kw: str(work / "home"))

    with isolation.handed_to("agent", repo):
        pass  # an untouched .git is handed back

    with pytest.raises(OSError, match="replaced"):
        with isolation.handed_to("agent", repo):
            (repo / ".git").rename(repo / ".git.old")
            shutil.copytree(repo / ".git.old", repo / ".git")
    shutil.rmtree(work)


def test_a_replaced_git_directory_and_every_nested_one_are_removed(monkeypatch):
    """The job's cleanup runs git in the workspace as the workflow user: no config the account wrote may be there."""
    def as_root(*argv):
        if argv[0] in ("find", "rm"):  # the removals for real; the test's own files need no chown
            subprocess.run(argv, check=True)

    monkeypatch.setattr(isolation, "_as_root", as_root)
    me = isolation.pwd.getpwuid(os.getuid())
    monkeypatch.setattr(isolation.pwd, "getpwnam", lambda user: me)
    monkeypatch.setattr(isolation, "kill_all", lambda user: None)
    monkeypatch.setattr(isolation, "_remove_files_of", lambda user: None)
    work = Path(tempfile.mkdtemp(prefix="ci-fix-test-", dir="/tmp"))
    work.chmod(0o711)
    repo = work / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(isolation.tempfile, "mkdtemp", lambda **_kw: str(work / "home"))

    with isolation.handed_to("agent", repo):
        # A dependency the job checked out, and a repository the agent made.
        subprocess.run(["git", "init", "-q", str(repo / "libbacktrace")], check=True)
        (repo / "libbacktrace" / "x.c").write_text("int x;\n")
        (repo / "deep" / "planted").mkdir(parents=True)
        (repo / "deep" / "planted" / ".git").write_text("gitdir: /tmp/elsewhere\n")  # a gitlink
    assert (repo / ".git").is_dir() and (repo / "libbacktrace" / "x.c").exists()
    assert not (repo / "libbacktrace" / ".git").exists() and not (repo / "deep" / "planted" / ".git").exists()

    with pytest.raises(OSError, match="replaced"):
        with isolation.handed_to("agent", repo):
            (repo / ".git").rename(repo / ".git.old")
            shutil.copytree(repo / ".git.old", repo / ".git")
    assert not (repo / ".git").exists()  # the impostor is gone, not handed back
    shutil.rmtree(work)


def test_a_hard_link_in_the_checkout_is_refused_going_in_and_removed_coming_back(monkeypatch):
    """``chown -P`` stops at a symbolic link but reaches through a hard one; macOS lets the account make them."""
    def as_root(*argv):
        if argv[0] in ("find", "rm"):
            subprocess.run(argv, check=True)

    monkeypatch.setattr(isolation, "_as_root", as_root)
    me = isolation.pwd.getpwuid(os.getuid())
    monkeypatch.setattr(isolation.pwd, "getpwnam", lambda user: me)
    monkeypatch.setattr(isolation, "kill_all", lambda user: None)
    monkeypatch.setattr(isolation, "_remove_files_of", lambda user: None)
    work = Path(tempfile.mkdtemp(prefix="ci-fix-test-", dir="/tmp"))
    work.chmod(0o711)
    repo = work / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(isolation.tempfile, "mkdtemp", lambda **_kw: str(work / "home"))
    outside = work / "runner-owned"
    outside.write_text("secret\n")

    with isolation.handed_to("agent", repo):
        os.link(outside, repo / "linked")  # what the account could do on macOS
    assert not (repo / "linked").exists() and outside.read_text() == "secret\n"

    os.link(outside, repo / "linked")
    with pytest.raises(OSError, match="hard links"):
        with isolation.handed_to("agent", repo):
            pass
    shutil.rmtree(work)


def test_every_root_command_ends_its_options_before_its_first_operand(monkeypatch):
    """macOS's chmod and chown take the first operand for the mode or owner,
    so a ``--`` after it is read as a file that does not exist."""
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(isolation, "_as_root", lambda *argv: calls.append(argv))
    me = isolation.pwd.getpwuid(os.getuid())
    monkeypatch.setattr(isolation.pwd, "getpwnam", lambda user: me)
    monkeypatch.setattr(isolation, "kill_all", lambda user: None)
    monkeypatch.setattr(isolation, "_remove_files_of", lambda user: None)
    work = Path(tempfile.mkdtemp(prefix="ci-fix-test-", dir="/tmp"))
    work.chmod(0o711)
    (work / "repo").mkdir()
    (work / "log").mkdir()

    with isolation.handed_to("agent", work / "repo", readable=(work / "log",)):
        pass
    shutil.rmtree(work)

    assert calls
    for argv in calls:
        if "--" in argv:
            options = argv[1:argv.index("--")]
            assert all(option.startswith("-") for option in options), argv


@pytest.mark.parametrize("listing, live", [
    (" 1000 Z\n 1000 Z+\n 0 Ss\n", []),  # only zombies: a container's PID 1 never reaps
    (" 1000 S\n 1000 Z\n", ["S"]),
    (" 1001 R\n", []),  # another account's
])
def test_a_zombie_the_command_left_does_not_count_as_surviving(monkeypatch, listing, live):
    monkeypatch.setattr(isolation.subprocess, "run",
                        lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, listing, ""))

    assert isolation._live_processes(1000) == live


@needs_user
def test_the_account_cannot_swap_git_for_one_of_its_own(checkout, monkeypatch):
    monkeypatch.setenv(isolation.USER_ENV, _USER)
    with pytest.raises(OSError, match="replaced"):
        engine.run_test_command(str(checkout), "mv .git .git.old && cp -r .git.old .git")


def test_root_is_reached_by_sudos_full_path(monkeypatch):
    """A command started with an empty environment finds nothing on PATH (macOS's sudo)."""
    from scripts.common import isolation as isolation_mod

    monkeypatch.setattr(isolation_mod.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(isolation_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert isolation_mod._root() == ["/usr/bin/sudo", "-n"]


def test_a_readable_directory_the_account_cannot_reach_fails_loudly(monkeypatch):
    """macOS's default temp directory is the runner's own: the agent would read nothing."""
    monkeypatch.setattr(isolation, "_as_root", lambda *argv: None)
    work = Path(tempfile.mkdtemp(prefix="ci-fix-test-", dir="/tmp"))
    work.chmod(0o711)
    (work / "repo").mkdir()
    private = work / "private"
    (private / "log").mkdir(parents=True)
    private.chmod(0o700)

    with pytest.raises(OSError, match="not traversable"):
        with isolation.handed_to("agent", work / "repo", readable=(private / "log",)):
            pass
    private.chmod(0o700 | 0o011)
    shutil.rmtree(work)
