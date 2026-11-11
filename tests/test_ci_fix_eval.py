from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts.ci_fix import eval as eval_mod
from scripts.ci_fix import run as run_mod
from scripts.ci_fix.eval import Case, load_cases, plan_case, render_score, score_case
from scripts.ci_fix.models import (
    CandidateAction,
    FailedTest,
    FixCandidate,
    FixOutcome,
    FixPlan,
    FixPolicy,
    FrontDoor,
    TargetKind,
)
from scripts.ci_fix.state import load, save

_SHA = "a" * 40
_PORT = "c" * 40
_KNOWN = (("src/net.c",), "@@ -1,2 +1,3 @@\n-    if (error == EINPROGRESS) {\n+    if (error == EINPROGRESS || error == EISCONN) {\n"
                          "+        /* macOS returns EISCONN on a repeated connect. */\n")


def _case(**fields) -> Case:
    values = dict(id="c", about="about", repo="o/r", run_id=1, job_id=2, job="test-ubuntu", sha=_SHA,
                  base_branch="unstable", expect_pull=9)
    values.update(fields)
    return Case(**values)


def _backport(**fields) -> Case:
    return _case(base_branch="9.0", pr=5, backport=True, expect_pull=0, expect_port=_PORT, **fields)


def test_every_case_in_the_repository_can_be_replayed_and_scored():
    cases = load_cases()

    assert cases, "the eval has no cases"
    for case in cases.values():
        plan = plan_case(case, "the job log")
        assert plan.front_door is FrontDoor.EVAL and plan.task.target.expected_sha == case.sha
        # A case on the default branch must not find the fix that landed after it.
        assert plan.task.as_of_failure is not case.backport


@pytest.mark.parametrize(("change", "problem"), [
    ({"id": "Not An Id"}, "its id is not"),
    ({"sha": "abc123"}, "not a full commit SHA"),
    ({"job_id": 0}, "it needs repo, run_id, job_id, job and base_branch"),
    ({"expect": {}}, "exactly one of a port and a pull request"),
    ({"expect": {"pull": 9, "port": _PORT}}, "exactly one of a port and a pull request"),
    ({"expect": {"port": _PORT}}, "expected of a backport case"),  # not a backport: nothing to port from
])
def test_a_case_that_could_not_be_replayed_or_scored_is_refused(tmp_path, change, problem):
    case = {"id": "c", "about": "a", "repo": "o/r", "run_id": 1, "job_id": 2, "job": "j", "sha": _SHA,
            "base_branch": "unstable", "expect": {"pull": 9}}
    path = tmp_path / "cases.yml"
    path.write_text(yaml.safe_dump({"cases": [{**case, **change}]}), encoding="utf-8")

    with pytest.raises(ValueError, match=problem):
        load_cases(path)

    path.write_text(yaml.safe_dump({"cases": [case, case]}), encoding="utf-8")
    with pytest.raises(ValueError, match="used twice"):
        load_cases(path)


def test_a_default_branch_case_is_planned_like_a_detector_issue_as_of_its_failure():
    case = _case(issue=7, scheduled=True, test_name="INCR fails", test_file="tests/unit/type/incr.tcl")

    task = plan_case(case, "log").task

    assert task.policy is FixPolicy.GENERAL and task.target.kind is TargetKind.DRAFT_PR
    assert task.as_of_failure and task.failure.scheduled
    assert task.failure.failed_tests == (FailedTest("INCR fails", "tests/unit/type/incr.tcl"),)
    assert (task.failure.head_sha, task.failure.issue_number, task.failure.job_id) == (_SHA, 7, 2)


def test_a_backport_case_is_planned_like_the_follow_up_and_sees_the_default_branch_as_it_is():
    task = plan_case(_backport(), "log").task

    assert task.policy is FixPolicy.BACKPORT and task.target.kind is TargetKind.UPDATE_PR
    assert task.target.branch == "agent/backport/sweep/9.0"
    assert not task.as_of_failure and not task.failure.scheduled and task.failure.pr_number == 5


def _patch(*lines: str, paths: tuple[str, ...] = ("src/net.c",), **fields) -> FixOutcome:
    body = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n" + "".join(f"+{line}\n" for line in lines)
    return FixOutcome(FixCandidate(CandidateAction.PATCH, patch=body, changed_paths=paths, **fields))


