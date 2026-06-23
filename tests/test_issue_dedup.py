"""Tests for the generic marker-based issue dedup publisher."""
from __future__ import annotations

from unittest.mock import MagicMock

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


def test_creates_new_issue_when_search_returns_nothing():
    mock_repo = MagicMock()
    mock_issue = MagicMock(number=1, html_url="https://x/issues/1")
    mock_repo.create_issue.return_value = mock_issue
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_gh.search_issues.return_value = iter([])

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert("o/r", fingerprint="fp1", render=_render_static())
    assert action == "created"
    mock_repo.create_issue.assert_called_once()


def test_create_applies_labels():
    mock_repo = MagicMock()
    mock_issue = MagicMock(number=1, html_url="https://x/issues/1")
    mock_repo.create_issue.return_value = mock_issue
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_gh.search_issues.return_value = iter([])

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    publisher.upsert("o/r", fingerprint="fp1",
                     render=_render_static(labels=("possible-bug", "fuzzer")))
    mock_issue.add_to_labels.assert_called_once_with("possible-bug", "fuzzer")


def test_updates_existing_increments_occurrence():
    marker = f"<!-- {NAMESPACE}:fp1 -->"
    existing = MagicMock(
        number=5, html_url="https://x/issues/5",
        body=f"{marker}\n<!-- {NAMESPACE}:occurrences:1 -->",
        title="old",
    )
    mock_repo = MagicMock()
    mock_repo.get_issue.return_value = existing
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_gh.search_issues.return_value = [existing]

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert("o/r", fingerprint="fp1", render=_render_static())
    assert action == "updated"
    edited_body = existing.edit.call_args.kwargs["body"]
    assert f"<!-- {NAMESPACE}:occurrences:2 -->" in edited_body
    existing.create_comment.assert_called_once()


def test_updates_reinjects_missing_marker():
    """If the loaded body is None or stripped of the marker, re-inject it
    so future runs continue to dedupe against this issue."""
    marker = f"<!-- {NAMESPACE}:fp1 -->"
    loaded = MagicMock(number=5, html_url="https://x/issues/5", body=None, title="old")
    mock_repo = MagicMock()
    mock_repo.get_issue.return_value = loaded
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    search_result = MagicMock(number=5, body=f"{marker}\n")
    mock_gh.search_issues.return_value = [search_result]

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    publisher.upsert("o/r", fingerprint="fp1", render=_render_static())
    edited_body = loaded.edit.call_args.kwargs["body"]
    assert marker in edited_body
    assert f"<!-- {NAMESPACE}:occurrences:2 -->" in edited_body


def test_search_failure_propagates_no_duplicate_issue():
    """A transient GitHub search failure must NOT silently fall through to
    create_issue, which would generate duplicate issues until search recovered."""
    mock_repo = MagicMock()
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_gh.search_issues.side_effect = RuntimeError("rate limited")

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    with pytest.raises(RuntimeError, match="rate limited"):
        publisher.upsert("o/r", fingerprint="fp1", render=_render_static())
    mock_repo.create_issue.assert_not_called()


def test_body_transform_applied_on_update():
    """``body_transform`` runs on the update path and its output feeds the
    marker/occurrence machinery, so callers can carry state forward (e.g.
    merging environments) into the edited body."""
    marker = f"<!-- {NAMESPACE}:fp1 -->"
    existing = MagicMock(
        number=5, html_url="https://x/issues/5",
        body=f"{marker}\n<!-- {NAMESPACE}:occurrences:1 -->\nEnvs: old",
        title="old",
    )
    mock_repo = MagicMock()
    mock_repo.get_issue.return_value = existing
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_gh.search_issues.return_value = [existing]

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
    mock_repo = MagicMock()
    mock_issue = MagicMock(number=1, html_url="https://x/issues/1")
    mock_repo.create_issue.return_value = mock_issue
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_gh.search_issues.return_value = iter([])

    sentinel = MagicMock(side_effect=AssertionError("transform ran on create"))
    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert(
        "o/r", fingerprint="fp1", render=_render_static(), body_transform=sentinel,
    )
    assert action == "created"
    sentinel.assert_not_called()


def test_idempotency_key_recorded_on_create():
    """When idempotency_key is supplied, the new issue body records it."""
    mock_repo = MagicMock()
    mock_issue = MagicMock(number=1, html_url="https://x/issues/1")
    mock_repo.create_issue.return_value = mock_issue
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_gh.search_issues.return_value = iter([])

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
    existing = MagicMock(number=5, html_url="https://x/issues/5", body=body, title="old")
    mock_repo = MagicMock()
    mock_repo.get_issue.return_value = existing
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_gh.search_issues.return_value = [existing]

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert("o/r", fingerprint="fp1",
                                 render=_render_static(), idempotency_key="run-42")
    assert action == "skipped-duplicate"
    existing.edit.assert_not_called()
    existing.create_comment.assert_not_called()


