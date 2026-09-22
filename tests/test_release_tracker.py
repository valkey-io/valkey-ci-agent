from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts.release import tracker as tracker_mod
from scripts.release.checks import CandidateCheck, CandidateCI
from scripts.release.models import ReleasePolicy

SHA = "a" * 40
TRACKER = tracker_mod.Tracker(
    repo="valkey-io/valkey",
    branch="9.1",
    version="9.1.2",
    stage="ga",
    tag="9.1.2",
    prep_branch="agent/release-cut/9.1.2-ga",
    prepare_run_id=123,
)


POLICY = ReleasePolicy(
    repo=TRACKER.repo,
    authorized_teams=("valkey-io/core-team",),
    branches=(TRACKER.branch,),
    checks_workflow="ci.yml",
    required_checks=("linux", "macos"),
)


def _run(*, status: str = "completed", conclusion: str | None = "success"):
    return SimpleNamespace(
        id=123,
        status=status,
        conclusion=conclusion,
        html_url="https://example/actions/runs/123",
    )


def _candidate_ci(*, linux: str = "success", macos: str = "success", status: str = "completed") -> CandidateCI:
    checks = tuple(
        CandidateCheck(
            name=name,
            status="completed" if conclusion in {"success", "failure"} else conclusion,
            conclusion=conclusion if conclusion in {"success", "failure"} else None,
            url=f"https://example/checks/{name}",
        )
        for name, conclusion in (("linux", linux), ("macos", macos))
    )
    return CandidateCI(
        workflow_url="https://example/actions/runs/456",
        workflow_status=status,
        workflow_conclusion="success" if all(check.passed for check in checks) else None,
        suite_id=7,
        checks=checks,
    )


def _issue() -> MagicMock:
    issue = MagicMock()
    issue.number = 42
    issue.user.login = "release-app[bot]"
    issue.get_comments.return_value = []
    return issue


def test_tracker_marker_round_trips_and_rejects_invalid_metadata() -> None:
    assert tracker_mod.parse_tracker(f"hello\n{TRACKER.marker()}\n") == TRACKER
    assert tracker_mod.parse_tracker("<!-- valkey-release-tracker:v1 {} -->") is None


@pytest.mark.parametrize(
    "payload",
    [
        '{"repo":null}',
        '{"repo":7}',
        '{"repo":"valkey-io/valkey --> forged"}',
        '{"repo":"valkey-io/valkey","branch":"9.1","version":"9.1.2","stage":"ga",'
        '"tag":"9.1.2","prep_branch":"agent/release-cut/9.1.2-ga","prepare_run_id":true}',
        "[" * 5000,
    ],
)
def test_poisoned_tracker_markers_are_contained(payload: str) -> None:
    assert tracker_mod.parse_tracker(f"{tracker_mod._TRACKER_PREFIX}{payload} -->") is None


def test_ensure_checks_issue_ownership_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    human = _issue()
    human.user.login = "maintainer"
    bot = _issue()
    bot.state = "open"
    bot.title = f"Release {TRACKER.tag}"
    bot.body = tracker_mod._issue_body(TRACKER, "valkey-io/valkey-ci-agent", include_marker=False)
    repo = MagicMock()
    repo.get_issues.return_value = [human, bot]
    gh = MagicMock()
    gh.get_repo.return_value = repo
    parsed = MagicMock(return_value=TRACKER)
    monkeypatch.setattr(tracker_mod, "_tracker_from_issue", parsed)

    assert tracker_mod.ensure_tracker(gh, TRACKER, agent_repo="valkey-io/valkey-ci-agent") is bot
    parsed.assert_called_once_with(bot, allow_body_fallback=True)


