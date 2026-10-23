"""Small, shared subprocess and environment helpers."""

from __future__ import annotations

import os
import subprocess
from typing import NoReturn

# Re-exported for existing importers; canonical definition lives in identity.py.
from scripts.common.identity import BOT_EMAIL, BOT_NAME  # noqa: F401


class GitCommandError(subprocess.CalledProcessError):
    """``CalledProcessError`` whose message carries git's stderr.

    The stock message names only the command and exit status, so an uncaught
    failure (or a caller that logs ``exc``) loses the one line that says why
    git refused. The tail is bounded so a noisy command cannot flood the log.
    """

    def __str__(self) -> str:
        detail = " ".join(str(self.stderr or self.stdout or "").split())[-500:]
        return (f"git {git_subcommand(self.cmd)} exited {self.returncode}: "
                f"{detail or 'no output'}")


def git_subcommand(cmd: object) -> str:
    """The git subcommand in *cmd*, skipping ``-c key=value`` and other flags."""
    args = list(cmd) if isinstance(cmd, (list, tuple)) else str(cmd).split()
    args = [str(a) for a in args]
    if args[:1] == ["git"]:
        args = args[1:]
    i = 0
    while i < len(args):
        if args[i] in ("-c", "-C"):
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return args[i]
    return "<command>"


def _raise_git_failure(result: subprocess.CompletedProcess[str]) -> NoReturn:
    # Drop the locked-config prefix so the message shows the git subcommand.
    args = list(result.args)
    cmd = ["git", *args[1 + len(LOCKED_GIT_CONFIG):]] if args[:1] == ["git"] else args
    raise GitCommandError(result.returncode, cmd, result.stdout, result.stderr)


# Bound every git invocation so a hung network/lock cannot stall the run.
# Generous enough for a clone or push, short enough to fail fast on a hang.
_GIT_TIMEOUT_S = 5 * 60


