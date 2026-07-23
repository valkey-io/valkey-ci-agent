"""Tests for the generic marker-based issue dedup publisher."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from scripts.common.issue_dedup import IssueContent, IssueDedupPublisher

NAMESPACE = "valkey-ci-agent:test"


def _render_static(*, title: str = "T", body: str = "B", comment: str = "C",
                   labels: tuple[str, ...] = ()):
    """Build a render callable that returns fixed content regardless of marker/count."""
    def _r(marker: str, occurrences: int) -> IssueContent:
        return IssueContent(
            title=title,
            body=f"{marker}\n<!-- {NAMESPACE}:occurrences:{occurrences} -->\n{body}",
            comment=f"{comment} #{occurrences}",
            labels=labels,
        )
    return _r


def _mock_issue(number: int, *, body: str | None = "", title: str = "",
                closed_at: datetime | None = None,
                raw_data: dict | None = None) -> MagicMock:
    """A listing-shaped issue mock. ``raw_data`` defaults to a plain-issue
    payload (no ``pull_request`` key)."""
    issue = MagicMock(number=number, html_url=f"https://x/issues/{number}")
    issue.body = body
    issue.title = title
    issue.closed_at = closed_at
    issue._rawData = raw_data if raw_data is not None else {}
    return issue


def _mock_gh(open_issues: list | None = None,
             closed_issues: list | None = None) -> tuple[MagicMock, MagicMock]:
    """A (gh, repo) mock pair whose repo serves the given listings and
    reloads issues from them by number."""
    open_issues = open_issues if open_issues is not None else []
    closed_issues = closed_issues if closed_issues is not None else []

    repo = MagicMock()

    def _get_issues(state=None, since=None, labels=None):
        return list(open_issues) if state == "open" else list(closed_issues)

    def _get_issue(number):
        return next(
            i for i in [*open_issues, *closed_issues] if i.number == number
        )

    repo.get_issues.side_effect = _get_issues
    repo.get_issue.side_effect = _get_issue

    gh = MagicMock()
    gh.get_repo.return_value = repo
    return gh, repo


def test_creates_new_issue_when_no_open_issue_matches():
    mock_gh, mock_repo = _mock_gh()
    mock_repo.create_issue.return_value = _mock_issue(1)

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert("o/r", fingerprint="fp1", render=_render_static())
    assert action == "created"
    mock_repo.create_issue.assert_called_once()
    # A renderer without labels must not send a labels argument at all.
    assert "labels" not in mock_repo.create_issue.call_args.kwargs


def test_create_applies_labels_atomically():
    """Labels go on the create call itself, not a follow-up labeling call:
    an issue created without its label would be invisible to a
    filter_label-scoped listing and duplicated on the next run."""
    mock_gh, mock_repo = _mock_gh()
    mock_issue = _mock_issue(1)
    mock_repo.create_issue.return_value = mock_issue

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    publisher.upsert("o/r", fingerprint="fp1",
                     render=_render_static(labels=("possible-bug", "fuzzer")))
    assert mock_repo.create_issue.call_args.kwargs["labels"] == [
        "possible-bug", "fuzzer",
    ]
    mock_issue.add_to_labels.assert_not_called()


def test_updates_existing_increments_occurrence():
    marker = f"<!-- {NAMESPACE}:fp1 -->"
    existing = _mock_issue(
        5, body=f"{marker}\n<!-- {NAMESPACE}:occurrences:1 -->", title="old",
    )
    mock_gh, _ = _mock_gh(open_issues=[existing])

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert("o/r", fingerprint="fp1", render=_render_static())
    assert action == "updated"
    edited_body = existing.edit.call_args.kwargs["body"]
    assert f"<!-- {NAMESPACE}:occurrences:2 -->" in edited_body
    existing.create_comment.assert_called_once()


def test_updates_reinjects_missing_marker():
    """If the reloaded body is None or stripped of the marker, re-inject it
    so future runs continue to dedupe against this issue."""
    marker = f"<!-- {NAMESPACE}:fp1 -->"
    listed = _mock_issue(5, body=f"{marker}\n")
    reloaded = _mock_issue(5, body=None, title="old")
    mock_gh, mock_repo = _mock_gh(open_issues=[listed])
    mock_repo.get_issue.side_effect = None
    mock_repo.get_issue.return_value = reloaded

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    publisher.upsert("o/r", fingerprint="fp1", render=_render_static())
    edited_body = reloaded.edit.call_args.kwargs["body"]
    assert marker in edited_body
    assert f"<!-- {NAMESPACE}:occurrences:2 -->" in edited_body


def test_listing_failure_propagates_no_duplicate_issue():
    """A transient GitHub listing failure must NOT silently fall through to
    create_issue, which would generate duplicate issues until the API
    recovered."""
    mock_gh, mock_repo = _mock_gh()
    mock_repo.get_issues.side_effect = RuntimeError("rate limited")

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    with pytest.raises(RuntimeError, match="rate limited"):
        publisher.upsert("o/r", fingerprint="fp1", render=_render_static())
    mock_repo.create_issue.assert_not_called()


def test_pull_requests_in_listing_are_ignored():
    """The REST issues listing returns pull requests too; a PR whose body
    happens to contain the marker must never be adopted as the issue."""
    marker = f"<!-- {NAMESPACE}:fp1 -->"
    pr = _mock_issue(
        3, body=f"{marker}\n", title="T",
        raw_data={"pull_request": {"url": "https://x/pulls/3"}},
    )
    mock_gh, mock_repo = _mock_gh(open_issues=[pr])
    mock_repo.create_issue.return_value = _mock_issue(1)

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert(
        "o/r", fingerprint="fp1", render=_render_static(), title_fallback="T",
    )
    assert action == "created"
    pr.edit.assert_not_called()


def test_open_listing_fetched_once_per_batch():
    """Multiple upserts on one publisher reuse the cached listing instead of
    re-fetching per failure; that flat cost is the point of listing over
    per-fingerprint search."""
    marker_a = f"<!-- {NAMESPACE}:fpA -->"
    marker_b = f"<!-- {NAMESPACE}:fpB -->"
    issue_a = _mock_issue(5, body=f"{marker_a}\n", title="A")
    issue_b = _mock_issue(6, body=f"{marker_b}\n", title="B")
    mock_gh, mock_repo = _mock_gh(open_issues=[issue_a, issue_b])

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action_a, _ = publisher.upsert("o/r", fingerprint="fpA", render=_render_static())
    action_b, _ = publisher.upsert("o/r", fingerprint="fpB", render=_render_static())

    assert (action_a, action_b) == ("updated", "updated")
    mock_repo.get_issues.assert_called_once_with(state="open")


def test_issue_created_earlier_in_batch_is_visible_to_later_upserts():
    """A fingerprint repeated within one batch must update the issue the
    batch just created, not file a duplicate. The search-based version
    depended on the search index catching up; the cache makes it exact."""
    known_issues: list = []
    mock_gh, mock_repo = _mock_gh(open_issues=known_issues)

    def _create_issue(title, body):
        issue = _mock_issue(1, body=body, title=title)
        # Keep the reload path (repo.get_issue) aware of the new issue.
        known_issues.append(issue)
        return issue

    mock_repo.create_issue.side_effect = _create_issue

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    first, _ = publisher.upsert("o/r", fingerprint="fp1", render=_render_static())
    second, _ = publisher.upsert("o/r", fingerprint="fp1", render=_render_static())

    assert first == "created"
    assert second == "updated"
    mock_repo.create_issue.assert_called_once()


def test_body_transform_applied_on_update():
    """``body_transform`` runs on the update path and its output feeds the
    marker/occurrence machinery, so callers can carry state forward (e.g.
    merging environments) into the edited body."""
    marker = f"<!-- {NAMESPACE}:fp1 -->"
    existing = _mock_issue(
        5, body=f"{marker}\n<!-- {NAMESPACE}:occurrences:1 -->\nEnvs: old",
        title="old",
    )
    mock_gh, _ = _mock_gh(open_issues=[existing])

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert(
        "o/r", fingerprint="fp1", render=_render_static(),
        body_transform=lambda b: b.replace("Envs: old", "Envs: old, new"),
    )
    assert action == "updated"
    edited_body = existing.edit.call_args.kwargs["body"]
    assert "Envs: old, new" in edited_body
    # Marker + bumped occurrence counter survive the transform.
    assert marker in edited_body
    assert f"<!-- {NAMESPACE}:occurrences:2 -->" in edited_body


def test_body_transform_not_applied_on_create():
    """The transform only makes sense for an existing body; on create it must
    not run (there is nothing to carry forward)."""
    mock_gh, mock_repo = _mock_gh()
    mock_repo.create_issue.return_value = _mock_issue(1)

    sentinel = MagicMock(side_effect=AssertionError("transform ran on create"))
    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert(
        "o/r", fingerprint="fp1", render=_render_static(), body_transform=sentinel,
    )
    assert action == "created"
    sentinel.assert_not_called()


def test_idempotency_key_recorded_on_create():
    """When idempotency_key is supplied, the new issue body records it."""
    mock_gh, mock_repo = _mock_gh()
    mock_repo.create_issue.return_value = _mock_issue(1)

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    publisher.upsert("o/r", fingerprint="fp1",
                     render=_render_static(), idempotency_key="run-42")
    body = mock_repo.create_issue.call_args.kwargs["body"]
    assert f"<!-- {NAMESPACE}:last-key:run-42 -->" in body


def test_idempotency_key_skips_duplicate_update():
    """A second upsert with the same idempotency_key must NOT bump the
    counter or comment; the same source event firing twice is a no-op.
    """
    marker = f"<!-- {NAMESPACE}:fp1 -->"
    body = (
        f"{marker}\n<!-- {NAMESPACE}:occurrences:1 -->\n"
        f"<!-- {NAMESPACE}:last-key:run-42 -->"
    )
    existing = _mock_issue(5, body=body, title="old")
    mock_gh, _ = _mock_gh(open_issues=[existing])

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert("o/r", fingerprint="fp1",
                                 render=_render_static(), idempotency_key="run-42")
    assert action == "skipped-duplicate"
    existing.edit.assert_not_called()
    existing.create_comment.assert_not_called()


def test_title_fallback_adopts_legacy_issue_and_restamps_marker():
    """When the marker match misses but an open issue has the exact fallback
    title, adopt it (migration off an older fingerprint) and re-stamp the body
    with the current marker so future runs dedupe on the marker."""
    new_marker = f"<!-- {NAMESPACE}:newfp -->"
    # Legacy issue: created under an old fingerprint, so its body carries a
    # different marker and the marker match returns nothing.
    legacy = _mock_issue(
        7,
        body=f"<!-- {NAMESPACE}:OLD raw::name -->\n<!-- {NAMESPACE}:occurrences:1 -->",
        title="[TEST-FAILURE] PSYNC2 in t.tcl",
    )
    mock_gh, _ = _mock_gh(open_issues=[legacy])

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, url = publisher.upsert(
        "o/r", fingerprint="newfp", render=_render_static(),
        title_fallback="[TEST-FAILURE] PSYNC2 in t.tcl",
    )
    assert action == "updated"
    assert url == "https://x/issues/7"
    edited = legacy.edit.call_args.kwargs["body"]
    assert new_marker in edited  # re-stamped so future runs match on marker
    assert f"<!-- {NAMESPACE}:occurrences:2 -->" in edited


def test_title_fallback_requires_exact_title_match():
    """A near-miss title must not be adopted; only an exact, case-sensitive
    title equals counts."""
    candidate = _mock_issue(
        8, body="x", title="[TEST-FAILURE] PSYNC2 in t.tcl EXTRA",
    )
    mock_gh, mock_repo = _mock_gh(open_issues=[candidate])
    mock_repo.create_issue.return_value = _mock_issue(1)

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert(
        "o/r", fingerprint="newfp", render=_render_static(),
        title_fallback="[TEST-FAILURE] PSYNC2 in t.tcl",
    )
    # No exact match -> falls through to create, not adopt.
    assert action == "created"
    mock_repo.create_issue.assert_called_once()


def test_title_fallback_matches_titles_unsafe_for_search_syntax():
    """Local matching handles titles that used to require query sanitizing
    (quotes, colons, HTML-comment arrows) with a plain exact comparison."""
    hostile_title = '[TEST-FAILURE] "evil" --> in: a:b.tcl'
    legacy = _mock_issue(9, body="no marker", title=hostile_title)
    mock_gh, _ = _mock_gh(open_issues=[legacy])

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, url = publisher.upsert(
        "o/r", fingerprint="newfp", render=_render_static(),
        title_fallback=hostile_title,
    )
    assert action == "updated"
    assert url == "https://x/issues/9"


def test_idempotency_key_different_value_still_updates():
    """A different idempotency_key (different source event) bumps as usual,
    and the new key replaces the old one in the body.
    """
    marker = f"<!-- {NAMESPACE}:fp1 -->"
    body = (
        f"{marker}\n<!-- {NAMESPACE}:occurrences:1 -->\n"
        f"<!-- {NAMESPACE}:last-key:run-42 -->"
    )
    existing = _mock_issue(5, body=body, title="old")
    mock_gh, _ = _mock_gh(open_issues=[existing])

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert("o/r", fingerprint="fp1",
                                 render=_render_static(), idempotency_key="run-99")
    assert action == "updated"
    edited = existing.edit.call_args.kwargs["body"]
    assert f"<!-- {NAMESPACE}:occurrences:2 -->" in edited
    assert f"<!-- {NAMESPACE}:last-key:run-99 -->" in edited
    assert f"<!-- {NAMESPACE}:last-key:run-42 -->" not in edited


# --- Recently-closed issue suppression ---


def _recent(hours: float = 1.0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def test_skips_creation_when_matching_issue_recently_closed():
    """If no open issue matches but a closed issue with the same marker was
    closed within the lookback window, creation is suppressed."""
    marker = f"<!-- {NAMESPACE}:fp1 -->"
    closed_issue = _mock_issue(
        10, body=f"{marker}\nold body", closed_at=_recent(),
    )
    mock_gh, mock_repo = _mock_gh(closed_issues=[closed_issue])

    publisher = IssueDedupPublisher(
        mock_gh, marker_namespace=NAMESPACE, closed_lookback=timedelta(days=1),
    )
    action, url = publisher.upsert("o/r", fingerprint="fp1", render=_render_static())

    assert action == "skipped-recently-closed"
    assert url == "https://x/issues/10"
    mock_repo.create_issue.assert_not_called()


def test_creates_issue_when_closed_candidate_does_not_match_marker():
    """A closed issue whose body does not actually contain the marker is
    ignored, so creation proceeds."""
    no_match = _mock_issue(99, body="unrelated body", closed_at=_recent())
    mock_gh, mock_repo = _mock_gh(closed_issues=[no_match])
    mock_repo.create_issue.return_value = _mock_issue(1)

    publisher = IssueDedupPublisher(
        mock_gh, marker_namespace=NAMESPACE, closed_lookback=timedelta(days=1),
    )
    action, _ = publisher.upsert("o/r", fingerprint="fp1", render=_render_static())

    assert action == "created"
    mock_repo.create_issue.assert_called_once()


def test_creates_issue_when_closed_before_lookback_window():
    """The ``since`` listing filter is on update time, so an issue updated
    recently but closed before the window can appear in the listing; the
    local ``closed_at`` check must reject it."""
    marker = f"<!-- {NAMESPACE}:fp1 -->"
    stale = _mock_issue(
        10, body=f"{marker}\nold body",
        closed_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    mock_gh, mock_repo = _mock_gh(closed_issues=[stale])
    mock_repo.create_issue.return_value = _mock_issue(1)

    publisher = IssueDedupPublisher(
        mock_gh, marker_namespace=NAMESPACE, closed_lookback=timedelta(days=1),
    )
    action, _ = publisher.upsert("o/r", fingerprint="fp1", render=_render_static())

    assert action == "created"
    mock_repo.create_issue.assert_called_once()


def test_closed_lookback_off_by_default():
    """The recently-closed check is opt-in: a publisher constructed without
    closed_lookback must not fetch the closed listing. Guards workflows
    like the fuzzer monitor, where a recurring incident must never be
    silently suppressed."""
    mock_gh, mock_repo = _mock_gh()
    mock_repo.create_issue.return_value = _mock_issue(1)

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert("o/r", fingerprint="fp1", render=_render_static())

    assert action == "created"
    # Only the open listing is fetched; no closed listing.
    mock_repo.get_issues.assert_called_once_with(state="open")


def test_recently_closed_legacy_issue_matched_via_title_fallback():
    """A legacy issue (old or missing marker) closed within the lookback
    window is found via the exact-title fallback, so creation is suppressed
    for migration cases too."""
    legacy_closed = _mock_issue(
        11, body="no marker here", title="[TEST-FAILURE] PSYNC2 in t.tcl",
        closed_at=_recent(),
    )
    mock_gh, mock_repo = _mock_gh(closed_issues=[legacy_closed])

    publisher = IssueDedupPublisher(
        mock_gh, marker_namespace=NAMESPACE, closed_lookback=timedelta(days=1),
    )
    action, url = publisher.upsert(
        "o/r", fingerprint="newfp", render=_render_static(),
        title_fallback="[TEST-FAILURE] PSYNC2 in t.tcl",
    )

    assert action == "skipped-recently-closed"
    assert url == "https://x/issues/11"
    mock_repo.create_issue.assert_not_called()


def test_recently_closed_title_fallback_requires_exact_match():
    """A near-miss title in the closed listing must not suppress creation."""
    near_miss = _mock_issue(
        12, body="no marker", title="[TEST-FAILURE] PSYNC2 in t.tcl EXTRA",
        closed_at=_recent(),
    )
    mock_gh, mock_repo = _mock_gh(closed_issues=[near_miss])
    mock_repo.create_issue.return_value = _mock_issue(1)

    publisher = IssueDedupPublisher(
        mock_gh, marker_namespace=NAMESPACE, closed_lookback=timedelta(days=1),
    )
    action, _ = publisher.upsert(
        "o/r", fingerprint="newfp", render=_render_static(),
        title_fallback="[TEST-FAILURE] PSYNC2 in t.tcl",
    )

    assert action == "created"
    mock_repo.create_issue.assert_called_once()


def test_closed_listing_fetched_once_per_batch():
    """The closed listing, like the open one, is fetched once and reused
    across upserts on the same publisher."""
    mock_gh, mock_repo = _mock_gh()
    mock_repo.create_issue.side_effect = [_mock_issue(1), _mock_issue(2)]

    publisher = IssueDedupPublisher(
        mock_gh, marker_namespace=NAMESPACE, closed_lookback=timedelta(days=1),
    )
    publisher.upsert("o/r", fingerprint="fp1", render=_render_static())
    publisher.upsert("o/r", fingerprint="fp2", render=_render_static())

    closed_calls = [
        c for c in mock_repo.get_issues.call_args_list
        if c.kwargs.get("state") == "closed"
    ]
    assert len(closed_calls) == 1


def test_filter_label_scopes_open_listing():
    """When filter_label is set, the open listing is scoped server-side
    to only that label."""
    mock_gh, mock_repo = _mock_gh()
    mock_repo.create_issue.return_value = _mock_issue(1)

    publisher = IssueDedupPublisher(
        mock_gh, marker_namespace=NAMESPACE, filter_label="test-failure",
    )
    publisher.upsert("o/r", fingerprint="fp1", render=_render_static())

    open_call = next(
        c for c in mock_repo.get_issues.call_args_list
        if c.kwargs.get("state") == "open"
    )
    assert open_call.kwargs["labels"] == ["test-failure"]


def test_filter_label_scopes_closed_listing():
    """When filter_label is set, the closed listing is also scoped."""
    mock_gh, mock_repo = _mock_gh()
    mock_repo.create_issue.return_value = _mock_issue(1)

    publisher = IssueDedupPublisher(
        mock_gh, marker_namespace=NAMESPACE, filter_label="test-failure",
        closed_lookback=timedelta(days=1),
    )
    publisher.upsert("o/r", fingerprint="fp1", render=_render_static())

    closed_call = next(
        c for c in mock_repo.get_issues.call_args_list
        if c.kwargs.get("state") == "closed"
    )
    assert closed_call.kwargs["labels"] == ["test-failure"]


def test_no_filter_label_lists_all():
    """Without filter_label, the open listing fetches all issues."""
    mock_gh, mock_repo = _mock_gh()
    mock_repo.create_issue.return_value = _mock_issue(1)

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    publisher.upsert("o/r", fingerprint="fp1", render=_render_static())

    open_call = next(
        c for c in mock_repo.get_issues.call_args_list
        if c.kwargs.get("state") == "open"
    )
    assert "labels" not in open_call.kwargs


def test_closed_lookback_custom_duration():
    """A custom closed_lookback value produces the correct ``since`` cutoff."""
    marker = f"<!-- {NAMESPACE}:fp1 -->"
    frozen_now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
    closed_issue = _mock_issue(
        10, body=f"{marker}\nold body",
        closed_at=frozen_now - timedelta(hours=1),
    )
    mock_gh, mock_repo = _mock_gh(closed_issues=[closed_issue])

    with patch("scripts.common.issue_dedup.datetime") as mock_dt:
        mock_dt.now.return_value = frozen_now
        publisher = IssueDedupPublisher(
            mock_gh, marker_namespace=NAMESPACE, closed_lookback=timedelta(hours=6),
        )
        action, _ = publisher.upsert("o/r", fingerprint="fp1", render=_render_static())

    assert action == "skipped-recently-closed"
    closed_call = next(
        c for c in mock_repo.get_issues.call_args_list
        if c.kwargs.get("state") == "closed"
    )
    # 12:00 UTC minus 6 hours = 06:00 UTC
    assert closed_call.kwargs["since"] == datetime(
        2026, 7, 21, 6, 0, 0, tzinfo=timezone.utc,
    )
