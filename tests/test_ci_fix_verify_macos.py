"""Tests for the patch-based macOS verifier.

These exercise dispatch, run correlation, and the verdict mapping with a mocked
GitHub client; no real dispatch happens.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from scripts.ci_fix.verify.base import VerificationPlan, VerifyEnv
from scripts.ci_fix.verify.macos import MacosVerifier, normalize_macos_verify_command


def _plan(command="make test", workdir=""):
    return VerificationPlan(
        env=VerifyEnv.MACOS, command=command, workdir=workdir,
        head_sha="abc1234", target_repo="owner/target",
    )


def _gh_with_run(*, conclusion, name_has_token=True, status="completed"):
    """A GitHub mock whose verify-macos workflow returns one run."""
    from datetime import datetime, timezone
    captured = {}

    def make_dispatch(ref, inputs):
        captured["ref"] = ref
        captured["inputs"] = inputs
        return True

    # created far in the future so it always passes the created_at>=since gate.
    run = SimpleNamespace(
        id=1, status=status, conclusion=conclusion,
        html_url="https://example/run/1",
        # GitHub: name is the workflow's name; the custom run-name lands in
        # display_title. The token is in display_title, never name.
        name="CI Fix Verify macOS",
        display_title="verify-macos PLACEHOLDER",
        created_at=datetime(2999, 1, 1, tzinfo=timezone.utc),
    )

    workflow = MagicMock()
    workflow.create_dispatch.side_effect = make_dispatch
    workflow.get_runs.return_value = [run]
    repo = MagicMock()
    repo.get_workflow.return_value = workflow
    repo.get_workflow_run.return_value = run
    gh = MagicMock()
    gh.get_repo.return_value = repo

    def set_token_into_run():
        # The custom run-name (display_title) carries the delimited marker; name
        # stays the workflow name, mirroring the real GitHub API shape.
        if name_has_token:
            run.display_title = f"verify-macos [token:{captured['inputs']['correlation']}]"

    return gh, captured, set_token_into_run, run


def _verifier(gh):
    return MacosVerifier(gh, agent_repo_full_name="owner/agent", ref="main", timeout=5)


def test_green_run_verifies(monkeypatch):
    monkeypatch.setattr("scripts.ci_fix.verify.macos.time.sleep", lambda *_: None)
    gh, captured, set_token, _run = _gh_with_run(conclusion="success")
    # create_dispatch must run first to populate the correlation token.
    orig = gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect

    def dispatch_then_token(ref, inputs):
        result = orig(ref, inputs)
        set_token()
        return result

    gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect = dispatch_then_token

    result = _verifier(gh).verify("/repo", _plan(), "diff --git a b\n")
    assert result.verified is True
    assert result.run_url == "https://example/run/1"
    # The patch and SHA were transported as inputs.
    assert captured["inputs"]["head_sha"] == "abc1234"
    assert captured["inputs"]["target_repo"] == "owner/target"


def test_dispatch_normalizes_root_src_object_make(monkeypatch):
    monkeypatch.setattr("scripts.ci_fix.verify.macos.time.sleep", lambda *_: None)
    gh, captured, set_token, _run = _gh_with_run(conclusion="success")
    orig = gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect

    def dispatch_then_token(ref, inputs):
        result = orig(ref, inputs)
        set_token()
        return result

    gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect = dispatch_then_token

    command = (
        "{ export AR=/opt/homebrew/opt/llvm/bin/llvm-ar; "
        "make -j3 src/unit/test_networking.o SERVER_CFLAGS='-Werror' USE_FAST_FLOAT=yes; }"
    )
    result = _verifier(gh).verify("/repo", _plan(command), "diff --git a b\n")
    assert result.verified is True
    assert captured["inputs"]["verify_command"] == (
        "{ export AR=/opt/homebrew/opt/llvm/bin/llvm-ar; "
        "make -C src -j3 unit/test_networking.o SERVER_CFLAGS=-Werror USE_FAST_FLOAT=yes; }"
    )


def test_dispatch_does_not_normalize_when_workdir_is_set(monkeypatch):
    monkeypatch.setattr("scripts.ci_fix.verify.macos.time.sleep", lambda *_: None)
    gh, captured, set_token, _run = _gh_with_run(conclusion="success")
    orig = gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect

    def dispatch_then_token(ref, inputs):
        result = orig(ref, inputs)
        set_token()
        return result

    gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect = dispatch_then_token

    command = "make src/unit/test_networking.o"
    result = _verifier(gh).verify("/repo", _plan(command, workdir="src"), "diff --git a b\n")
    assert result.verified is True
    assert captured["inputs"]["workdir"] == "src"
    assert captured["inputs"]["verify_command"] == command


def test_failed_run_refuses(monkeypatch):
    monkeypatch.setattr("scripts.ci_fix.verify.macos.time.sleep", lambda *_: None)
    gh, captured, set_token, _run = _gh_with_run(conclusion="failure")
    orig = gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect

    def dispatch_then_token(ref, inputs):
        r = orig(ref, inputs)
        set_token()
        return r

    gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect = dispatch_then_token

    result = _verifier(gh).verify("/repo", _plan(), "diff\n")
    assert result.verified is False
    assert "did not pass" in result.detail


def test_failed_run_includes_log_tail(monkeypatch):
    monkeypatch.setattr("scripts.ci_fix.verify.macos.time.sleep", lambda *_: None)
    gh, captured, set_token, run = _gh_with_run(conclusion="failure")
    orig = gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect

    def dispatch_then_token(ref, inputs):
        r = orig(ref, inputs)
        set_token()
        return r

    gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect = dispatch_then_token

    artifact_client = MagicMock()
    artifact_client.download_run_logs.return_value = {
        "verify-macos/1_Check out target repo.txt": b"checkout log\n",
        "verify-macos/5_Run targeted verification.txt":
            b"unit/test_vset.c:137:25: error: variable length array folded\n",
    }
    verifier = MacosVerifier(
        gh, agent_repo_full_name="owner/agent", ref="main", timeout=5,
        artifact_client=artifact_client,
    )

    result = verifier.verify("/repo", _plan(), "diff\n")
    assert result.verified is False
    assert "unit/test_vset.c:137" in result.output_tail
    # Only the verify step's log is surfaced; unrelated steps are excluded.
    assert "checkout log" not in result.output_tail
    artifact_client.download_run_logs.assert_called_once_with("owner/agent", run.id)


def test_failed_run_tail_keeps_verify_step_over_large_other_logs(monkeypatch):
    monkeypatch.setattr("scripts.ci_fix.verify.macos.time.sleep", lambda *_: None)
    gh, captured, set_token, _run = _gh_with_run(conclusion="failure")
    orig = gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect

    def dispatch_then_token(ref, inputs):
        r = orig(ref, inputs)
        set_token()
        return r

    gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect = dispatch_then_token

    # A later step emits a log far larger than the tail cap; the verify step's
    # failure must still survive because we tail that step's log directly.
    artifact_client = MagicMock()
    artifact_client.download_run_logs.return_value = {
        "verify-macos/5_Run targeted verification.txt":
            b"unit/test_vset.c:137:25: error: variable length array folded\n",
        "verify-macos/9_Upload artifacts.txt": b"noise\n" * 5000,
    }
    verifier = MacosVerifier(
        gh, agent_repo_full_name="owner/agent", ref="main", timeout=5,
        artifact_client=artifact_client,
    )

    result = verifier.verify("/repo", _plan(), "diff\n")
    assert result.verified is False
    assert "unit/test_vset.c:137" in result.output_tail
    assert "noise" not in result.output_tail


def test_failed_run_without_artifact_client_has_empty_tail(monkeypatch):
    monkeypatch.setattr("scripts.ci_fix.verify.macos.time.sleep", lambda *_: None)
    gh, captured, set_token, _run = _gh_with_run(conclusion="failure")
    orig = gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect

    def dispatch_then_token(ref, inputs):
        r = orig(ref, inputs)
        set_token()
        return r

    gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect = dispatch_then_token

    result = _verifier(gh).verify("/repo", _plan(), "diff\n")
    assert result.verified is False
    assert result.output_tail == ""


def test_oversized_patch_refuses_without_dispatch():
    gh = MagicMock()
    result = _verifier(gh).verify("/repo", _plan(), "x" * (200 * 1024))
    assert result.verified is False
    assert "too large" in result.detail
    gh.get_repo.assert_not_called()


def test_dispatch_failure_refuses(monkeypatch):
    monkeypatch.setattr("scripts.ci_fix.verify.macos.time.sleep", lambda *_: None)
    gh = MagicMock()
    gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect = RuntimeError("boom")
    result = _verifier(gh).verify("/repo", _plan(), "diff\n")
    assert result.verified is False
    assert "could not dispatch" in result.detail


def test_run_never_appears_times_out(monkeypatch):
    """If no run ever carries the token, the verifier times out and refuses."""
    monkeypatch.setattr("scripts.ci_fix.verify.macos.time.sleep", lambda *_: None)
    monkeypatch.setattr("scripts.ci_fix.verify.macos.time.time",
                        _advancing_clock(step=10))
    gh, captured, _set, _run = _gh_with_run(conclusion="success", name_has_token=False)
    result = _verifier(gh).verify("/repo", _plan(), "diff\n")
    assert result.verified is False
    assert "did not complete" in result.detail


def test_stale_run_before_dispatch_is_ignored(monkeypatch):
    """A token-bearing run created before dispatch must not be trusted."""
    from datetime import datetime, timezone
    monkeypatch.setattr("scripts.ci_fix.verify.macos.time.sleep", lambda *_: None)
    monkeypatch.setattr("scripts.ci_fix.verify.macos.time.time", _advancing_clock(step=10))
    gh, captured, set_token, run = _gh_with_run(conclusion="success")
    run.created_at = datetime(2000, 1, 1, tzinfo=timezone.utc)  # ancient
    orig = gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect

    def dispatch_then_token(ref, inputs):
        r = orig(ref, inputs)
        set_token()
        return r

    gh.get_repo.return_value.get_workflow.return_value.create_dispatch.side_effect = dispatch_then_token
    result = _verifier(gh).verify("/repo", _plan(), "diff\n")
    assert result.verified is False
    assert "did not complete" in result.detail


def test_normalize_macos_verify_command_rewrites_root_src_object_targets():
    command = (
        "{\n"
        "export AR=/opt/homebrew/opt/llvm/bin/llvm-ar; "
        "export RANLIB=/opt/homebrew/opt/llvm/bin/llvm-ranlib; "
        "make -j3 src/unit/test_networking.o SERVER_CFLAGS='-Werror' USE_FAST_FLOAT=yes\n"
        "} && {\n"
        "export AR=/opt/homebrew/opt/llvm/bin/llvm-ar; "
        "export RANLIB=/opt/homebrew/opt/llvm/bin/llvm-ranlib; "
        "make -j3 all-with-unit-tests SERVER_CFLAGS='-Werror' USE_FAST_FLOAT=yes\n"
        "}"
    )
    assert normalize_macos_verify_command(command) == (
        "{\n"
        "export AR=/opt/homebrew/opt/llvm/bin/llvm-ar; "
        "export RANLIB=/opt/homebrew/opt/llvm/bin/llvm-ranlib; "
        "make -C src -j3 unit/test_networking.o SERVER_CFLAGS=-Werror USE_FAST_FLOAT=yes\n"
        "} && {\n"
        "export AR=/opt/homebrew/opt/llvm/bin/llvm-ar; "
        "export RANLIB=/opt/homebrew/opt/llvm/bin/llvm-ranlib; "
        "make -j3 all-with-unit-tests SERVER_CFLAGS='-Werror' USE_FAST_FLOAT=yes\n"
        "}"
    )


def test_normalize_macos_verify_command_leaves_safe_make_commands():
    assert normalize_macos_verify_command("make -C src unit/test_networking.o") == (
        "make -C src unit/test_networking.o"
    )
    assert normalize_macos_verify_command("cd src && make unit/test_networking.o") == (
        "cd src && make unit/test_networking.o"
    )
    assert normalize_macos_verify_command("make all-with-unit-tests") == "make all-with-unit-tests"


def _advancing_clock(step):
    # Start near "now" so dispatched_at (=time.time()) is a realistic epoch and
    # created_at comparisons behave; advance by `step` each call to cross the
    # timeout deadline within a few polls.
    state = {"t": 1_700_000_000.0}

    def _now():
        state["t"] += step
        return state["t"]

    return _now
