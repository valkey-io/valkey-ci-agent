from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from github.PaginatedList import PaginatedList

from scripts.ci_fix import issue_dispatch, issues, labelled
from scripts.ci_fix.issue_state import AGENT_LABEL
from scripts.ci_fix.models import MAX_FAILED_ATTEMPTS
from tests.test_ci_fix_issues import (
    _BOT,
    _issue,
    _job_issue_body,
    _linked_pr_event,
    _test_issue_body,
)


def _detector_issue(number, **kwargs):
    issue = _issue(number=number, **kwargs)
    issue.get_comments = lambda: list(issue.comments)
    return issue


def _reported(issue, run_id, action="refused"):
    failure = issues.parse_issue(issue)
    issues._terminal_comment(  # the comment the issue fix posts on finishing
        issue, replace(failure, run_id=run_id),
        repo_full_name="o/r", action=action, summary="no safe fix",
    )
    issue.comments[-1].user = SimpleNamespace(login=_BOT)


def _workflow(*runs, failed=()):
    """``runs`` as (status, title); ``failed`` the titles of failed completed runs."""
    def get_runs(status=None):
        if status == "failure":
            return [SimpleNamespace(status="completed", conclusion="failure", display_title=title)
                    for title in failed]
        return [SimpleNamespace(status=s, display_title=title) for s, title in runs]
    return SimpleNamespace(get_runs=get_runs)


def _dispatch(listed, workflow, *, max_runs=3):
    dispatched = []
    decisions = issue_dispatch.dispatch_issues(
        SimpleNamespace(get_issues=lambda **_kwargs: listed), workflow,
        repo_full_name="o/r", bot_login=_BOT, max_runs=max_runs,
        dispatch=lambda issue, run_id: dispatched.append(issue),
    )
    return dispatched, {d.issue: d.reason for d in decisions if d.action == "skipped"}


def test_empty_lazy_run_collection_does_not_crash():
    class EmptyRequester:
        per_page = 30

        def requestJsonAndCheck(self, *_args, **_kwargs):
            return {}, []

    runs = PaginatedList(SimpleNamespace, EmptyRequester(), firstUrl="/actions/workflows/1/runs")
    workflow = SimpleNamespace(get_runs=lambda: runs)

    assert issue_dispatch.active_runs(workflow) == []


def test_eligible_issues_are_dispatched_newest_first_until_the_cap_counts_running_ones():
    workflow = _workflow(
        ("in_progress", "CI fix issue #9"), ("completed", "CI fix issue #8"), ("queued", "CI fix issue #1"),
    )

    dispatched, skipped = _dispatch([_detector_issue(n) for n in (9, 8, 7, 6)], workflow, max_runs=4)

    assert dispatched == [8, 7]  # two of four already active
    assert "in progress" in skipped[9]


def test_nothing_is_listed_when_the_cap_is_already_reached():
    def unlisted(**_kwargs):
        raise AssertionError("listed issues with no free run")

    decisions = issue_dispatch.dispatch_issues(
        SimpleNamespace(get_issues=unlisted), _workflow(("in_progress", "CI fix issue #9")),
        repo_full_name="o/r", bot_login=_BOT, max_runs=1, dispatch=lambda _n, _r: None,
    )

    assert decisions == []


def test_issues_that_need_no_run_are_skipped_with_their_reason():
    linked = _detector_issue(5)
    linked.timeline.append(_linked_pr_event(5))
    tried = _detector_issue(4)
    _reported(tried, run_id=100)
    listed = [
        _detector_issue(6, assignees=("someone",)),
        linked,
        tried,
        _detector_issue(3, author="someone"),
    ]

    dispatched, skipped = _dispatch(listed, _workflow())

    assert dispatched == []
    assert skipped == {
        6: "issue #6 is assigned",
        5: "issue #5 already has an open linked pull request",
        4: "the bot is done with run 100",
    }  # #3 is not a detector issue at all


def test_a_newer_occurrence_than_the_one_reported_gets_another_run():
    issue = _detector_issue(7, body=_test_issue_body(run_id=200))
    _reported(issue, run_id=100)

    dispatched, _skipped = _dispatch([issue], _workflow())

    assert dispatched == [7]


def _done(issue):
    return issues.fix_reported(issue, issues.parse_issue(issue), repo_full_name="o/r", bot_login=_BOT)


