"""Looking after the pull request the bot opened for a labelled issue, until a committer takes it over."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from github.GithubException import GithubException

from scripts.backport import ci_followup
from scripts.backport.registry import BranchEntry, RepoEntry
from scripts.ci_fix import github_runs, upkeep
from scripts.ci_fix.github_runs import FailedJob
from scripts.ci_fix.issue_state import AGENT_LABEL
from scripts.ci_fix.models import (
    MAX_FAILED_ATTEMPTS,
    CandidateAction,
    FixCandidate,
    FixPlan,
    FixResult,
    FrontDoor,
    ResultKind,
    TargetKind,
)
from scripts.common.identity import BOT_EMAIL, BOT_NAME

_BOT = "valkeyrie-ops[bot]"
_REPO = "valkey-io/valkey"
_HEAD = "a" * 40
_TIP = "b" * 40
_BRANCH = "agent/fix/issue-7-500"
_AT = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
# The bots can push too: a bot's review takes nothing over because it is a bot's.
_PERMISSIONS = {"committer": "write", "triager": "read", "copilot[bot]": "write", "valkeyrie-ops[bot]": "write"}


def _login(name):
    return SimpleNamespace(login=name)


def _commit(name):
    """A commit as GitHub returns it: the bot's git identity maps to no account, so its commits have no author."""
    if name == BOT_NAME:
        return SimpleNamespace(author=None, commit=SimpleNamespace(author=SimpleNamespace(name=BOT_NAME, email=BOT_EMAIL)))
    git_author = SimpleNamespace(name=name or "Someone", email=f"{name or 'someone'}@example.com")
    return SimpleNamespace(author=_login(name) if name else None, commit=SimpleNamespace(author=git_author))


def _pull(*, mergeable=True, draft=True, commits=(BOT_NAME,), reviewers=(), comments=(), head_repo=_REPO):
    posted: list[str] = []
    ready: list[bool] = []
    pull = SimpleNamespace(
        number=77, state="open", draft=draft, mergeable=mergeable, user=_login(_BOT),
        base=SimpleNamespace(ref="unstable"),
        head=SimpleNamespace(ref=_BRANCH, sha=_HEAD, repo=SimpleNamespace(full_name=head_repo)),
        get_commits=lambda: [_commit(name) for name in commits],
        get_reviews=lambda: [SimpleNamespace(user=_login(name), body="") for name in reviewers],
        get_issue_comments=lambda: [SimpleNamespace(user=_login(_BOT), body=body) for body in comments],
        get_files=lambda: [SimpleNamespace(filename="src/t_string.c", patch="@@ -1 +1 @@\n-before\n+right")],
        create_issue_comment=posted.append,
        mark_ready_for_review=lambda: ready.append(True),
    )
    pull.posted, pull.ready = posted, ready
    return pull


def _issue(*, state="open", events=None):
    label = SimpleNamespace(
        id=500, event="labeled", label=SimpleNamespace(name=AGENT_LABEL), actor=_login("committer"), created_at=_AT,
    )
    return SimpleNamespace(
        number=7, title="[BUG] value is wrong", body="It says before.", state=state, assignees=[],
        labels=[], get_events=lambda: [label, *(events or ())],
        get_comments=lambda: [SimpleNamespace(user=_login("committer"), body="It should say right.")],
    )


def _run(run_id=10, *, sha=_HEAD, branch=_BRANCH, conclusion="failure", status="completed"):
    return SimpleNamespace(
        id=run_id, workflow_id=100, head_sha=sha, head_branch=branch, status=status, conclusion=conclusion,
        run_attempt=1, path=".github/workflows/ci.yml",
    )


