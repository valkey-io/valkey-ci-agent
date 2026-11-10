from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from github.PaginatedList import PaginatedList

from scripts.ci_fix import issue_dispatch, issues
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
