"""Plan the complete Git change represented by a merged pull request."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

SourceChangeStrategy = Literal["merge", "squash", "single"]


class SourceChangeError(RuntimeError):
    """Raised when a pull request's source history cannot be resolved safely."""


_REBASE_MERGE_UNSUPPORTED = (
    "pull request was rebase-merged: its merge SHA is only the final "
    "rewritten commit, not the complete change. Backport it manually."
)


@dataclass(frozen=True)
class SourceChangePlan:
    """A deterministic plan for applying one pull request.

    ``commits`` is the authoritative merge or squash commit.
    ``source_commits`` retains the complete source identity. Callers must
    not substitute a different SHA.
    """

    strategy: SourceChangeStrategy
    commits: tuple[str, ...]
    merge_commit_sha: str | None
    source_commits: tuple[str, ...]
    aggregate_patch_id: str

    def __post_init__(self) -> None:
        if len(self.commits) != 1:
            raise ValueError(
                "source-change plans must contain exactly one authoritative commit"
            )


def prepare_source_change(
    repo_dir: str,
    source_pr_number: int,
    merge_commit_sha: str | None,
    commit_shas: Sequence[str],
    *,
    source_commits_complete: bool = True,
    remote: str = "origin",
    git_env: Mapping[str, str] | None = None,
) -> SourceChangePlan:
    """Fetch the authoritative PR objects and return their application plan.

    When the source commit listing is incomplete (the API pages out on very
    large pull requests), classification falls back to the authoritative PR
    head: its tip is fetched and appended, which is all the patch-identity
    comparison needs. A squash of a 300-commit PR still classifies as
    squash; a rebase merge still fails closed.
    """

    source_commits = tuple(sha for sha in commit_shas if sha)
    if not source_commits_complete:
        tip = _fetch_pr_head_tip(
            repo_dir,
            source_pr_number,
            remote,
            git_env,
        )
        source_commits = (
            *(sha for sha in source_commits if sha != tip),
            tip,
        )
    missing_source_commits = tuple(
        sha for sha in source_commits if not _commit_exists(repo_dir, sha)
    )
    if missing_source_commits:
        source_ref = (
            f"+refs/pull/{source_pr_number}/head:"
            f"refs/valkey-ci-agent/backport/{source_pr_number}/head"
        )
        _git(
            repo_dir,
            "fetch",
            "--no-tags",
            remote,
            source_ref,
            env=git_env,
        )
        still_missing = tuple(
            sha for sha in missing_source_commits if not _commit_exists(repo_dir, sha)
        )
        if still_missing:
            raise SourceChangeError(
                "PR head does not contain source commit(s): "
                + ", ".join(still_missing)
            )

    if merge_commit_sha and not _commit_exists(repo_dir, merge_commit_sha):
        _git(
            repo_dir,
            "fetch",
            "--no-tags",
            remote,
            merge_commit_sha,
            env=git_env,
        )
        if not _commit_exists(repo_dir, merge_commit_sha):
            raise SourceChangeError(
                f"could not fetch merge commit {merge_commit_sha}"
            )

    return plan_source_change(repo_dir, merge_commit_sha, source_commits)


def _fetch_pr_head_tip(
    repo_dir: str,
    source_pr_number: int,
    remote: str,
    git_env: Mapping[str, str] | None,
) -> str:
    ref = f"refs/valkey-ci-agent/backport/{source_pr_number}/head"
    _git(
        repo_dir,
        "fetch",
        "--no-tags",
        remote,
        f"+refs/pull/{source_pr_number}/head:{ref}",
        env=git_env,
    )
    return _git(repo_dir, "rev-parse", ref).stdout.strip()