def test_ensure_reuse_preserves_live_status_and_rebinds_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Prepare rerun must not reset the dashboard to the empty initial
    render; it only points the authority marker at the new preparation run."""
    live_status = (
        f"{tracker_mod._STATUS_MARKER}\n{TRACKER.marker()}\n"
        "**Phase: Publish** — waiting for release approval\n"
    )
    status_comment = MagicMock()
    status_comment.body = live_status
    status_comment.user.login = "release-app[bot]"
    issue = _issue()
    issue.state = "open"
    issue.get_comments.return_value = [status_comment]
    repo = MagicMock()
    repo.get_issues.return_value = [issue]
    gh = MagicMock()
    gh.get_repo.return_value = repo
    monkeypatch.setattr(tracker_mod, "_tracker_from_issue", MagicMock(return_value=TRACKER))
    rerun = tracker_mod.Tracker(**{**TRACKER.__dict__, "prepare_run_id": 456})

    assert tracker_mod.ensure_tracker(gh, rerun, agent_repo="valkey-io/valkey-ci-agent") is issue

    issue.create_comment.assert_not_called()
    status_comment.edit.assert_called_once()
    updated = status_comment.edit.call_args.args[0]
    assert rerun.marker() in updated
    assert TRACKER.marker() not in updated
    assert "waiting for release approval" in updated


def test_ensure_reuse_with_unchanged_marker_leaves_comment_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_comment = MagicMock()
    status_comment.body = f"{tracker_mod._STATUS_MARKER}\n{TRACKER.marker()}\nlive evidence\n"
    status_comment.user.login = "release-app[bot]"
    issue = _issue()
    issue.state = "open"
    issue.get_comments.return_value = [status_comment]
    repo = MagicMock()
    repo.get_issues.return_value = [issue]
    gh = MagicMock()
    gh.get_repo.return_value = repo
    monkeypatch.setattr(tracker_mod, "_tracker_from_issue", MagicMock(return_value=TRACKER))

    tracker_mod.ensure_tracker(gh, TRACKER, agent_repo="valkey-io/valkey-ci-agent")

    issue.create_comment.assert_not_called()
    status_comment.edit.assert_not_called()



@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"repo": "valkey"}, "owner/name"),
        ({"branch": "unstable"}, "MAJOR.MINOR"),
        ({"version": "9.1"}, "MAJOR.MINOR.PATCH"),
        ({"stage": "rc0"}, "ga or rcN"),
        ({"tag": "9.1.3"}, "does not match"),
        ({"prep_branch": "agent/release-cut/other"}, "not canonical"),
        ({"prepare_run_id": True}, "must be positive"),
        ({"prepare_run_id": 0}, "must be positive"),
    ],
)
def test_tracker_validation_refuses_each_invalid_identity(
    changes: dict[str, object],
    message: str,
) -> None:
    candidate = tracker_mod.Tracker(**{**TRACKER.__dict__, **changes})
    with pytest.raises(ValueError, match=message):
        tracker_mod._validate_tracker(candidate)


def test_bot_status_comment_is_authoritative_over_edited_issue_body() -> None:
    issue = _issue()
    issue.body = tracker_mod.Tracker(
        **{**TRACKER.__dict__, "version": "9.1.3", "tag": "9.1.3", "prep_branch": "agent/release-cut/9.1.3-ga"}
    ).marker()
    comment = SimpleNamespace(
        user=SimpleNamespace(login=issue.user.login),
        body=f"{tracker_mod._STATUS_MARKER}\n{TRACKER.marker()}\nlive status",
    )
    issue.get_comments.return_value = [comment]
    assert tracker_mod._tracker_from_issue(issue) == TRACKER


def test_only_bot_owned_issues_are_accepted_as_dashboards() -> None:
    bot_issue = SimpleNamespace(user=SimpleNamespace(login="release-app[bot]"))
    owner_issue = SimpleNamespace(user=SimpleNamespace(login="SarthakAggarwal97"))
    other_issue = SimpleNamespace(user=SimpleNamespace(login="maintainer"))

    assert tracker_mod._is_bot_owned(bot_issue)
    assert not tracker_mod._is_bot_owned(owner_issue)
    assert not tracker_mod._is_bot_owned(other_issue)


def test_prep_pr_fallback_survives_deleted_head_branch() -> None:
    repo = MagicMock()
    expected = SimpleNamespace(
        head=SimpleNamespace(
            ref=TRACKER.prep_branch,
            repo=SimpleNamespace(full_name=TRACKER.repo),
        )
    )
    repo.get_pulls.side_effect = [[], [expected]]
    assert tracker_mod._find_prep_pr(repo, TRACKER) is expected
    assert repo.get_pulls.call_count == 2


def test_prepare_failure_is_visible_with_a_direct_next_action() -> None:
    body, summary = tracker_mod._render_status(
        TRACKER,
        prepare_run=_run(conclusion="failure"),
        pr=None,
        branch_head=SHA,
        candidate_sha="",
        candidate_ci=None,
        publish_run=None,
        release=None,
        production_run=None,
        agent_repo="valkey-io/valkey-ci-agent",
        dispatched=False,
    )
    assert summary == "preparation failed"
    assert "Release preparation failed" in body
    assert "rerun Prepare Release" in body
    assert "> [!CAUTION]" in body
    assert "img.shields.io/badge/-Prepare-cf222e" in body
    assert not any(symbol in body for symbol in "✅❌⏳⛔🛑⚠️🟦🟥🟩⬜")
    assert "—" not in body


def test_downstream_follow_up_links_cover_ga_outputs_without_new_api_access() -> None:
    body, summary = tracker_mod._render_status(
        TRACKER,
        prepare_run=_run(),
        pr=None,
        branch_head=SHA,
        candidate_sha="",
        candidate_ci=None,
        publish_run=None,
        release=SimpleNamespace(html_url="https://example/releases/9.1.2"),
        production_run=_run(),
        agent_repo="valkey-io/valkey-ci-agent",
        dispatched=False,
    )

    assert summary == "production automation completed"
    assert "valkey-hashes/blob/main/README" in body
    assert "valkey-container/pulls?q=is%3Apr+head%3Aupdate-9.1.2" in body
    assert "valkey-doc/tree/9.1.2" in body
    assert "valkey-io.github.io/pulls?q=is%3Apr+head%3Aupdate-website-9.1.2" in body
    assert "valkey-helm/pulls?q=is%3Apr+head%3Aupdate-valkey-9.1.2" in body
    assert "valkey-bundle/pulls?q=is%3Apr+head%3Avalkey-bundle-update" in body
    assert "**Manual follow-up:**" in body
    assert "Release owner review" in body
    assert "review and merge every linked downstream PR" in body


def test_8_0_follow_up_omits_bundle_and_requires_manual_pr_merges() -> None:
    tracker = tracker_mod.Tracker(
        **{
            **TRACKER.__dict__,
            "branch": "8.0",
            "version": "8.0.12",
            "tag": "8.0.12",
            "prep_branch": "agent/release-cut/8.0.12-ga",
        }
    )
    body, summary = tracker_mod._render_status(
        tracker,
        prepare_run=_run(),
        pr=None,
        branch_head=SHA,
        candidate_sha="",
        candidate_ci=None,
        publish_run=None,
        release=SimpleNamespace(html_url="https://example/releases/8.0.12"),
        production_run=_run(),
        agent_repo="valkey-io/valkey-ci-agent",
        dispatched=False,
    )

    assert summary == "production automation completed"
    assert "**Manual follow-up:**" in body
    assert "review and merge every linked downstream PR" in body
    assert "valkey-bundle" not in body
    assert "Bundle" not in body


def test_rc_follow_up_omits_ga_only_outputs() -> None:
    rc = tracker_mod.Tracker(
        **{
            **TRACKER.__dict__,
            "version": "9.1.0",
            "stage": "rc1",
            "tag": "9.1.0-rc1",
            "prep_branch": "agent/release-cut/9.1.0-rc1",
        }
    )
    links = tracker_mod._downstream_links(rc)
    assert "Container PR search" in links
    assert "Bundle PR search" in links
    assert "Documentation" not in links
    assert "Website" not in links
    assert "Helm" not in links


def test_issue_body_is_a_compact_maintainer_control_center() -> None:
    body = tracker_mod._issue_body(TRACKER, "valkey-io/valkey-ci-agent")
    assert '<div align="center">' in body
    assert "stable release identity and operator guidance" in body
    assert "Prepare` → `Review notes` → `Candidate CI` → `Qualification" in body
    assert f"[`{TRACKER.prep_branch}`](https://github.com/{TRACKER.repo}/tree/{TRACKER.prep_branch})" in body
    assert "Prepare run 123" in body
    assert "## Human checkpoints" in body
    assert "- [ ]" not in body
    assert "review and merge every linked downstream PR" in body
    assert "confirm Bundle" not in body
    assert "Editing this issue never authorizes" in body
    assert "—" not in body


def test_merged_pr_at_branch_head_dispatches_publication_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue = _issue()
    repo = MagicMock()
    agent = MagicMock()
    agent.default_branch = "main"
    agent.get_workflow_run.return_value = _run()
    workflow = MagicMock()
    workflow.create_dispatch.return_value = None  # PyGithub may return no body on HTTP 204.
    pr = SimpleNamespace(
        merged=True,
        merge_commit_sha=SHA,
        number=7,
        html_url="https://example/pull/7",
        state="closed",
        draft=False,
    )
    monkeypatch.setattr(tracker_mod, "_find_prep_pr", lambda *a: pr)
    monkeypatch.setattr(tracker_mod, "_find_release", lambda *a: None)
    monkeypatch.setattr(tracker_mod, "_branch_head", lambda *a: SHA)
    monkeypatch.setattr(tracker_mod, "_find_run", lambda *a: None)
    monkeypatch.setattr(tracker_mod, "evaluate_candidate_ci", lambda *a: _candidate_ci())
    monkeypatch.setattr(tracker_mod, "_find_production_run", lambda *a: None)

    result = tracker_mod._sync_one(
        issue,
        TRACKER,
        repo,
        agent,
        MagicMock(),
        workflow,
        agent_repo="valkey-io/valkey-ci-agent",
        policy=POLICY,
        dispatch=True,
    )

    workflow.create_dispatch.assert_called_once_with(
        "main",
        inputs={"branch": "9.1", "candidate_sha": SHA},
    )
    assert result == "#42: publication dispatched"
    assert "| Qualification |" in issue.create_comment.call_args.args[0]
    assert "img.shields.io/badge/-Starting-0969da" in issue.create_comment.call_args.args[0]


def test_moved_branch_blocks_automatic_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    issue = _issue()
    agent = MagicMock()
    agent.get_workflow_run.return_value = _run()
    workflow = MagicMock()
    pr = SimpleNamespace(
        merged=True,
        merge_commit_sha=SHA,
        number=7,
        html_url="https://example/pull/7",
        state="closed",
        draft=False,
    )
    monkeypatch.setattr(tracker_mod, "_find_prep_pr", lambda *a: pr)
    monkeypatch.setattr(tracker_mod, "_find_release", lambda *a: None)
    monkeypatch.setattr(tracker_mod, "_branch_head", lambda *a: "b" * 40)
    monkeypatch.setattr(tracker_mod, "_find_run", lambda *a: None)
    monkeypatch.setattr(tracker_mod, "evaluate_candidate_ci", lambda *a: _candidate_ci())

    result = tracker_mod._sync_one(
        issue,
        TRACKER,
        MagicMock(),
        agent,
        MagicMock(),
        workflow,
        agent_repo="valkey-io/valkey-ci-agent",
        policy=POLICY,
        dispatch=True,
    )

    workflow.create_dispatch.assert_not_called()
    assert result == "#42: candidate invalidated by branch movement"
    assert "Rerun Prepare Release" in issue.create_comment.call_args.args[0]


def test_existing_exact_publication_run_prevents_duplicate_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue = _issue()
    agent = MagicMock()
    agent.get_workflow_run.return_value = _run()
    workflow = MagicMock()
    publication = _run(status="in_progress", conclusion=None)
    pr = SimpleNamespace(
        merged=True,
        merge_commit_sha=SHA,
        number=7,
        html_url="https://example/pull/7",
        state="closed",
        draft=False,
    )
    monkeypatch.setattr(tracker_mod, "_find_prep_pr", lambda *a: pr)
    monkeypatch.setattr(tracker_mod, "_find_release", lambda *a: None)
    monkeypatch.setattr(tracker_mod, "_branch_head", lambda *a: SHA)
    find_run = MagicMock(return_value=publication)
    monkeypatch.setattr(tracker_mod, "_find_run", find_run)
    monkeypatch.setattr(tracker_mod, "evaluate_candidate_ci", lambda *a: _candidate_ci())

    result = tracker_mod._sync_one(
        issue,
        TRACKER,
        MagicMock(),
        agent,
        MagicMock(),
        workflow,
        agent_repo="valkey-io/valkey-ci-agent",
        policy=POLICY,
        dispatch=True,
    )

    title = f"Publish release on {TRACKER.branch} @ {SHA}"
    find_run.assert_called_once_with(workflow, title, SHA)
    workflow.create_dispatch.assert_not_called()
    assert result == "#42: validating and qualifying"


def test_stale_waiting_publication_is_cancelled_before_redispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publication requires its workflow revision to still be controller main
    (release-publish.yml fails otherwise), so an in-flight run from an older
    controller commit can never succeed: it is cancelled and re-dispatched,
    keeping exactly one live approval prompt."""
    issue = _issue()
    repo = MagicMock()
    agent = MagicMock()
    agent.default_branch = "main"
    agent.get_workflow_run.return_value = _run()
    workflow = MagicMock()
    workflow.create_dispatch.return_value = True
    stale = _run(status="waiting", conclusion=None)
    stale.head_sha = "b" * 40
    stale.cancel = MagicMock(return_value=True)
    pr = SimpleNamespace(
        merged=True,
        merge_commit_sha=SHA,
        number=7,
        html_url="https://example/pull/7",
        state="closed",
        draft=False,
    )
    monkeypatch.setattr(tracker_mod, "_find_prep_pr", lambda *a: pr)
    monkeypatch.setattr(tracker_mod, "_find_release", lambda *a: None)
    monkeypatch.setattr(tracker_mod, "_branch_head", MagicMock(side_effect=[SHA, "c" * 40]))
    monkeypatch.setattr(tracker_mod, "_find_run", MagicMock(side_effect=[None, stale]))
    monkeypatch.setattr(tracker_mod, "evaluate_candidate_ci", lambda *a: _candidate_ci())
    monkeypatch.setattr(tracker_mod, "_find_production_run", lambda *a: None)

    result = tracker_mod._sync_one(
        issue,
        TRACKER,
        repo,
        agent,
        MagicMock(),
        workflow,
        agent_repo="valkey-io/valkey-ci-agent",
        policy=POLICY,
        dispatch=True,
    )

    stale.cancel.assert_called_once_with()
    workflow.create_dispatch.assert_called_once_with(
        "main",
        inputs={"branch": "9.1", "candidate_sha": SHA},
    )
    assert result == "#42: publication dispatched"


