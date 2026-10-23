"""Commit and push a CI-fix candidate to a bot-owned branch.

The CI-fix agent follows the same publishing model as the backport agent:

* the agent may edit a disposable checkout, but it never commits or pushes;
* code reapplies the resulting patch (or cherry-picks a merged upstream fix)
  in a clean clone;
* only the bot namespaces are writable; and
* an exact ``--force-with-lease`` prevents overwriting a branch that moved.

The push starts normal repository CI. It never merges the branch.
"""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Callable

from scripts.ci_fix.models import CandidateAction, FixCandidate, FixTarget, TargetKind
from scripts.ci_fix.policy import (
    WRITABLE_BRANCH_PREFIXES,
    is_writable_branch,
)
from scripts.common.git_auth import github_https_url
from scripts.common.git_clone import REPO_RE, is_full_sha
from scripts.common.proc import (
    BOT_EMAIL,
    BOT_NAME,
    default_branch_ref,
    git_output,
    git_paths,
    is_ancestor,
    run_git,
)

PrePushCheck = Callable[[], str]

class PushRefused(Exception):
    """Publication was permanently refused by a safety or stale-target check."""


class PushStale(PushRefused):
    """The exact remote lease no longer matches the publication target."""


class PushFailed(Exception):
    """Publication failed operationally and may succeed when retried."""


def push_candidate(
    candidate: FixCandidate,
    target: FixTarget,
    *,
    git_env: dict[str, str],
    pre_push_check: PrePushCheck | None = None,
) -> str:
    """Commit ``candidate`` on ``target.expected_sha`` and push it.

    ``UPDATE_PR`` requires the remote branch to still point at
    ``expected_sha``. ``DRAFT_PR`` creates a new branch and requires that the
    remote ref not exist. Returns the new commit SHA.
    """
    _validate_target(target)
    create = target.kind is TargetKind.DRAFT_PR
    if candidate.action is CandidateAction.PATCH:
        return _push_patch(
            candidate,
            target,
            git_env=git_env,
            pre_push_check=pre_push_check,
            create=create,
        )
    if candidate.action is CandidateAction.PORT:
        return _push_port(
            candidate,
            target,
            git_env=git_env,
            pre_push_check=pre_push_check,
            create=create,
        )
    raise PushRefused("Refusing to push: the agent did not prepare a fix.")


def _push_patch(
    candidate: FixCandidate,
    target: FixTarget,
    *,
    git_env: dict[str, str],
    pre_push_check: PrePushCheck | None,
    create: bool,
) -> str:
    if not candidate.changed_paths:
        raise PushRefused("Refusing to push: the candidate has no changed paths.")
    if not candidate.patch.strip():
        raise PushRefused("Refusing to push: the candidate patch is empty.")

    with tempfile.TemporaryDirectory(prefix="ci-fix-push-") as tempdir:
        repo_dir = Path(tempdir) / "repo"
        _clone(target.base_repo_full_name or target.repo_full_name, repo_dir)
        try:
            run_git(str(repo_dir), "checkout", "--detach", target.expected_sha)
            _apply_patch(str(repo_dir), candidate.patch)
            staged = _staged_paths(str(repo_dir))
            expected = tuple(sorted(candidate.changed_paths))
            if staged != expected:
                raise PushRefused(
                    "Refusing to push: the patch staged unexpected paths "
                    f"{staged!r} (expected {expected!r})."
                )
            _set_identity(repo_dir)
            run_git(str(repo_dir), "commit", "-m", commit_message(candidate))
            _push(
                repo_dir,
                target,
                git_env=git_env,
                pre_push_check=pre_push_check,
                create=create,
            )
            return git_output(str(repo_dir), "rev-parse", "HEAD").strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            raise PushFailed(f"CI-fix Git operation failed: {exc}") from exc


def _push_port(
    candidate: FixCandidate,
    target: FixTarget,
    *,
    git_env: dict[str, str],
    pre_push_check: PrePushCheck | None,
    create: bool,
) -> str:
    commit = candidate.port_commit.strip()
    if not is_full_sha(commit):
        raise PushRefused(f"Refusing to port malformed commit {commit!r}.")

    with tempfile.TemporaryDirectory(prefix="ci-fix-port-") as tempdir:
        repo_dir = Path(tempdir) / "repo"
        _clone(target.base_repo_full_name or target.repo_full_name, repo_dir)
        try:
            run_git(str(repo_dir), "checkout", "--detach", target.expected_sha)
            check_port(repo_dir, commit, target.expected_sha)
            _set_identity(repo_dir)
            try:
                run_git(str(repo_dir), "cherry-pick", "-x", commit)
            except subprocess.CalledProcessError as exc:
                reason = cherry_pick_refusal(repo_dir, commit)
                if reason:
                    raise PushRefused(reason) from exc
                raise
            _push(
                repo_dir,
                target,
                git_env=git_env,
                pre_push_check=pre_push_check,
                create=create,
            )
            return git_output(str(repo_dir), "rev-parse", "HEAD").strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            raise PushFailed(f"CI-fix Git operation failed: {exc}") from exc


