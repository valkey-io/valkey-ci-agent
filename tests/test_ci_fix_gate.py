"""Tests for the authorization and integrity gate.

The gate is the security boundary, so the tests lean on the refusal paths:
malformed commands, non-members, cross-repo runs, and moved branches must all
fail closed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from scripts.ci_fix.gate import (
    GateRejection,
    build_fix_request,
    is_authorized,
    parse_command,
)
from scripts.ci_fix.models import FixRequest

_RUN_URL = "https://github.com/valkey-io/valkey/actions/runs/27559908167"


# --- parse_command ---

def test_parse_command_basic():
    cmd = parse_command(f"@valkeyrie-bot fix {_RUN_URL}")
    assert cmd is not None
    assert cmd.run_owner == "valkey-io"
    assert cmd.run_repo == "valkey"
    assert cmd.run_id == 27559908167
    assert cmd.hint == ""


def test_parse_command_with_hint():
    cmd = parse_command(f"@valkeyrie-bot fix {_RUN_URL} look at the NAN payload")
    assert cmd is not None
    assert cmd.hint == "look at the NAN payload"


def test_parse_command_accepts_valkeyrie_ops():
    cmd = parse_command(f"@valkeyrie-ops fix {_RUN_URL}")
    assert cmd is not None
    assert cmd.run_owner == "valkey-io"
    assert cmd.run_id == 27559908167


def test_parse_command_case_insensitive():
    cmd = parse_command(f"@Valkeyrie-Bot FIX {_RUN_URL}")
    assert cmd is not None


def test_parse_command_ignores_unrelated_comment():
    assert parse_command("thanks, lgtm!") is None
    assert parse_command("@valkeyrie-bot please review") is None


def test_parse_command_ignores_quoted_mention_mid_comment():
    # The command must start the comment; quoting it in discussion must not fire.
    body = f"Did you try `@valkeyrie-bot fix {_RUN_URL}`? It worked for me."
    assert parse_command(body) is None


def test_parse_command_requires_run_url():
    assert parse_command("@valkeyrie-bot fix https://example.com/not-a-run") is None


# --- is_authorized (fail closed) ---

def _team_membership_gh(state):
    membership = SimpleNamespace(state=state)
    team = MagicMock()
    team.get_team_membership.return_value = membership
    org = MagicMock()
    org.get_team_by_slug.return_value = team
    gh = MagicMock()
    gh.get_organization.return_value = org
    return gh


def test_authorized_active_member():
    gh = _team_membership_gh("active")
    assert is_authorized(gh, "valkey-io", "contributors", "alice") is True


def test_pending_member_not_authorized():
    gh = _team_membership_gh("pending")
    assert is_authorized(gh, "valkey-io", "contributors", "bob") is False


def test_membership_read_error_fails_closed():
    gh = MagicMock()
    gh.get_organization.side_effect = RuntimeError("403 no permission")
    assert is_authorized(gh, "valkey-io", "contributors", "carol") is False


def test_empty_username_not_authorized():
    assert is_authorized(MagicMock(), "valkey-io", "contributors", "") is False


def test_allowlist_authorizes_without_team(monkeypatch):
    """An allowlisted login is authorized without a team read (fork testing)."""
    monkeypatch.setenv("CI_FIX_AUTH_ALLOWLIST", "alice, bob")
    gh = MagicMock()
    gh.get_organization.side_effect = AssertionError("team should not be queried")
    assert is_authorized(gh, "valkey-io", "contributors", "bob") is True


def test_allowlist_empty_by_default(monkeypatch):
    """With no allowlist set, only team membership authorizes."""
    monkeypatch.delenv("CI_FIX_AUTH_ALLOWLIST", raising=False)
    gh = _team_membership_gh("pending")
    assert is_authorized(gh, "valkey-io", "contributors", "carol") is False


# --- build_fix_request ---

def _gh_for_request(*, member_state="active", pr_head_sha="abc123",
                    pr_head_ref="agent/backport/sweep/8.0",
                    pr_head_repo="valkey-io/valkey",
                    run_head_sha="abc123", run_head_branch="agent/backport/sweep/8.0",
                    run_status="completed", run_conclusion="failure"):
    gh = _team_membership_gh(member_state)
    pr = SimpleNamespace(
        head=SimpleNamespace(
            sha=pr_head_sha, ref=pr_head_ref,
            repo=SimpleNamespace(full_name=pr_head_repo),
        )
    )
    run = SimpleNamespace(head_sha=run_head_sha, head_branch=run_head_branch,
                          status=run_status, conclusion=run_conclusion)
    repo = MagicMock()
    repo.get_pull.return_value = pr
    repo.get_workflow_run.return_value = run
    gh.get_repo.return_value = repo
    return gh


def _cmd():
    return parse_command(f"@valkeyrie-bot fix {_RUN_URL}")


def test_build_fix_request_happy_path():
    gh = _gh_for_request()
    result = build_fix_request(
        gh, command=_cmd(), pr_repo_full_name="valkey-io/valkey",
        pr_number=3988, commenter="alice",
    )
    assert isinstance(result, FixRequest)
    assert result.head_sha == "abc123"
    assert result.run_id == 27559908167
    assert result.requested_by == "alice"


def test_build_fix_request_rejects_non_member():
    gh = _gh_for_request(member_state="pending")
    result = build_fix_request(
        gh, command=_cmd(), pr_repo_full_name="valkey-io/valkey",
        pr_number=3988, commenter="stranger",
    )
    assert isinstance(result, GateRejection)
    assert "not an active member" in result.reason


def test_build_fix_request_rejects_cross_repo_run():
    gh = _gh_for_request()
    other_cmd = parse_command(
        "@valkeyrie-bot fix https://github.com/someone/else/actions/runs/123"
    )
    result = build_fix_request(
        gh, command=other_cmd, pr_repo_full_name="valkey-io/valkey",
        pr_number=3988, commenter="alice",
    )
    assert isinstance(result, GateRejection)
    assert "not this PR's repository" in result.reason


def test_build_fix_request_rejects_fork_head():
    # A PR whose head lives on an external fork must not be pushed to.
    gh = _gh_for_request(pr_head_repo="someoneelse/valkey")
    result = build_fix_request(
        gh, command=_cmd(), pr_repo_full_name="valkey-io/valkey",
        pr_number=3988, commenter="alice",
    )
    assert isinstance(result, GateRejection)
    assert "someoneelse/valkey" in result.reason


def test_build_fix_request_rejects_moved_branch():
    gh = _gh_for_request(pr_head_sha="newsha999", run_head_sha="oldsha111")
    result = build_fix_request(
        gh, command=_cmd(), pr_repo_full_name="valkey-io/valkey",
        pr_number=3988, commenter="alice",
    )
    assert isinstance(result, GateRejection)
    assert "moved" in result.reason


def test_build_fix_request_rejects_branch_mismatch():
    gh = _gh_for_request(
        pr_head_ref="agent/backport/sweep/8.0",
        run_head_branch="some-other-branch",
    )
    result = build_fix_request(
        gh, command=_cmd(), pr_repo_full_name="valkey-io/valkey",
        pr_number=3988, commenter="alice",
    )
    assert isinstance(result, GateRejection)
    assert "does not match" in result.reason


def test_build_fix_request_rejects_empty_sha():
    """A missing PR or run head SHA must fail closed, not compare equal as ''."""
    gh = _gh_for_request(pr_head_sha="", run_head_sha="")
    result = build_fix_request(
        gh, command=_cmd(), pr_repo_full_name="valkey-io/valkey",
        pr_number=3988, commenter="alice",
    )
    assert isinstance(result, GateRejection)
    assert "head commit" in result.reason


def test_build_fix_request_rejects_in_progress_run():
    """A run still in progress has no downloadable logs yet - refuse with a retry hint."""
    gh = _gh_for_request(run_status="in_progress", run_conclusion="")
    result = build_fix_request(
        gh, command=_cmd(), pr_repo_full_name="valkey-io/valkey",
        pr_number=3988, commenter="alice",
    )
    assert isinstance(result, GateRejection)
    assert "not finished" in result.reason


def test_build_fix_request_accepts_completed_run_regardless_of_conclusion():
    """The gate no longer judges the run's overall conclusion.

    A completed run is accepted (even 'success' or 'cancelled'); whether there
    is a real failure to act on is decided per-job downstream in the pipeline.
    """
    gh = _gh_for_request(run_conclusion="cancelled")
    result = build_fix_request(
        gh, command=_cmd(), pr_repo_full_name="valkey-io/valkey",
        pr_number=3988, commenter="alice",
    )
    assert isinstance(result, FixRequest)


def test_build_fix_request_refuses_unknown_head_repo():
    # If the PR head repo can't be determined, fail closed.
    gh = _gh_for_request(pr_head_repo="")
    result = build_fix_request(
        gh, command=_cmd(), pr_repo_full_name="valkey-io/valkey",
        pr_number=3988, commenter="alice",
    )
    assert isinstance(result, GateRejection)
    assert "head repository" in result.reason


def test_hint_is_limited_to_the_invocation_line():
    url = "https://github.com/o/r/actions/runs/5"
    cmd = parse_command(f"@valkeyrie-bot fix {url} only the NAN test\n\nthanks all!")
    assert cmd is not None
    assert cmd.hint == "only the NAN test"