def test_a_patch_is_scored_by_how_much_of_it_is_the_known_fix():
    same = score_case(_case(), _patch("if (error == EINPROGRESS || error == EISCONN) {",
                                      "/* macOS returns EISCONN on a repeated connect. */"), _KNOWN)
    assert (same.verdict, same.shared_percent, same.shared_files) == ("match", 100, ("src/net.c",))

    other_way = score_case(_case(), _patch("if (error != 0) return VALKEY_ERR;"), _KNOWN)
    assert other_way.verdict == "differs" and "same file another way" in other_way.detail
    assert other_way.replaced_percent == 0

    # The same fix worded another way shares no added line, but replaces the line the known fix does
    # (the bot's fixes for #5217 and #5285 against the ones people merged).
    reworded = FixOutcome(FixCandidate(
        CandidateAction.PATCH, changed_paths=("src/net.c",),
        patch="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-    if (error == EINPROGRESS) {\n+    if (error == EISCONN || error == EINPROGRESS) {\n",
    ))
    close = score_case(_case(), reworded, _KNOWN)
    assert (close.verdict, close.shared_percent, close.replaced_percent) == ("differs", 0, 100)
    assert "replaces 100% of the lines the known fix replaces" in close.detail

    # The fix the bot pushed for #5330's failure changed the test; a person fixed libvalkey.
    elsewhere = score_case(_case(), _patch("assert_match {*Could not connect*} $output", paths=("tests/unit/cli.tcl",)), _KNOWN)
    assert elsewhere.verdict == "differs" and "other files" in elsewhere.detail and elsewhere.shared_files == ()


def test_a_port_is_scored_by_the_commit_it_took():
    case = _backport()

    assert score_case(case, FixOutcome(FixCandidate(CandidateAction.PORT, port_commit=_PORT))).verdict == "match"
    assert score_case(case, FixOutcome(FixCandidate(CandidateAction.PATCH, adapts=_PORT.upper(), patch="p"))).verdict == "match"
    assert score_case(case, FixOutcome(FixCandidate(CandidateAction.PORT, port_commit="d" * 40))).verdict == "differs"
    assert score_case(case, _patch("a fix of its own")).verdict == "differs"
    # A port where a person wrote a fix is another change.
    assert score_case(_case(), FixOutcome(FixCandidate(CandidateAction.PORT, port_commit=_PORT)), _KNOWN).verdict == "differs"


def test_a_refusal_and_a_failed_fix_are_scores_too():
    refused = score_case(_case(), FixOutcome(FixCandidate(CandidateAction.REFUSE, summary="No command can run it.")))
    failed = score_case(_case(), FixOutcome(error="the agent timed out"))

    assert (refused.verdict, refused.detail) == ("refused", "No command can run it.")
    assert (failed.verdict, failed.detail) == ("failed", "the agent timed out")


def test_the_report_shows_both_changes_and_a_known_gap():
    case = _case(gap="Nothing here runs the validator.")
    outcome = _patch("if (error != 0) return VALKEY_ERR;", root_cause="connect() is retried",
                     verification="With the fix, `make` builds.")

    report = render_score(case, score_case(case, outcome, _KNOWN), outcome, _KNOWN[1])

    assert "### CI fix eval `c`: **differs**" in report
    assert "- Expected: pull request #9" in report and "- Known gap: Nothing here runs the validator." in report
    assert "**The bot's root cause:** connect() is retried" in report
    assert "<summary>The bot's patch</summary>" in report and "<summary>The known fix</summary>" in report


def _artifacts(log: str = "the log") -> SimpleNamespace:
    return SimpleNamespace(download_job_log=lambda *_args: log)


def test_a_replay_is_planned_by_the_production_planner_and_never_published(tmp_path, monkeypatch):
    """The eval is a front door of scripts.ci_fix.run: the same plan step, then the same fix jobs."""
    monkeypatch.setattr(eval_mod, "load_cases", lambda: {"c": _case()})
    monkeypatch.setattr(run_mod, "job_for", lambda _gh, _task: None)  # this job cannot host its fix

    outputs = run_mod.plan(object(), _artifacts(), FrontDoor.EVAL, {"case": "c"}, bot_login="bot", out=tmp_path)

    assert outputs["mode"] == "local"
    plan = load(tmp_path / run_mod.PLAN_FILE, FixPlan)
    assert plan.front_door is FrontDoor.EVAL and plan.job_log == "the log" and plan.task.as_of_failure
    # The publish step refuses it before it reads an outcome or touches GitHub.
    with pytest.raises(ValueError, match="never published"):
        run_mod.publish_and_report(object(), tmp_path, tmp_path, git_env={}, run_url="u")


