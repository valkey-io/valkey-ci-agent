"""Commit a validated fix and push it to the backport PR's own branch.

This is the only place ``ci_fix`` mutates a remote, so it carries the push
discipline:

- The fix is committed authored as the bot, without a DCO sign-off - a human
  must certify the change before it can be merged upstream. Local git
  commands run with a scrubbed environment so a repository git hook can never
  read a credential from the ambient environment.
- The push target must live in the allowed agent namespace
  (``agent/backport/...``) on the PR's own head repo. Anything else is refused.
- The push is fast-forward only: the refspec is ``HEAD:<branch>`` with no
  ``+``, so git itself rejects a non-fast-forward rather than overwriting.

The branch is never merged. The push re-triggers the PR's normal CI.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from scripts.ci_fix.models import FixProposal
from scripts.common.git_auth import github_https_url
from scripts.common.git_clone import REPO_RE, SHA_RE
from scripts.common.proc import BOT_EMAIL, BOT_NAME, EmptyPatch, build_approved_patch, git_output, run_git

logger = logging.getLogger(__name__)

ALLOWED_BRANCH_PREFIX = "agent/backport/"


class PushRefused(Exception):
    """Raised when a push target falls outside the allowed namespace."""


def commit_and_push_fix(
    repo_dir: str,
    *,
    head_repo_full_name: str,
    head_branch: str,
    head_sha: str,
    proposal: FixProposal,
    changed_paths: tuple[str, ...],
    git_env: dict[str, str],
) -> str:
    """Commit the working-tree fix and push it to the PR head branch.

    The verified checkout is treated as untrusted: test commands may have
    modified ``.git/config`` or hooks. We only extract a binary patch for the
    approved paths, then apply it in a fresh clone at ``head_sha``. The clean
    clone is the only checkout that receives credentials. Returns the new
    commit SHA. Raises ``PushRefused`` if any trust-boundary check fails.
    """
    if not head_branch.startswith(ALLOWED_BRANCH_PREFIX):
        # The prefix is a convention, not proof the branch is bot-owned: the
        # push is contained by the fast-forward-only refspec (can only append,
        # never rewrite), the gate's same-repo head requirement, and the App
        # token being scoped to the one target repo.
        raise PushRefused(
            f"Refusing to push to {head_branch!r}: ci_fix only pushes to branches "
            f"under {ALLOWED_BRANCH_PREFIX}."
        )
    if not REPO_RE.fullmatch(head_repo_full_name):
        raise PushRefused(f"Refusing to push to malformed repo {head_repo_full_name!r}.")
    if not SHA_RE.fullmatch(head_sha):
        raise PushRefused(f"Refusing to push from malformed head SHA {head_sha!r}.")
    if not changed_paths:
        raise PushRefused("Refusing to push: no approved changed paths to stage.")
    if not _is_valid_branch_name(head_branch):
        raise PushRefused(f"Refusing to push to malformed branch {head_branch!r}.")

    try:
        patch = build_approved_patch(repo_dir, changed_paths)
    except EmptyPatch as exc:
        raise PushRefused(f"Refusing to push: {exc}.") from exc

    with tempfile.TemporaryDirectory(prefix="ci-fix-push-") as tmpdir:
        clean_repo = Path(tmpdir) / "repo"
        _clone_clean(head_repo_full_name, clean_repo)
        try:
            run_git(str(clean_repo), "checkout", head_sha)
            run_git(str(clean_repo), "checkout", "-B", head_branch)
            _apply_patch(str(clean_repo), patch)

            staged = _staged_paths(str(clean_repo))
            if staged != tuple(sorted(changed_paths)):
                raise PushRefused(
                    "Refusing to push: approved patch staged unexpected paths "
                    f"{staged!r} (expected {tuple(sorted(changed_paths))!r})."
                )

            run_git(str(clean_repo), "config", "user.name", BOT_NAME)
            run_git(str(clean_repo), "config", "user.email", BOT_EMAIL)
            run_git(str(clean_repo), "commit", "-m", _commit_message(proposal))

            run_git(str(clean_repo), "remote", "set-url", "origin", github_https_url(head_repo_full_name))
            run_git(str(clean_repo), "push", "origin", f"HEAD:{head_branch}", env=git_env)
        except subprocess.CalledProcessError as exc:
            # Keep the pipeline's "every outcome is a comment" guarantee: a git
            # failure in the clean clone (unreachable SHA, non-fast-forward
            # push, etc.) becomes a refusal, never an uncaught crash.
            detail = (exc.stderr or str(exc)).strip()[:300]
            raise PushRefused(f"Refusing to push: git failed: {detail}") from exc

        return git_output(str(clean_repo), "rev-parse", "HEAD").strip()


def _clone_clean(head_repo_full_name: str, dest: Path) -> None:
    url = github_https_url(head_repo_full_name)
    try:
        run_git(None, "clone", "--filter=blob:none", url, str(dest))
    except subprocess.CalledProcessError as exc:
        raise PushRefused(f"Refusing to push: clone failed: {(exc.stderr or '')[:300]}") from exc


def _apply_patch(repo_dir: str, patch: str) -> None:
    try:
        run_git(repo_dir, "apply", "--index", "--whitespace=nowarn", "-", input=patch)
    except subprocess.CalledProcessError as exc:
        raise PushRefused(
            f"Refusing to push: approved patch did not apply cleanly: {(exc.stderr or '')[:300]}"
        ) from exc


def _staged_paths(repo_dir: str) -> tuple[str, ...]:
    out = git_output(repo_dir, "diff", "--cached", "--name-only", "-z", "HEAD")
    return tuple(sorted(path for path in out.split("\0") if path))


def _is_valid_branch_name(branch: str) -> bool:
    try:
        run_git(None, "check-ref-format", "--branch", branch)
    except subprocess.CalledProcessError:
        return False
    return True


def _commit_message(proposal: FixProposal) -> str:
    """A focused commit message: a subject naming the test, then the cause.

    Mirrors the maintainer-authored style of the reference fixes.
    """
    subject = f"Fix CI test failure: {proposal.failing_check}"[:72]
    return f"{subject}\n\n{proposal.root_cause}\n"
