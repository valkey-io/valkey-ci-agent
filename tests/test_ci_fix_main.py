"""Tests for the ci_fix workflow entry point."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from scripts.ci_fix import main as main_mod
from scripts.ci_fix.main import _parse_event, main
from scripts.ci_fix.models import FixOutcome, OutcomeKind

_RUN_URL = "https://github.com/valkey-io/valkey/actions/runs/27559908167"


def _event(*, action="created", is_pr=True, body=f"@valkeyrie-bot fix {_RUN_URL}",
           login="alice", number=3988, repo="valkey-io/valkey"):
    issue: dict = {"number": number}
    if is_pr:
        issue["pull_request"] = {"url": "..."}
    return {
        "action": action,
        "issue": issue,
        "comment": {"body": body, "user": {"login": login}},
        "repository": {"full_name": repo},
    }


def test_parse_event_happy():
    parsed = _parse_event(_event())
    assert parsed == ("valkey-io/valkey", 3988, "alice", f"@valkeyrie-bot fix {_RUN_URL}")


def test_parse_event_ignores_non_pr():
    assert _parse_event(_event(is_pr=False)) is None


def test_parse_event_ignores_non_created():
    assert _parse_event(_event(action="edited")) is None


def test_parse_event_ignores_missing_fields():
    assert _parse_event(_event(login="")) is None


def _write_event(tmp_path, event) -> str:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    return str(path)


def test_main_ignores_non_command_comment(tmp_path, monkeypatch):
    event_path = _write_event(tmp_path, _event(body="thanks, lgtm"))
    rc = main(["--event-path", event_path, "--target-token", "t"])
    assert rc == 0


def test_main_ignores_non_pr_comment(tmp_path):
    event_path = _write_event(tmp_path, _event(is_pr=False))
    rc = main(["--event-path", event_path, "--target-token", "t"])
    assert rc == 0


def test_main_runs_pipeline_and_comments(tmp_path, monkeypatch):
    event_path = _write_event(tmp_path, _event())

    pushed = FixOutcome(kind=OutcomeKind.PUSHED, summary="done", commit_sha="abc")
    fake_run = MagicMock(return_value=pushed)
    posted = {}

    monkeypatch.setattr(main_mod, "Github", MagicMock())
    monkeypatch.setattr(main_mod, "ArtifactClient", MagicMock())
    monkeypatch.setattr(main_mod, "run_ci_fix", fake_run)
    monkeypatch.setattr(main_mod, "_post_comment",
                        lambda gh, repo, num, body: posted.update(repo=repo, num=num, body=body))

    rc = main(["--event-path", event_path, "--target-token", "tok"])
    assert rc == 0
    assert fake_run.called
    assert posted["repo"] == "valkey-io/valkey"
    assert posted["num"] == 3988


def test_main_runs_dispatch_request_and_comments(monkeypatch):
    pushed = FixOutcome(kind=OutcomeKind.PUSHED, summary="done", commit_sha="abc")
    fake_run = MagicMock(return_value=pushed)
    posted = {}

    monkeypatch.setattr(main_mod, "Github", MagicMock())
    monkeypatch.setattr(main_mod, "ArtifactClient", MagicMock())
    monkeypatch.setattr(main_mod, "run_ci_fix", fake_run)
    monkeypatch.setattr(main_mod, "_post_comment",
                        lambda gh, repo, num, body: posted.update(repo=repo, num=num, body=body))

    rc = main([
        "--target-token", "tok",
        "--repo", "valkey-io/valkey",
        "--pr", "3988",
        "--run-url", _RUN_URL,
        "--commenter", "alice",
        "--hint", "look at payload",
    ])
    assert rc == 0
    assert fake_run.call_args.kwargs["commenter"] == "alice"
    assert fake_run.call_args.kwargs["command"].hint == "look at payload"
    assert posted["repo"] == "valkey-io/valkey"
    assert posted["num"] == 3988


def test_main_returns_nonzero_on_failed_outcome(tmp_path, monkeypatch):
    event_path = _write_event(tmp_path, _event())
    failed = FixOutcome(kind=OutcomeKind.FAILED, summary="clone failed")

    monkeypatch.setattr(main_mod, "Github", MagicMock())
    monkeypatch.setattr(main_mod, "ArtifactClient", MagicMock())
    monkeypatch.setattr(main_mod, "run_ci_fix", MagicMock(return_value=failed))
    monkeypatch.setattr(main_mod, "_post_comment", lambda *a, **k: None)

    rc = main(["--event-path", event_path, "--target-token", "tok"])
    assert rc == 1


def test_main_unexpected_error_still_posts_comment(tmp_path, monkeypatch):
    """An unexpected pipeline exception must become a FAILED comment, not a crash."""
    event_path = _write_event(tmp_path, _event())
    posted = {}

    monkeypatch.setattr(main_mod, "Github", MagicMock())
    monkeypatch.setattr(main_mod, "ArtifactClient", MagicMock())
    monkeypatch.setattr(main_mod, "run_ci_fix",
                        MagicMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(main_mod, "_post_comment",
                        lambda gh, repo, num, body: posted.update(body=body))

    rc = main(["--event-path", event_path, "--target-token", "tok"])
    assert rc == 1
    assert "internal error" in posted["body"].lower()
    # The raw exception text must not leak into the public comment.
    assert "boom" not in posted["body"]


def test_verify_runs_env_parsing(monkeypatch):
    from scripts.ci_fix.main import _MAX_VERIFY_RUNS, _verify_runs
    from scripts.ci_fix.review import DEFAULT_VERIFY_RUNS

    monkeypatch.delenv("CI_FIX_VERIFY_RUNS", raising=False)
    assert _verify_runs() == DEFAULT_VERIFY_RUNS

    monkeypatch.setenv("CI_FIX_VERIFY_RUNS", "5")
    assert _verify_runs() == 5

    monkeypatch.setenv("CI_FIX_VERIFY_RUNS", "0")
    assert _verify_runs() == 1

    monkeypatch.setenv("CI_FIX_VERIFY_RUNS", "999")
    assert _verify_runs() == _MAX_VERIFY_RUNS

    monkeypatch.setenv("CI_FIX_VERIFY_RUNS", "not-a-number")
    assert _verify_runs() == DEFAULT_VERIFY_RUNS