def test_stale_completed_failure_does_not_suppress_redispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publication run that already failed at an older controller commit is
    not adopted: the head-filtered lookup misses it and the completed-run
    fallback is head-scoped, so the watcher dispatches a fresh run."""
    issue = _issue()
    repo = MagicMock()
    agent = MagicMock()
    agent.default_branch = "main"
    agent.get_workflow_run.return_value = _run()
    workflow = MagicMock()
    workflow.create_dispatch.return_value = True
    failed = _run(status="completed", conclusion="failure")
    failed.head_sha = "b" * 40
    pr = SimpleNamespace(
        merged=True,
        merge_commit_sha=SHA,
        number=7,
        html_url="https://example/pull/7",
        state="closed",
        draft=False,
    )
    monkeypatch.setattr(tracker_mod, "_find_prep_pr", lambda *a: pr)
    monkeypatch.setattr(tracker_mod, "_find_release", lambda *a: None)
    monkeypatch.setattr(tracker_mod, "_branch_head", MagicMock(side_effect=[SHA, "c" * 40]))
    monkeypatch.setattr(tracker_mod, "_find_run", MagicMock(side_effect=[None, failed]))
    monkeypatch.setattr(tracker_mod, "evaluate_candidate_ci", lambda *a: _candidate_ci())
    monkeypatch.setattr(tracker_mod, "_find_production_run", lambda *a: None)

    result = tracker_mod._sync_one(
        issue,
        TRACKER,
        repo,
        agent,
        MagicMock(),
        workflow,
        agent_repo="valkey-io/valkey-ci-agent",
        policy=POLICY,
        dispatch=True,
    )

    workflow.create_dispatch.assert_called_once_with(
        "main",
        inputs={"branch": "9.1", "candidate_sha": SHA},
    )
    assert result == "#42: publication dispatched"


def _titled(title: str, *, status: str = "completed", conclusion: str | None = "success", run_id: int = 123):
    run = _run(status=status, conclusion=conclusion)
    run.id = run_id
    run.display_title = title
    run.head_sha = "b" * 40  # an older controller head, as after any merge
    # The real 9.2.0-rc1 run: qualification finished 22:37, the approval wait
    # ended 22:48, the run itself 22:49. Present so a test can prove the run's
    # end is not attributed to an earlier stage.
    run.updated_at = datetime(2026, 9, 16, 22, 49, tzinfo=timezone.utc)
    # the stale-active path cancels before re-dispatching
    run.cancel = MagicMock(return_value=True)
    return run


def _publish_workflow(*history):
    """A workflow whose get_runs honours the status filter, newest first."""
    workflow = MagicMock()
    workflow.name = "Publish Release"

    def get_runs(head_sha: str = "", status: str = ""):
        runs = list(history)
        if head_sha:
            runs = [run for run in runs if getattr(run, "head_sha", "") == head_sha]
        if status == "success":
            runs = [r for r in runs if r.status == "completed" and r.conclusion == "success"]
        return runs

    workflow.get_runs.side_effect = get_runs
    return workflow


def _shipped_body(monkeypatch: pytest.MonkeyPatch, workflow, *, release: Any = ...) -> str:
    """Render a tracker whose candidate shipped, driving the real lookups."""
    issue = _issue()
    agent = MagicMock()
    agent.default_branch = "main"
    agent.get_workflow_run.return_value = _run()
    pr = SimpleNamespace(
        merged=True,
        merge_commit_sha=SHA,
        number=7,
        html_url="https://example/pull/7",
        state="closed",
        draft=False,
    )
    if release is ...:
        release = SimpleNamespace(
            tag_name=TRACKER.tag,
            html_url=f"https://example/releases/{TRACKER.tag}",
            draft=False,
            prerelease=True,
            published_at=datetime(2026, 9, 16, 22, 49, tzinfo=timezone.utc),
        )
    monkeypatch.setattr(tracker_mod, "_find_prep_pr", lambda *a: pr)
    monkeypatch.setattr(tracker_mod, "_find_release", lambda *a: release)
    # controller main has moved on since publication
    monkeypatch.setattr(tracker_mod, "_branch_head", MagicMock(side_effect=[SHA, "c" * 40]))
    monkeypatch.setattr(tracker_mod, "evaluate_candidate_ci", lambda *a: _candidate_ci())
    monkeypatch.setattr(tracker_mod, "_find_production_run", lambda *a: None)

    tracker_mod._sync_one(
        issue,
        TRACKER,
        MagicMock(),
        agent,
        MagicMock(),
        workflow,
        agent_repo="valkey-io/valkey-ci-agent",
        policy=POLICY,
        dispatch=True,
    )
    return issue.create_comment.call_args.args[0]


_TITLE = f"Publish release on {TRACKER.branch} @ {SHA}"


def test_shipped_release_keeps_qualification_passed_after_controller_main_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SUCCEEDED publication from an older controller head still renders.

    The head-filtered lookup exists so a stale run cannot suppress a
    re-dispatch, but the controller's own main advances after every release
    (a merge into valkey-ci-agent is enough). Applying that filter to a
    completed success made a published release render as one that never
    qualified: 'Qualification: Not started / No Publish run' beside
    'Publication: Published'. Observed on the 9.2.0-rc1 tracker.
    """
    # A newer success for a DIFFERENT candidate sits in front of ours, so a
    # lookup that forgot to match the title would name the wrong run.
    other = _titled("Publish release on 9.2 @ " + "d" * 40, run_id=999)
    body = _shipped_body(monkeypatch, _publish_workflow(other, _titled(_TITLE, run_id=800)))
    assert "No Publish run" not in body
    assert "Qualification has not passed" not in body
    assert "img.shields.io/badge/-Passed-1a7f37" in body
    assert "Publish run 800" in body
    assert "Publish run 999" not in body


