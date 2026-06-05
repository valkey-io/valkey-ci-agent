"""Tests for fuzzer-specific issue rendering."""
from __future__ import annotations

from scripts.fuzzer.issue_renderer import (
    MARKER_NAMESPACE,
    _build_title,
    _render_body,
    _render_comment,
    render_for,
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
        suggested_labels=["possible-valkey-bug"],
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
    assert f"<!-- {MARKER_NAMESPACE}:occurrences:1 -->" in body
    assert "Node crash" in body
    assert "crash found" in body


def test_render_for_returns_callable_with_labels():
    """The factory hands IssueDedupPublisher.upsert a callable that produces
    fully-populated IssueContent including labels."""
    cb = render_for(_analysis())
    content = cb("<!-- marker -->", 3)
    assert content.title == "[fuzzer-run] Node crash"
    assert "<!-- marker -->" in content.body
    assert f"<!-- {MARKER_NAMESPACE}:occurrences:3 -->" in content.body
    assert "Occurrence #3" in content.comment
    assert content.labels == ("possible-valkey-bug",)