def test_a_failed_attempt_posts_nothing_and_its_failed_run_counts_instead():
    """While the bot is unstable, a failed attempt is not commented on the issue."""
    issue = _detector_issue(4, body=_test_issue_body(run_id=100))
    issues._terminal_comment(
        issue, issues.parse_issue(issue), repo_full_name="o/r", action="failed", summary="agent crashed",
    )
    assert issue.comments == [] and not _done(issue)

    seen = []
    issue_dispatch.dispatch_issues(
        SimpleNamespace(get_issues=lambda **_kwargs: [issue]), _workflow(),
        repo_full_name="o/r", bot_login=_BOT, max_runs=3,
        dispatch=lambda number, run_id: seen.append((number, run_id)),
    )
    assert seen == [(4, 100)]  # the run's name carries the occurrence

    once = _workflow(failed=["CI fix issue #4 (run 100)", "CI fix issue #4 (run 99)", "CI fix issue #4"])
    assert _dispatch([issue], once)[0] == [4]
    twice = _workflow(failed=["CI fix issue #4 (run 100)"] * 2)
    dispatched, skipped = _dispatch([issue], twice)
    assert dispatched == [] and skipped[4] == "2 attempts at run 100 failed"


def test_a_result_ends_the_occurrence():
    opened = _detector_issue(5, body=_test_issue_body(run_id=100))
    _reported(opened, 100, action="opened")
    assert _done(opened)


def test_another_occurrences_result_does_not_count_for_this_one():
    issue = _detector_issue(4, body=_test_issue_body(run_id=200))
    _reported(issue, 100, action="opened")  # an earlier occurrence's draft

    assert not _done(issue)


def test_a_result_recorded_before_the_marker_still_counts():
    issue = _detector_issue(4, body=_test_issue_body(run_id=100))
    issue.comments.append(SimpleNamespace(
        user=SimpleNamespace(login=_BOT),
        body="**Automatic CI fix \u2014 failed.** The automatic fix attempt failed: boom\n\n"
             "Source: [run 100](https://github.com/o/r/actions/runs/100).",
    ))

    assert _done(issue)


# --- the other queue: issues a committer labelled --------------------------------------


def _labelled_issue(number, *, title="[BUG] wrong reply", event=500):
    applied = SimpleNamespace(
        id=event, event="labeled", label=SimpleNamespace(name=AGENT_LABEL), actor=SimpleNamespace(login="committer"),
        created_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
    )
    return SimpleNamespace(
        number=number, title=title, pull_request=None, labels=[SimpleNamespace(name=AGENT_LABEL)],
        get_events=lambda: [applied], get_comments=lambda: [],
    )


def _dispatch_labelled(listed, workflow, monkeypatch, *, max_runs=3, publish="", held=""):
    monkeypatch.setenv(labelled.PUBLICATION_ENV, publish)
    monkeypatch.setattr(labelled, "hold_reason", lambda _repo, _bot: held)
    dispatched = []
    decisions = issue_dispatch.dispatch_labelled(
        SimpleNamespace(get_issues=lambda **_kwargs: listed), workflow, bot_login=_BOT, max_runs=max_runs,
        dispatch=lambda issue, label, attempt: dispatched.append((issue, label, attempt)),
    )
    return dispatched, {d.issue: d.reason for d in decisions if d.action == "skipped"}


def test_labelled_issues_are_dispatched_bugs_first_until_the_cap_counts_running_ones(monkeypatch):
    workflow = _workflow(("in_progress", "Agent fix issue #9 (label 500)"), ("completed", "Agent fix issue #7 (label 500)"))
    listed = [_labelled_issue(9), _labelled_issue(8, title="[NEW] an option"), _labelled_issue(7), _labelled_issue(6)]

    dispatched, skipped = _dispatch_labelled(listed, workflow, monkeypatch)

    # One of three runs is active; the feature request waits for the bugs.
    assert dispatched == [(7, 500, 1), (6, 500, 1)]
    assert skipped == {9: "a ci-fix-labelled.yml run for it is in progress"}


def test_no_labelled_issue_is_listed_when_the_cap_is_already_reached(monkeypatch):
    def unlisted(**_kwargs):
        raise AssertionError("listed issues with no free run")

    workflow = _workflow(("queued", "Agent fix issue #9 (label 500)"))
    decisions = issue_dispatch.dispatch_labelled(
        SimpleNamespace(get_issues=unlisted), workflow, bot_login=_BOT, max_runs=1, dispatch=lambda *_args: None,
    )

    assert decisions == []