def test_a_case_whose_log_is_gone_says_it_has_aged_out(monkeypatch):
    monkeypatch.setattr(eval_mod, "load_cases", lambda: {"c": _case()})

    with pytest.raises(ValueError, match="aged out"):
        eval_mod.plan_request(object(), _artifacts(""), {"case": "c"})
    with pytest.raises(ValueError, match="no eval case 'missing'"):
        eval_mod.plan_request(object(), _artifacts(), {"case": "missing"})


def test_a_replay_that_could_not_be_planned_reports_nowhere():
    plan = plan_case(_case(), "log")
    result = SimpleNamespace(kind=SimpleNamespace(value="failed"), summary="planning failed")

    assert eval_mod.report_result(pytest.fail, plan, result) == {
        "repo": "o/r", "action": "failed", "summary": "planning failed",
    }


def _score_cli(monkeypatch, *argv, files=()):
    monkeypatch.setenv("TARGET_TOKEN", "t")
    monkeypatch.setattr(eval_mod, "load_cases", lambda: {"c": _case()})
    monkeypatch.setattr(eval_mod, "Github", lambda **_kwargs: SimpleNamespace(
        get_repo=lambda _name: SimpleNamespace(get_pull=lambda _n: SimpleNamespace(get_files=lambda: list(files)))))
    return eval_mod.main(["score", "--case", "c", *argv])


def test_score_reads_the_fix_jobs_outcome_and_the_known_pull_request(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    save(tmp_path / "result" / run_mod.OUTCOME_FILE, _patch("if (error == EINPROGRESS || error == EISCONN) {"))
    known = [SimpleNamespace(filename="src/net.c", patch=_KNOWN[1])]
    summary = tmp_path / "summary.md"

    _score_cli(monkeypatch, "--outcome", str(tmp_path / "result"), "--out", str(tmp_path / "score"), files=known)
    assert load(tmp_path / "score" / eval_mod.SCORE_FILE, eval_mod.Score).verdict == "match"
    assert "**match**" in capsys.readouterr().out

    # A fix job that crashed left no outcome: that is a score, and it reaches the run's summary.
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    _score_cli(monkeypatch, "--outcome", str(tmp_path / "nothing"), "--out", str(tmp_path / "score"))
    assert load(tmp_path / "score" / eval_mod.SCORE_FILE, eval_mod.Score).verdict == "failed"
    assert "**failed**" in summary.read_text(encoding="utf-8")


def test_the_eval_runs_the_production_fix_and_can_write_nowhere():
    """It calls ci-fix-run.yml, so a replay is prepared inside the failed job like a real fix.

    Three things keep it from publishing: the App's secrets are not passed, the shared run plans a
    replay with the workflow's own read-only token and skips its publish job, and run.py refuses.
    """
    text = Path(".github/workflows/ci-fix-eval.yml").read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)

    assert set(workflow["on"]) == {"workflow_dispatch"} and workflow["permissions"] == {}
    assert set(workflow["jobs"]) == {"fix", "score"}
    fix = workflow["jobs"]["fix"]
    assert fix["uses"] == "./.github/workflows/ci-fix-run.yml" and fix["with"]["front_door"] == "eval"
    assert fix["secrets"] == {"AWS_ROLE_ARN": "${{ secrets.AWS_ROLE_ARN }}"}
    assert fix["permissions"] == {"actions": "read", "contents": "read", "id-token": "write"}
    assert workflow["jobs"]["score"]["permissions"] == {"contents": "read"}
    assert "create-github-app-token" not in text and "scripts.ci_fix.run publish" not in text

    shared = yaml.load(Path(".github/workflows/ci-fix-run.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert "inputs.front_door != 'eval'" in shared["jobs"]["publish"]["if"]
    token = next(step for step in shared["jobs"]["plan"]["steps"] if step.get("id") == "target-token")
    assert token["if"] == "inputs.front_door != 'eval'"
    # A real fix still plans and publishes with the App: only its secrets stopped being required of a replay.
    assert shared["on"]["workflow_call"]["secrets"]["AWS_ROLE_ARN"] == {"required": "true"}
