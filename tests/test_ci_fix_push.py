from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.ci_fix import push as push_mod
from scripts.ci_fix.models import (
    CandidateAction,
    FixCandidate,
    FixTarget,
    TargetKind,
)
from scripts.ci_fix.push import PushFailed, PushRefused, commit_message, push_candidate


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _remote(tmp_path: Path, branch: str = "agent/backport/sweep/9.0") -> tuple[Path, str, str]:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "unstable")
    _git(work, "config", "user.name", "Base")
    _git(work, "config", "user.email", "base@example.com")
    (work / "seed.txt").write_text("seed\n")
    _git(work, "add", "seed.txt")
    _git(work, "commit", "-qm", "seed")
    head_sha = _git(work, "rev-parse", "HEAD").strip()
    _git(work, "branch", branch, head_sha)

    (work / "upstream.txt").write_text("fixed upstream\n")
    _git(work, "add", "upstream.txt")
    _git(
        work,
        "-c",
        "user.name=Real Author",
        "-c",
        "user.email=author@example.com",
        "commit",
        "-qm",
        "upstream fix\n\nSigned-off-by: Real Author <author@example.com>",
    )
    port_sha = _git(work, "rev-parse", "HEAD").strip()

    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/unstable")
    return bare, head_sha, port_sha


def _diverged_port_remote(
    tmp_path: Path,
    *,
    base_text: str | None,
    upstream_text: str,
    target_text: str,
) -> tuple[Path, str, str]:
    work = tmp_path / "diverged-work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "unstable")
    _git(work, "config", "user.name", "Base")
    _git(work, "config", "user.email", "base@example.com")
    seed = work / "seed.txt"
    seed.write_text("seed\n")
    _git(work, "add", "seed.txt")
    if base_text is not None:
        (work / "collision.txt").write_text(base_text)
        _git(work, "add", "collision.txt")
    _git(work, "commit", "-qm", "seed")
    base_sha = _git(work, "rev-parse", "HEAD").strip()
    branch = "agent/backport/sweep/9.0"
    _git(work, "branch", branch, base_sha)

    (work / "collision.txt").write_text(upstream_text)
    _git(work, "add", "collision.txt")
    _git(work, "commit", "-qm", "upstream fix")
    port_sha = _git(work, "rev-parse", "HEAD").strip()

    _git(work, "checkout", "-q", branch)
    (work / "collision.txt").write_text(target_text)
    _git(work, "add", "collision.txt")
    _git(work, "commit", "-qm", "target change")
    head_sha = _git(work, "rev-parse", "HEAD").strip()

    bare = tmp_path / "diverged-remote.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/unstable")
    return bare, head_sha, port_sha


def _merge_port_remote(tmp_path: Path) -> tuple[Path, str, str]:
    work = tmp_path / "merge-work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "unstable")
    _git(work, "config", "user.name", "Base")
    _git(work, "config", "user.email", "base@example.com")
    (work / "seed.txt").write_text("seed\n")
    _git(work, "add", "seed.txt")
    _git(work, "commit", "-qm", "seed")
    head_sha = _git(work, "rev-parse", "HEAD").strip()
    _git(work, "branch", "agent/backport/sweep/9.0", head_sha)
    _git(work, "checkout", "-qb", "fix")
    (work / "fix.txt").write_text("fix\n")
    _git(work, "add", "fix.txt")
    _git(work, "commit", "-qm", "fix")
    _git(work, "checkout", "-q", "unstable")
    (work / "main.txt").write_text("main\n")
    _git(work, "add", "main.txt")
    _git(work, "commit", "-qm", "main")
    _git(work, "merge", "-q", "--no-ff", "-m", "merge fix", "fix")
    port_sha = _git(work, "rev-parse", "HEAD").strip()

    bare = tmp_path / "merge-remote.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/unstable")
    return bare, head_sha, port_sha