def _gh(pull, issue=None, *, runs=(), last_edited=None):
    def permission(login):
        if login not in _PERMISSIONS:
            raise GithubException(404, {"message": "Not Found"}, None)
        return _PERMISSIONS[login]

    repo = SimpleNamespace(
        full_name=_REPO, default_branch="unstable",
        get_pulls=lambda state="open", **_kwargs: [pull] if state == "open" else [],
        get_pull=lambda _number: pull, get_issue=lambda _number: issue or _issue(),
        get_branch=lambda _branch: SimpleNamespace(commit=SimpleNamespace(sha=_TIP)),
        get_collaborator_permission=permission,
        get_workflow_runs=lambda branch, head_sha: [
            run for run in runs if (run.head_branch, run.head_sha) == (branch, head_sha)
        ],
        requester=SimpleNamespace(graphql_query=lambda _query, _variables: (
            {}, {"data": {"repository": {"issue": {"lastEditedAt": last_edited}}}},
        )),
    )
    return SimpleNamespace(get_repo=lambda _name: repo)


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    entry = RepoEntry(
        repo=_REPO, project_owner="valkey-io", project_owner_type="organization", language="c",
        branches=(BranchEntry(branch="9.0", project_number=18),), automatic_ci_followup=True,
        ci_followup_ignored_jobs=("*dco*",),
    )
    monkeypatch.setattr(upkeep, "load_registry", lambda _path: SimpleNamespace(get_repo=lambda _name: entry))
    monkeypatch.delenv("AGENT_FIX_PUSH_REPO", raising=False)


def _jobs(monkeypatch, by_run):
    """The failed jobs of each run, as the follow-up and the base branch's own CI read them."""
    def failed(_gh, _repo, run_id, **_kwargs):
        return list(by_run.get(run_id, ()))

    monkeypatch.setattr(github_runs, "failed_jobs_for_run", failed)
    monkeypatch.setattr(upkeep, "failed_jobs_for_run", failed)


def _plan(gh, prior_plans=()):
    return upkeep.plan_request(gh, None, {"repo": _REPO}, bot_login=_BOT, prior_plans=prior_plans)


# --- whose pull request it is ----------------------------------------------------------


def test_a_pull_request_only_the_bot_has_touched_is_still_the_bots():
    pull = _pull(commits=(BOT_NAME, _BOT), reviewers=("triager", "stranger", "copilot[bot]"))

    assert not upkeep.taken_over(_gh(pull).get_repo(_REPO), pull, _BOT)


@pytest.mark.parametrize("pull", [
    _pull(commits=(BOT_NAME, "committer")),
    _pull(commits=(BOT_NAME, "")),  # somebody's commit that GitHub maps to no account either
    _pull(reviewers=("stranger", "committer")),
])
def test_a_committers_review_or_anyone_elses_push_takes_it_over(pull):
    assert upkeep.taken_over(_gh(pull).get_repo(_REPO), pull, _BOT)


def test_a_push_is_rechecked_against_a_committer_taking_over_during_the_run():
    """The run took hours: a review that arrived meanwhile stops the push."""
    def plan_for(pull):
        task = SimpleNamespace(failure=SimpleNamespace(repo_full_name=_REPO, pr_number=77))
        return upkeep.taken_over_since(_gh(pull), SimpleNamespace(task=task), _BOT)

    assert plan_for(_pull()) == ""
    assert "A committer has reviewed this pull request or pushed to it since." == plan_for(_pull(reviewers=("committer",)))


def test_a_pull_request_a_committer_has_is_left_alone(monkeypatch):
    pull = _pull(mergeable=False, reviewers=("committer",))
    _jobs(monkeypatch, {10: [FailedJob("test-ubuntu-latest", id=3)]})

    result = _plan(_gh(pull, runs=[_run()]))

    assert result["action"] == "skipped"
    assert result["reason"] == "#77: a committer has reviewed it or pushed to it"
    assert pull.posted == [] and pull.ready == []


def test_nothing_is_planned_without_an_open_pull_request():
    gh = _gh(_pull())
    gh.get_repo(_REPO).get_pulls = lambda **_kwargs: []

    assert _plan(gh) == {"repo": _REPO, "action": "skipped", "reason": "no open pull request"}


