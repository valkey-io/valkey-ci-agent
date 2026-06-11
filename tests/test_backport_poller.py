from __future__ import annotations

from types import SimpleNamespace

from scripts.backport import poller
from scripts.backport.registry import load_registry
from scripts.backport.sweep_models import BranchSweepResult, CandidateResult


def _registry(tmp_path) -> str:
    path = tmp_path / "repos.yml"
    path.write_text(
        """
repos:
  - repo: org/core
    project_owner: org
    project_owner_type: organization
    language: c
    build_commands:
      - make test
    branches:
      - branch: "1.0"
        project_number: 1
""",
        encoding="utf-8",
    )
    return str(path)


def _branch(tmp_path):
    registry = load_registry(_registry(tmp_path))
    return registry.get_branch("org/core", "1.0")


def test_poll_branch_skips_when_open_pr_exists(monkeypatch, tmp_path):
    repo_entry, branch_entry = _branch(tmp_path)

    monkeypatch.setattr(poller, "Github", lambda *a, **k: object())
    existing = SimpleNamespace(number=42, html_url="https://example/pr/42")
    monkeypatch.setattr(poller, "find_existing_pr", lambda *a, **k: existing)

    def _must_not_run(*a, **k):
        raise AssertionError("run_backport_sweep called despite open PR")

    monkeypatch.setattr(poller, "run_backport_sweep", _must_not_run)

    result = poller.poll_branch(
        repo_entry=repo_entry,
        branch_entry=branch_entry,
        github_token="token",
    )

    assert result["action"] == "skipped-open-pr"
    assert result["pr"] == "https://example/pr/42"
    assert result["branch"] == "1.0"


def test_poll_branch_sweeps_when_no_open_pr(monkeypatch, tmp_path):
    repo_entry, branch_entry = _branch(tmp_path)

    monkeypatch.setattr(poller, "Github", lambda *a, **k: object())
    monkeypatch.setattr(poller, "find_existing_pr", lambda *a, **k: None)

    captured = {}

    def _fake_sweep(*, repo_entry, branch_entry, github_token, max_candidates):
        captured["max_candidates"] = max_candidates
        return BranchSweepResult(
            target_branch=branch_entry.branch,
            candidates_found=3,
            results=[
                CandidateResult(1, "first", "applied"),
                CandidateResult(2, "second", "skipped-conflict"),
            ],
            pr_url="https://example/pr/99",
        )

    monkeypatch.setattr(poller, "run_backport_sweep", _fake_sweep)

    result = poller.poll_branch(
        repo_entry=repo_entry,
        branch_entry=branch_entry,
        github_token="token",
        max_candidates=2,
    )

    assert result["action"] == "swept"
    assert result["found"] == 3
    assert result["applied"] == 1
    assert result["pr"] == "https://example/pr/99"
    assert not result["error"]
    assert captured["max_candidates"] == 2


def test_poll_branch_degrades_when_pr_check_fails(monkeypatch, tmp_path):
    repo_entry, branch_entry = _branch(tmp_path)

    monkeypatch.setattr(poller, "Github", lambda *a, **k: object())

    def _boom(*a, **k):
        raise RuntimeError("github api down")

    monkeypatch.setattr(poller, "find_existing_pr", _boom)

    def _must_not_run(*a, **k):
        raise AssertionError("run_backport_sweep called after failed PR check")

    monkeypatch.setattr(poller, "run_backport_sweep", _must_not_run)

    result = poller.poll_branch(
        repo_entry=repo_entry,
        branch_entry=branch_entry,
        github_token="token",
    )

    assert result["action"] == "error"
    assert result["error"] == "github api down"
    assert result["branch"] == "1.0"


def test_poll_branch_dry_run_reports_would_sweep(monkeypatch, tmp_path):
    repo_entry, branch_entry = _branch(tmp_path)

    monkeypatch.setattr(poller, "Github", lambda *a, **k: object())
    monkeypatch.setattr(poller, "find_existing_pr", lambda *a, **k: None)

    def _must_not_run(*a, **k):
        raise AssertionError("run_backport_sweep called during dry run")

    monkeypatch.setattr(poller, "run_backport_sweep", _must_not_run)

    result = poller.poll_branch(
        repo_entry=repo_entry,
        branch_entry=branch_entry,
        github_token="token",
        dry_run=True,
    )

    assert result["action"] == "would-sweep"
    assert result["branch"] == "1.0"


def test_poll_branch_passes_max_candidates_through(monkeypatch, tmp_path):
    repo_entry, branch_entry = _branch(tmp_path)

    monkeypatch.setattr(poller, "Github", lambda *a, **k: object())
    monkeypatch.setattr(poller, "find_existing_pr", lambda *a, **k: None)

    captured = {}

    def _fake_sweep(*, repo_entry, branch_entry, github_token, max_candidates):
        captured["max_candidates"] = max_candidates
        return BranchSweepResult(
            target_branch=branch_entry.branch,
            candidates_found=0,
        )

    monkeypatch.setattr(poller, "run_backport_sweep", _fake_sweep)

    poller.poll_branch(
        repo_entry=repo_entry,
        branch_entry=branch_entry,
        github_token="token",
        max_candidates=5,
    )

    assert captured["max_candidates"] == 5
