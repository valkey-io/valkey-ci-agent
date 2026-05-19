"""Tests for fuzzer issue publisher."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scripts.fuzzer.issue_publisher import (
    FuzzerIssuePublisher,
    _build_title,
    _render_body,
)
from scripts.fuzzer.models import FuzzerRunAnalysis, FuzzerSignal


def _analysis(**kw) -> FuzzerRunAnalysis:
    defaults = dict(
        repo="valkey-io/valkey-fuzzer", workflow_file="fuzzer-run.yml",
        run_id=100, run_url="https://github.com/r/actions/runs/100",
        conclusion="failure", head_sha="abc", overall_status="anomalous",
        triage_verdict="likely-core-valkey-bug", summary="crash found",
        anomalies=[FuzzerSignal("Node crash", "critical", "segfault")],
        incident_fingerprint="fp_test_12345678901",
    )
    defaults.update(kw)
    return FuzzerRunAnalysis(**defaults)


def test_build_title_from_root_cause():
    assert _build_title(_analysis(root_cause_category="split-brain")) == "[fuzzer-run] Split Brain"


def test_build_title_from_anomaly():
    assert _build_title(_analysis()) == "[fuzzer-run] Node crash"


def test_render_body_contains_essentials():
    body = _render_body(_analysis(), "<!-- marker -->", occurrences=1)
    assert "<!-- marker -->" in body
    assert "occurrences:1" in body
    assert "Node crash" in body
    assert "crash found" in body


def test_creates_new_issue_when_search_returns_nothing():
    mock_repo = MagicMock()
    mock_issue = MagicMock(number=1, html_url="https://x/issues/1")
    mock_repo.create_issue.return_value = mock_issue
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_gh.search_issues.return_value = iter([])

    action, _ = FuzzerIssuePublisher(mock_gh).upsert_issue(
        "valkey-io/valkey-fuzzer", _analysis(),
    )
    assert action == "created"


def test_updates_existing_issue_on_search_hit():
    marker = "<!-- valkey-ci-agent:fuzzer-issue:fp_test_12345678901 -->"
    existing = MagicMock(
        number=5, html_url="https://x/issues/5",
        body=f"{marker}\n<!-- valkey-ci-agent:occurrences:1 -->",
        title="[fuzzer-run] old",
    )
    mock_repo = MagicMock()
    mock_repo.get_issue.return_value = existing
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_gh.search_issues.return_value = [existing]

    action, _ = FuzzerIssuePublisher(mock_gh).upsert_issue(
        "valkey-io/valkey-fuzzer", _analysis(),
    )
    assert action == "updated"
    existing.edit.assert_called_once()
    existing.create_comment.assert_called_once()


def test_updates_existing_reinjects_missing_marker():
    """If the loaded body is None or has been stripped of the marker,
    re-inject it so future runs continue to dedupe against this issue."""
    marker = "<!-- valkey-ci-agent:fuzzer-issue:fp_test_12345678901 -->"
    loaded = MagicMock(
        number=5, html_url="https://x/issues/5",
        body=None, title="[fuzzer-run] old",
    )
    mock_repo = MagicMock()
    mock_repo.get_issue.return_value = loaded
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    search_result = MagicMock(number=5, body=f"{marker}\n")
    mock_gh.search_issues.return_value = [search_result]

    action, _ = FuzzerIssuePublisher(mock_gh).upsert_issue(
        "valkey-io/valkey-fuzzer", _analysis(),
    )
    assert action == "updated"
    edited_body = loaded.edit.call_args.kwargs["body"]
    assert marker in edited_body
    assert "<!-- valkey-ci-agent:occurrences:2 -->" in edited_body


def test_search_failure_propagates_no_duplicate_issue():
    """A transient GitHub search failure must NOT silently fall through to
    create_issue — that would generate duplicate issues on every cron run
    until the search recovered. The error propagates and the surrounding
    main.py loop records this run as 'error' instead."""
    mock_repo = MagicMock()
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_gh.search_issues.side_effect = RuntimeError("rate limited")

    with pytest.raises(RuntimeError, match="rate limited"):
        FuzzerIssuePublisher(mock_gh).upsert_issue(
            "valkey-io/valkey-fuzzer", _analysis(),
        )
    mock_repo.create_issue.assert_not_called()
