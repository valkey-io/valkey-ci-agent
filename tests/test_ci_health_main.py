"""Tests for the ``scripts.ci_health.main`` command line."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import pytest

from scripts.ci_health.main import build_parser, main
from scripts.ci_health.store import Cache


class _Client:
    def __init__(self, token: str, repo: str) -> None:
        self.token = token
        self.repo = repo
        self.requests_made = 0
        self.runs = {"daily.yml": [{
            "id": 1, "path": ".github/workflows/daily.yml", "conclusion": "failure", "status": "completed",
            "head_branch": "unstable", "head_sha": "abc", "run_started_at": "2026-01-15T08:00:00Z",
            "created_at": "2026-01-15T08:00:00Z", "updated_at": "2026-01-15T09:00:00Z",
        }]}

    def list_runs(self, workflow_file: str, **kwargs) -> List[Dict]:
        self.requests_made += 1
        return self.runs.get(workflow_file, [])

    def list_jobs(self, run_id: int) -> List[Dict]:
        self.requests_made += 1
        return [{"id": 10, "name": "test-ubuntu", "status": "completed", "conclusion": "failure"}]

    def job_log(self, job_id: int) -> str:
        self.requests_made += 1
        return "[err]: some_test in tests/unit/type/string.tcl\nExpected OK\n"

    def compare_commits(self, base_sha: str, head_sha: str) -> List[Dict]:
        return []


class _RefusingClient(_Client):
    def list_runs(self, workflow_file: str, **kwargs) -> List[Dict]:
        from github import GithubException

        raise GithubException(401, {"message": "Bad credentials"})


def test_parser_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_sync_fills_the_store_and_prints_a_summary(tmp_path: Path, capsys) -> None:
    db = tmp_path / "nested" / "history.sqlite"
    made: List[_Client] = []

    def factory(token: str, repo: str) -> _Client:
        client = _Client(token, repo)
        made.append(client)
        return client

    rc = main(["--db", str(db), "--token", "t", "--repo", "valkey-io/valkey", "sync", "--workflow", "daily"],
              make_client=factory)
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["new_runs_fetched"] == 1 and summary["new_failures_parsed"] >= 1
    assert "weekly" not in summary  # --workflow daily skips the weekly split
    assert made[0].token == "t" and made[0].repo == "valkey-io/valkey"
    # The store was closed cleanly: no write-ahead log left beside the file.
    assert not (tmp_path / "nested" / "history.sqlite-wal").exists()
    reopened = Cache(str(db))
    assert reopened.query_runs(workflow="daily.yml")
    reopened.close()


def test_sync_without_workflow_filter_runs_the_weekly_split(tmp_path: Path, capsys) -> None:
    rc = main(["--db", str(tmp_path / "h.sqlite"), "--token", "t", "sync", "--weekly-budget", "5"],
              make_client=_Client)
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["weekly"]["budget_used"] <= 5


def test_sync_exits_non_zero_when_the_token_is_refused(tmp_path: Path, capsys) -> None:
    rc = main(["--db", str(tmp_path / "h.sqlite"), "--token", "t", "sync", "--workflow", "daily"],
              make_client=_RefusingClient)
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["auth_failed"]


def test_scorecard_and_blame_write_json_files(tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "h.sqlite")
    assert main(["--db", db, "--token", "t", "sync", "--workflow", "daily"], make_client=_Client) == 0
    capsys.readouterr()

    out = tmp_path / "report" / "scorecards.json"
    assert main(["--db", db, "scorecard", "--days", "60", "-o", str(out)], make_client=_Client) == 0
    data = json.loads(out.read_text())
    assert [card["test_name"] for card in data["scorecards"]] == ["some_test in tests/unit/type/string.tcl"]

    blame_out = tmp_path / "report" / "blame.json"
    assert main(["--db", db, "blame", "-o", str(blame_out)], make_client=_Client) == 0
    assert isinstance(json.loads(blame_out.read_text()), list)


def test_scorecard_to_stdout_without_a_token(tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "h.sqlite")
    Cache(db).close()
    assert main(["--db", db, "scorecard"], make_client=_Client) == 0
    assert isinstance(json.loads(capsys.readouterr().out), dict)