def _validate_target(target: FixTarget) -> None:
    if target.kind is TargetKind.SUGGEST:
        # A suggestion's branch is the contributor's own; it is never pushed to.
        raise PushRefused("Refusing to push to a suggestion target.")
    if not REPO_RE.fullmatch(target.repo_full_name):
        raise PushRefused(f"Refusing to push to malformed repo {target.repo_full_name!r}.")
    if target.base_repo_full_name and not REPO_RE.fullmatch(target.base_repo_full_name):
        raise PushRefused(
            f"Refusing to clone malformed base repo {target.base_repo_full_name!r}."
        )
    if not is_full_sha(target.expected_sha):
        raise PushRefused(f"Refusing to push from malformed SHA {target.expected_sha!r}.")
    if not is_writable_branch(target.branch):
        raise PushRefused(
            f"Refusing to push to {target.branch!r}: CI fix only writes under "
            f"{', '.join(WRITABLE_BRANCH_PREFIXES)}."
        )
    try:
        run_git(None, "check-ref-format", "--branch", target.branch)
    except subprocess.CalledProcessError as exc:
        raise PushRefused(f"Refusing to push to malformed branch {target.branch!r}.") from exc


def _clone(repo_full_name: str, destination: Path) -> None:
    try:
        run_git(
            None,
            "clone",
            "--filter=blob:none",
            github_https_url(repo_full_name),
            str(destination),
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise PushFailed(f"CI-fix clone failed: {exc}") from exc


def _set_identity(repo_dir: Path) -> None:
    run_git(str(repo_dir), "config", "user.name", BOT_NAME)
    run_git(str(repo_dir), "config", "user.email", BOT_EMAIL)


def _apply_patch(repo_dir: str, patch: str) -> None:
    run_git(
        repo_dir,
        "apply",
        "--index",
        "--whitespace=nowarn",
        "-",
        input=patch,
    )


def _staged_paths(repo_dir: str) -> tuple[str, ...]:
    return tuple(sorted(git_paths(
        repo_dir,
        "diff",
        "--cached",
        "--no-renames",
        "--name-only",
        "-z",
        "HEAD",
    )))


def _push(
    repo_dir: Path,
    target: FixTarget,
    *,
    git_env: dict[str, str],
    pre_push_check: PrePushCheck | None,
    create: bool,
) -> None:
    if not _is_ancestor(str(repo_dir), target.expected_sha, "HEAD"):
        raise PushRefused(
            "Refusing to push: the generated commit does not descend from the expected head."
        )
    # A workflow or action change runs with the repository's own privileges
    # once pushed, so a person applies it, never the bot. This also covers a
    # ported upstream commit, and is why the push token has no workflows scope.
    ci_paths = [
        path for path in git_paths(
            str(repo_dir), "diff", "--no-renames", "--name-only", "-z", target.expected_sha, "HEAD",
        )
        if path.startswith(".github/")
    ]
    if ci_paths:
        raise PushRefused(
            f"Refusing to push: the fix changes {', '.join(ci_paths)}; a maintainer must "
            "apply CI definition changes."
        )
    if pre_push_check is not None:
        try:
            reason = pre_push_check()
        except Exception as exc:  # noqa: BLE001 - a failed recheck denies the push
            raise PushFailed(
                f"The publication target could not be revalidated: {exc}"
            ) from exc
        if reason:
            raise PushRefused(f"Refusing to push: {reason}")

    destination = f"refs/heads/{target.branch}"
    expected_remote = "" if create else target.expected_sha
    run_git(
        str(repo_dir),
        "remote",
        "set-url",
        "origin",
        github_https_url(target.repo_full_name),
    )
    if create:
        # An empty lease rejects an existing ref only when the push changes
        # it: pushing a commit identical to the one already there succeeds as
        # a no-op. Creating means the branch must not exist at all.
        try:
            existing = run_git(
                str(repo_dir), "ls-remote", "--heads", "origin", destination, env=git_env,
            ).stdout.strip()
        except subprocess.CalledProcessError as exc:
            raise PushFailed(f"CI-fix push failed: {exc}") from exc
        if existing:
            raise PushStale("Refusing to push: the target branch changed after revalidation.")
    try:
        run_git(
            str(repo_dir),
            "push",
            f"--force-with-lease={destination}:{expected_remote}",
            "origin",
            f"HEAD:{destination}",
            env=git_env,
        )
    except subprocess.CalledProcessError as exc:
        if _is_stale_lease(exc):
            raise PushStale(
                "Refusing to push: the target branch changed after revalidation."
            ) from exc
        raise PushFailed(f"CI-fix push failed: {exc}") from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise PushFailed(f"CI-fix push failed: {exc}") from exc


def check_port(repo_dir: Path, commit: str, head_sha: str) -> None:
    """Raise ``PushRefused`` unless ``commit`` can be cherry-picked onto ``head_sha``.

    The engine runs this before it tests a port, and the publisher again in
    its clean clone.
    """
    _verify_merged_upstream(repo_dir, commit, head_sha)
    if _commit_parent_count(repo_dir, commit) > 1:
        raise PushRefused(
            f"Refusing to port {commit[:12]}: merge commits require an "
            "explicit mainline parent."
        )


def _verify_merged_upstream(repo_dir: Path, commit: str, head_sha: str) -> None:
    default_ref = _default_branch_ref(repo_dir)
    if not _is_ancestor(str(repo_dir), commit, default_ref, missing_is_false=True):
        raise PushRefused(
            f"Refusing to port {commit[:12]}: it is not merged into {default_ref}."
        )
    if _is_ancestor(str(repo_dir), commit, head_sha):
        raise PushRefused(
            f"Refusing to port {commit[:12]}: it is already present on the target head."
        )


def _commit_parent_count(repo_dir: Path, commit: str) -> int:
    line = git_output(
        str(repo_dir),
        "rev-list",
        "--parents",
        "--max-count=1",
        commit,
    ).strip()
    fields = line.split()
    if not fields or fields[0].lower() != commit.lower():
        raise PushFailed(f"CI-fix could not inspect commit {commit[:12]}.")
    return len(fields) - 1


def cherry_pick_refusal(repo_dir: Path, commit: str) -> str:
    """Return a permanent refusal reason for a deterministic pick failure."""
    unmerged = git_paths(
        str(repo_dir),
        "diff",
        "--name-only",
        "--diff-filter=U",
        "-z",
    )
    if unmerged:
        return (
            f"Refusing to port {commit[:12]}: the cherry-pick conflicts with "
            f"the target head in {', '.join(unmerged)}."
        )
    if _cherry_pick_in_progress(repo_dir) and not _staged_paths(str(repo_dir)):
        return (
            f"Refusing to port {commit[:12]}: the cherry-pick is empty or "
            "already equivalent on the target head."
        )
    return ""


def _cherry_pick_in_progress(repo_dir: Path) -> bool:
    try:
        git_output(
            str(repo_dir),
            "rev-parse",
            "--verify",
            "--quiet",
            "CHERRY_PICK_HEAD",
        )
        return True
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return False
        raise


def _default_branch_ref(repo_dir: Path) -> str:
    ref = default_branch_ref(str(repo_dir))
    if not ref:
        raise PushRefused("Refusing to port: the default branch could not be resolved.")
    return ref


def _is_ancestor(
    repo_dir: str,
    ancestor: str,
    descendant: str,
    *,
    missing_is_false: bool = False,
) -> bool:
    """Whether ``ancestor`` is in ``descendant``'s history; git that cannot tell is a failure.

    With ``missing_is_false``, a commit git cannot place (one this clone does
    not have) counts as not an ancestor.
    """
    try:
        answer = is_ancestor(repo_dir, ancestor, descendant)
    except OSError as exc:
        raise PushFailed(f"CI-fix Git ancestry check failed: {exc}") from exc
    if answer is None:
        if missing_is_false:
            return False
        raise PushFailed(f"CI-fix Git ancestry check failed: is {ancestor[:12]} in {descendant}?")
    return answer


def _is_stale_lease(exc: subprocess.CalledProcessError) -> bool:
    """Return whether Git definitively rejected an out-of-date lease."""
    detail = f"{getattr(exc, 'stderr', '') or ''}\n{getattr(exc, 'stdout', '') or ''}"
    return "stale info" in detail.lower()


def commit_message(candidate: FixCandidate) -> str:
    """Return a concise bot-authored commit message for an authored patch."""
    subject = fit_subject(f"Fix {candidate.failing_check or 'CI failure'}")
    paragraphs = [candidate.root_cause.strip(), candidate.reasoning.strip()]
    body = "\n\n".join(
        textwrap.fill(
            paragraph,
            width=72,
            break_long_words=False,
            break_on_hyphens=False,
        )
        for paragraph in paragraphs
        if paragraph
    )
    return f"{subject}\n\n{body}\n" if body else subject


def fit_subject(subject: str) -> str:
    """Fit a Git commit or PR subject to the conventional 72 characters."""
    subject = " ".join(subject.split()).strip(" .")
    if len(subject) <= 72:
        return subject
    clipped = subject[:72].rstrip()
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.rstrip(" .")
