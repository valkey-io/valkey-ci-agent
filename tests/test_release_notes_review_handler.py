from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.ai.runtime import AgentRunResult
from scripts.common.proc import build_approved_patch, git_output, run_git
from scripts.release_notes import release_format as rn
from scripts.release_notes import review_handler
from scripts.release_notes.review import (
    ReleasePR,
    ReviewComment,
    ReviewRefused,
    ReviewThread,
    SelectedReview,
    parse_request,
    parse_status,
    review_batch_id,
    review_targets,
    status_body,
    validate_release_pr,
)
from scripts.release_notes.review_handler import (
    LiveBatch,
    handle_review_request,
    load_batch,
    publish_patch,
    run_review_edit,
)

_BOT = "valkeyrie-ops[bot]"
_HEAD = "a" * 40


class StatusComment:
    def __init__(self) -> None:
        self.id = 99
        thread = _thread()
        reviews = (SelectedReview(thread, thread.comments[0]),)
        self.body = status_body(
            "addressing",
            _HEAD,
            review_batch_id(reviews),
            1,
            targets=review_targets(reviews),
        )
        self.user = SimpleNamespace(login=_BOT)

    def edit(self, body: str) -> None:
        self.body = body


def _pr(status: StatusComment | None = None) -> SimpleNamespace:
    status = status or StatusComment()
    return SimpleNamespace(
        number=42,
        state="open",
        user=SimpleNamespace(login=_BOT),
        head=SimpleNamespace(
            repo=SimpleNamespace(full_name="valkey-io/valkey"),
            ref="agent/release-cut/9.1.1-ga",
            sha=_HEAD,
        ),
        base=SimpleNamespace(ref="9.1"),
        get_files=lambda: [
            SimpleNamespace(filename="00-RELEASENOTES", status="modified"),
            SimpleNamespace(filename="src/version.h", status="modified"),
        ],
        get_issue_comment=lambda _comment_id: status,
        create_review_comment_reply=MagicMock(),
    )


def _release() -> ReleasePR:
    return validate_release_pr(_pr(), "valkey-io/valkey")


def _comment(comment_id: int = 10, body: str = "Please improve this.") -> ReviewComment:
    return ReviewComment(
        database_id=comment_id,
        body=body,
        created_at=f"2026-08-21T00:00:{comment_id:02d}Z",
        diff_hunk="@@ -10 +10 @@",
        author_login="alice",
        author_type="User",
    )


def _thread(comment: ReviewComment | None = None) -> ReviewThread:
    return ReviewThread(
        node_id="PRRT_thread",
        resolved=False,
        outdated=False,
        path="00-RELEASENOTES",
        line=20,
        comments=(comment or _comment(),),
    )


def _batch(
    *,
    release: ReleasePR | None = None,
    comment: ReviewComment | None = None,
) -> LiveBatch:
    release = release or _release()
    thread = _thread(comment)
    return LiveBatch(
        release=release,
        pull_request=_pr(),
        status_comment=StatusComment(),
        reviews=(SelectedReview(thread, thread.latest_human_comment()),),
    )


def _github(pr: SimpleNamespace) -> MagicMock:
    gh = MagicMock()
    gh.get_repo.return_value = SimpleNamespace(
        get_pull=lambda _number: pr,
    )
    return gh


def _notes(release: ReleasePR) -> str:
    return rn.render_release_notes(
        {"Bug Fixes": ["Current fix by @alice (#100)"]},
        version=release.version,
        stage=release.stage,
        urgency="HIGH",
        date="2026-08-21",
        prior_text="",
        contributors=["Alice @alice"],
        display_name=release.profile.display_name,
        categories=release.profile.categories,
    )


def _agent_result() -> AgentRunResult:
    return AgentRunResult(
        profile="release_notes_review_edit_only",
        stdout="",
        stderr="",
        returncode=0,
        prompt_sha256="0" * 64,
        cwd="/tmp/repo",
        allowed_tools="Read,Edit,MultiEdit,Grep,Glob",
        model="fable",
        started_at="2026-08-21T00:00:00+00:00",
        finished_at="2026-08-21T00:00:01+00:00",
    )


def _init_repo(path: Path, release: ReleasePR) -> None:
    run_git(None, "init", "-q", "-b", release.head_branch, str(path))
    (path / "src").mkdir()
    (path / release.notes_path).write_text(_notes(release), encoding="utf-8")
    (path / release.version_path).write_text("#define VERSION 1\n", encoding="utf-8")
    run_git(str(path), "config", "user.name", "Test")
    run_git(str(path), "config", "user.email", "test@example.com")
    run_git(str(path), "add", ".")
    run_git(str(path), "commit", "-q", "-m", "initial")


