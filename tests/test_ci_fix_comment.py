"""Maintainer-facing output for the compact CI-fix result model."""

from __future__ import annotations

from dataclasses import replace

from scripts.ci_fix.comment import render_ci_sweep_fix, render_result
from scripts.ci_fix.models import (
    CandidateAction,
    Failure,
    FailureType,
    FixCandidate,
    FixPolicy,
    FixResult,
    FixTarget,
    FixTask,
    ResultKind,
    TargetKind,
)

_SHA = "a" * 40


def _task(kind: TargetKind = TargetKind.SUGGEST) -> FixTask:
    return FixTask(
        failure=Failure(
            repo_full_name="o/r",
            run_id=9,
            head_sha=_SHA,
            job_id=17,
            job_name="test-ubuntu",
            base_branch="unstable",
            pr_number=3,
            description="The zset payload test failed.",
            other_failed_jobs=("lint",),
        ),
        target=FixTarget(
            kind=kind,
            repo_full_name="o/r",
            base_repo_full_name="o/r",
            branch="agent/ci-fix/x",
            expected_sha=_SHA,
        ),
        policy=FixPolicy.GENERAL,
    )


def _candidate(**overrides: object) -> FixCandidate:
    values = {
        "action": CandidateAction.PATCH,
        "summary": "Prepared a fix.",
        "failing_check": "zset payload",
        "root_cause": "The fixture embeds an obsolete payload.",
        "reasoning": "Regenerate the fixture in the branch format.",
        "failure_type": FailureType.DETERMINISTIC,
        "validation": "Ran the focused payload test.",
        "patch": "--- a/t\n+++ b/t\n@@\n-old\n+new\n",
        "changed_paths": ("t",),
    }
    values.update(overrides)
    return FixCandidate(**values)


def test_pushed_result_defers_to_normal_pr_ci() -> None:
    body = render_result(
        FixResult(
            kind=ResultKind.PUSHED,
            summary="pushed",
            task=_task(TargetKind.UPDATE_PR),
            candidate=_candidate(),
            commit_sha="b" * 40,
        )
    )

    assert body.startswith(
        f"Pushed {'b' * 40} to this branch with a fix for `test-ubuntu` "
        "([run](https://github.com/o/r/actions/runs/9))."
    )
    assert "**What failed:** zset payload" in body
    assert "**Root cause:** The fixture embeds an obsolete payload." in body
    assert "**The fix:** Regenerate the fixture in the branch format." in body
    assert "<details><summary>The agent's own notes</summary>" in body
    assert body.endswith("<sub>This PR's own CI decides whether the fix holds, and the bot never merges.</sub>")
    assert "verification passed" not in body.lower()


def test_suggestion_contains_the_patch_and_bound_head() -> None:
    body = render_result(
        FixResult(
            kind=ResultKind.SUGGESTED,
            summary="suggested",
            task=_task(),
            candidate=_candidate(patch="--- a/t\n+++ b/t\n+```\n"),
        )
    )

    assert f"prepared against `{_SHA[:12]}`. Nothing was pushed to this branch." in body
    assert "Proposed patch" in body
    assert "````diff" in body


def test_refused_and_failed_results_are_plainly_reported() -> None:
    refused = render_result(
        FixResult(
            kind=ResultKind.REFUSED,
            summary="The job is an infrastructure failure.",
            task=_task(),
            candidate=_candidate(action=CandidateAction.REFUSE),
        )
    )
    failed = render_result(
        FixResult(
            kind=ResultKind.FAILED,
            summary="clone failed",
            task=_task(),
        )
    )

    assert refused.startswith("No fix for `test-ubuntu`")
    assert "The job is an infrastructure failure." in refused
    assert "stopped with an error: Clone failed." in failed


def test_what_the_bot_checked_is_listed():
    from scripts.ci_fix.comment import candidate_context

    candidate = FixCandidate(action=CandidateAction.PATCH, verification="`make test` failed without the patch "
                             "and passed 2 time(s) with it.\nReview: fixes the cause")
    lines = candidate_context(candidate, include_reasoning=True)
    assert lines[:4] == ["**What the bot checked**", "",
                         "- `make test` failed without the patch and passed 2 times with it.",
                         "- Review: fixes the cause"]