def git_output(repo_dir: str, *args: str, timeout: int = _GIT_TIMEOUT_S) -> str:
    """Run a git command in ``repo_dir`` and return its stdout.

    Raises ``subprocess.CalledProcessError`` (with git's stderr attached to the
    message) on a non-zero exit, or ``subprocess.TimeoutExpired`` if it does not
    finish within ``timeout`` (so a hung git/network/lock never blocks the run
    indefinitely). Use this when the command's output is the point (``diff``,
    ``rev-parse``); for fire-and-forget commands use the caller's own runner.

    Runs with the same *scrubbed* environment as ``run_git`` (process basics
    only, no inherited tokens). These commands run after untrusted PR code has
    executed in the worktree, so a planted repo-local git config
    (``core.sshCommand``, ``core.fsmonitor``, a diff/filter driver) must never
    execute with the parent's credentials in scope. ``LOCKED_GIT_CONFIG``
    disables hooks; the scrubbed env removes the secrets such config could
    otherwise read. Every caller is a local operation that needs no credentials.
    """
    result = subprocess.run(
        ["git", *LOCKED_GIT_CONFIG, *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=filter_env(_GIT_SAFE_ENV),
    )
    if result.returncode != 0:
        _raise_git_failure(result)
    return result.stdout


def is_ancestor(repo_dir: str, ancestor: str, descendant: str) -> bool | None:
    """True/False for a definite ``git merge-base --is-ancestor`` answer.

    git exits 1 for "not an ancestor" and anything else (128 for a bad object,
    a timeout, a scrubbed-config refusal) for a broken question. Only the
    definite 1 is False; a broken question is None so callers cannot mistake
    an unreadable history for a negative answer.
    """
    try:
        git_output(repo_dir, "merge-base", "--is-ancestor", ancestor, descendant)
    except subprocess.CalledProcessError as exc:
        return False if exc.returncode == 1 else None
    except subprocess.TimeoutExpired:
        return None
    return True


def run_git(
    repo_dir: str | None,
    *args: str,
    env: dict[str, str] | None = None,
    input: str | None = None,
    timeout: int = _GIT_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    """Run a git command, raising on failure, with an explicit environment.

    Unlike ``git_output`` this returns the full result and is for commands run
    for effect (``commit``, ``push``, ``clone``, ``apply``). ``repo_dir`` may be
    ``None`` for commands that do not run inside a repository (e.g. ``clone``).
    ``input`` feeds stdin (e.g. ``git apply -``). ``env`` defaults to a
    *scrubbed* environment (process basics only, no inherited secrets) so a
    repository git hook can never read tokens from the ambient environment
    during, e.g., ``git commit``. Pass an explicit ``env`` (such as a
    ``GitAuth`` env) when a command needs credentials.

    On a non-zero exit the raised ``CalledProcessError`` carries git's stderr,
    so failures are diagnosable.

    Repository-defined hooks are disabled on every invocation
    (``core.hooksPath=/dev/null``): the working tree may have been written by
    untrusted code, so a ``pre-push``/``pre-commit`` hook must never run - least
    of all the authenticated ``push`` that carries credentials.
    """
    result = subprocess.run(
        ["git", *LOCKED_GIT_CONFIG, *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        input=input,
        env=env if env is not None else filter_env(_GIT_SAFE_ENV),
        timeout=timeout,
    )
    if result.returncode != 0:
        _raise_git_failure(result)
    return result


# Disable repo-controlled code execution paths. The working tree is untrusted
# (an AI or a test may have written files, .git/hooks/*, or .git/config), so no
# git command may execute hooks, credential helpers, or external diff drivers.
# ``--literal-pathspecs`` makes every path argument a literal path: a file whose
# name carries pathspec magic (e.g. ``:(glob)*``) must not be able to broaden a
# ``diff``/``ls-files`` beyond the approved paths.
LOCKED_GIT_CONFIG = (
    "--literal-pathspecs",
    "-c", "core.hooksPath=/dev/null",
    "-c", "credential.helper=",
    "-c", "diff.external=",
)
# A strict read also keeps a planted ``core.fsmonitor`` command from running.
_FSMONITOR_OFF = ("-c", "core.fsmonitor=false")
_STRICT_GIT_CONFIG = (*LOCKED_GIT_CONFIG, *_FSMONITOR_OFF)

# Process-runtime environment variables that carry no credential: the minimal
# set any scrubbed subprocess needs. Other allowlists extend this.
PROCESS_BASICS = ("PATH", "HOME", "TMPDIR", "TMP", "USER", "LOGNAME", "LANG", "LC_ALL")

# CA-bundle locations a scrubbed subprocess needs to make HTTPS calls. Carry no
# credential, so they extend an allowlist without widening the trust boundary.
NETWORK_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")

# Minimal environment for local git commands: process basics only.
_GIT_SAFE_ENV = PROCESS_BASICS


def git_safe_env() -> dict[str, str]:
    """The scrubbed git environment, also ignoring user and system config.

    For the CI fix's ``strict`` reads of a checkout an agent edited: a global
    ``~/.gitconfig`` is writable by the same user that runs untrusted tests and
    edit agents, so it must not be able to add hooks or commands.
    """
    env = filter_env(_GIT_SAFE_ENV)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def filter_env(allowlist: tuple[str, ...]) -> dict[str, str]:
    """Return os.environ filtered to non-empty allowlisted names.

    The single place that turns an env allowlist into a concrete environment,
    so subprocess sandboxing semantics stay identical everywhere.
    """
    allowed = set(allowlist)
    return {name: value for name, value in os.environ.items() if name in allowed and value}


def default_branch_ref(repo_dir: str) -> str:
    """``origin/<default branch>`` of a clone, or "" when it cannot be resolved."""
    try:
        ref = git_output(repo_dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip()
    except subprocess.CalledProcessError:
        return ""
    return ref if ref.startswith("origin/") else ""


def git_paths(repo_dir: str, *args: str) -> tuple[str, ...]:
    """The NUL-separated paths a ``-z`` git listing prints.

    A path may be any bytes, not only UTF-8: surrogateescape keeps them as
    they are, and passing such a path back to git reproduces the same bytes.
    """
    result = subprocess.run(
        ["git", *_STRICT_GIT_CONFIG, *args], cwd=repo_dir, capture_output=True,
        encoding="utf-8", errors="surrogateescape", timeout=_GIT_TIMEOUT_S, env=git_safe_env(),
    )
    if result.returncode != 0:
        _raise_git_failure(result)
    return tuple(path for path in result.stdout.split("\0") if path)


def worktree_changed_paths(repo_dir: str, *, strict: bool = False) -> tuple[str, ...]:
    """Return tracked-but-modified and untracked paths in the working tree.

    Combines ``git diff --name-only HEAD`` (tracked changes) with
    ``git ls-files --others --exclude-standard`` (new files), NUL-delimited so
    paths with spaces survive. Used to see exactly which files an edit step
    touched.

    ``strict`` lists a rename as both of its paths and reads the checkout as
    ``git_paths`` does, for a checkout an agent with Bash edited.
    """
    paths: set[str] = set()
    if strict:
        for args in (
            ("diff", "--no-renames", "--name-only", "-z", "HEAD"),
            ("ls-files", "--others", "--exclude-standard", "-z"),
        ):
            paths.update(git_paths(repo_dir, *args))
        return tuple(sorted(paths))
    for args in (
        ("diff", "--name-only", "-z", "HEAD"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        out = run_git(repo_dir, *args).stdout
        paths.update(p for p in out.split("\0") if p)
    return tuple(sorted(paths))


class EmptyPatch(Exception):
    """Raised when the approved paths produce no patch content."""


def build_approved_patch(repo_dir: str, changed_paths: tuple[str, ...], *, strict: bool = False) -> str:
    """Return the exact patch for ``changed_paths`` that a push would apply.

    Includes untracked approved files (staged with ``--intent-to-add`` so they
    appear in the diff) and uses ``--binary`` so the patch reapplies verbatim
    in a clean clone. This is the single source of truth for "the change": the
    skeptic review inspects this patch, and the push reapplies it - so the
    review never judges something different from what ships.

    Raises ``EmptyPatch`` when there are no approved paths, or when the approved
    paths yield no content, so an empty or vanished edit is refused rather than
    silently approved (and never broadened to the whole tree).

    ``strict``, for a checkout an agent with Bash edited, reads it as
    ``git_paths`` does and builds the patch from the bytes, without renames.
    """
    if not changed_paths:
        raise EmptyPatch("no approved paths to build a patch from")
    if strict:
        return _strict_patch(repo_dir, changed_paths)
    untracked = tuple(
        p
        for p in run_git(
            repo_dir, "ls-files", "--others", "--exclude-standard", "-z", "--", *changed_paths
        ).stdout.split("\0")
        if p
    )
    if untracked:
        run_git(repo_dir, "add", "--intent-to-add", "--", *untracked)
    patch = git_output(repo_dir, "diff", "--no-ext-diff", "--binary", "HEAD", "--", *changed_paths)
    if not patch.strip():
        raise EmptyPatch("approved paths produced an empty patch")
    return patch


def _strict_patch(repo_dir: str, changed_paths: tuple[str, ...]) -> str:
    untracked = git_paths(repo_dir, "ls-files", "--others", "--exclude-standard", "-z", "--", *changed_paths)
    if untracked:
        run_git(repo_dir, *_FSMONITOR_OFF, "add", "--intent-to-add", "--", *untracked, env=git_safe_env())
    # No renames: the push re-checks the staged paths against ``changed_paths``,
    # which lists both sides. No textconv: the patch must be the bytes, and a
    # textconv driver is a command.
    patch = run_git(
        repo_dir, *_FSMONITOR_OFF, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary",
        "HEAD", "--", *changed_paths, env=git_safe_env(),
    ).stdout
    if not patch.strip():
        raise EmptyPatch("approved paths produced an empty patch")
    return patch