# --- a merge conflict ------------------------------------------------------------------


def test_a_conflicting_change_is_made_again_on_the_base_branchs_tip():
    plan = _plan(_gh(_pull(mergeable=False)))

    assert isinstance(plan, FixPlan) and plan.front_door is FrontDoor.UPKEEP
    task, request = plan.task, plan.task.request
    assert task.target.kind is TargetKind.UPDATE_PR
    assert (task.target.branch, task.target.expected_sha, task.target.replaces) == (_BRANCH, _TIP, _HEAD)
    assert (task.failure.head_sha, task.failure.base_branch, task.failure.pr_number) == (_TIP, "unstable", 77)
    assert (task.failure.job_name, task.failure.workflow) == ("test-ubuntu-latest", "ci")
    assert (request.number, request.label_event, request.pr_number) == (7, 500, 77)
    assert request.earlier == "--- a/src/t_string.c\n+++ b/src/t_string.c\n@@ -1 +1 @@\n-before\n+right\n"
    assert request.comments == ("committer: It should say right.",) and not request.review


def test_a_pull_request_github_has_not_said_conflicts_is_not_made_again(monkeypatch):
    """``mergeable`` is null while GitHub works it out."""
    _jobs(monkeypatch, {})

    result = _plan(_gh(_pull(mergeable=None), runs=[_run(status="in_progress", conclusion=None)]))

    assert result["action"] == "skipped" and "conflicts" not in result["reason"]


def test_the_first_pull_request_that_needs_a_change_gets_the_run():
    taken, conflicting = _pull(mergeable=False, reviewers=("committer",)), _pull(mergeable=False)
    conflicting.number = 78
    gh = _gh(taken)
    repo = gh.get_repo(_REPO)
    repo.get_pulls = lambda state="open", **_kwargs: [taken, conflicting] if state == "open" else []
    repo.get_pull = {77: taken, 78: conflicting}.__getitem__

    plan = _plan(gh)

    assert isinstance(plan, FixPlan) and plan.task.failure.pr_number == 78


def test_a_conflict_the_bot_answered_for_is_not_tried_again():
    answered = upkeep._REBASE_MARKER.format(head=_HEAD, outcome="refused")
    other_head = upkeep._REBASE_MARKER.format(head="c" * 40, outcome="pushed")

    result = _plan(_gh(_pull(mergeable=False, comments=(other_head, answered))))

    assert result["reason"] == "#77: it conflicts, and the bot already answered for this head"
    assert isinstance(_plan(_gh(_pull(mergeable=False, comments=(other_head,)))), FixPlan)


def test_a_conflict_is_given_up_after_its_attempts_failed():
    earlier = _plan(_gh(_pull(mergeable=False)))
    failed = (earlier,) * MAX_FAILED_ATTEMPTS

    result = _plan(_gh(_pull(mergeable=False)), prior_plans=failed)

    assert f"{MAX_FAILED_ATTEMPTS} attempts to make the change again failed" in result["reason"]
    # An attempt whose fix job never started says nothing, nor does one for another head.
    unstarted = (earlier, replace(earlier, never_started=True))
    moved = tuple(replace(plan, task=replace(plan.task, target=replace(plan.task.target, replaces="c" * 40))) for plan in failed)
    assert isinstance(_plan(_gh(_pull(mergeable=False)), prior_plans=unstarted), FixPlan)
    assert isinstance(_plan(_gh(_pull(mergeable=False)), prior_plans=moved), FixPlan)


@pytest.mark.parametrize(("issue", "last_edited"), [
    (_issue(state="closed"), None),
    (_issue(), "2026-11-20T13:00:00Z"),
    (_issue(events=[SimpleNamespace(id=501, event="renamed", created_at=_AT + timedelta(hours=1))]), None),
])
def test_a_change_is_not_made_again_for_an_issue_nobody_vouches_for_now(issue, last_edited):
    gh = _gh(_pull(mergeable=False), issue, last_edited=last_edited)

    assert upkeep.pull_request_task(gh.get_repo(_REPO), _pull(), _BOT) is None
    assert "its issue is closed or was edited since it was labelled" in _plan(gh)["reason"]