@pytest.mark.parametrize(
    "newer",
    [
        _titled(_TITLE, conclusion="failure", run_id=901),
        _titled(_TITLE, conclusion="cancelled", run_id=902),
        _titled(_TITLE, status="in_progress", conclusion=None, run_id=903),
    ],
    ids=["newer-failure", "newer-cancelled", "newer-active"],
)
def test_a_later_publish_attempt_does_not_hide_the_run_that_shipped(
    monkeypatch: pytest.MonkeyPatch, newer
) -> None:
    # A maintainer re-dispatching Publish for a candidate that already
    # shipped is ordinary recovery, and the operational lookup answers with
    # that newest run (active first, else newest completed whatever its
    # conclusion). History must still name the run that produced the
    # release, so the display lookup asks GitHub for successes only.
    body = _shipped_body(monkeypatch, _publish_workflow(newer, _titled(_TITLE, run_id=800)))
    assert "No Publish run" not in body
    assert "Publish run 800" in body
    assert "img.shields.io/badge/-Failed-cf222e" not in body


def test_an_unshipped_stale_success_still_triggers_the_redispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The mutant this guards against: adopting the stale success before or
    # regardless of the dispatch decision. With no release, the watcher must
    # still dispatch, and the dashboard must not claim publication happened.
    workflow = _publish_workflow(_titled(_TITLE, run_id=700))
    workflow.create_dispatch.return_value = True
    body = _shipped_body(monkeypatch, workflow, release=None)

    workflow.create_dispatch.assert_called_once_with(
        "main",
        inputs={"branch": TRACKER.branch, "candidate_sha": SHA},
    )
    assert "Publish workflow dispatched" in body
    assert "img.shields.io/badge/-Publication-0969da" not in body