def test_load_batch_refetches_authorized_reviews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr = _pr()
    monkeypatch.setattr(
        review_handler,
        "list_review_threads",
        lambda *_args: (_thread(),),
    )

    batch = load_batch(
        _github(pr),
        MagicMock(),
        parse_request("valkey", "42", _HEAD, "99"),
        bot_login=_BOT,
        authorize=lambda login: login == "alice",
    )

    assert batch.release.number == 42
    assert batch.reviews[0].comment.database_id == 10


def test_edit_pass_may_only_change_current_notes(
    tmp_path: Path,
) -> None:
    release = _release()
    repo = tmp_path / "repo"
    _init_repo(repo, release)
    batch = _batch(release=replace(release, head_sha=git_output(str(repo), "rev-parse", "HEAD").strip()))

    def edit(_prompt: str, cwd: str) -> AgentRunResult:
        path = Path(cwd) / release.notes_path
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "Current fix",
                "Clearer current fix",
            ),
            encoding="utf-8",
        )
        return _agent_result()

    patch, candidate = run_review_edit(str(repo), batch, edit_agent=edit)

    assert "Clearer current fix" in candidate
    assert "00-RELEASENOTES" in patch

    def edit_other(_prompt: str, cwd: str) -> AgentRunResult:
        (Path(cwd) / "README.md").write_text("unexpected\n", encoding="utf-8")
        return _agent_result()

    run_git(str(repo), "reset", "--hard", "HEAD")
    with pytest.raises(ReviewRefused, match="only the notes file"):
        run_review_edit(str(repo), batch, edit_agent=edit_other)


def test_handler_revalidates_complete_batch_before_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _batch()
    current = _batch(comment=_comment(11, "A newer comment."))
    monkeypatch.setattr(
        review_handler,
        "load_batch",
        MagicMock(side_effect=[initial, current]),
    )
    monkeypatch.setattr(review_handler, "_clone", lambda *_args: None)
    monkeypatch.setattr(
        review_handler,
        "run_review_edit",
        lambda *_args, **_kwargs: ("patch", "candidate"),
    )

    def publish(**kwargs) -> str:
        kwargs["before_push"]("c" * 40)
        return "c" * 40

    outcome = handle_review_request(
        MagicMock(),
        MagicMock(),
        parse_request("valkey", "42", _HEAD, "99"),
        token="token",
        bot_login=_BOT,
        publish=lambda _release, **kwargs: publish(**kwargs),
    )

    assert outcome.success is False
    assert "changed" in outcome.reason
    assert ":failed:" in initial.status_comment.body


def test_handler_updates_status_replies_and_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = _batch()
    monkeypatch.setattr(
        review_handler,
        "load_batch",
        MagicMock(side_effect=[batch, batch]),
    )
    monkeypatch.setattr(review_handler, "_clone", lambda *_args: None)
    monkeypatch.setattr(
        review_handler,
        "run_review_edit",
        lambda *_args, **_kwargs: ("patch", "candidate"),
    )
    monkeypatch.setattr(
        review_handler,
        "_reply_and_resolve",
        lambda *_args: (1, 1, []),
    )

    def publish(_release, **kwargs) -> str:
        kwargs["before_push"]("c" * 40)
        return "c" * 40

    outcome = handle_review_request(
        MagicMock(),
        MagicMock(),
        parse_request("valkey", "42", _HEAD, "99"),
        token="token",
        bot_login=_BOT,
        publish=publish,
    )

    assert outcome.success is True
    assert (outcome.replied, outcome.resolved) == (1, 1)
    assert batch.status_comment.body.startswith("Addressed 1 release-note")
    marker = parse_status(batch.status_comment, _BOT)
    assert marker is not None
    assert marker.head_sha == marker.commit_sha == "c" * 40