def test_a_pull_request_whose_labelling_is_gone_is_not_changed():
    issue = _issue()
    issue.get_events = lambda: []

    assert upkeep.pull_request_task(_gh(_pull(), issue).get_repo(_REPO), _pull(), _BOT) is None


def test_the_task_of_changing_the_pull_request_is_on_its_head():
    gh = _gh(_pull())

    task = upkeep.pull_request_task(gh.get_repo(_REPO), _pull(), _BOT, review=("committer: rename it",))

    assert (task.target.kind, task.target.expected_sha, task.target.replaces) == (TargetKind.UPDATE_PR, _HEAD, "")
    assert (task.failure.head_sha, task.failure.pr_number) == (_HEAD, 77)
    assert task.request.review == ("committer: rename it",) and task.request.pr_number == 77


# --- its own CI ------------------------------------------------------------------------


def test_a_failure_of_its_own_ci_is_planned_like_a_followup(monkeypatch):
    _jobs(monkeypatch, {10: [FailedJob("DCO", id=1), FailedJob("test-ubuntu-latest", id=3)]})

    plan = _plan(_gh(_pull(), runs=[_run(), _run(20, sha=_TIP, branch="unstable", conclusion="success")]))

    assert isinstance(plan, FixPlan) and plan.front_door is FrontDoor.UPKEEP and plan.task.request is None
    failure, target = plan.task.failure, plan.task.target
    assert (failure.run_id, failure.job_id, failure.job_name, failure.workflow) == (10, 3, "test-ubuntu-latest", "ci")
    assert (failure.head_sha, failure.base_branch, failure.pr_number) == (_HEAD, "unstable", 77)
    assert "issue #7" in failure.description and "choose `refuse`" in failure.description
    assert (target.kind, target.branch, target.expected_sha, target.replaces) == (TargetKind.UPDATE_PR, _BRANCH, _HEAD, "")
    assert plan.markers == tuple(ci_followup.markers(failure, _run(), (FailedJob("test-ubuntu-latest", id=3),)))
    assert f"head={_HEAD} run=10 attempt=1 job=3" in plan.markers[0]


def test_a_failure_on_the_bots_fork_is_fixed_there_once_the_fork_is_the_bots(monkeypatch):
    _jobs(monkeypatch, {10: [FailedJob("test-ubuntu-latest", id=3)]})
    client = _gh(_pull(head_repo="valkeyrie/valkey"), runs=[_run()])

    assert _plan(client)["reason"] == "#77: head-repository-mismatch"
    monkeypatch.setenv("AGENT_FIX_PUSH_REPO", "valkeyrie/valkey")
    plan = _plan(client)

    assert isinstance(plan, FixPlan)
    assert (plan.task.target.repo_full_name, plan.task.target.base_repo_full_name) == ("valkeyrie/valkey", _REPO)


def test_a_failure_the_base_branch_has_too_is_not_the_pull_requests_to_fix(monkeypatch):
    _jobs(monkeypatch, {
        10: [FailedJob("test-ubuntu-latest", id=3)], 20: [FailedJob("test-ubuntu-latest", id=9)],
    })
    pull = _pull()

    result = _plan(_gh(pull, runs=[_run(), _run(20, sha=_TIP, branch="unstable")]))

    assert result["reason"] == "#77: no-unhandled-actionable-failures"
    assert pull.ready == []  # its CI is not green either


def test_a_base_branch_whose_ci_cannot_be_read_does_not_stop_the_fix(monkeypatch):
    _jobs(monkeypatch, {10: [FailedJob("test-ubuntu-latest", id=3)]})
    gh = _gh(_pull(), runs=[_run()])

    def unreadable(_branch):
        raise RuntimeError("GitHub is down")

    gh.get_repo(_REPO).get_branch = unreadable

    assert isinstance(_plan(gh), FixPlan)