def _job(name: str, *, conclusion: str = "success", started: int = 0, completed: int = 0):
    return SimpleNamespace(
        name=name,
        conclusion=conclusion,
        started_at=datetime(2026, 9, 16, 22, started, tzinfo=timezone.utc) if started else None,
        completed_at=datetime(2026, 9, 16, 22, completed, tzinfo=timezone.utc) if completed else None,
    )


_REAL_JOBS = (
    # the shape of the real 9.2.0-rc1 publish run
    _job("Validate candidate and render plan", started=33, completed=34),
    _job("Qualify exact candidate / Generate build matrix", started=34, completed=34),
    _job("Qualify exact candidate / Qualify x86 archives / Build package", started=34, completed=37),
    _job("Qualify exact candidate / Qualification summary", started=37, completed=37),
    # a skipped child whose timestamps are out of order, as GitHub reports them
    _job("Qualify exact candidate / Publish package", conclusion="skipped", started=43, completed=43),
    _job("Bind qualification revision to approval plan", started=37, completed=38),
    _job("Publish approved release", started=48, completed=49),
)


def _jobs_api(*jobs, latest=None):
    """A run.jobs() that honours GitHub's filter: `all` spans every attempt."""

    def api(_filter=None):
        assert _filter in {"all", "latest", None}, _filter
        return list(latest if (_filter != "all" and latest is not None) else jobs)

    return api


def test_publish_evidence_reads_each_stage_from_its_own_job() -> None:
    # One run spans qualification, the approval wait and publication, so the
    # run's end (22:49) belongs to none of the first two: qualification
    # finished at 22:37 and approval released the publish job at 22:48.
    run = SimpleNamespace(id=1, jobs=_jobs_api(*_REAL_JOBS))
    qualified_at, approved_at, published = tracker_mod._publish_evidence(run)
    assert qualified_at == datetime(2026, 9, 16, 22, 37, tzinfo=timezone.utc)
    assert approved_at == datetime(2026, 9, 16, 22, 48, tzinfo=timezone.utc)
    assert published is True


def test_publish_evidence_ignores_skipped_children_and_degrades_silently() -> None:
    # A skipped child reports nonsensical ordering, and a renamed job must
    # cost a timestamp and the proof rather than produce a wrong one or raise.
    renamed = (_job("Qualify something else / Summary", started=37, completed=44),)
    empty = tracker_mod._PublishEvidence()
    assert tracker_mod._publish_evidence(SimpleNamespace(id=1, jobs=_jobs_api(*renamed))) == empty

    def boom(_filter=None):
        raise RuntimeError("jobs unavailable")

    assert tracker_mod._publish_evidence(SimpleNamespace(id=1, jobs=boom)) == empty
    assert tracker_mod._publish_evidence(None) == empty


def test_publish_evidence_spans_every_attempt_of_a_rerun() -> None:
    # A rerun keeps one run id; the default (latest) listing shows only the
    # newest attempt, in which qualification failed and publish never ran.
    # Attempt 1 is the one that shipped, and it is only visible under `all`.
    attempt_2 = (
        _job(
            "Qualify exact candidate / Qualify x86 archives / Build package",
            conclusion="failure",
            started=50,
            completed=52,
        ),
        _job("Publish approved release", conclusion="skipped", started=0, completed=0),
    )
    run = SimpleNamespace(id=1, jobs=_jobs_api(*_REAL_JOBS, *attempt_2, latest=attempt_2))
    evidence = tracker_mod._publish_evidence(run)
    assert evidence.published is True
    assert evidence.approved_at == datetime(2026, 9, 16, 22, 48, tzinfo=timezone.utc)
    # the failed attempt's qualification does not move the finish time
    assert evidence.qualified_at == datetime(2026, 9, 16, 22, 37, tzinfo=timezone.utc)


def test_passed_rows_state_each_stage_own_completion_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shipped = _titled(_TITLE)
    shipped.jobs = _jobs_api(*_REAL_JOBS)
    body = _shipped_body(monkeypatch, _publish_workflow(shipped))
    qualification = next(line for line in body.splitlines() if "| Qualification |" in line)
    approval = next(line for line in body.splitlines() if "| Release approval |" in line)
    assert "· finished 2026-09-16 22:37 UTC" in qualification
    assert "· approved 2026-09-16 22:48 UTC" in approval
    # never the run's own end, which is 11 minutes after qualification
    assert "22:49" not in qualification


def test_a_rerun_of_the_shipped_run_does_not_erase_its_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A rerun keeps ONE run id and the listing shows only the newest
    # attempt, so `status=success` stops matching the moment someone reruns
    # a shipped publication. The proof is the protected publish job having
    # succeeded in some attempt, which `jobs?filter=all` still reports.
    rerun = _titled(_TITLE, status="in_progress", conclusion=None, run_id=555)
    rerun.jobs = _jobs_api(*_REAL_JOBS)
    body = _shipped_body(monkeypatch, _publish_workflow(rerun))
    assert "No Publish run" not in body
    assert "Qualification has not passed" not in body
    assert "img.shields.io/badge/-Passed-1a7f37" in body
    assert "latest attempt in_progress" in body
    assert "· approved 2026-09-16 22:48 UTC" in body