def test_handler_preserves_recovery_state_when_push_result_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = _batch()
    monkeypatch.setattr(
        review_handler,
        "load_batch",
        MagicMock(side_effect=[batch, batch]),
    )
    monkeypatch.setattr(review_handler, "_clone", lambda *_args: None)
    monkeypatch.setattr(
        review_handler,
        "run_review_edit",
        lambda *_args, **_kwargs: ("patch", "candidate"),
    )
    gh = MagicMock()
    gh.get_repo.return_value.get_pull.return_value = SimpleNamespace(
        head=SimpleNamespace(sha="c" * 40)
    )

    def publish(_release, **kwargs) -> str:
        kwargs["before_push"]("c" * 40)
        raise RuntimeError("push connection closed")

    outcome = handle_review_request(
        gh,
        MagicMock(),
        parse_request("valkey", "42", _HEAD, "99"),
        token="token",
        bot_login=_BOT,
        publish=publish,
    )

    assert outcome.success is False
    marker = parse_status(batch.status_comment, _BOT)
    assert marker is not None
    assert marker.status == "addressed"
    assert marker.head_sha == marker.commit_sha == "c" * 40


@pytest.mark.parametrize(
    "newest_comment",
    [
        _comment(11, "A new follow-up."),
        _comment(10, "Edited feedback with the same GitHub id."),
    ],
)
def test_changed_comment_after_reply_prevents_thread_resolution(
    monkeypatch: pytest.MonkeyPatch,
    newest_comment: ReviewComment,
) -> None:
    batch = _batch()
    newest = _thread(newest_comment)
    monkeypatch.setattr(
        review_handler,
        "list_review_threads",
        MagicMock(side_effect=[(batch.reviews[0].thread,), (newest,)]),
    )
    resolve = MagicMock()
    monkeypatch.setattr("scripts.release_notes.review.resolve_review_thread", resolve)
    gh = _github(batch.pull_request)

    replied, resolved, failures = review_handler._reply_and_resolve(
        gh,
        MagicMock(),
        batch,
        "c" * 40,
        _BOT,
    )

    assert (replied, resolved, failures) == (1, 0, [])
    resolve.assert_not_called()


def test_existing_bot_reply_is_not_duplicated_during_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = _batch()
    human = batch.reviews[0].comment
    commit_sha = "c" * 40
    reply = ReviewComment(
        database_id=11,
        body=(
            "Addressed in "
            f"[`{commit_sha[:12]}`](https://github.com/valkey-io/valkey/commit/{commit_sha})."
        ),
        created_at="2026-08-21T00:00:11Z",
        diff_hunk="@@ -10 +10 @@",
        author_login=_BOT,
        author_type="Bot",
    )
    thread = _thread()
    thread = replace(thread, comments=(human, reply))
    monkeypatch.setattr(
        review_handler,
        "list_review_threads",
        MagicMock(side_effect=[(thread,), (thread,)]),
    )
    resolve = MagicMock()
    monkeypatch.setattr("scripts.release_notes.review.resolve_review_thread", resolve)
    gh = _github(batch.pull_request)

    replied, resolved, failures = review_handler._reply_and_resolve(
        gh,
        MagicMock(),
        batch,
        commit_sha,
        _BOT,
    )

    assert (replied, resolved, failures) == (0, 1, [])
    batch.pull_request.create_review_comment_reply.assert_not_called()
    resolve.assert_called_once()
    assert resolve.call_args.args[1] == thread.node_id


def test_publish_applies_patch_in_clean_clone_and_pushes_normally(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release = _release()
    seed = tmp_path / "seed"
    _init_repo(seed, release)
    head_sha = git_output(str(seed), "rev-parse", "HEAD").strip()
    release = replace(release, head_sha=head_sha)
    remote = tmp_path / "remote.git"
    run_git(None, "clone", "--bare", str(seed), str(remote))

    edit = tmp_path / "edit"
    run_git(None, "clone", str(remote), str(edit))
    notes = edit / release.notes_path
    candidate = notes.read_text(encoding="utf-8").replace(
        "Current fix",
        "Clearer current fix",
    )
    notes.write_text(candidate, encoding="utf-8")
    patch = build_approved_patch(str(edit), (release.notes_path,))

    def clone_local(_release: ReleasePR, destination: Path) -> None:
        run_git(None, "clone", str(remote), str(destination))
        run_git(str(destination), "checkout", head_sha)

    monkeypatch.setattr(review_handler, "_clone", clone_local)
    monkeypatch.setattr(review_handler, "github_https_url", lambda _repo: str(remote))
    before_push = MagicMock()

    commit_sha = publish_patch(
        release,
        patch=patch,
        candidate=candidate,
        token="token",
        before_push=before_push,
    )

    before_push.assert_called_once_with(commit_sha)
    pushed = subprocess.run(
        [
            "git",
            "--git-dir",
            str(remote),
            "show",
            f"{release.head_branch}:{release.notes_path}",
        ],
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert commit_sha != head_sha
    assert "Clearer current fix" in pushed
