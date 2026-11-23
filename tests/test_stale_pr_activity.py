"""Tests for working out a pull request's last real activity from its timeline."""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.stale_pr.activity import compute_activity

AUTHOR = "contributor"
MAINTAINERS = {"maintainer", "other-maintainer"}


def _at(day: str) -> datetime:
    return datetime.fromisoformat(f"{day}T00:00:00+00:00")


def _user(login: str, kind: str = "User") -> dict:
    return {"login": login, "type": kind}


def _commit(day: str, *, parents: int = 1, author_day: str | None = None) -> dict:
    return {
        "event": "committed",
        "sha": "a" * 40,
        "author": {"date": f"{author_day or day}T00:00:00Z"},
        "committer": {"date": f"{day}T00:00:00Z"},
        "parents": [{"sha": "b" * 40}] * parents,
    }


def _comment(day: str, login: str, kind: str = "User") -> dict:
    return {"event": "commented", "user": _user(login, kind), "created_at": f"{day}T00:00:00Z"}


def _review(day: str | None, login: str) -> dict:
    return {
        "event": "reviewed",
        "user": _user(login),
        "state": "changes_requested",
        "submitted_at": f"{day}T00:00:00Z" if day else None,
    }


def _activity(events: list[dict], **kwargs):
    return compute_activity(events, author=AUTHOR, is_maintainer=MAINTAINERS.__contains__, **kwargs)


def test_dates_split_by_kind() -> None:
    activity = _activity([
        _commit("2023-01-01"),
        _comment("2023-03-01", AUTHOR),
        _review("2023-05-01", "maintainer"),
    ])
    assert activity.last_code_at == _at("2023-01-01")
    assert activity.last_author_at == _at("2023-03-01")
    assert activity.last_maintainer_at == _at("2023-05-01")


def test_newest_event_of_each_kind_wins_regardless_of_order() -> None:
    activity = _activity([
        _comment("2023-06-01", AUTHOR),
        _comment("2023-02-01", AUTHOR),
        _commit("2023-04-01"),
        _commit("2023-01-01"),
    ])
    assert activity.last_author_at == _at("2023-06-01")
    assert activity.last_code_at == _at("2023-04-01")


def test_empty_timeline_has_no_dates() -> None:
    activity = _activity([])
    assert activity.last_code_at is None
    assert activity.last_author_at is None
    assert activity.last_maintainer_at is None


def test_rebased_commit_uses_committer_date() -> None:
    activity = _activity([_commit("2024-01-01", author_day="2022-01-01")])
    assert activity.last_code_at == _at("2024-01-01")


def test_force_push_counts_as_code() -> None:
    activity = _activity([
        _commit("2023-01-01"),
        {"event": "head_ref_force_pushed", "actor": _user(AUTHOR), "created_at": "2023-09-01T00:00:00Z"},
    ])
    assert activity.last_code_at == _at("2023-09-01")


def test_merge_from_base_branch_is_not_code() -> None:
    activity = _activity([_commit("2023-01-01"), _commit("2024-01-01", parents=2)])
    assert activity.last_code_at == _at("2023-01-01")


def test_bot_comments_are_ignored() -> None:
    activity = _activity([
        _comment("2023-01-01", "maintainer"),
        _comment("2024-01-01", "codecov[bot]", kind="Bot"),
        _comment("2024-02-01", "github-actions[bot]"),
        _comment("2024-03-01", "valkeyrie-bot"),
    ])
    assert activity.last_maintainer_at == _at("2023-01-01")
    assert activity.last_author_at is None


def test_ignored_logins_are_skipped() -> None:
    activity = _activity([_comment("2024-01-01", "maintainer")], ignored_logins=["Maintainer"])
    assert activity.last_maintainer_at is None


def test_non_maintainer_comments_do_not_count() -> None:
    activity = _activity([_comment("2024-01-01", "passer-by")])
    assert activity.last_author_at is None
    assert activity.last_maintainer_at is None


def test_author_who_is_maintainer_counts_as_author() -> None:
    events = [_comment("2024-01-01", "maintainer")]
    activity = compute_activity(events, author="maintainer", is_maintainer=MAINTAINERS.__contains__)
    assert activity.last_author_at == _at("2024-01-01")
    assert activity.last_maintainer_at is None


def test_author_login_matches_case_insensitively() -> None:
    activity = _activity([_comment("2024-01-01", "Contributor")])
    assert activity.last_author_at == _at("2024-01-01")


def test_pending_review_is_ignored() -> None:
    activity = _activity([_review(None, "maintainer")])
    assert activity.last_maintainer_at is None


def test_inline_review_comments_count() -> None:
    activity = _activity([{
        "event": "line-commented",
        "comments": [
            {"user": _user("maintainer"), "created_at": "2023-01-01T00:00:00Z"},
            {"user": _user(AUTHOR), "created_at": "2023-02-01T00:00:00Z"},
        ],
    }])
    assert activity.last_maintainer_at == _at("2023-01-01")
    assert activity.last_author_at == _at("2023-02-01")


def test_events_that_are_not_work_are_ignored() -> None:
    activity = _activity([
        {"event": "labeled", "actor": _user("maintainer"), "created_at": "2024-01-01T00:00:00Z"},
        {"event": "cross-referenced", "actor": _user(AUTHOR), "created_at": "2024-01-01T00:00:00Z"},
        {"event": "assigned", "actor": _user("maintainer"), "created_at": "2024-01-01T00:00:00Z"},
    ])
    assert activity.last_code_at is None
    assert activity.last_author_at is None
    assert activity.last_maintainer_at is None


def test_malformed_dates_are_skipped() -> None:
    activity = _activity([
        {"event": "commented", "user": _user(AUTHOR), "created_at": "not a date"},
        {"event": "committed", "committer": {}, "parents": []},
    ])
    assert activity.last_author_at is None
    assert activity.last_code_at is None


def test_dates_are_timezone_aware() -> None:
    activity = _activity([_commit("2023-01-01")])
    assert activity.last_code_at is not None
    assert activity.last_code_at.tzinfo == timezone.utc
