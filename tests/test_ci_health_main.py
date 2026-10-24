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
        self.listings: List[str] = []
        self.runs = {"daily.yml": [{
            "id": 1, "path": ".github/workflows/daily.yml", "conclusion": "failure", "status": "completed",
            "head_branch": "unstable", "head_sha": "abc", "run_started_at": "2026-01-15T08:00:00Z",
            "created_at": "2026-01-15T08:00:00Z", "updated_at": "2026-01-15T09:00:00Z",
        }]}

    def list_runs(self, workflow_file: str, **kwargs) -> List[Dict]:
        self.requests_made += 1
        self.listings.append(workflow_file)
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


def test_sync_without_workflow_filter_runs_the_weekly_split_without_relisting(tmp_path: Path, capsys) -> None:
    made: List[_Client] = []

    def factory(token: str, repo: str) -> _Client:
        client = _Client(token, repo)
        made.append(client)
        return client

    rc = main(["--db", str(tmp_path / "h.sqlite"), "--token", "t", "sync", "--weekly-budget", "5"],
              make_client=factory)
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["weekly"]["budget_used"] <= 5
    # Three workflows listed once each; the weekly split did not list again.
    assert made[0].listings == ["daily.yml", "weekly.yml", "ci.yml"]


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


def test_report_writes_the_site_and_passes_when_daily_is_fresh(tmp_path: Path, capsys) -> None:
    from datetime import datetime, timezone

    db = str(tmp_path / "h.sqlite")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    class _FreshClient(_Client):
        def __init__(self, token: str, repo: str) -> None:
            super().__init__(token, repo)
            run = self.runs["daily.yml"][0]
            run["run_started_at"] = run["created_at"] = f"{today}T00:10:00Z"
            run["updated_at"] = f"{today}T05:00:00Z"

    assert main(["--db", db, "--token", "t", "sync", "--workflow", "daily"], make_client=_FreshClient) == 0
    capsys.readouterr()
    site = tmp_path / "site"
    assert main(["--db", db, "report", "--site", str(site), "--no-ci"], make_client=_FreshClient) == 0
    names = sorted(p.name for p in site.iterdir())
    assert names == ["blame.json", "index.html", "releases.html", "report-releases.json",
                     "report.json", "report.md", "scorecards.json"]
    page = (site / "index.html").read_text()
    assert "some_test" in page and "<script>" in page  # rendered with the bundled assets
    assert "report-ci.json" not in names


def test_report_fails_when_the_newest_daily_run_is_stale(tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "h.sqlite")
    # _Client's one run is dated 2026-01-15, long past any two-day window.
    assert main(["--db", db, "--token", "t", "sync", "--workflow", "daily"], make_client=_Client) == 0
    capsys.readouterr()
    site = tmp_path / "site"
    assert main(["--db", db, "report", "--site", str(site)], make_client=_Client) == 1
    # The pages are still written, so a stale night can be inspected; only the exit code refuses.
    assert (site / "index.html").exists() and (site / "report-ci.json").exists()


def test_slack_dry_run_prints_the_message_and_posts_nothing(tmp_path: Path, capsys, monkeypatch) -> None:
    from scripts.ci_health import main as main_mod

    db = str(tmp_path / "h.sqlite")
    assert main(["--db", db, "--token", "t", "sync", "--workflow", "daily"], make_client=_Client) == 0
    capsys.readouterr()
    monkeypatch.setattr(main_mod, "post", lambda *a, **k: pytest.fail("posted during --dry-run"))
    issues = tmp_path / "issues.json"
    from scripts.ci_health.fingerprint import fingerprint_for_name

    fp = fingerprint_for_name("some_test in tests/unit/type/string.tcl")
    issues.write_text(json.dumps({fp: "https://github.com/valkey-io/valkey/issues/1"}))
    rc = main(["--db", db, "slack", "--dry-run", "--days", "400", "--dashboard-url", "https://d/", "--issues", str(issues)],
              make_client=_Client)
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("🔴 *Daily CI failed*")
    assert f"<https://github.com/valkey-io/valkey/issues/1|issue> · <https://d/#test={fp}|↗>" in out


def test_slack_posts_to_the_webhook(tmp_path: Path, capsys, monkeypatch) -> None:
    from scripts.ci_health import main as main_mod

    db = str(tmp_path / "h.sqlite")
    assert main(["--db", db, "--token", "t", "sync", "--workflow", "daily"], make_client=_Client) == 0
    posted = []
    monkeypatch.setattr(main_mod, "post", lambda message, url: posted.append((message.failed, url)))
    assert main(["--db", db, "slack", "--days", "400", "--webhook-url", "https://hooks.test/x"], make_client=_Client) == 0
    assert posted == [(True, "https://hooks.test/x")]


def test_slack_without_a_webhook_fails(tmp_path: Path, capsys, monkeypatch) -> None:
    db = str(tmp_path / "h.sqlite")
    assert main(["--db", db, "--token", "t", "sync", "--workflow", "daily"], make_client=_Client) == 0
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert main(["--db", db, "slack", "--days", "400"], make_client=_Client) == 1