def plan_source_change(
    repo_dir: str,
    merge_commit_sha: str | None,
    commit_shas: Sequence[str],
) -> SourceChangePlan:
    """Return a complete application plan for a merged pull request.

    GitHub's ``merge_commit_sha`` has different meanings by merge method. It
    is a real merge commit for "merge", an aggregate commit for "squash", and
    only the final rewritten commit for a multi-commit "rebase and merge".

    For a one-parent merge SHA and multiple source commits, compare exact
    patch identities. A matching aggregate is a squash commit; a non-match
    means the pull request was rebase-merged and the merge SHA is NOT the
    complete change — replaying a rebased series is not supported, so this
    fails closed and the pull request must be backported manually.
    Disconnected or unreadable histories fail closed too.
    """

    source_commits = tuple(sha for sha in commit_shas if sha)
    if len(set(source_commits)) != len(source_commits):
        raise SourceChangeError("source commit list contains duplicate SHAs")

    if not merge_commit_sha:
        if not source_commits:
            raise SourceChangeError("pull request has neither a merge SHA nor source commits")
        if len(source_commits) > 1:
            raise SourceChangeError(_REBASE_MERGE_UNSUPPORTED)
        parents = _commit_parents(repo_dir, source_commits[0])
        if not parents:
            raise SourceChangeError(f"source commit {source_commits[0]} has no parent")
        return SourceChangePlan(
            strategy="single",
            commits=source_commits,
            merge_commit_sha=None,
            source_commits=source_commits,
            aggregate_patch_id=_exact_patch_id(repo_dir, parents[0], source_commits[0]),
        )

    merge_parents = _commit_parents(repo_dir, merge_commit_sha)
    if not merge_parents:
        raise SourceChangeError(f"merge SHA {merge_commit_sha} has no parent")

    if len(merge_parents) > 1:
        return SourceChangePlan(
            strategy="merge",
            commits=(merge_commit_sha,),
            merge_commit_sha=merge_commit_sha,
            source_commits=source_commits,
            aggregate_patch_id=_exact_patch_id(
                repo_dir,
                merge_parents[0],
                merge_commit_sha,
            ),
        )

    if not source_commits:
        raise SourceChangeError(
            "source commit list is empty; cannot safely classify a one-parent "
            "merge SHA as squash or rebase"
        )

    merge_patch_id = _exact_patch_id(
        repo_dir,
        merge_parents[0],
        merge_commit_sha,
    )
    if len(source_commits) <= 1:
        return SourceChangePlan(
            strategy="single",
            commits=(merge_commit_sha,),
            merge_commit_sha=merge_commit_sha,
            source_commits=source_commits,
            aggregate_patch_id=merge_patch_id,
        )

    if merge_commit_sha in source_commits:
        raise SourceChangeError(_REBASE_MERGE_UNSUPPORTED)

    source_base = _unique_merge_base(
        repo_dir,
        merge_parents[0],
        source_commits[-1],
    )
    source_patch_id = _exact_patch_id(
        repo_dir,
        source_base,
        source_commits[-1],
    )
    if source_patch_id and source_patch_id == merge_patch_id:
        return SourceChangePlan(
            strategy="squash",
            commits=(merge_commit_sha,),
            merge_commit_sha=merge_commit_sha,
            source_commits=source_commits,
            aggregate_patch_id=merge_patch_id,
        )

    raise SourceChangeError(
        f"merge SHA {merge_commit_sha} does not match the aggregate patch "
        f"of the source commits ({source_base}..{source_commits[-1]}). The "
        "pull request was either rebase-merged (its merge SHA is only the "
        "final rewritten commit) or the base branch advanced over the same "
        "lines before the merge. Backport it manually."
    )


def _unique_merge_base(repo_dir: str, left: str, right: str) -> str:
    result = _git(repo_dir, "merge-base", "--all", left, right)
    bases = tuple(line for line in result.stdout.splitlines() if line)
    if not bases:
        raise SourceChangeError(
            f"no merge base exists for {left} and {right}; source history is "
            "disconnected"
        )
    if len(bases) > 1:
        raise SourceChangeError(
            f"ambiguous merge base for {left} and {right}: found {len(bases)}; "
            "cannot safely classify this pull request as squash or rebase"
        )
    return bases[0]


def _commit_parents(repo_dir: str, sha: str) -> tuple[str, ...]:
    result = _git(repo_dir, "rev-list", "--parents", "-n", "1", sha)
    fields = result.stdout.strip().split()
    if not fields or fields[0] != sha:
        raise SourceChangeError(f"could not resolve commit {sha}")
    return tuple(fields[1:])


def _commit_exists(repo_dir: str, sha: str) -> bool:
    result = _git(
        repo_dir,
        "cat-file",
        "-e",
        f"{sha}^{{commit}}",
        check=False,
    )
    return result.returncode == 0


def _exact_patch_id(repo_dir: str, base: str, tip: str) -> str:
    # Ignore surrounding context so an adjacent base-branch change made before
    # a squash merge does not change the identity of the source changes.
    diff = _git_bytes(
        repo_dir,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--unified=0",
        base,
        tip,
    )
    result = subprocess.run(
        ["git", "patch-id", "--verbatim"],
        cwd=repo_dir,
        input=diff.stdout,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SourceChangeError(
            f"could not compute patch identity for {base}..{tip}: "
            f"{result.stderr.decode('utf-8', errors='replace').strip()[:300]}"
        )
    fields = result.stdout.decode("ascii", errors="replace").strip().split()
    return fields[0] if fields else ""


def _git(
    repo_dir: str,
    *args: str,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if check and result.returncode != 0:
        raise SourceChangeError(
            f"git {' '.join(args)} failed: {result.stderr.strip()[:300]}"
        )
    return result


def _git_bytes(
    repo_dir: str,
    *args: str,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SourceChangeError(
            f"git {' '.join(args)} failed: "
            f"{result.stderr.decode('utf-8', errors='replace').strip()[:300]}"
        )
    return result
