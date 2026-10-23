"""Run untrusted commands as a separate, unprivileged account.

A process running as the workflow user can read the credentialed parent's
``/proc/<pid>/environ`` (the job's credentials are there), rewrite files the
publish step later trusts, or leave a process behind. When ``CI_FIX_AGENT_USER``
names an unprivileged account (the workflows create one), a command runs as
that account over a checkout handed to it for the command, with a fresh home
and temp directory; afterwards every process it owns is killed, every file it
left outside the checkout is deleted, and the checkout is handed back.
``.git`` stays the workflow user's throughout, so the command cannot move
HEAD, add a commit, or install a filter that changes the patch built later.

The workflow user gains root with ``sudo`` on a GitHub-hosted runner, and is
root already in a job's container, which has no sudo. On macOS, which has no
``runuser``, the command runs through ``sudo -u``.
"""

from __future__ import annotations

import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

USER_ENV = "CI_FIX_AGENT_USER"

# Where an unprivileged account can leave files: the shared temp directories
# (on macOS, also its own under /var/folders) and its own home.
_WRITABLE_ROOTS = (
    ("/private/tmp", "/private/var/tmp", "/private/var/folders") if sys.platform == "darwin"
    else ("/tmp", "/var/tmp", "/dev/shm", "/run/lock", "/var/crash")
)


def isolated_user() -> str:
    """The account to run untrusted commands as, or "" to run them directly.

    Inside GitHub Actions the account is required: running a command that can
    reach the job's credentials is never the fallback.
    """
    user = os.environ.get(USER_ENV, "").strip()
    if not user and os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        raise RuntimeError(f"{USER_ENV} is not set; refusing to run untrusted commands as the workflow user")
    return user


@contextmanager
def handed_to(user: str, root: Path, readable: tuple[Path, ...] = ()) -> Iterator[str]:
    """Give ``root``'s files (not ``.git``) to ``user``; yield a fresh home directory.

    ``readable`` directories are made readable to ``user`` for the command.
    ``chown -h -P`` changes symbolic links themselves and never follows them,
    so a link in the checkout cannot redirect the privileged chown.
    """
    root = root.resolve()
    for path in (root, *(directory.resolve() for directory in readable)):
        for parent in path.parents:
            if not _traversable(user, parent):
                raise OSError(f"{user} cannot reach {path}: {parent} is not traversable")
    for directory in readable:
        # ``--`` before the mode: macOS's chmod reads its first operand as
        # the mode, so a ``--`` after it is taken for a file name.
        _as_root("chmod", "-R", "--", "a+rX", str(directory))
    runner = f"{os.getuid()}:{os.getgid()}"
    account = pwd.getpwnam(user)
    home = tempfile.mkdtemp(prefix="ci-fix-home-", dir=root.parent)
    git_dir = root / ".git"
    git_identity = _identity(git_dir)
    # ``-P`` does not help against a hard link: the chown would reach the
    # file it names, wherever that is. A checkout has none; one is refused.
    linked = _hard_links(root)
    if linked:
        raise OSError(f"{root} holds hard links, which the chown would follow: {', '.join(linked[:5])}")
    _as_root("chown", "-R", "-h", "-P", "--", f"{account.pw_uid}:{account.pw_gid}", str(root), home)
    if git_identity is not None:
        _as_root("chown", "-R", "-h", "-P", "--", runner, str(git_dir))
    try:
        yield home
    finally:
        kill_all(user)
        _as_root("rm", "-rf", "--", home)
        # Where hard links are not protected (macOS), ``user`` could have
        # linked a file of the runner's, or root's, into the checkout: the
        # chown below would hand that file to the workflow user, and the
        # next session's to ``user``. The links go first, as root, so the
        # chown never reaches past the checkout.
        _remove_hard_links(root)
        # Take the checkout back first: then nothing in it belongs to
        # ``user``, and the sweep below cannot delete any of it.
        _as_root("chown", "-R", "-h", "-P", "--", runner, str(root))
        _remove_files_of(user)
        # Any other repository inside the checkout (a dependency the job
        # checked out into it) was ``user``'s: its config can name a
        # fsmonitor or a hook that the job's own cleanup, run as the
        # workflow user with its tokens, would execute. None is needed any
        # more, so none is handed back.
        _remove_nested_git_dirs(root)
        # ``user`` owned the checkout's directory, so it could have renamed
        # .git and left one of its own: the chown above would hand that to
        # the workflow user, whose next git command would run any filter or
        # merge driver it configures. Only the original .git is trusted; an
        # impostor is removed before anything can read it.
        if git_identity is not None and _identity(git_dir) != git_identity:
            _as_root("rm", "-rf", "--", str(git_dir))
            raise OSError(f"{user} replaced {git_dir}; the checkout can no longer be trusted")