def test_a_labelling_is_given_up_after_its_attempts_failed(monkeypatch):
    failed = ["Agent fix issue #7 (label 500)"] * MAX_FAILED_ATTEMPTS + [
        "Agent fix issue #6 (label 500)", "Agent fix issue #5 (label 499)", "Agent fix issue #5",
    ]
    listed = [_labelled_issue(7), _labelled_issue(6), _labelled_issue(5)]

    dispatched, skipped = _dispatch_labelled(listed, _workflow(failed=failed), monkeypatch)

    # The next attempt's number goes with it: the last one reports its failure on the issue.
    # A failed run for an earlier labelling of the issue, or a manual one, does not count.
    assert dispatched == [(6, 500, 2), (5, 500, 1)]
    assert skipped == {7: f"{MAX_FAILED_ATTEMPTS} attempts at this labelling failed"}


def test_labelled_issues_wait_while_no_more_pull_requests_may_be_opened(monkeypatch):
    listed = [_labelled_issue(7), _labelled_issue(6)]
    held = "5 of the bot's pull requests for labelled issues are open (the most is 5)"

    dispatched, skipped = _dispatch_labelled(listed, _workflow(), monkeypatch, publish="draft-pr", held=held)

    assert dispatched == [] and skipped == {7: held, 6: held}
    # A change posted as a comment opens no pull request, so nothing holds it.
    assert _dispatch_labelled(listed, _workflow(), monkeypatch, held=held)[0] == [(7, 500, 1), (6, 500, 1)]
    assert _dispatch_labelled(listed, _workflow(), monkeypatch, publish="draft-pr")[0] == [(7, 500, 1), (6, 500, 1)]


def test_the_dispatcher_starts_the_labelled_workflow_with_inputs_it_declares(monkeypatch):
    started = []
    workflow = SimpleNamespace(create_dispatch=lambda ref, inputs: started.append((ref, inputs)) or True)
    agent_repo = SimpleNamespace(get_workflow=lambda name: {issue_dispatch.LABELLED_WORKFLOW: workflow}[name])
    repos = {"valkey-io/valkey": SimpleNamespace(), "valkey-io/valkey-ci-agent": agent_repo}
    monkeypatch.setattr(issue_dispatch, "Github", lambda **_kwargs: SimpleNamespace(get_repo=repos.__getitem__))
    monkeypatch.setattr(
        issue_dispatch, "dispatch_labelled", lambda _repo, _workflow, *, dispatch, **_kwargs: dispatch(7, 500, 2) or [],
    )
    for name, value in (("TARGET_TOKEN", "t"), ("GITHUB_TOKEN", "g"), ("GITHUB_REPOSITORY", "valkey-io/valkey-ci-agent"),
                        ("GITHUB_REF_NAME", "main")):
        monkeypatch.setenv(name, value)

    issue_dispatch.main(["--repo", "valkey-io/valkey", "--queue", "labelled", "--max-runs", "2"])

    assert started == [("main", {"issue": "7", "label_event": "500", "attempt": "2"})]
    declared = yaml.safe_load(
        Path(".github/workflows", issue_dispatch.LABELLED_WORKFLOW).read_text(encoding="utf-8"),
    )[True]["workflow_dispatch"]["inputs"]
    assert set(started[0][1]) <= set(declared)


@pytest.mark.parametrize(("label_event", "replay", "label"), [
    (500, "", "500"), (500, "c" * 40, "500"), (0, "", None), (0, "c" * 40, None),
])
def test_the_dispatcher_reads_the_labelled_workflows_run_name(label_event, replay, label):
    workflow = yaml.safe_load(Path(".github/workflows", issue_dispatch.LABELLED_WORKFLOW).read_text(encoding="utf-8"))
    template = workflow["run-name"]
    assert template == (
        "Agent fix issue #${{ inputs.issue }}"
        "${{ inputs.label_event && format(' (label {0})', inputs.label_event) || '' }}"
        "${{ inputs.replay && ' (replay)' || '' }}"
    )
    # The name as GitHub renders that template.
    name = re.sub(r"\$\{\{.*?\}\}", "{}", template).format(
        7, f" (label {label_event})" if label_event else "", " (replay)" if replay else "",
    )

    match = issue_dispatch.LABELLED_RUN_NAME.match(name)

    assert match is not None and (match.group("issue"), match.group("run")) == ("7", label)
    assert issue_dispatch.RUN_NAME.match(name) is None
