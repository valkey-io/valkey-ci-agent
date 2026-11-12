from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ai.runtime import AGENT_PROFILES
from scripts.ci_fix import agent as agent_mod
from scripts.ci_fix.models import (
    CandidateAction,
    Failure,
    FailureType,
    FixPolicy,
    FixTarget,
    FixTask,
    TargetKind,
)


def _task(
    *,
    kind: TargetKind = TargetKind.UPDATE_PR,
    policy: FixPolicy = FixPolicy.GENERAL,
) -> FixTask:
    failure = Failure(
        repo_full_name="owner/repo",
        run_id=123,
        run_attempt=2,
        head_sha="a" * 40,
        job_id=456,
        job_name="unit tests",
        base_branch="main",
        description="test_alpha failed",
        hint="look at the parser",
        pr_number=7,
        other_failed_jobs=("lint",),
    )
    target = FixTarget(
        kind=kind,
        repo_full_name="owner/repo",
        branch="agent/ci-fix/7",
        expected_sha=failure.head_sha,
    )
    return FixTask(failure, target, policy)


def _result(payload: dict[str, object], *, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        stdout=json.dumps(payload),
        stderr="agent error" if returncode else "",
        returncode=returncode,
    )


def test_ci_fix_profiles_match_the_publication_boundary() -> None:
    edit = AGENT_PROFILES["ci_fix_edit"]
    suggest = AGENT_PROFILES["ci_fix_suggest"]

    assert edit.allowed_tools == "Read,Edit,MultiEdit,Write,Grep"
    assert edit.disallowed_tools == "Bash,Glob"
    assert edit.confined is True
    assert edit.timeout is None and edit.max_turns is None

    assert suggest.allowed_tools == "Read,Edit,MultiEdit,Write,Grep"
    assert suggest.disallowed_tools == "Bash,Glob"
    assert suggest.confined is True
    # Every CI-fix agent runs as the isolated account: the edit agent runs
    # commands, the reviewer reads the tree they left, and a suggestion reads
    # a contributor's checkout.
    assert edit.isolated and suggest.isolated and AGENT_PROFILES["ci_fix_review"].isolated
    assert suggest.timeout is None and suggest.max_turns is None


def test_run_fix_agent_uses_exact_log_and_one_edit_session(tmp_path, monkeypatch) -> None:
    seen = {}
    job_log = "FAILED test_alpha\nexpected 1, got 2\n"

    def fake_run_agent(profile, prompt, **kwargs):
        seen["profile"] = profile
        seen["prompt"] = prompt
        seen.update(kwargs)
        log_dir = Path(kwargs["extra_dirs"][0])
        assert list(log_dir.iterdir())[0].read_text(encoding="utf-8") == job_log
        return _result(
            {
                "action": "patch",
                "summary": "Fix parser boundary",
                "failing_check": "test_alpha",
                "root_cause": "The parser skipped the final token.",
                "reasoning": "Keep the last token in the result.",
                "failure_type": "deterministic",
                "changed_paths": ["src/parser.c"],
                "port_commit": "",
                "validation": "pytest tests/test_parser.py -k alpha",
            }
        )

    monkeypatch.setattr(agent_mod, "run_agent", fake_run_agent)

    candidate = agent_mod.run_fix_agent(_task(), str(tmp_path), job_log)

    assert seen["profile"] == "ci_fix_edit"
    assert seen["cwd"] == str(tmp_path)
    assert "actions/runs/123/attempts/2" in seen["prompt"]
    assert "Job: unit tests (job id 456)" in seen["prompt"]
    assert "Other failed jobs, out of scope: lint" in seen["prompt"]
    assert "Normal target-repository CI is authoritative" in seen["prompt"]
    assert "Choose `refuse` only when you can justify no\nchange" in seen["prompt"]
    assert "one change is\ncorrect under each of them" in seen["prompt"]
    assert "Do not commit or push" in seen["prompt"]
    assert candidate.action is CandidateAction.PATCH
    assert candidate.failure_type is FailureType.DETERMINISTIC
    assert candidate.changed_paths == ("src/parser.c",)
    assert candidate.validation == "pytest tests/test_parser.py -k alpha"


