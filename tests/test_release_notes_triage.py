"""Tests for the AI triage of label-less PRs, with a faked run_fn."""

from __future__ import annotations

import json

from scripts.release_notes.models import MergedPR
from scripts.release_notes.triage import build_prompt, triage


def _pr(number: int, author: str = "alice", body: str = "", sha: str = "") -> MergedPR:
    return MergedPR(number=number, title=f"PR {number}", author=author, url=f"https://x/{number}",
                    body=body, labels=(), merge_commit_sha=sha)


def _stream(obj: dict) -> str:
    """Wrap a JSON object in a stream-json 'result' event, like the claude CLI."""
    return json.dumps({"type": "result", "result": json.dumps(obj)})


def _fake_run(obj, *, exit_code: int = 0):
    """Build a run_fn that returns the given object as stream-json output."""
    def _run(prompt, **kwargs):
        return _stream(obj), "", exit_code
    return _run


def _fake_run_raw(text: str, *, exit_code: int = 0):
    """Build a run_fn that returns arbitrary (possibly unparseable) text."""
    def _run(prompt, **kwargs):
        return text, "", exit_code
    return _run


class TestBuildPrompt:
    def test_lists_pr_numbers_and_asks_for_verdicts(self) -> None:
        prompt = build_prompt([_pr(40), _pr(41)])
        assert "40" in prompt and "41" in prompt
        assert '"verdicts"' in prompt
        assert '"include"' in prompt

    def test_inlines_diff_when_supplied(self) -> None:
        prompt = build_prompt([_pr(40)], diffs={40: "DIFFSTAT+PATCH"})
        assert '"diff": "DIFFSTAT+PATCH"' in prompt

    def test_omits_diff_field_when_absent_or_empty(self) -> None:
        prompt = build_prompt([_pr(40), _pr(41)], diffs={41: ""})
        payload = prompt.split("## Pull requests", 1)[1]
        assert '"diff"' not in payload

    def test_prompt_treats_pr_text_as_untrusted(self) -> None:
        prompt = build_prompt([_pr(1)])
        assert "untrusted" in prompt.lower()


class TestTriage:
    def test_empty_input_returns_empty_result(self) -> None:
        result = triage([], repo_dir="/tmp", run_fn=_fake_run({"verdicts": []}))
        assert result.included == () and result.excluded == () and result.undecided == ()

    def test_partitions_include_and_exclude(self) -> None:
        prs = [_pr(1), _pr(2)]
        run = _fake_run({"verdicts": [
            {"pr": 1, "include": True, "reason": "adds a command"},
            {"pr": 2, "include": False, "reason": "test-only"},
        ]})
        result = triage(prs, repo_dir="/tmp", run_fn=run)
        assert [(d.pr_number, d.reason) for d in result.included] == [(1, "adds a command")]
        assert [(d.pr_number, d.reason) for d in result.excluded] == [(2, "test-only")]
        assert result.undecided == ()

    def test_uncertain_flag_carried_through(self) -> None:
        run = _fake_run({"verdicts": [{"pr": 1, "include": True, "uncertain": True}]})
        result = triage([_pr(1)], repo_dir="/tmp", run_fn=run)
        assert result.included[0].uncertain is True

    def test_unknown_pr_number_dropped(self) -> None:
        # A verdict for a PR not in the batch is dropped; that PR ends up undecided.
        run = _fake_run({"verdicts": [{"pr": 999, "include": True}]})
        result = triage([_pr(1)], repo_dir="/tmp", run_fn=run)
        assert result.included == () and result.excluded == ()
        assert result.undecided == (1,)

    def test_missing_verdict_leaves_pr_undecided(self) -> None:
        # The model returns a verdict for one PR but not the other; the omitted one
        # is undecided, never silently included or dropped.
        run = _fake_run({"verdicts": [{"pr": 1, "include": True}]})
        result = triage([_pr(1), _pr(2)], repo_dir="/tmp", run_fn=run)
        assert [d.pr_number for d in result.included] == [1]
        assert result.undecided == (2,)

    def test_non_boolean_include_is_undecided(self) -> None:
        # A verdict whose "include" is not a real bool is not guessed either way.
        run = _fake_run({"verdicts": [{"pr": 1, "include": "yes"}]})
        result = triage([_pr(1)], repo_dir="/tmp", run_fn=run)
        assert result.included == () and result.excluded == ()
        assert result.undecided == (1,)

    def test_duplicate_verdict_keeps_first(self) -> None:
        run = _fake_run({"verdicts": [
            {"pr": 1, "include": True, "reason": "first"},
            {"pr": 1, "include": False, "reason": "second"},
        ]})
        result = triage([_pr(1)], repo_dir="/tmp", run_fn=run)
        assert [(d.pr_number, d.reason) for d in result.included] == [(1, "first")]
        assert result.excluded == ()

    def test_unparseable_batch_leaves_all_undecided(self) -> None:
        run = _fake_run_raw("not json at all", exit_code=1)
        result = triage([_pr(1), _pr(2)], repo_dir="/tmp", run_fn=run)
        assert result.included == () and result.excluded == ()
        assert result.undecided == (1, 2)