def test_a_green_draft_is_marked_ready_for_review(monkeypatch):
    _jobs(monkeypatch, {})
    pull = _pull()
    # A workflow that was skipped, or said nothing either way, is not a failure.
    runs = [_run(conclusion="success"), _run(11, conclusion="skipped"), _run(12, conclusion="neutral")]
    for workflow_id, run in enumerate(runs, 100):
        run.workflow_id = workflow_id

    result = _plan(_gh(pull, runs=runs))

    assert pull.ready == [True]
    assert result["reason"] == "#77: its CI is green: marked ready for review"


def test_a_ci_fix_that_keeps_failing_is_given_up(monkeypatch):
    """A failed attempt leaves no comment: the plans of earlier runs are the record, as for a backport PR."""
    _jobs(monkeypatch, {10: [FailedJob("test-ubuntu-latest", id=3)]})
    client = _gh(_pull(), runs=[_run()])
    earlier = _plan(client)

    assert isinstance(_plan(client, prior_plans=(earlier,) * (MAX_FAILED_ATTEMPTS - 1)), FixPlan)
    result = _plan(client, prior_plans=(earlier,) * MAX_FAILED_ATTEMPTS)

    assert result["reason"] == "#77: no-unhandled-actionable-failures"


@pytest.mark.parametrize(("pull", "runs"), [
    (_pull(), [_run(status="in_progress", conclusion=None)]),
    (_pull(), []),
    (_pull(draft=False), [_run(conclusion="success")]),
])
def test_a_pull_request_is_not_marked_ready_before_its_ci_is_green(monkeypatch, pull, runs):
    _jobs(monkeypatch, {})

    result = _plan(_gh(pull, runs=runs))

    assert result["action"] == "skipped" and pull.ready == []


# --- the report ------------------------------------------------------------------------


def _reported(kind, *, rebase=True, **result):
    pull = _pull(mergeable=False)
    gh = _gh(pull)
    plan = _plan(gh)
    assert isinstance(plan, FixPlan) and (plan.task.request is not None) is rebase
    record = upkeep.report_result(gh, plan, FixResult(kind=kind, summary="done", task=plan.task, **result))
    return pull, record


def test_a_change_made_again_is_reported_on_the_pull_request_with_its_marker():
    candidate = FixCandidate(action=CandidateAction.PATCH, root_cause="The value is stale.")

    pull, record = _reported(ResultKind.PUSHED, candidate=candidate, commit_sha="c" * 40)

    assert len(pull.posted) == 1
    assert f"the change was made again on `{_TIP[:12]}` and pushed as {'c' * 40}" in pull.posted[0]
    assert pull.posted[0].endswith(upkeep._REBASE_MARKER.format(head=_HEAD, outcome="pushed"))
    assert (record["action"], record["pr"], record["commit"]) == ("pushed", 77, "c" * 40)
    # The head the answer was for is now answered.
    assert upkeep._REBASE_MARKER_RE.search(pull.posted[0]).group("head") == _HEAD


def test_a_failed_attempt_at_a_conflict_posts_nothing():
    pull, record = _reported(ResultKind.FAILED)

    assert pull.posted == [] and record["action"] == "failed"


def test_a_ci_fix_is_reported_as_the_followup_reports_it(monkeypatch):
    _jobs(monkeypatch, {10: [FailedJob("test-ubuntu-latest", id=3)]})
    plan = _plan(_gh(_pull(), runs=[_run()]))
    seen = []
    monkeypatch.setattr(ci_followup, "report_result", lambda _gh, plan, result: seen.append((plan, result)) or {"action": "pushed"})
    result = FixResult(kind=ResultKind.PUSHED, summary="pushed", task=plan.task)

    assert upkeep.report_result(object(), plan, result) == {"action": "pushed"}
    assert seen == [(plan, result)]