def test_suggestion_uses_confined_profile_and_backport_policy(tmp_path, monkeypatch) -> None:
    seen = {}

    def fake_run_agent(profile, prompt, **kwargs):
        seen["profile"] = profile
        seen["prompt"] = prompt
        return _result({"action": "refuse", "summary": "No safe adaptation"})

    monkeypatch.setattr(agent_mod, "run_agent", fake_run_agent)

    candidate = agent_mod.run_fix_agent(
        _task(kind=TargetKind.SUGGEST, policy=FixPolicy.BACKPORT),
        str(tmp_path),
        "failure",
    )

    assert seen["profile"] == "ci_fix_suggest"
    assert "This is a backport branch" in seen["prompt"]
    assert "Prepare a patch for a PR comment only" in seen["prompt"]
    assert '"action": "patch|refuse"' in seen["prompt"]
    assert "- `port`:" not in seen["prompt"]
    assert '"port_commit": "must be empty"' in seen["prompt"]
    assert candidate.action is CandidateAction.REFUSE


def test_a_port_commit_is_normalized(tmp_path, monkeypatch) -> None:
    commit = "ABCDEF12" * 5
    monkeypatch.setattr(
        agent_mod,
        "run_agent",
        lambda *args, **kwargs: _result(
            {
                "action": "port",
                "summary": "Port the upstream repair",
                "port_commit": commit,
                "validation": "model says it passed",
            }
        ),
    )

    candidate = agent_mod.run_fix_agent(_task(), str(tmp_path), "failure")

    assert candidate.action is CandidateAction.PORT
    assert candidate.port_commit == commit.lower()


def test_an_infrastructure_label_does_not_discard_the_agents_patch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        agent_mod,
        "run_agent",
        lambda *args, **kwargs: _result(
            {
                "action": "patch",
                "summary": "",
                "failure_type": "infrastructure",
                "validation": "kept",
            }
        ),
    )

    candidate = agent_mod.run_fix_agent(_task(), str(tmp_path), "runner disappeared")

    # The action is the agent's decision; the label is only reported.
    assert candidate.action is CandidateAction.PATCH
    assert candidate.failure_type is FailureType.INFRASTRUCTURE
    assert candidate.validation == "kept"


def test_agent_failure_or_ambiguous_output_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        agent_mod,
        "run_agent",
        lambda *args, **kwargs: _result({}, returncode=2),
    )
    with pytest.raises(RuntimeError, match="exit code 2"):
        agent_mod.run_fix_agent(_task(), str(tmp_path), "failure")

    monkeypatch.setattr(
        agent_mod,
        "run_agent",
        lambda *args, **kwargs: SimpleNamespace(
            stdout='{"action":"patch"} {"action":"refuse"}',
            stderr="",
            returncode=0,
        ),
    )
    with pytest.raises(ValueError, match="unambiguous result"):
        agent_mod.run_fix_agent(_task(), str(tmp_path), "failure")


def test_the_agent_is_told_what_code_established_and_shown_ports_first(tmp_path, monkeypatch) -> None:
    from scripts.ci_fix.models import FixContext

    seen = {}

    def fake_run_agent(profile, prompt, **kwargs):
        seen["prompt"] = prompt
        directory = Path(kwargs["extra_dirs"][0])
        seen["files"] = {path.name: path.read_text() for path in directory.iterdir()}
        return _result({"action": "refuse", "summary": "Already fixed"})

    monkeypatch.setattr(agent_mod, "run_agent", fake_run_agent)
    context = FixContext(
        prior_fixes="`cccccccccccc` Deflake the test (#9) (port candidate)",
        prior_fixes_detail="# detail with the diff",
        verification="Code checks your change with `./runtest ...` (40 loop(s)).",
        verified_by_code=True,
        guide="# Fixing a failing Valkey test",
    )

    agent_mod.run_fix_agent(_task(), str(tmp_path), "log", context=context)

    prompt = seen["prompt"]
    assert "Deflake the test (#9) (port candidate)" in prompt
    assert "choose `port` with its full SHA rather than writing a new fix" in prompt
    assert "### Verification\nCode checks your change with `./runtest ...`" in prompt
    assert "CI job log is your evidence" in prompt
    assert "test-guide.md before you change anything" in prompt
    assert "the repository's own guidelines for code and tests" in prompt
    # Code verifies with its own command, and the test is never bent to it.
    assert "only that counts: do not claim to have run anything" in prompt
    assert "never change a test so that it can run alone" in prompt
    assert "test_command" not in prompt and "Only a change already merged" not in prompt
    assert prompt.index("- `port`:") < prompt.index("- `patch`:")
    assert seen["files"]["prior-fixes.md"] == "# detail with the diff"
    assert seen["files"]["test-guide.md"] == "# Fixing a failing Valkey test"