def test_a_shipping_run_that_failed_after_publishing_still_reads_as_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # onboard-backports failing AFTER the release exists makes the run's
    # conclusion `failure`, so `status=success` never finds it. Its publish
    # job succeeded, and that is what created the release.
    shipped = _titled(_TITLE, conclusion="failure", run_id=808)
    shipped.jobs = _jobs_api(*_REAL_JOBS, _job("Onboard first-GA backport automation", conclusion="failure"))
    body = _shipped_body(monkeypatch, _publish_workflow(shipped))
    for stage in ("| Qualification |", "| Release approval |"):
        row = next(line for line in body.splitlines() if stage in line)
        assert "latest attempt failure" in row, row
        assert "-Unverified-" not in row, row
    assert "img.shields.io/badge/-Passed-1a7f37" in body
    assert "img.shields.io/badge/-Approved-1a7f37" in body


def test_a_failed_run_beside_a_release_is_flagged_not_claimed_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The run for this exact candidate failed in qualification and its publish
    # job never ran, yet a release exists: someone created it by hand. The
    # run's conclusion alone cannot tell this from onboard-backports failing
    # after a real publication; the publish job can.
    failed = _titled(_TITLE, conclusion="failure", run_id=909)
    failed.jobs = _jobs_api(
        _job(
            "Qualify exact candidate / Qualify x86 archives / Build package",
            conclusion="failure",
            started=34,
            completed=36,
        ),
        _job("Publish approved release", conclusion="skipped"),
    )
    body = _shipped_body(monkeypatch, _publish_workflow(failed))
    for stage in ("| Qualification |", "| Release approval |"):
        row = next(line for line in body.splitlines() if stage in line)
        assert "img.shields.io/badge/-Unverified-9a6700" in row, row
        assert "[Publish run 909](" in row and "did not publish it" in row, row
        assert "-Passed-" not in row and "-Approved-" not in row, row
    assert "investigate an out-of-band release" in body


def test_jobs_are_not_read_while_nothing_would_show_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A running run with no release renders no stamp and needs no proof, so
    # the extra jobs call is skipped on that pass.
    running = _titled(_TITLE, status="in_progress", conclusion=None, run_id=1111)
    running.head_sha = "c" * 40
    running.jobs = MagicMock()
    _shipped_body(monkeypatch, _publish_workflow(running), release=None)
    running.jobs.assert_not_called()
    running.cancel.assert_not_called()


def test_a_release_with_no_publish_run_is_flagged_not_claimed_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This is also how a hand-created, out-of-band release looks, so the row
    # must not go green, and "wait for qualification" would be impossible
    # advice: an existing release suppresses any further dispatch.
    body = _shipped_body(monkeypatch, _publish_workflow())
    for stage in ("| Qualification |", "| Release approval |"):
        row = next(line for line in body.splitlines() if stage in line)
        assert "img.shields.io/badge/-Unverified-9a6700" in row, row
        assert "-Passed-" not in row and "-Approved-" not in row, row
    assert "no matching Publish run found" in body
    assert "investigate an out-of-band release" in body
    assert "Wait for exact-candidate qualification" not in body


def test_a_publish_run_is_never_cancelled_once_the_release_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The run may still be finishing post-publication work (first-GA backport
    # onboarding). Cancelling it would interrupt that AND destroy the evidence
    # this dashboard reports.
    active = _titled(_TITLE, status="in_progress", conclusion=None, run_id=606)
    _shipped_body(monkeypatch, _publish_workflow(active))
    active.cancel.assert_not_called()


def test_an_unshipped_stale_active_run_is_still_cancelled_and_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The behaviour the release gate must not disturb: nothing shipped, so a
    # run from an older controller head can never succeed and is replaced.
    active = _titled(_TITLE, status="in_progress", conclusion=None, run_id=707)
    workflow = _publish_workflow(active)
    workflow.create_dispatch.return_value = True
    _shipped_body(monkeypatch, workflow, release=None)
    active.cancel.assert_called_once_with()
    workflow.create_dispatch.assert_called_once_with(
        "main",
        inputs={"branch": TRACKER.branch, "candidate_sha": SHA},
    )


def test_publication_row_states_when_the_release_was_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The release's own published_at, not the Publish run's end: one run
    # spans qualification, approval and publication, so attributing its
    # finish to an earlier stage would misreport by the approval wait
    # (11 minutes on the 9.2.0-rc1 run).
    body = _shipped_body(monkeypatch, _publish_workflow(_titled(_TITLE)))
    assert "· published 2026-09-16 22:49 UTC" in body
    # The run's own end (22:49 on this fixture) must never be attributed to a
    # stage it merely contains; those rows carry their own job's time or none.
    for stage in ("| Qualification |", "| Release approval |"):
        row = next(line for line in body.splitlines() if stage in line)
        assert "22:49" not in row, row


def test_stamp_is_silent_for_anything_that_is_not_a_moment() -> None:
    # A release or run without the field, or a lazily-loaded object that
    # yields a string, must not raise on a rendered dashboard.
    assert tracker_mod._stamp(None, "published") == ""
    assert tracker_mod._stamp("not-a-datetime", "published") == ""
    assert tracker_mod._stamp(SimpleNamespace(), "finished") == ""
    assert tracker_mod._stamp(datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc), "finished") == (
        " · finished 2026-01-02 03:04 UTC"
    )


def test_find_run_prefers_active_match_over_newer_completed_duplicate() -> None:
    title = f"Publish release on {TRACKER.branch} @ {SHA}"
    workflow = MagicMock()
    workflow.name = "Publish Release"
    cancelled = SimpleNamespace(display_title=title, status="completed", conclusion="cancelled")
    active = SimpleNamespace(display_title=title, status="in_progress", conclusion=None)
    workflow.get_runs.return_value = [cancelled, active]

    assert tracker_mod._find_run(workflow, title) is active