def _patch_for(tmp_path: Path, head_sha: str, remote: Path, path: str = "fix.txt") -> str:
    checkout = tmp_path / f"patch-{path.replace('/', '-')}"
    _git(tmp_path, "clone", "-q", str(remote), str(checkout))
    _git(checkout, "checkout", "-q", head_sha)
    destination = checkout / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("the fix\n")
    _git(checkout, "add", "--intent-to-add", path)
    return _git(checkout, "diff", "--binary", "HEAD")


def _target(
    head_sha: str,
    *,
    kind: TargetKind = TargetKind.UPDATE_PR,
    branch: str = "agent/backport/sweep/9.0",
) -> FixTarget:
    return FixTarget(
        kind=kind,
        repo_full_name="valkey-io/valkey",
        branch=branch,
        expected_sha=head_sha,
    )


def _candidate(patch: str, path: str = "fix.txt") -> FixCandidate:
    return FixCandidate(
        action=CandidateAction.PATCH,
        failing_check="unit test",
        root_cause="The fixture is stale.",
        reasoning="Refresh the fixture.",
        patch=patch,
        changed_paths=(path,),
    )


def test_patch_is_committed_to_the_existing_branch_with_an_exact_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, head_sha, _port_sha = _remote(tmp_path)
    patch = _patch_for(tmp_path, head_sha, remote)
    monkeypatch.setattr(push_mod, "github_https_url", lambda _repo: str(remote))

    pushed = push_candidate(_candidate(patch), _target(head_sha), git_env={})

    assert _git(remote, "rev-parse", "agent/backport/sweep/9.0").strip() == pushed
    assert _git(remote, "rev-parse", f"{pushed}^").strip() == head_sha
    assert _git(remote, "show", f"{pushed}:fix.txt") == "the fix\n"
    assert "Signed-off-by:" not in _git(remote, "log", "-1", "--format=%B", pushed)


def test_a_suggestion_target_is_never_pushed() -> None:
    """A suggestion's branch is the contributor's, even when its name looks like the bot's."""
    target = _target("b" * 40, kind=TargetKind.SUGGEST)

    with pytest.raises(PushRefused, match="suggestion target"):
        push_candidate(_candidate("diff"), target, git_env={})


@pytest.mark.parametrize("path", [".github/workflows/fix.yml", ".github/actions/setup/action.yml"])
def test_a_ci_definition_change_is_never_pushed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """A pushed workflow runs with the repository's privileges; a person applies it."""
    remote, head_sha, _port_sha = _remote(tmp_path)
    patch = _patch_for(tmp_path, head_sha, remote, path)
    monkeypatch.setattr(push_mod, "github_https_url", lambda _repo: str(remote))

    with pytest.raises(PushRefused, match=f"changes {path}"):
        push_candidate(_candidate(patch, path), _target(head_sha), git_env={})
    assert _git(remote, "rev-parse", "agent/backport/sweep/9.0").strip() == head_sha


def test_new_draft_target_requires_the_branch_not_to_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, head_sha, _port_sha = _remote(tmp_path)
    patch = _patch_for(tmp_path, head_sha, remote)
    monkeypatch.setattr(push_mod, "github_https_url", lambda _repo: str(remote))
    target = _target(
        head_sha,
        kind=TargetKind.DRAFT_PR,
        branch="agent/ci-fix/issue-9-100-500",
    )

    pushed = push_candidate(_candidate(patch), target, git_env={})
    assert _git(remote, "rev-parse", target.branch).strip() == pushed
    with pytest.raises(PushRefused, match="target branch changed"):
        push_candidate(_candidate(patch), target, git_env={})


def test_moved_or_deleted_existing_branch_is_not_recreated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, head_sha, _port_sha = _remote(tmp_path)
    patch = _patch_for(tmp_path, head_sha, remote)
    monkeypatch.setattr(push_mod, "github_https_url", lambda _repo: str(remote))

    def delete_at_last_check() -> str:
        _git(remote, "update-ref", "-d", "refs/heads/agent/backport/sweep/9.0")
        return ""

    with pytest.raises(PushRefused):
        push_candidate(
            _candidate(patch),
            _target(head_sha),
            git_env={},
            pre_push_check=delete_at_last_check,
        )
    assert "agent/backport/sweep/9.0" not in _git(remote, "branch", "--list")