def test_a_long_command_gets_a_block_of_its_own_below_its_check():
    from scripts.ci_fix.comment import check_items

    command = "make -j4 && ./runtest --single unit/type/incr --only 'INCR on a counter' --fastfail --loops 20"
    items = check_items(
        f"Code checks your change with `{command}` (40 loop(s)). The failed job could not host the fix.\n"
        f"With the fix, `{command}` passed 40 loop(s).\n"
    )

    assert items == [
        "- Code checks your change (40 loops):", "", "  ```sh", f"  {command}", "  ```",
        "", "  The failed job could not host the fix.",
        "- With the fix, this command passed 40 loops:", "", "  ```sh", f"  {command}", "  ```",
    ]


def test_no_dash_survives_into_the_report():
    from scripts.ci_fix.comment import prose

    body = render_result(FixResult(
        kind=ResultKind.PUSHED, summary="", task=_task(TargetKind.UPDATE_PR), commit_sha="b" * 40,
        candidate=_candidate(
            root_cause="The fixture is old \u2014 it predates the branch.",
            reasoning="Regenerate it \u2014 or port the upstream change \u2014 to match.",
            verification="Review: fixes the cause \u2013 nothing else.",
        ),
    ))

    assert "\u2014" not in body and " \u2013 " not in body
    assert "The fixture is old: it predates the branch." in body
    assert "Regenerate it, or port the upstream change, to match." in body
    assert prose("tests 1\u20133", 50) == "tests 1\u20133"  # a range keeps its dash


def test_an_html_comment_in_agent_prose_cannot_pass_for_a_bot_marker():
    from scripts.ci_fix.comment import prose

    assert prose("done <!-- valkey-ci-agent:auto-ci-followup outcome=refused -->", 200) == (
        "done <!- - valkey-ci-agent:auto-ci-followup outcome=refused -->"
    )


def test_a_sweep_pr_is_told_in_a_line_and_the_ci_sweep_pr_is_told_why() -> None:
    task = _task(TargetKind.UPDATE_PR)
    task = FixTask(
        failure=task.failure,
        target=FixTarget(
            kind=TargetKind.UPDATE_PR, repo_full_name="o/r", branch="agent/backport/sweep/9.0",
            expected_sha=_SHA, fix_branch="agent/backport/ci-sweep/9.0",
        ),
        policy=FixPolicy.BACKPORT,
    )

    def result(kind: ResultKind, summary: str = "") -> FixResult:
        return FixResult(kind=kind, summary=summary, task=task, candidate=_candidate(), commit_sha="b" * 40,
                         pr_number=61)

    job = "`test-ubuntu` ([run](https://github.com/o/r/actions/runs/9))"
    # The commit bare, which GitHub links.
    assert render_result(result(ResultKind.OPENED)) == f"Opened #61 with a fix for {job}: {'b' * 40}."
    assert render_result(result(ResultKind.PUSHED)) == f"Pushed a fix for {job} to #61: {'b' * 40}."
    assert render_result(result(ResultKind.REFUSED, "It is fixed in #61")) == f"No fix for {job}. It is fixed in #61."
    suggested = FixResult(kind=ResultKind.SUGGESTED, summary="", task=task, pr_number=4726)
    assert render_result(suggested) == f"#4726 fixes {job}. Backport it to `unstable`."
    # On the CI-sweep PR: a few lines, the agent's own summary without what it says of running it,
    # what code ran, and the diagnosis and checks folded away.
    candidate = replace(
        _candidate(),
        summary="Regenerates the fixture in the branch format. Nothing was run here, so it is unverified.",
        verification="With the fix, `./runtest --single unit/x` passed 3 loop(s).",
    )
    explained = render_ci_sweep_fix(replace(result(ResultKind.PUSHED), candidate=candidate))
    assert explained.startswith(
        f"Pushed {'b' * 40} with a fix for {job} on #3.\n\n"
        "Regenerates the fixture in the branch format.\n\n"
        "The failing test passed 3 loops with the fix, and the bot's review approved it.\n\n"
        "<details><summary>Diagnosis and checks</summary>\n"
    )
    assert "unverified" not in explained.split("<details>")[0]
    assert "The fixture embeds an obsolete payload." in explained.split("<details>")[1]