def test_title_fallback_adopts_legacy_issue_and_restamps_marker():
    """When the marker search misses but an open issue has the exact fallback
    title, adopt it (migration off an older fingerprint) and re-stamp the body
    with the current marker so future runs dedupe on the marker."""
    new_marker = f"<!-- {NAMESPACE}:newfp -->"
    # Legacy issue: created under an old fingerprint, so its body carries a
    # different marker and the marker search returns nothing.
    legacy = MagicMock(
        number=7, html_url="https://x/issues/7",
        body=f"<!-- {NAMESPACE}:OLD raw::name -->\n<!-- {NAMESPACE}:occurrences:1 -->",
        title="[TEST-FAILURE] PSYNC2 in t.tcl",
    )
    mock_repo = MagicMock()
    mock_repo.get_issue.return_value = legacy
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    # First search (by marker) misses; second (by title) finds the legacy issue.
    mock_gh.search_issues.side_effect = [iter([]), [legacy]]

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
    """A near-miss title (search returned a noisy candidate) must not be
    adopted; only an exact, case-sensitive title equals counts."""
    candidate = MagicMock(number=8, body="x", title="[TEST-FAILURE] PSYNC2 in t.tcl EXTRA")
    mock_repo = MagicMock()
    mock_issue = MagicMock(number=1, html_url="https://x/issues/1")
    mock_repo.create_issue.return_value = mock_issue
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_gh.search_issues.side_effect = [iter([]), [candidate]]

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert(
        "o/r", fingerprint="newfp", render=_render_static(),
        title_fallback="[TEST-FAILURE] PSYNC2 in t.tcl",
    )
    # No exact match -> falls through to create, not adopt.
    assert action == "created"
    mock_repo.create_issue.assert_called_once()


def test_title_fallback_not_consulted_when_marker_matches():
    """If the marker search already found the issue, the title fallback search
    must not run at all (avoids a needless second API call)."""
    marker = f"<!-- {NAMESPACE}:fp1 -->"
    existing = MagicMock(
        number=5, html_url="https://x/issues/5",
        body=f"{marker}\n<!-- {NAMESPACE}:occurrences:1 -->", title="old",
    )
    mock_repo = MagicMock()
    mock_repo.get_issue.return_value = existing
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_gh.search_issues.return_value = [existing]

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert(
        "o/r", fingerprint="fp1", render=_render_static(), title_fallback="anything",
    )
    assert action == "updated"
    # Exactly one search performed (marker), no fallback-by-title search.
    assert mock_gh.search_issues.call_count == 1


def test_title_fallback_query_tokenizes_unsafe_title():
    """The fallback search query is built from word tokens, never raw title
    text, so quotes/colons in the title can't break search syntax."""
    captured = {}

    def _capture(query):
        captured.setdefault("queries", []).append(query)
        return iter([])

    mock_repo = MagicMock()
    mock_repo.create_issue.return_value = MagicMock(number=1, html_url="https://x/issues/1")
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_gh.search_issues.side_effect = _capture

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    publisher.upsert(
        "o/r", fingerprint="newfp", render=_render_static(),
        title_fallback='[TEST-FAILURE] "evil" --> in: a:b.tcl',
    )
    title_query = captured["queries"][1]  # [0] is the marker search
    assert '"' not in title_query
    assert "-->" not in title_query
    assert "in:title" in title_query


def test_idempotency_key_different_value_still_updates():
    """A different idempotency_key (different source event) bumps as usual,
    and the new key replaces the old one in the body.
    """
    marker = f"<!-- {NAMESPACE}:fp1 -->"
    body = (
        f"{marker}\n<!-- {NAMESPACE}:occurrences:1 -->\n"
        f"<!-- {NAMESPACE}:last-key:run-42 -->"
    )
    existing = MagicMock(number=5, html_url="https://x/issues/5", body=body, title="old")
    mock_repo = MagicMock()
    mock_repo.get_issue.return_value = existing
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_gh.search_issues.return_value = [existing]

    publisher = IssueDedupPublisher(mock_gh, marker_namespace=NAMESPACE)
    action, _ = publisher.upsert("o/r", fingerprint="fp1",
                                 render=_render_static(), idempotency_key="run-99")
    assert action == "updated"
    edited = existing.edit.call_args.kwargs["body"]
    assert f"<!-- {NAMESPACE}:occurrences:2 -->" in edited
    assert f"<!-- {NAMESPACE}:last-key:run-99 -->" in edited
    assert f"<!-- {NAMESPACE}:last-key:run-42 -->" not in edited
