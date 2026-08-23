from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.release_notes.review import (
    ReviewComment,
    ReviewThread,
    SelectedReview,
    parse_status,
    review_batch_id,
    review_targets,
    status_body,
    validate_release_pr,
)
from scripts.release_notes.review_poll import (
    dispatch_release_review,
    poll_once,
)

_BOT = "valkeyrie-ops[bot]"
_HEAD = "a" * 40


def _batch_id(thread_id: str = "thread-one", comment_id: int = 1) -> str:
    thread = _thread(thread_id, comment_id)
    return review_batch_id((SelectedReview(thread, thread.comments[0]),))


class StatusComment:
    def __init__(
        self,
        body: str,
        comment_id: int = 99,
        *,
        updated_at: datetime | None = None,
    ) -> None:
        self.id = comment_id
        self.body = body
        self.user = SimpleNamespace(login=_BOT)
        self.updated_at = updated_at or datetime.now(timezone.utc)

    def edit(self, body: str) -> None:
        self.body = body


def _comment(comment_id: int, login: str = "alice") -> ReviewComment:
    return ReviewComment(
        database_id=comment_id,
        body="Please improve this wording.",
        created_at=f"2026-08-21T00:00:{comment_id:02d}Z",
        diff_hunk="@@ -10 +10 @@",
        author_login=login,
        author_type="User",
    )


def _thread(thread_id: str, comment_id: int, login: str = "alice") -> ReviewThread:
    return ReviewThread(
        node_id=thread_id,
        resolved=False,
        outdated=False,
        path="00-RELEASENOTES",
        line=20,
        comments=(_comment(comment_id, login),),
    )


def _pr(issue_comments: list[StatusComment] | None = None) -> SimpleNamespace:
    comments = issue_comments or []

    def create_issue_comment(body: str) -> StatusComment:
        comment = StatusComment(body, 100 + len(comments))
        comments.append(comment)
        return comment

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
        get_issue_comments=lambda: list(comments),
        create_issue_comment=create_issue_comment,
        issue_comments=comments,
    )


def _github(pr: SimpleNamespace) -> MagicMock:
    gh = MagicMock()
    gh.get_repo.return_value = SimpleNamespace(
        get_pulls=lambda **_kwargs: [pr],
    )
    return gh


def _poll(
    monkeypatch: pytest.MonkeyPatch,
    pr: SimpleNamespace,
    threads: tuple[ReviewThread, ...],
    dispatch: MagicMock,
) -> int:
    monkeypatch.setattr(
        "scripts.release_notes.review_poll.list_review_threads",
        lambda *_args: threads,
    )
    return poll_once(
        _github(pr),
        MagicMock(),
        repositories=("valkey-io/valkey",),
        bot_login=_BOT,
        dispatch=dispatch,
        authorize=lambda login: login in {"alice", "bob"},
    )


def test_poller_batches_every_actionable_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr = _pr()
    dispatch = MagicMock()

    count = _poll(
        monkeypatch,
        pr,
        (_thread("thread-two", 2, "bob"), _thread("thread-one", 1)),
        dispatch,
    )

    assert count == 1
    release, status_id = dispatch.call_args.args
    assert release.number == 42
    assert status_id == 100
    assert pr.issue_comments[0].body.startswith(
        "Addressing 2 release-note review comments."
    )


@pytest.mark.parametrize("marker_status", ["addressing", "addressed", "refused"])
def test_existing_status_blocks_same_head(
    monkeypatch: pytest.MonkeyPatch,
    marker_status: str,
) -> None:
    kwargs = (
        {
            "commit_sha": "c" * 40,
            "repo_full_name": "valkey-io/valkey",
        }
        if marker_status == "addressed"
        else {"reason": "test"}
        if marker_status == "refused"
        else {}
    )
    pr = _pr(
        [
            StatusComment(
                status_body(
                    marker_status,
                    _HEAD,
                    _batch_id(),
                    1,
                    **kwargs,
                )
            )
        ]
    )
    dispatch = MagicMock()

    assert _poll(
        monkeypatch,
        pr,
        (_thread("thread-one", 1),),
        dispatch,
    ) == 0
    dispatch.assert_not_called()


def test_failed_status_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    pr = _pr(
        [
            StatusComment(
                status_body(
                    "failed",
                    _HEAD,
                    _batch_id(),
                    1,
                    reason="temporary",
                )
            )
        ]
    )
    dispatch = MagicMock()

    assert _poll(
        monkeypatch,
        pr,
        (_thread("thread-one", 1),),
        dispatch,
    ) == 1
    dispatch.assert_called_once()


def test_expired_addressing_status_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr = _pr(
        [
            StatusComment(
                status_body("addressing", _HEAD, _batch_id(), 1),
                updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
        ]
    )
    dispatch = MagicMock()

    assert _poll(
        monkeypatch,
        pr,
        (_thread("thread-one", 1),),
        dispatch,
    ) == 1
    dispatch.assert_called_once()


def test_poller_recovers_bookkeeping_after_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = _thread("thread-one", 1)
    selected = (SelectedReview(thread, thread.comments[0]),)
    status = StatusComment(
        status_body(
            "addressing",
            "b" * 40,
            review_batch_id(selected),
            1,
            commit_sha=_HEAD,
            targets=review_targets(selected),
        )
    )
    pr = _pr([status])
    reconcile = MagicMock(return_value=(1, 1, []))
    monkeypatch.setattr(
        "scripts.release_notes.review_poll.reconcile_review_targets",
        reconcile,
    )

    assert _poll(monkeypatch, pr, (thread,), MagicMock()) == 0
    marker = parse_status(status, _BOT)
    assert marker is not None
    assert marker.status == "addressed"
    assert marker.head_sha == marker.commit_sha == _HEAD
    reconcile.assert_called_once()


def test_refusal_does_not_block_a_new_comment_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = StatusComment(
        status_body(
            "refused",
            _HEAD,
            _batch_id(),
            1,
            reason="no valid edit",
        )
    )
    pr = _pr([refused])
    dispatch = MagicMock()

    assert _poll(
        monkeypatch,
        pr,
        (_thread("thread-one", 2),),
        dispatch,
    ) == 1
    dispatch.assert_called_once()


def test_dispatch_failure_updates_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr = _pr()
    dispatch = MagicMock(side_effect=RuntimeError("dispatch unavailable"))

    assert _poll(
        monkeypatch,
        pr,
        (_thread("thread-one", 1),),
        dispatch,
    ) == 0
    assert "Failed to address" in pr.issue_comments[0].body
    assert ":failed:" in pr.issue_comments[0].body


def test_dispatcher_passes_only_pr_identity() -> None:
    workflow = MagicMock()
    gh = MagicMock()
    gh.get_repo.return_value.get_workflow.return_value = workflow
    pr = _pr()
    release = validate_release_pr(pr, "valkey-io/valkey")

    dispatch_release_review(gh)(release, 99)

    assert workflow.create_dispatch.call_args.args == (
        "main",
        {
            "repo": "valkey",
            "pr": "42",
            "head_sha": _HEAD,
            "status_comment_id": "99",
        },
    )