def test_release_must_be_published_at_the_exact_candidate() -> None:
    repo = MagicMock()
    repo.get_releases.return_value = [SimpleNamespace(tag_name=TRACKER.tag, draft=False, prerelease=False)]
    repo.get_git_ref.return_value.object = SimpleNamespace(type="commit", sha="b" * 40)
    with pytest.raises(RuntimeError, match="expected candidate"):
        tracker_mod._find_release(repo, TRACKER.tag, SHA, False)


def test_draft_or_wrong_kind_release_is_not_accepted() -> None:
    repo = MagicMock()
    repo.get_releases.return_value = [SimpleNamespace(tag_name=TRACKER.tag, draft=True, prerelease=False)]
    with pytest.raises(RuntimeError, match="draft"):
        tracker_mod._find_release(repo, TRACKER.tag, SHA, False)
    repo.get_releases.return_value = [SimpleNamespace(tag_name=TRACKER.tag, draft=False, prerelease=True)]
    with pytest.raises(RuntimeError, match="prerelease"):
        tracker_mod._find_release(repo, TRACKER.tag, SHA, False)


def test_successful_prepare_waits_truthfully_for_delayed_pr() -> None:
    body, summary = tracker_mod._render_status(
        TRACKER,
        prepare_run=_run(),
        pr=None,
        branch_head=SHA,
        candidate_sha="",
        candidate_ci=None,
        publish_run=None,
        release=None,
        production_run=None,
        agent_repo="valkey-io/valkey-ci-agent",
        dispatched=False,
    )

    assert summary == "preparation completed"
    assert "Release preparation completed and the release-notes PR is pending" in body
    assert "Wait for the release-notes PR to appear" in body


def test_failed_candidate_ci_is_visible_but_does_not_block_qualification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue = _issue()
    agent = MagicMock()
    agent.default_branch = "main"
    agent.get_workflow_run.return_value = _run()
    workflow = MagicMock()
    pr = SimpleNamespace(
        merged=True,
        merge_commit_sha=SHA,
        number=7,
        html_url="https://example/pull/7",
        state="closed",
        draft=False,
    )
    monkeypatch.setattr(tracker_mod, "_find_prep_pr", lambda *a: pr)
    monkeypatch.setattr(tracker_mod, "_find_release", lambda *a: None)
    monkeypatch.setattr(tracker_mod, "_branch_head", lambda *a: SHA)
    monkeypatch.setattr(tracker_mod, "_find_run", lambda *a: None)
    monkeypatch.setattr(tracker_mod, "evaluate_candidate_ci", lambda *a: _candidate_ci(macos="failure"))
    monkeypatch.setattr(tracker_mod, "_find_production_run", lambda *a: None)

    result = tracker_mod._sync_one(
        issue,
        TRACKER,
        MagicMock(),
        agent,
        MagicMock(),
        workflow,
        agent_repo="valkey-io/valkey-ci-agent",
        policy=POLICY,
        dispatch=True,
    )

    workflow.create_dispatch.assert_called_once_with(
        "main",
        inputs={"branch": "9.1", "candidate_sha": SHA},
    )
    assert result == "#42: publication dispatched"
    body = issue.create_comment.call_args.args[0]
    assert "[Candidate CI run 456](https://example/actions/runs/456)" in body
    assert "Advisory only; inspect if unexpected: macos." in body
    assert "[`macos` check](https://example/checks/macos)" in body


def test_unavailable_candidate_ci_does_not_strand_qualification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue = _issue()
    agent = MagicMock()
    agent.default_branch = "main"
    agent.get_workflow_run.return_value = _run()
    workflow = MagicMock()
    pr = SimpleNamespace(
        merged=True,
        merge_commit_sha=SHA,
        number=7,
        html_url="https://example/pull/7",
        state="closed",
        draft=False,
    )
    monkeypatch.setattr(tracker_mod, "_find_prep_pr", lambda *a: pr)
    monkeypatch.setattr(tracker_mod, "_find_release", lambda *a: None)
    monkeypatch.setattr(tracker_mod, "_branch_head", lambda *a: SHA)
    monkeypatch.setattr(tracker_mod, "_find_run", lambda *a: None)
    monkeypatch.setattr(
        tracker_mod,
        "evaluate_candidate_ci",
        MagicMock(side_effect=RuntimeError("checks API unavailable")),
    )
    monkeypatch.setattr(tracker_mod, "_find_production_run", lambda *a: None)

    result = tracker_mod._sync_one(
        issue,
        TRACKER,
        MagicMock(),
        agent,
        MagicMock(),
        workflow,
        agent_repo="valkey-io/valkey-ci-agent",
        policy=POLICY,
        dispatch=True,
    )

    workflow.create_dispatch.assert_called_once()
    assert result == "#42: publication dispatched"
    body = issue.create_comment.call_args.args[0]
    assert "img.shields.io/badge/-Unavailable-9a6700" in body
    assert "Advisory only; inspect progress logs if this persists." in body


def test_sync_rejects_policy_for_a_different_repository() -> None:
    wrong_policy = ReleasePolicy(
        repo="valkey-io/other",
        authorized_teams=POLICY.authorized_teams,
        branches=POLICY.branches,
        checks_workflow=POLICY.checks_workflow,
        required_checks=POLICY.required_checks,
    )

    with pytest.raises(ValueError, match="does not match tracker target"):
        tracker_mod.sync_trackers(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            target_repo=TRACKER.repo,
            agent_repo="valkey-io/valkey-ci-agent",
            automation_repo="valkey-io/valkey-release-automation",
            policy=wrong_policy,
        )