def test_patch_apply_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, head_sha, _port_sha = _remote(tmp_path)
    monkeypatch.setattr(push_mod, "github_https_url", lambda _repo: str(remote))

    with pytest.raises(PushFailed, match="Git operation failed"):
        push_candidate(
            _candidate("this is not a patch"),
            _target(head_sha),
            git_env={},
        )


def test_non_lease_push_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, head_sha, _port_sha = _remote(tmp_path)
    patch = _patch_for(tmp_path, head_sha, remote)
    monkeypatch.setattr(push_mod, "github_https_url", lambda _repo: str(remote))
    real_run_git = push_mod.run_git

    def run_git(repo_dir, *args, **kwargs):
        if args[:1] == ("push",):
            raise subprocess.CalledProcessError(
                128,
                ["git", "push"],
                stderr="fatal: unable to access remote",
            )
        return real_run_git(repo_dir, *args, **kwargs)

    monkeypatch.setattr(push_mod, "run_git", run_git)

    with pytest.raises(PushFailed, match="push failed"):
        push_candidate(_candidate(patch), _target(head_sha), git_env={})


def test_revalidation_api_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, head_sha, _port_sha = _remote(tmp_path)
    patch = _patch_for(tmp_path, head_sha, remote)
    monkeypatch.setattr(push_mod, "github_https_url", lambda _repo: str(remote))

    def unavailable() -> str:
        raise RuntimeError("GitHub unavailable")

    with pytest.raises(PushFailed, match="could not be revalidated"):
        push_candidate(
            _candidate(patch),
            _target(head_sha),
            git_env={},
            pre_push_check=unavailable,
        )