def test_without_a_verification_plan_the_agent_must_not_claim_a_test_run(tmp_path, monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(agent_mod, "run_agent", lambda profile, prompt, **kw: seen.update(prompt=prompt) or _result(
        {"action": "refuse", "summary": "no"}))

    agent_mod.run_fix_agent(_task(), str(tmp_path), "log")

    assert "do not claim to have\nrun anything" in seen["prompt"]
    assert "What code established" not in seen["prompt"]


def test_a_fix_that_will_not_be_run_says_what_is_and_is_not_established(tmp_path, monkeypatch) -> None:
    from scripts.ci_fix.models import FixContext

    seen = {}
    monkeypatch.setattr(agent_mod, "run_agent", lambda profile, prompt, **kw: seen.update(prompt=prompt) or _result(
        {"action": "refuse", "summary": "no"}))

    agent_mod.run_fix_agent(_task(), str(tmp_path), "log", context=FixContext(
        reviewed_only=True, verification="No command here can run the failing check, so this fix was reviewed, not run."))

    assert "Unless\nyou name it in `failing_tests` and code can run it" in seen["prompt"]
    assert "say plainly what the log and the code establish and what they\ndo not" in seen["prompt"]
    assert "### Verification\nNo command here can run the failing check" in seen["prompt"]
    assert "If you can, name it in `failing_tests`" in seen["prompt"]


def test_the_agent_may_name_the_tests_the_log_shows_failing(tmp_path, monkeypatch) -> None:
    """A valgrind or sanitizer report, or a test client's exception, names no test on its result line."""
    from scripts.ci_fix.models import FailedTest

    tests = [{"file": "tests/unit/moduleapi/rdbloadsave.tcl", "name": "Module rdbloadsave with bgsave"},
             {"file": "tests/unit/a.tcl", "name": ""}, {"file": "", "name": "no file"}, "not a test",
             {"file": "tests/unit/a.tcl", "name": ""}]
    monkeypatch.setattr(agent_mod, "run_agent", lambda *_a, **_k: _result(
        {"action": "patch", "summary": "fixed", "changed_paths": ["a.c"], "failing_tests": tests}))

    candidate = agent_mod.run_fix_agent(_task(), str(tmp_path), "log")

    assert candidate.failing_tests == (
        FailedTest("Module rdbloadsave with bgsave", "tests/unit/moduleapi/rdbloadsave.tcl"),
        FailedTest("", "tests/unit/a.tcl"),
    )


def test_where_an_upstream_change_comes_first_the_agent_is_told_and_its_claim_is_read(
    tmp_path, monkeypatch,
) -> None:
    from scripts.ci_fix.models import FixContext

    seen = {}
    monkeypatch.setattr(agent_mod, "run_agent", lambda profile, prompt, **kw: seen.update(prompt=prompt) or _result(
        {"action": "patch", "summary": "adapted", "changed_paths": ["a.c"], "adapts_commit": "D" * 40}))

    context = FixContext(upstream_first=True, unported_changes="# `src/a.c`\n\n- `abc` 2026-11-04 Free the hint\n",
                         unported_index="- `abc` 2026-11-04 Free the hint | src/a.c\n")
    candidate = agent_mod.run_fix_agent(_task(), str(tmp_path), "log", context=context)

    assert "A change already merged on the default branch comes first" in seen["prompt"]
    # Where none fixes the failure, the fix is written on the branch, not refused.
    assert "write the fix here as a `patch`" in seen["prompt"] and "belongs there first" not in seen["prompt"]
    assert "### Upstream changes this branch lacks" in seen["prompt"] and "upstream-changes.md" in seen["prompt"]
    # And every commit it lacks, by the files it changes: the fix may be in a file the log does not name.
    assert "### Every upstream commit this branch lacks" in seen["prompt"] and "upstream-commits.md" in seen["prompt"]
    assert "other-release-branches.md" not in seen["prompt"]

    # What the other release branches' pull requests changed in the same files: one failure, one fix.
    sibling = FixContext(upstream_first=True, sibling_changes="# Release branch `9.1`\n")
    agent_mod.run_fix_agent(_task(), str(tmp_path), "log", context=sibling)
    assert "other-release-branches.md" in seen["prompt"]
    assert "the same fix on every release branch" in seen["prompt"]
    assert candidate.adapts == "d" * 40


def test_the_review_is_told_when_a_release_branchs_patch_is_not_an_upstream_change(tmp_path, monkeypatch) -> None:
    from scripts.ci_fix.models import FixCandidate, FixContext

    seen = {}
    monkeypatch.setattr(agent_mod, "run_agent", lambda profile, prompt, **kw: seen.update(prompt=prompt) or _result(
        {"approved": True, "reasoning": "fine"}))
    own = FixCandidate(action=CandidateAction.PATCH, patch="diff", failing_check="t", root_cause="race")

    agent_mod.review_candidate(own, str(tmp_path), "log", context=FixContext(upstream_first=True))
    assert "the patch is a fix written for this branch" in seen["prompt"]

    agent_mod.review_candidate(own, str(tmp_path), "log", context=FixContext())
    assert "written for this branch" not in seen["prompt"]
def _stream(result: str, *, returncode: int = 1) -> SimpleNamespace:
    """A session's stream as the CLI prints it: events, then the result."""
    events = [{"type": "assistant", "message": {}}, {"type": "result", "subtype": "success", "result": result}]
    return SimpleNamespace(stdout="\n".join(json.dumps(event) for event in events), stderr="", returncode=returncode)


_UNAVAILABLE = "API Error: 503 Bedrock is unable to process your request. This is a server-side issue."


@pytest.mark.parametrize(("result", "again"), [
    (_UNAVAILABLE, True),
    ("API Error: 429 Too many requests", True),
    ("API Error: Connection error.", True),
    ("API Error: 400 The prompt is too long", False),  # the request's own fault: it would fail again
    ('{"action": "refuse"}', False),
])
def test_only_a_model_api_error_worth_another_try_is_one(result, again) -> None:
    assert bool(agent_mod.api_error(_stream(result).stdout)) is again


def test_a_fix_session_that_ended_on_a_model_api_error_says_so(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(agent_mod, "run_agent", lambda *_args, **_kwargs: _stream(_UNAVAILABLE))

    with pytest.raises(agent_mod.ModelUnavailable, match="API Error: 503"):
        agent_mod.run_fix_agent(_task(), str(tmp_path), "log")


def test_a_review_that_ended_on_a_model_api_error_is_run_once_more(tmp_path, monkeypatch) -> None:
    """The candidate has already passed its test: a 503 is no reason to throw the attempt away."""
    from scripts.ci_fix.models import FixCandidate

    sessions = iter([_stream(_UNAVAILABLE), _result({"approved": True, "reasoning": "fine"})])
    prompts, slept = [], []
    monkeypatch.setattr(agent_mod, "run_agent", lambda _profile, prompt, **_kw: prompts.append(prompt) or next(sessions))
    monkeypatch.setattr(agent_mod.time, "sleep", slept.append)
    candidate = FixCandidate(action=CandidateAction.PATCH, patch="diff", failing_check="t", root_cause="race")

    approved, reasoning = agent_mod.review_candidate(candidate, str(tmp_path), "log", timeout=600)

    assert approved and reasoning == "fine"
    assert len(prompts) == 2 and prompts[0] == prompts[1] and slept == [60]

    # Twice is a failed attempt, as any review that judged nothing is.
    monkeypatch.setattr(agent_mod, "run_agent", lambda *_args, **_kwargs: _stream(_UNAVAILABLE))
    with pytest.raises(RuntimeError, match="the review agent failed"):
        agent_mod.review_candidate(candidate, str(tmp_path), "log")


def test_the_review_sees_the_port_verification_and_guide(tmp_path, monkeypatch) -> None:
    from scripts.ci_fix.models import FixCandidate, FixContext

    seen = {}
    monkeypatch.setattr(agent_mod, "run_agent", lambda profile, prompt, **kw: seen.update(prompt=prompt) or _result(
        {"approved": True, "reasoning": "fine"}))
    candidate = FixCandidate(action=CandidateAction.PORT, port_commit="c" * 40, patch="diff", failing_check="t",
                             root_cause="race")
    context = FixContext(verification="Code checks your change with `make`.", guide="# guide")

    approved, _ = agent_mod.review_candidate(candidate, str(tmp_path), "log", context=context)

    assert approved
    assert f"ports upstream commit {'c' * 40}" in seen["prompt"]
    assert "What code checked:\nCode checks your change with `make`." in seen["prompt"]
    assert "It is a claim: check it against the log" in seen["prompt"]
    # A port is merged upstream, where it was reviewed: its design is not judged again. The first
    # run on a new model turned down the one merged fix for a flake because it widened an assertion.
    assert "do not judge its design again" in seen["prompt"]
    assert "a looser threshold unless" not in seen["prompt"] and "is a rejection" not in seen["prompt"]
    assert "for a state that cannot happen" not in seen["prompt"]

    # A fix written here is judged on its merits, against the repository's conventions; and a
    # verified candidate's review reads what the checks found, and what a patch says it adapts.
    adapted = FixCandidate(action=CandidateAction.PATCH, adapts="d" * 40, patch="diff",
                           verification="With the fix, `make` builds.",
                           reasoning="waits for the save", validation="the log does not show the timer")
    agent_mod.review_candidate(adapted, str(tmp_path), "log", context=context)
    # The reviewer reads why the author chose the change and what it ruled out, is not to reject
    # for a failure that was never reproduced (none is), and says what it would approve instead.
    assert "Why this change: waits for the save" in seen["prompt"]
    assert "The author's notes: the log does not show the timer" in seen["prompt"]
    assert "no reason to reject" in seen["prompt"] and "say what change you\nwould approve" in seen["prompt"]
    assert "What code checked:\nWith the fix, `make` builds." in seen["prompt"]
    assert f"adapts upstream commit {'d' * 40}" in seen["prompt"]
    assert "a looser threshold unless" in seen["prompt"] and "do not judge its design again" not in seen["prompt"]
    # Written fixes are also held to the test-quality rules in the bot's guide.
    assert "for a state that cannot happen" in seen["prompt"]
    assert "comments that narrate the fix" in seen["prompt"]
    assert "a skip tag instead of a fix, is a rejection" in seen["prompt"]


@pytest.mark.parametrize("result", [
    _result({}, returncode=1),
    SimpleNamespace(stdout="no verdict here", stderr="", returncode=0),
])
def test_a_review_that_judged_nothing_fails_the_attempt_rather_than_rejecting_the_patch(
    tmp_path, monkeypatch, result,
) -> None:
    from scripts.ci_fix.models import FixCandidate, FixContext

    monkeypatch.setattr(agent_mod, "run_agent", lambda *_a, **_k: result)
    candidate = FixCandidate(action=CandidateAction.PATCH, patch="diff", failing_check="t", root_cause="race")

    with pytest.raises((RuntimeError, ValueError)):
        agent_mod.review_candidate(candidate, str(tmp_path), "log", context=FixContext())


def test_the_files_an_agent_reads_sit_beside_its_checkout(tmp_path, monkeypatch) -> None:
    """Not in the default temp directory, which the isolated account may not enter."""
    seen = {}

    def run_agent(profile, prompt, **kwargs):
        seen["dirs"] = kwargs["extra_dirs"]
        return _result({"action": "refuse", "summary": "no"})

    monkeypatch.setattr(agent_mod, "run_agent", run_agent)
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    agent_mod.run_fix_agent(_task(), str(checkout), "log")

    (log_dir,) = seen["dirs"]
    assert Path(log_dir).parent == tmp_path.resolve()
