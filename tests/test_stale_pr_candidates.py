"""Tests for deciding which pull requests are due for a stale review."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.stale_pr.candidates import decide, pick_candidates
from scripts.stale_pr.models import CandidatePolicy, PRActivity

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
POLICY = CandidatePolicy()


def _ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


def _activity(code: int | None = None, author: int | None = None, maintainer: int | None = None) -> PRActivity:
    return PRActivity(
        last_code_at=_ago(code) if code is not None else None,
        last_author_at=_ago(author) if author is not None else None,
        last_maintainer_at=_ago(maintainer) if maintainer is not None else None,
    )


def _decide(activity: PRActivity, *, number: int = 1, opened: int = 1000, labels=(), **kwargs):
    return decide(
        number, activity=activity, opened_at=_ago(opened), labels=labels,
        now=NOW, policy=kwargs.pop("policy", POLICY), **kwargs,
    )


def test_old_code_and_old_comments_is_due() -> None:
    decision = _decide(_activity(code=400, author=200, maintainer=300))
    assert decision.due
    assert decision.reason == "no code for 400 days and no comments for 200 days"


def test_recent_code_is_not_due() -> None:
    decision = _decide(_activity(code=100, author=400))
    assert not decision.due
    assert decision.reason == "code changed 100 days ago"


def test_recent_author_comment_is_not_due() -> None:
    assert not _decide(_activity(code=400, author=30)).due


def test_recent_maintainer_comment_is_not_due() -> None:
    decision = _decide(_activity(code=400, author=400, maintainer=30))
    assert not decision.due
    assert decision.reason == "author or maintainer commented 30 days ago"


def test_thresholds_are_inclusive_at_the_limit() -> None:
    assert _decide(_activity(code=365, author=180)).due
    assert not _decide(_activity(code=364, author=180)).due
    assert not _decide(_activity(code=365, author=179)).due


def test_missing_dates_fall_back_to_when_the_pr_was_opened() -> None:
    assert _decide(_activity(), opened=400).due
    assert not _decide(_activity(), opened=100).due


def test_pr_with_old_code_but_no_comments_uses_opened_date_for_comments() -> None:
    assert not _decide(_activity(code=400), opened=100).due


def test_skip_label_wins_over_age() -> None:
    decision = _decide(_activity(code=900, author=900), labels=["bug", "Do-Not-Close"])
    assert not decision.due
    assert decision.reason == "has skip label Do-Not-Close"


def test_default_skip_labels() -> None:
    old = _activity(code=900, author=900)
    assert not _decide(old, labels=["to-be-merged"]).due
    assert not _decide(old, labels=["do-not-close"]).due


def test_major_decision_pending_is_still_a_candidate() -> None:
    # It may already be merged through a newer PR, so it still gets reviewed.
    assert _decide(_activity(code=900, author=900), labels=["major-decision-pending"]).due


def test_skip_labels_can_be_replaced() -> None:
    policy = CandidatePolicy(skip_labels=frozenset({"keep"}))
    old = _activity(code=900, author=900)
    assert _decide(old, labels=["do-not-close"], policy=policy).due
    assert not _decide(old, labels=["keep"], policy=policy).due


def test_keep_request_wins_over_age() -> None:
    decision = _decide(_activity(code=900, author=900), keep_requested=True)
    assert not decision.due
    assert decision.reason == "someone asked to keep it open"


def test_pr_already_waiting_for_a_reply_is_skipped() -> None:
    decision = _decide(_activity(code=900, author=900), awaiting_reply=True)
    assert not decision.due
    assert decision.reason == "already waiting for a reply"


def test_author_reply_makes_pr_due_again_only_after_six_months() -> None:
    assert not _decide(_activity(code=900, author=179)).due
    assert _decide(_activity(code=900, author=180)).due


def test_custom_thresholds() -> None:
    policy = CandidatePolicy(days_without_code=30, days_without_comments=10)
    assert _decide(_activity(code=40, author=20), policy=policy).due


def test_pick_candidates_oldest_first_and_capped() -> None:
    decisions = [
        _decide(_activity(code=500, author=500), number=1),
        _decide(_activity(code=900, author=900), number=2),
        _decide(_activity(code=10, author=10), number=3),
        _decide(_activity(code=700, author=700), number=4),
    ]
    picked = pick_candidates(decisions, CandidatePolicy(max_per_run=2))
    assert [d.number for d in picked] == [2, 4]


def test_pick_candidates_breaks_ties_by_pr_number() -> None:
    decisions = [_decide(_activity(code=500, author=500), number=n) for n in (9, 3, 5)]
    assert [d.number for d in pick_candidates(decisions, POLICY)] == [3, 5, 9]
