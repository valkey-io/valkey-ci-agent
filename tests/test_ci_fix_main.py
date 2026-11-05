"""The PR command front door: its plan and its report on the pull request."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.ci_fix import main as main_mod
from scripts.ci_fix.gate import GateError, GateRejection
from scripts.ci_fix.models import (
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
            branch="agent/ci-fix/x",
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
    assert "Pushed `bbbbbbbbbbbb`" in wired.posted[0][2]
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