def test_port_preserves_the_merged_commit_author(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, head_sha, port_sha = _remote(tmp_path)
    monkeypatch.setattr(push_mod, "github_https_url", lambda _repo: str(remote))
    candidate = FixCandidate(
        action=CandidateAction.PORT,
        failing_check="build",
        port_commit=port_sha,
    )

    pushed = push_candidate(candidate, _target(head_sha), git_env={})

    assert _git(remote, "log", "-1", "--format=%an <%ae>", pushed).strip() == (
        "Real Author <author@example.com>"
    )
    body = _git(remote, "log", "-1", "--format=%B", pushed)
    assert "Signed-off-by: Real Author" in body
    assert "cherry picked from commit" in body


def test_port_must_be_merged_on_the_default_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, head_sha, _port_sha = _remote(tmp_path)
    work = tmp_path / "rogue"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    _git(work, "checkout", "-qb", "rogue", head_sha)
    _git(work, "config", "user.name", "Rogue")
    _git(work, "config", "user.email", "rogue@example.com")
    (work / "rogue.txt").write_text("rogue\n")
    _git(work, "add", "rogue.txt")
    _git(work, "commit", "-qm", "rogue")
    rogue_sha = _git(work, "rev-parse", "HEAD").strip()
    _git(work, "push", "-q", str(remote), "rogue")
    monkeypatch.setattr(push_mod, "github_https_url", lambda _repo: str(remote))

    with pytest.raises(PushRefused, match="not merged"):
        push_candidate(
            FixCandidate(action=CandidateAction.PORT, port_commit=rogue_sha),
            _target(head_sha),
            git_env={},
        )


def test_conflicting_port_is_permanently_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, head_sha, port_sha = _diverged_port_remote(
        tmp_path,
        base_text="base\n",
        upstream_text="upstream\n",
        target_text="target\n",
    )
    monkeypatch.setattr(push_mod, "github_https_url", lambda _repo: str(remote))

    with pytest.raises(PushRefused, match="cherry-pick conflicts"):
        push_candidate(
            FixCandidate(action=CandidateAction.PORT, port_commit=port_sha),
            _target(head_sha),
            git_env={},
        )


def test_equivalent_port_is_permanently_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, head_sha, port_sha = _diverged_port_remote(
        tmp_path,
        base_text=None,
        upstream_text="same fix\n",
        target_text="same fix\n",
    )
    monkeypatch.setattr(push_mod, "github_https_url", lambda _repo: str(remote))

    with pytest.raises(PushRefused, match="empty or already equivalent"):
        push_candidate(
            FixCandidate(action=CandidateAction.PORT, port_commit=port_sha),
            _target(head_sha),
            git_env={},
        )


def test_merge_commit_port_is_permanently_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, head_sha, port_sha = _merge_port_remote(tmp_path)
    monkeypatch.setattr(push_mod, "github_https_url", lambda _repo: str(remote))

    with pytest.raises(PushRefused, match="merge commits require"):
        push_candidate(
            FixCandidate(action=CandidateAction.PORT, port_commit=port_sha),
            _target(head_sha),
            git_env={},
        )


def test_operational_cherry_pick_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, head_sha, port_sha = _remote(tmp_path)
    monkeypatch.setattr(push_mod, "github_https_url", lambda _repo: str(remote))
    real_run_git = push_mod.run_git

    def run_git(repo_dir, *args, **kwargs):
        if args[:1] == ("cherry-pick",):
            raise subprocess.CalledProcessError(
                128,
                ["git", "cherry-pick"],
                stderr="fatal: could not write new index file",
            )
        return real_run_git(repo_dir, *args, **kwargs)

    monkeypatch.setattr(push_mod, "run_git", run_git)

    with pytest.raises(PushFailed, match="Git operation failed"):
        push_candidate(
            FixCandidate(action=CandidateAction.PORT, port_commit=port_sha),
            _target(head_sha),
            git_env={},
        )


@pytest.mark.parametrize(
    "branch",
    ["unstable", "9.0", "feature/x", "agent/release-cut/9.0.1"],
)
def test_only_ci_fix_and_backport_namespaces_are_writable(branch: str) -> None:
    with pytest.raises(PushRefused, match="only writes under"):
        push_candidate(
            FixCandidate(action=CandidateAction.PORT, port_commit="b" * 40),
            _target("a" * 40, branch=branch),
            git_env={},
        )


def test_commit_message_is_concise_and_wraps_the_explanation() -> None:
    candidate = FixCandidate(
        action=CandidateAction.PATCH,
        failing_check="a very long failing check name " * 5,
        root_cause="A sufficiently long root cause explanation " * 8,
        reasoning="Use the existing helper instead.",
    )
    message = commit_message(candidate)

    assert len(message.splitlines()[0]) <= 72
    assert all(len(line) <= 72 for line in message.splitlines())


def test_a_ported_upstream_commit_that_changes_ci_definitions_is_not_pushed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "w"
    work.mkdir()
    _git(work, "init", "-q", "-b", "unstable")
    _git(work, "config", "user.name", "Base")
    _git(work, "config", "user.email", "base@example.com")
    (work / "seed.txt").write_text("seed\n")
    _git(work, "add", "seed.txt")
    _git(work, "commit", "-qm", "seed")
    head_sha = _git(work, "rev-parse", "HEAD").strip()
    _git(work, "branch", "agent/backport/sweep/9.0", head_sha)
    (work / ".github" / "workflows").mkdir(parents=True)
    (work / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    _git(work, "add", ".github")
    _git(work, "commit", "-qm", "upstream workflow fix")
    port_sha = _git(work, "rev-parse", "HEAD").strip()
    bare = tmp_path / "r.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/unstable")
    monkeypatch.setattr(push_mod, "github_https_url", lambda _repo: str(bare))

    with pytest.raises(PushRefused, match="changes .github/workflows/ci.yml"):
        push_candidate(FixCandidate(action=CandidateAction.PORT, port_commit=port_sha),
                       _target(head_sha), git_env={})
