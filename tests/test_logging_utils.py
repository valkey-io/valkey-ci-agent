"""Tests for the shared log format, run annotations, and groups."""

from __future__ import annotations

import logging

from scripts.common import logging_utils
from scripts.common.logging_utils import _TagFormatter, log_group, log_outcome


def _record(path: str) -> logging.LogRecord:
    return logging.LogRecord("x", logging.INFO, path, 1, "hello %s", ("world",), None)


def test_records_are_tagged_by_feature_area_not_module_path():
    fmt = _TagFormatter("[%(tag)s] %(message)s")
    assert fmt.format(_record("/w/scripts/release/reconcile.py")) == "[release] hello world"
    assert fmt.format(_record("/w/scripts/agent_fix/main.py")) == "[agent-fix] hello world"
    # Shared helpers act for their caller, so the file is the useful tag.
    assert fmt.format(_record("/w/scripts/common/proc.py")) == "[proc] hello world"


def test_outcome_is_annotated_only_in_actions(monkeypatch, capsys, caplog):
    log = logging.getLogger("test.outcome")
    caplog.set_level(logging.INFO, logger="test.outcome")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    log_outcome(log, logging.INFO, "done %d", 1)
    assert capsys.readouterr().err == ""

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    log_outcome(log, logging.WARNING, "half %s", "done")
    log_outcome(log, logging.ERROR, "failed")
    log_outcome(log, logging.INFO, "ok")
    err = capsys.readouterr().err.splitlines()
    assert err == ["::warning::half done", "::error::failed", "::notice::ok"]
    assert [r.getMessage() for r in caplog.records][-3:] == ["half done", "failed", "ok"]


def test_outcome_cannot_forge_a_second_workflow_command(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    log_outcome(logging.getLogger("t"), logging.INFO, "%s", "a\n::error::forged 100%")
    assert capsys.readouterr().err.splitlines() == ["::notice::a ::error::forged 100%25"]


def test_annotate_escapes_command_data_for_direct_callers(monkeypatch, capsys):
    from scripts.common.logging_utils import annotate

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    annotate(logging.ERROR, "a\r\n::error::forged 100%")
    assert capsys.readouterr().err.splitlines() == ["::error::a%0D%0A::error::forged 100%25"]


def test_groups_do_not_nest(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with log_group("outer"), log_group("inner"):
        pass
    assert capsys.readouterr().err.splitlines() == ["::group::outer", "::endgroup::"]
    assert logging_utils._group_open is False


def test_group_closes_when_the_body_raises(monkeypatch, capsys):
    import pytest

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(RuntimeError), log_group("work"):
        raise RuntimeError("boom")
    assert capsys.readouterr().err.splitlines()[-1] == "::endgroup::"
    assert logging_utils._group_open is False


def test_outcome_log_line_cannot_forge_a_workflow_command(monkeypatch, capsys, caplog):
    """The raw log line is printed before the annotation; it must be one line too."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    caplog.set_level(logging.INFO, logger="t.forge")
    log_outcome(logging.getLogger("t.forge"), logging.INFO, "PR %s", "x\n::warning::forged")
    assert caplog.records[-1].getMessage() == "PR x ::warning::forged"
    assert capsys.readouterr().err.splitlines() == ["::notice::PR x ::warning::forged"]


def test_outcome_with_a_literal_percent_and_no_args(caplog):
    caplog.set_level(logging.INFO, logger="t.pct")
    log_outcome(logging.getLogger("t.pct"), logging.INFO, "100% done")
    assert caplog.records[-1].getMessage() == "100% done"
