from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.release_notes import release_format as rn
from scripts.release_notes.review import (
    ReleasePR,
    ReviewComment,
    ReviewRefused,
    ReviewThread,
    list_review_threads,
    parse_request,
    parse_status,
    resolve_review_thread,
    review_batch_id,
    review_payload_json,
    selected_reviews,
    status_body,
    validate_notes_edit,
    validate_release_pr,
)

_BOT = "valkeyrie-ops[bot]"
_HEAD = "a" * 40
_BATCH = "b" * 16


def _pr() -> SimpleNamespace:
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
    )


def _release() -> ReleasePR:
    return validate_release_pr(_pr(), "valkey-io/valkey")


def _comment(
    comment_id: int,
    *,
    login: str = "alice",
    author_type: str = "User",
    body: str = "Please improve this wording.",
) -> ReviewComment:
    return ReviewComment(
        database_id=comment_id,
        body=body,
        created_at=f"2026-08-21T00:00:{comment_id:02d}Z",
        diff_hunk="@@ -10 +10 @@",
        author_login=login,
        author_type=author_type,
    )


def _thread(
    thread_id: str,
    comments: tuple[ReviewComment, ...],
    *,
    resolved: bool = False,
    outdated: bool = False,
    path: str = "00-RELEASENOTES",
) -> ReviewThread:
    return ReviewThread(
        node_id=thread_id,
        resolved=resolved,
        outdated=outdated,
        path=path,
        line=20,
        comments=comments,
    )


def _notes(release: ReleasePR) -> str:
    older = rn.render_release_notes(
        {"Bug Fixes": ["Older fix by @bob (#90)"]},
        version="9.1.0",
        stage="ga",
        urgency="HIGH",
        date="2026-07-01",
        prior_text="",
        contributors=["Bob @bob"],
        display_name=release.profile.display_name,
        categories=release.profile.categories,
    )
    return rn.render_release_notes(
        {"Bug Fixes": ["Current fix by @alice (#100)"]},
        version=release.version,
        stage=release.stage,
        urgency="HIGH",
        date="2026-08-21",
        prior_text=older,
        contributors=["Alice @alice"],
        display_name=release.profile.display_name,
        categories=release.profile.categories,
    )


def test_parse_request_and_release_pr_contract() -> None:
    request = parse_request("valkey", "42", _HEAD, "99")
    release = _release()

    assert request.repo_full_name == "valkey-io/valkey"
    assert request.pr_number == release.number == 42
    assert release.head_branch == "agent/release-cut/9.1.1-ga"
    assert release.notes_path == "00-RELEASENOTES"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repo_name", "../../other"),
        ("pr_number", "0"),
        ("head_sha", "abc"),
        ("status_comment_id", "false"),
    ],
)
def test_parse_request_rejects_untrusted_inputs(field: str, value: str) -> None:
    values = {
        "repo_name": "valkey",
        "pr_number": "42",
        "head_sha": _HEAD,
        "status_comment_id": "99",
    }
    values[field] = value
    with pytest.raises(ReviewRefused):
        parse_request(**values)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda pr: setattr(pr, "state", "closed"),
        lambda pr: setattr(pr.user, "login", "alice"),
        lambda pr: setattr(pr.head.repo, "full_name", "alice/valkey"),
        lambda pr: setattr(pr.head, "ref", "feature"),
        lambda pr: setattr(pr.base, "ref", "main"),
        lambda pr: setattr(
            pr,
            "get_files",
            lambda: [SimpleNamespace(filename="README.md", status="modified")],
        ),
    ],
)
def test_validate_release_pr_rejects_other_pr_shapes(mutate) -> None:
    pr = _pr()
    mutate(pr)
    with pytest.raises(ReviewRefused):
        validate_release_pr(pr, "valkey-io/valkey")


