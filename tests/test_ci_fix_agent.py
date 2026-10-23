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

    assert edit.allowed_tools == "Read,Edit,MultiEdit,Write,Grep,Glob,Bash"
    assert edit.confined is False
    assert edit.timeout <= 45 * 60
    assert edit.max_turns == 160

    assert suggest.allowed_tools == "Read,Edit,MultiEdit,Write,Grep"
    assert suggest.disallowed_tools == "Bash,Glob"
    assert suggest.confined is True
    # Every CI-fix agent runs as the isolated account: the edit agent runs
    # commands, the reviewer reads the tree they left, and a suggestion reads
    # a contributor's checkout.
    assert edit.isolated and suggest.isolated and AGENT_PROFILES["ci_fix_review"].isolated
    assert suggest.timeout <= 45 * 60
    assert suggest.max_turns == 160


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
    assert "Port an already-merged upstream fix" in seen["prompt"]
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


def test_infrastructure_classification_cannot_produce_a_patch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        agent_mod,
        "run_agent",
        lambda *args, **kwargs: _result(
            {
                "action": "patch",
                "summary": "",
                "failure_type": "infrastructure",
                "validation": "ignored",
            }
        ),
    )

    candidate = agent_mod.run_fix_agent(_task(), str(tmp_path), "runner disappeared")

    assert candidate.action is CandidateAction.REFUSE
    assert candidate.failure_type is FailureType.INFRASTRUCTURE
    assert candidate.validation == ""


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
        reproduction="Reproduced intermittently with `./runtest ...` (failed within 20 loops).",
        reproduction_output="[err]: the test in tests/unit/a.tcl",
        verified_by_code=True,
        guide="# Fixing a failing Valkey test",
        guide_paths=("tests/README.md",),
    )

    agent_mod.run_fix_agent(_task(), str(tmp_path), "log", context=context)

    prompt = seen["prompt"]
    assert "Deflake the test (#9) (port candidate)" in prompt
    assert "choose `port` with its full SHA rather than writing a new fix" in prompt
    assert "Reproduced intermittently" in prompt
    assert "`tests/README.md`" in prompt
    # Code verifies with its own reproduction, so no test command is demanded.
    assert "Otherwise leave it empty." in prompt
    assert prompt.index("- `port`:") < prompt.index("- `patch`:")
    assert seen["files"]["prior-fixes.md"] == "# detail with the diff"
    assert seen["files"]["reproduction.log"] == "[err]: the test in tests/unit/a.tcl"
    assert seen["files"]["test-guide.md"] == "# Fixing a failing Valkey test"


def test_without_a_reproduction_the_agent_must_give_a_test_command(tmp_path, monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(agent_mod, "run_agent", lambda profile, prompt, **kw: seen.update(prompt=prompt) or _result(
        {"action": "refuse", "summary": "no"}))

    agent_mod.run_fix_agent(_task(), str(tmp_path), "log")

    assert "where it must fail, and with it, where it\nmust pass" in seen["prompt"]
    assert "What code established" not in seen["prompt"]


def test_the_review_sees_the_port_reproduction_and_guide(tmp_path, monkeypatch) -> None:
    from scripts.ci_fix.models import FixCandidate, FixContext

    seen = {}
    monkeypatch.setattr(agent_mod, "run_agent", lambda profile, prompt, **kw: seen.update(prompt=prompt) or _result(
        {"approved": True, "reasoning": "fine"}))
    candidate = FixCandidate(action=CandidateAction.PORT, port_commit="c" * 40, patch="diff", failing_check="t",
                             root_cause="race")

    approved, _ = agent_mod.review_candidate(
        candidate, str(tmp_path), "log",
        context=FixContext(reproduction="Reproduced every time.", guide="# guide"),
    )

    assert approved
    assert f"ports upstream commit {'c' * 40}" in seen["prompt"]
    assert "Reproduction, by code: Reproduced every time." in seen["prompt"]
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