def test_sync_logs_invalid_marker_and_continues_to_healthy_tracker(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    poisoned = _issue()
    poisoned.number = 40
    healthy = _issue()
    healthy.number = 42
    repo = MagicMock()
    repo.get_issues.return_value = [poisoned, healthy]
    agent = MagicMock()
    automation = MagicMock()
    monkeypatch.setattr(tracker_mod, "_repo", MagicMock(side_effect=[repo, agent, automation]))
    monkeypatch.setattr(tracker_mod, "_ensure_label", lambda *a: object())
    monkeypatch.setattr(tracker_mod, "_tracker_from_issue", MagicMock(side_effect=[None, TRACKER]))
    sync_one = MagicMock(return_value="#42: refreshed")
    monkeypatch.setattr(tracker_mod, "_sync_one", sync_one)

    results = tracker_mod.sync_trackers(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        target_repo=TRACKER.repo,
        agent_repo="valkey-io/valkey-ci-agent",
        automation_repo="valkey-io/valkey-release-automation",
        policy=POLICY,
    )

    assert results == ["#40: invalid tracker metadata", "#42: refreshed"]
    assert "invalid metadata" in caplog.text
    sync_one.assert_called_once()


def test_off_policy_tracker_is_rejected_before_issue_or_dispatch_mutation() -> None:
    off_policy = tracker_mod.Tracker(
        **{
            **TRACKER.__dict__,
            "branch": "8.0",
            "version": "8.0.12",
            "tag": "8.0.12",
            "prep_branch": "agent/release-cut/8.0.12-ga",
        }
    )
    issue = _issue()
    workflow = MagicMock()

    with pytest.raises(ValueError, match="not allowed by release policy"):
        tracker_mod._sync_one(
            issue,
            off_policy,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            workflow,
            agent_repo="valkey-io/valkey-ci-agent",
            policy=POLICY,
            dispatch=True,
        )

    issue.edit.assert_not_called()
    workflow.create_dispatch.assert_not_called()


def test_sync_cli_uses_the_shared_bounded_poll_loop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("TARGET_GITHUB_TOKEN", "AGENT_GITHUB_TOKEN", "AUTOMATION_GITHUB_TOKEN"):
        monkeypatch.setenv(name, "token")
    monkeypatch.setattr(tracker_mod, "Github", lambda **kwargs: SimpleNamespace(auth=kwargs["auth"]))
    monkeypatch.setattr(tracker_mod, "load_policy", lambda path: POLICY)
    sync = MagicMock(return_value=["#42: refreshed"])
    monkeypatch.setattr(tracker_mod, "sync_trackers", sync)

    def run_twice(poll, args, **kwargs):
        assert args.poll_interval_seconds == 300
        assert args.poll_duration_seconds == 3300
        assert kwargs["logger"] is tracker_mod.logger
        return [poll(), poll()]

    monkeypatch.setattr(tracker_mod, "run_poll_loop_from_args", run_twice)

    assert tracker_mod.main([
        "sync",
        "--poll-interval-seconds",
        "300",
        "--poll-duration-seconds",
        "3300",
    ]) == 0

    assert sync.call_count == 2
    assert capsys.readouterr().out.splitlines() == ["#42: refreshed", "#42: refreshed"]


def test_tracker_outputs_refuse_multiline_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    with pytest.raises(ValueError, match="multiline workflow output refused"):
        tracker_mod._write_outputs({"issue_url": "safe\nforged=true"})


def test_unchanged_status_does_not_churn_comment_for_timestamp_only() -> None:
    issue = _issue()
    comment = MagicMock()
    comment.user.login = issue.user.login
    comment.body = "<!-- valkey-release-tracker:status -->\nsame\nStatus last changed 2026-08-20 08:25 UTC\n"
    issue.get_comments.return_value = [comment]

    tracker_mod._upsert_status(issue, "same\nStatus last changed 2026-08-20 09:30 UTC")

    comment.edit.assert_not_called()
    issue.create_comment.assert_not_called()


def test_pending_candidate_ci_is_linked_without_blocking_qualification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue = _issue()
    agent = MagicMock()
    agent.default_branch = "main"
    agent.get_workflow_run.return_value = _run()
    workflow = MagicMock()
    pr = SimpleNamespace(
        merged=True,
        merge_commit_sha=SHA,
        number=7,
        html_url="https://example/pull/7",
        state="closed",
        draft=False,
    )
    monkeypatch.setattr(tracker_mod, "_find_prep_pr", lambda *a: pr)
    monkeypatch.setattr(tracker_mod, "_find_release", lambda *a: None)
    monkeypatch.setattr(tracker_mod, "_branch_head", lambda *a: SHA)
    monkeypatch.setattr(tracker_mod, "_find_run", lambda *a: None)
    monkeypatch.setattr(
        tracker_mod,
        "evaluate_candidate_ci",
        lambda *a: _candidate_ci(macos="in_progress", status="in_progress"),
    )
    monkeypatch.setattr(tracker_mod, "_find_production_run", lambda *a: None)

    result = tracker_mod._sync_one(
        issue,
        TRACKER,
        MagicMock(),
        agent,
        MagicMock(),
        workflow,
        agent_repo="valkey-io/valkey-ci-agent",
        policy=POLICY,
        dispatch=True,
    )

    workflow.create_dispatch.assert_called_once_with(
        "main",
        inputs={"branch": "9.1", "candidate_sha": SHA},
    )
    assert result == "#42: publication dispatched"
    body = issue.create_comment.call_args.args[0]
    assert "[PR #7](https://example/pull/7)" in body
    assert f"[Candidate `{SHA[:12]}`](https://github.com/{TRACKER.repo}/commit/{SHA})" in body
    assert "[Candidate CI run 456](https://example/actions/runs/456)" in body
    assert "1 of 2 configured checks passed" in body
    assert "Advisory only; still running: macos." in body
    assert "| Candidate CI |" in body
    assert "—" not in body
    assert not any(symbol in body for symbol in "✅❌⏳⛔🛑⚠️🟦🟥🟩⬜")


def test_refresh_issue_body_migrates_legacy_dashboard_idempotently() -> None:
    issue = _issue()
    issue.title = f"Release {TRACKER.tag}"
    issue.body = "## Maintainer checklist\n\n- [ ] Legacy action"

    tracker_mod._refresh_issue_body(issue, TRACKER, "valkey-io/valkey-ci-agent")

    rendered = issue.edit.call_args.kwargs["body"]
    assert issue.edit.call_args.kwargs["title"] == f"Release {TRACKER.tag}"
    assert "## Maintainer checklist" not in rendered
    assert "## Human checkpoints" in rendered
    assert TRACKER.marker() not in rendered
    assert f"[{TRACKER.prep_branch!r}]" not in rendered
    assert f"https://github.com/{TRACKER.repo}/tree/{TRACKER.prep_branch}" in rendered

    issue.body = rendered
    issue.edit.reset_mock()
    tracker_mod._refresh_issue_body(issue, TRACKER, "valkey-io/valkey-ci-agent")
    issue.edit.assert_not_called()