def test_status_comment_is_visible_and_bot_owned() -> None:
    body = status_body("addressing", _HEAD, _BATCH, 2)
    comment = SimpleNamespace(
        body=body,
        user=SimpleNamespace(login=_BOT),
    )

    assert body.startswith("Addressing 2 release-note review comments.")
    marker = parse_status(comment, _BOT)
    assert marker is not None
    assert (marker.status, marker.head_sha, marker.batch_id, marker.count) == (
        "addressing",
        _HEAD,
        _BATCH,
        2,
    )
    assert parse_status(comment, "someone-else[bot]") is None


def test_selects_every_actionable_thread_and_latest_human_comment() -> None:
    threads = (
        _thread("two", (_comment(4, login="bob"),)),
        _thread(
            "one",
            (
                _comment(1),
                _comment(2, body="Use exact wording."),
                _comment(3, login=_BOT, author_type="Bot"),
            ),
        ),
        _thread("resolved", (_comment(5),), resolved=True),
        _thread("wrong-path", (_comment(6),), path="README.md"),
        _thread("unauthorized", (_comment(7, login="mallory"),)),
    )

    reviews = selected_reviews(
        threads,
        "00-RELEASENOTES",
        lambda login: login in {"alice", "bob"},
    )

    assert [
        (review.thread.node_id, review.comment.database_id)
        for review in reviews
    ] == [("one", 2), ("two", 4)]
    assert '"selected_comment_id": 2' in review_payload_json(reviews)


def test_batch_identity_changes_when_comment_body_is_edited() -> None:
    before = selected_reviews(
        (_thread("one", (_comment(1, body="Ambiguous feedback."),)),),
        "00-RELEASENOTES",
        lambda _login: True,
    )
    after = selected_reviews(
        (_thread("one", (_comment(1, body="Use this exact wording."),)),),
        "00-RELEASENOTES",
        lambda _login: True,
    )

    assert review_batch_id(before) != review_batch_id(after)


def test_review_thread_graphql_contract_and_resolution() -> None:
    gql = MagicMock()
    gql.execute.side_effect = [
        {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {
                                "id": "PRRT_thread",
                                "isResolved": False,
                                "isOutdated": False,
                                "path": "00-RELEASENOTES",
                                "line": 20,
                                "comments": {
                                    "pageInfo": {"hasNextPage": False},
                                    "nodes": [
                                        {
                                            "databaseId": 10,
                                            "body": "Please revise this.",
                                            "createdAt": "2026-08-21T00:00:00Z",
                                            "diffHunk": "@@ -10 +10 @@",
                                            "author": {
                                                "login": "alice",
                                                "__typename": "User",
                                            },
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                }
            }
        },
        {
            "resolveReviewThread": {
                "thread": {"id": "PRRT_thread", "isResolved": True}
            }
        },
    ]

    threads = list_review_threads(gql, "valkey-io/valkey", 42)
    resolve_review_thread(gql, threads[0].node_id)

    assert threads[0].latest_human_comment().database_id == 10
    assert "reviewThreads(first: 100)" in gql.execute.call_args_list[0].args[0]
    assert "resolveReviewThread" in gql.execute.call_args_list[1].args[0]


def test_review_thread_query_refuses_truncation() -> None:
    gql = MagicMock()
    gql.execute.return_value = {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": True},
                    "nodes": [],
                }
            }
        }
    }
    with pytest.raises(ReviewRefused, match="more than 100"):
        list_review_threads(gql, "valkey-io/valkey", 42)


def test_notes_edit_must_stay_in_current_dated_section() -> None:
    release = _release()
    original = _notes(release)
    candidate = original.replace(
        "Current fix by @alice",
        "Clearer current fix by @alice",
    )

    validate_notes_edit(original, candidate, release)

    with pytest.raises(ReviewRefused):
        validate_notes_edit(original, candidate.replace("release notes", "notes"), release)
    with pytest.raises(ReviewRefused):
        validate_notes_edit(
            original,
            candidate.replace("Older fix by @bob", "Changed older fix by @bob"),
            release,
        )
    with pytest.raises(ReviewRefused):
        validate_notes_edit(
            original,
            candidate.replace("Alice @alice", "Changed @alice"),
            release,
        )
