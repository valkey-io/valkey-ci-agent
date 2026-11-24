"""The PR command front door: its plan and its report on the pull request."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.agent_fix import main as main_mod
from scripts.agent_fix.gate import GateError, GateRejection
from scripts.agent_fix.models import (
    Failure,
    FixPlan,
    FixPolicy,
    FixResult,
    FixTarget,
    FixTask,
    FrontDoor,
    ResultKind,
    TargetKind,
)

_SHA = "a" * 40
_BOT = "valkeyrie-ops[bot]"
_REQUEST = {"repo": "valkey-io/valkey", "pr": 42, "commenter": "alice", "comment_id": "7"}


def _task() -> FixTask:
    return FixTask(
        failure=Failure("valkey-io/valkey", 9, _SHA, 10, "build", base_branch="unstable", pr_number=42),
        target=FixTarget(
            kind=TargetKind.UPDATE_PR,
            repo_full_name="valkey-io/valkey",
            base_repo_full_name="valkey-io/valkey",
            branch="agent/fix/x",
            expected_sha=_SHA,
        ),
        policy=FixPolicy.GENERAL,
    )


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTOR", _BOT)
    posted: list[tuple[str, int, str]] = []
    reacted: list[tuple[int, ResultKind]] = []
    monkeypatch.setattr(main_mod, "_post_comment", lambda _gh, repo, pr, body: posted.append((repo, pr, body)))
    monkeypatch.setattr(
        main_mod, "_react_outcome", lambda _gh, _repo, comment_id, kind: reacted.append((comment_id, kind)),
    )

    def gate(result):
        build = MagicMock(return_value=result)
        monkeypatch.setattr(main_mod, "build_fix_task", build)
        return build

    return SimpleNamespace(posted=posted, reacted=reacted, gate=gate)


def test_an_authorized_command_is_planned_against_its_exact_job(wired) -> None:
    build = wired.gate(_task())

    plan = main_mod.plan_request(MagicMock(), None, _REQUEST, bot_login=_BOT)

    assert plan == FixPlan(front_door=FrontDoor.PR, task=_task(), comment_id=7)
    assert build.call_args.kwargs["commenter"] == "alice"
    assert wired.posted == [] and wired.reacted == []


def test_a_gate_refusal_is_posted_and_reacted_to(wired) -> None:
    wired.gate(GateRejection("not authorized"))

    result = main_mod.plan_request(MagicMock(), None, _REQUEST, bot_login=_BOT)

    assert result["action"] == ResultKind.REFUSED.value
    assert "No fix prepared: not authorized" in wired.posted[0][2]
    assert wired.reacted == [(7, ResultKind.REFUSED)]


def test_a_gate_error_stays_in_the_workflow(wired) -> None:
    wired.gate(GateError("temporary GitHub failure"))

    result = main_mod.plan_request(MagicMock(), None, _REQUEST, bot_login=_BOT)

    assert result["action"] == ResultKind.FAILED.value
    assert wired.posted == []
    assert wired.reacted == [(7, ResultKind.FAILED)]  # -1, no comment


def test_the_commenter_is_trusted_only_from_the_poller(wired, monkeypatch) -> None:
    build = wired.gate(_task())
    monkeypatch.setenv("GITHUB_ACTOR", "mallory")

    main_mod.plan_request(MagicMock(), None, _REQUEST, bot_login=_BOT)

    assert build.call_args.kwargs["commenter"] == "mallory"


@pytest.mark.parametrize("request_", [
    {**_REQUEST, "pr": 0},
    {**_REQUEST, "repo": "someone/valkey"},
    {**_REQUEST, "run_url": "https://example.com/no"},
])
def test_a_malformed_request_is_an_error(wired, request_) -> None:
    wired.gate(AssertionError("the gate is not reached"))

    with pytest.raises(ValueError):
        main_mod.plan_request(MagicMock(), None, request_, bot_login=_BOT)


def test_the_result_is_commented_and_reacted_to(wired) -> None:
    plan = FixPlan(front_door=FrontDoor.PR, task=_task(), comment_id=7)
    result = FixResult(kind=ResultKind.PUSHED, summary="pushed", task=_task(), commit_sha="b" * 40)

    report = main_mod.report_result(MagicMock(), plan, result)

    assert report["action"] == "pushed"
    assert f"Pushed {'b' * 40} to this branch" in wired.posted[0][2]
    assert wired.reacted == [(7, ResultKind.PUSHED)]


def test_a_failed_result_is_only_a_reaction(wired) -> None:
    plan = FixPlan(front_door=FrontDoor.PR, task=_task(), comment_id=7)
    result = FixResult(kind=ResultKind.FAILED, summary="not published", task=_task())

    report = main_mod.report_result(MagicMock(), plan, result)

    assert report["action"] == ResultKind.FAILED.value
    assert wired.posted == []
    assert wired.reacted == [(7, ResultKind.FAILED)]


def test_a_refusal_is_commented_and_reacted_to(wired) -> None:
    plan = FixPlan(front_door=FrontDoor.PR, task=_task(), comment_id=7)
    result = FixResult(kind=ResultKind.REFUSED, summary="not a code defect", task=_task())

    report = main_mod.report_result(MagicMock(), plan, result)

    assert report["action"] == ResultKind.REFUSED.value
    assert "No fix for `build`" in wired.posted[0][2]
    assert "Not a code defect." in wired.posted[0][2]
    assert wired.reacted == [(7, ResultKind.REFUSED)]


def test_a_comment_that_cannot_be_posted_fails_the_run_but_still_reacts(wired, monkeypatch) -> None:
    monkeypatch.setattr(main_mod, "_post_comment", MagicMock(side_effect=RuntimeError("403")))
    plan = FixPlan(front_door=FrontDoor.PR, task=_task(), comment_id=7)
    result = FixResult(kind=ResultKind.PUSHED, summary="pushed", task=_task(), commit_sha="b" * 40)

    with pytest.raises(RuntimeError):
        main_mod.report_result(MagicMock(), plan, result)
    assert wired.reacted == [(7, ResultKind.PUSHED)]


def test_reaction_uses_plus_one_for_published_result() -> None:
    requester = MagicMock()
    gh = MagicMock()
    gh.get_repo.return_value = SimpleNamespace(_requester=requester)

    main_mod._react_outcome(gh, "o/r", 7, ResultKind.SUGGESTED)

    requester.requestJsonAndCheck.assert_called_once_with(
        "POST",
        "/repos/o/r/issues/comments/7/reactions",
        input={"content": "+1"},
    )


def test_reaction_uses_minus_one_for_refusal() -> None:
    requester = MagicMock()
    gh = MagicMock()
    gh.get_repo.return_value = SimpleNamespace(_requester=requester)

    main_mod._react_outcome(gh, "o/r", 7, ResultKind.REFUSED)

    requester.requestJsonAndCheck.assert_called_once_with(
        "POST",
        "/repos/o/r/issues/comments/7/reactions",
        input={"content": "-1"},
    )


# --- `address`: a committer's review comments on the bot's own pull request --------------

_ADDRESS = {**_REQUEST, "command": "address", "hint": "keep it small"}


def _review_thread(*comments, path="src/a.c", line=12, resolved=False, outdated=False):
    return SimpleNamespace(
        path=path, line=line, resolved=resolved, outdated=outdated,
        comments=[SimpleNamespace(author_login=login, body=body) for login, body in comments],
    )


def _change_pull(*, ref="agent/fix/issue-7-500", author=_BOT, state="open", reviews=()):
    return SimpleNamespace(
        number=42, state=state, user=SimpleNamespace(login=author),
        head=SimpleNamespace(ref=ref, sha=_SHA, repo=SimpleNamespace(full_name="valkey-io/valkey")),
        get_reviews=lambda: [SimpleNamespace(user=SimpleNamespace(login=login), body=body) for login, body in reviews],
    )


@pytest.fixture
def review(wired, monkeypatch):
    """The `address` plan with GitHub faked: who is a committer, the review threads, and the task of the change."""
    monkeypatch.delenv("AGENT_FIX_PUSH_REPO", raising=False)
    # The bot can push too: what it wrote is left out because it is the bot's.
    monkeypatch.setattr(
        main_mod.labelled, "has_write_access", lambda _repo, login: login in ("alice", "committer", "valkeyrie-ops"),
    )
    monkeypatch.setattr(main_mod, "GitHubGraphQLClient", lambda token: token)
    asked: list[tuple[str, ...]] = []

    def change_task(_repo, _pull, _bot, *, review):
        asked.append(review)
        return wired.task

    monkeypatch.setattr(main_mod.upkeep, "pull_request_task", change_task)

    def plan(pull, threads=(), request=_ADDRESS):
        monkeypatch.setattr(main_mod, "list_review_threads", lambda _gql, _repo, _number: tuple(threads))
        gh = MagicMock()
        gh.get_repo.return_value = SimpleNamespace(full_name="valkey-io/valkey", get_pull=lambda _number: pull)
        return main_mod.plan_request(gh, None, request, bot_login=_BOT)

    wired.task, wired.asked, wired.plan = _task(), asked, plan
    return wired


def test_a_committers_review_comments_are_planned_as_one_change(review) -> None:
    pull = _change_pull(reviews=[("committer", "Please  rename\nthe helper."), ("stranger", "LGTM"), ("committer", "")])
    threads = [
        _review_thread(("committer", "Use the existing helper."), ("stranger", "or delete it"), ("valkeyrie-ops", "ok")),
        _review_thread(("committer", "already done"), resolved=True),
        _review_thread(("committer", "on an older diff"), outdated=True),
        _review_thread(("committer", "The whole file."), path="tests/unit/a.tcl", line=None),
    ]

    plan = review.plan(pull, threads)

    assert isinstance(plan, FixPlan) and plan.front_door is FrontDoor.PR and plan.comment_id == 7
    assert plan.task.failure.hint == "keep it small" and plan.task.target == _task().target
    # Committers' words only, from unresolved threads on the current diff, then the command's own hint.
    assert review.asked == [(
        "committer: Please rename the helper.",
        "committer on `src/a.c` line 12: Use the existing helper.",
        "committer on `tests/unit/a.tcl`: The whole file.",
        "alice: keep it small",
    )]
    assert review.posted == [] and review.reacted == []


def test_only_a_committer_may_have_review_comments_addressed(review) -> None:
    result = review.plan(_change_pull(), request={**_ADDRESS, "commenter": "mallory"})

    assert result["action"] == ResultKind.REFUSED.value and review.asked == []
    assert "`address` is a committer's command, and mallory does not have write access" in review.posted[0][2]
    assert review.reacted == [(7, ResultKind.REFUSED)]


@pytest.mark.parametrize("pull", [
    _change_pull(ref="agent/backport/sweep/9.0"),
    _change_pull(author="mallory"),
    _change_pull(state="closed"),
])
def test_review_comments_are_addressed_only_on_the_bots_own_change(review, pull) -> None:
    result = review.plan(pull, [_review_thread(("committer", "Use the helper."))])

    assert result["action"] == ResultKind.REFUSED.value and review.asked == []
    assert "an open pull request the bot opened for a labelled issue" in review.posted[0][2]


def test_there_has_to_be_a_committers_comment_to_address(review) -> None:
    threads = [_review_thread(("stranger", "why?")), _review_thread(("committer", "done"), resolved=True)]

    result = review.plan(_change_pull(reviews=[("stranger", "nit")]), threads, request={**_ADDRESS, "hint": ""})

    assert result["action"] == ResultKind.REFUSED.value and review.asked == []
    assert "There is no unresolved review comment from a committer to apply." in review.posted[0][2]


def test_review_comments_are_not_addressed_for_an_issue_nobody_vouches_for(review) -> None:
    review.task = None

    result = review.plan(_change_pull())

    assert result["action"] == ResultKind.REFUSED.value
    assert "No change was pushed: Its issue is closed, or was edited since it was labelled." in review.posted[0][2]


def test_a_review_that_cannot_be_read_stays_in_the_workflow(review, monkeypatch) -> None:
    def unreadable(_repo, _login):
        raise RuntimeError("GitHub is down")

    monkeypatch.setattr(main_mod.labelled, "has_write_access", unreadable)

    result = review.plan(_change_pull())

    assert result["action"] == ResultKind.FAILED.value
    assert review.posted == [] and review.reacted == [(7, ResultKind.FAILED)]