def _traversable(user: str, directory: Path) -> bool:
    """Whether ``user`` can pass through ``directory``.

    By its mode, or else by an ACL (job.sh grants the account one on the
    runner's home, which is not ``o+x``), which only a test as the account
    itself sees. A test that cannot run as ``user`` is a directory it cannot
    pass through.
    """
    if directory.stat().st_mode & stat.S_IXOTH:
        return True
    test = [_program("test"), "-x", str(directory)]
    if sys.platform == "darwin":
        argv = [_program("sudo"), "-n", "-u", user, "--", *test]
    else:
        argv = [*_root(), _program("runuser"), "-u", user, "--", *test]
    return subprocess.run(argv, capture_output=True, check=False).returncode == 0


# Every file under ``root`` with more than one link, ``.git`` aside, which the
# workflow user keeps and git never hard-links.
def _hard_link_find(root: Path) -> list[str]:
    git_dir = str(root / ".git")
    return [
        "find", str(root), "-xdev", "-mindepth", "1",
        "!", "-path", git_dir, "!", "-path", f"{git_dir}/*", "!", "-type", "d", "-links", "+1",
    ]


def _hard_links(root: Path) -> list[str]:
    """The hard links under ``root``, read as the workflow user, who holds the checkout between sessions."""
    result = subprocess.run([*_hard_link_find(root), "-print"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise OSError(f"find in {root} failed: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line]


def _remove_hard_links(root: Path) -> None:
    _as_root(*_hard_link_find(root), "-delete")


def _remove_nested_git_dirs(root: Path) -> None:
    """Delete every ``.git`` (directory or gitlink file) under ``root`` but its own."""
    _as_root(
        "find", str(root), "-mindepth", "2", "-name", ".git", "-prune",
        "-exec", "rm", "-rf", "{}", "+",  # paths under ``root``: none starts with a dash
    )


def _identity(path: Path) -> tuple[int, int] | None:
    """(device, inode) of a real directory at ``path``, without following links."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino) if stat.S_ISDIR(info.st_mode) else (-1, -1)


def as_user(user: str, env: dict[str, str], argv: list[str], *, home: str) -> list[str]:
    """``argv`` run as ``user`` with ``env``, in ``home`` as its home and temp directory."""
    scrubbed = {
        **env, "HOME": home, "TMPDIR": home, "TMP": home, "USER": user, "LOGNAME": user,
        # The worktree is the account's but .git is not; let git read it.
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*",
    }
    # Every program by its full path: the command may be started with an
    # empty environment, and macOS's sudo then finds nothing on PATH.
    command = [_program("env"), "-i", *(f"{key}={value}" for key, value in scrubbed.items()), *argv]
    if sys.platform == "darwin":
        return [_program("sudo"), "-n", "-u", user, "--", *command]
    # runuser, not ``sudo -u``: it needs only root, which a runner's sudo
    # grants. It is often in an sbin directory.
    return [*_root(), _program("runuser"), "-u", user, "--", *command]


def _program(name: str) -> str:
    return shutil.which(name) or name


def _remove_files_of(user: str) -> None:
    roots = [root for root in (*_WRITABLE_ROOTS, pwd.getpwnam(user).pw_dir) if os.path.isdir(root)]
    _as_root("find", *roots, "-xdev", "-mindepth", "1", "-user", user, "-delete")


def kill_all(user: str) -> None:
    """Kill every process of ``user``, and wait until none can run.

    A command run as ``user`` is started through root's ``sudo`` or
    ``runuser``, which the workflow user cannot signal: on a timeout this is
    how it is stopped, and the root-owned wrapper then exits by itself.
    """
    deadline = time.monotonic() + 30
    uid = pwd.getpwnam(user).pw_uid
    while True:
        subprocess.run([*_root(), "pkill", "-KILL", "-u", user], capture_output=True, check=False)
        if not _live_processes(uid):
            return
        if time.monotonic() > deadline:
            raise OSError(f"processes of {user} survived the command")
        time.sleep(0.2)


def _live_processes(uid: int) -> list[str]:
    """The states of ``uid``'s processes that can still run, zombies excluded.

    In a job's container PID 1 is ``tail -f /dev/null``, which never reaps:
    a killed process the command orphaned (a test server) stays a zombie for
    good. A zombie runs nothing and holds nothing open, so it is gone.
    """
    listed = subprocess.run(["ps", "-A", "-o", "uid=", "-o", "stat="], capture_output=True, text=True, check=False)
    if listed.returncode != 0:
        raise OSError(f"ps failed: {listed.stderr.strip()}")
    states = []
    for line in listed.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == str(uid) and not fields[1].startswith("Z"):
            states.append(fields[1])
    return states


def _root() -> list[str]:
    """What runs a command as root: nothing for root itself, else sudo."""
    return [] if os.geteuid() == 0 else [_program("sudo"), "-n"]


def _as_root(*argv: str) -> None:
    result = subprocess.run([*_root(), *argv], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise OSError(f"{' '.join(argv)} as root failed: {result.stderr.strip()}")
