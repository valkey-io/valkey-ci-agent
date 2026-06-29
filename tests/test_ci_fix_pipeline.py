"""Tests for the push step, comment renderer, and pipeline orchestration."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from scripts.ci_fix.comment import render_comment
from scripts.ci_fix.gate import GateRejection, parse_command
from scripts.ci_fix.models import (
    FixOutcome,
    FixPath,
    FixProposal,
    OutcomeKind,
    ReviewVerdict,
    RunResult,
)
from scripts.ci_fix.pipeline import run_ci_fix
from scripts.ci_fix.push import PushRefused, commit_and_push_fix

_RUN_URL = "https://github.com/valkey-io/valkey/actions/runs/27559908167"


def _proposal(path: FixPath = FixPath.AUTHOR) -> FixProposal:
    return FixProposal(
        path=path, failing_check="corrupt payload: zset listpack with NAN score",
        root_cause="payload embeds RDB v80; branch is v11", reasoning="scaffolding fix",
        confidence=0.9, failing_job_hint="test-ubuntu-latest",
        build_command="make", verify_command="./runtest --single x",
    )


def _passed_run() -> RunResult:
    return RunResult(ran=True, passed=True, exit_code=0,
                     command="make && ./runtest --single x", output_tail="All tests passed")


# --- push: namespace guard ---

def test_push_refuses_non_namespaced_branch():
    with pytest.raises(PushRefused, match="agent/backport/"):
        commit_and_push_fix(
            "/repo", head_repo_full_name="valkey-io/valkey",
            head_branch="some-contributor-branch", head_sha="abc1234", proposal=_proposal(),
            changed_paths=("test.tcl",), git_env={},
        )


def test_push_refuses_empty_changed_paths():
    with pytest.raises(PushRefused, match="no approved changed paths"):
        commit_and_push_fix(
            "/repo", head_repo_full_name="valkey-io/valkey",
            head_branch="agent/backport/sweep/8.0", head_sha="abc1234", proposal=_proposal(),
            changed_paths=(), git_env={},
        )


def test_push_stages_only_approved_paths(tmp_path, monkeypatch):
    """A stray untracked file from a test must not be committed (P2)."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "agent/backport/sweep/8.0", str(repo)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "seed.txt").write_text("seed")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "seed"], check=True, capture_output=True)
    head_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "origin",
                    "HEAD:agent/backport/sweep/8.0"], check=True, capture_output=True)
    monkeypatch.setattr("scripts.ci_fix.push.github_https_url", lambda _f: str(remote))

    (repo / "test.tcl").write_text("the approved fix")
    (repo / "stray-artifact.log").write_text("test output that must not be committed")

    commit_and_push_fix(
        str(repo), head_repo_full_name="valkey-io/valkey",
        head_branch="agent/backport/sweep/8.0", head_sha=head_sha, proposal=_proposal(),
        changed_paths=("test.tcl",), git_env={},
    )
    committed = subprocess.run(
        ["git", "--git-dir", str(remote), "show", "--name-only", "--format=",
         "refs/heads/agent/backport/sweep/8.0"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "test.tcl" in committed
    assert "stray-artifact.log" not in committed


def test_push_commits_and_pushes(tmp_path, monkeypatch):
    """A real local repo: the fix is committed (no sign-off) and pushed to a bare remote."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "agent/backport/sweep/8.0", str(repo)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "seed.txt").write_text("seed")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "seed"], check=True, capture_output=True)
    head_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "origin",
                    "HEAD:agent/backport/sweep/8.0"], check=True, capture_output=True)

    # The push step rewrites origin via github_https_url; point it at the local bare remote.
    monkeypatch.setattr("scripts.ci_fix.push.github_https_url", lambda _full_name: str(remote))

    # The "fix": an edit in the working tree.
    (repo / "test.tcl").write_text("fixed payload")

    commit_sha = commit_and_push_fix(
        str(repo), head_repo_full_name="valkey-io/valkey",
        head_branch="agent/backport/sweep/8.0", head_sha=head_sha, proposal=_proposal(),
        changed_paths=("test.tcl",), git_env={},
    )

    assert len(commit_sha) == 40
    msg = subprocess.run(
        ["git", "--git-dir", str(remote), "log", "-1", "--format=%B",
         "refs/heads/agent/backport/sweep/8.0"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "Signed-off-by:" not in msg
    assert "NAN score" in msg


def test_push_uses_clean_clone_not_source_git_config(tmp_path, monkeypatch):
    """A credential helper planted in the verified checkout must not run."""
    remote = tmp_path / "remote.git"
    leak = tmp_path / "leak.txt"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "agent/backport/sweep/8.0", str(repo)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "seed.txt").write_text("seed")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "seed"], check=True, capture_output=True)
    head_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "origin",
                    "HEAD:agent/backport/sweep/8.0"], check=True, capture_output=True)
    monkeypatch.setattr("scripts.ci_fix.push.github_https_url", lambda _full_name: str(remote))

    subprocess.run(
        ["git", "-C", str(repo), "config", "credential.helper",
         f"!sh -c 'echo $GIT_PASSWORD > {leak}; exit 1'"],
        check=True,
    )
    (repo / "test.tcl").write_text("fixed payload")

    commit_and_push_fix(
        str(repo), head_repo_full_name="valkey-io/valkey",
        head_branch="agent/backport/sweep/8.0", head_sha=head_sha, proposal=_proposal(),
        changed_paths=("test.tcl",), git_env={"GIT_PASSWORD": "secret"},
    )

    assert not leak.exists()


# --- comment renderer ---

def test_render_pushed_comment_includes_evidence():
    outcome = FixOutcome(
        kind=OutcomeKind.PUSHED, summary="pushed",
        proposal=_proposal(), run_result=_passed_run(),
        review=ReviewVerdict(approved=True, reasoning="minimal and correct"),
        commit_sha="abcdef1234567890",
    )
    body = render_comment(outcome)
    assert "NAN score" in body
    assert "abcdef123456" in body
    assert "make && ./runtest" in body
    assert "minimal and correct" in body
    assert "do not merge" in body.lower()


def test_render_pushed_surfaces_target_check_and_run_link():
    # The comment should lead with the target check's result, not the noise,
    # and link the failing run for provenance.
    noisy = "\n".join(
        ["[ok]: other test (1 ms)"] * 30
        + ["[ok]: corrupt payload: zset listpack with NAN score (12 ms)"]
    )
    outcome = FixOutcome(
        kind=OutcomeKind.PUSHED, summary="pushed",
        proposal=_proposal(),
        run_result=RunResult(ran=True, passed=True, exit_code=0,
                             command="make && ./runtest", output_tail=noisy),
        review=ReviewVerdict(approved=True, reasoning="ok"),
        commit_sha="abcdef1234567890",
        failing_run_url="https://github.com/o/r/actions/runs/9",
    )
    body = render_comment(outcome)
    assert "previously-failing check now passes" in body
    # The target line appears before the collapsed full output.
    assert body.index("NAN score") < body.index("Full verification output")
    assert "actions/runs/9" in body


def test_render_port_comment_uses_pr_ci_as_authority():
    outcome = FixOutcome(
        kind=OutcomeKind.PUSHED, summary="pushed",
        proposal=_proposal(FixPath.PORT),
        review=ReviewVerdict(approved=True, reasoning="Ported upstream commit 9f374e15848d"),
        commit_sha="abcdef1234567890",
        verify_backend="upstream-port",
    )
    body = render_comment(outcome)
    assert "ported upstream fix" in body
    assert "normal CI is the verification authority" in body
    assert "Targeted verification" not in body


def test_render_pushed_comment_escapes_backticks_in_output():
    # Untrusted output containing a code fence must not break out of the block.
    malicious = RunResult(
        ran=True, passed=True, exit_code=0, command="make && ./runtest --single x",
        output_tail="ok\n```\n## injected heading\n",
    )
    outcome = FixOutcome(
        kind=OutcomeKind.PUSHED, summary="pushed",
        proposal=_proposal(), run_result=malicious,
        review=ReviewVerdict(approved=True, reasoning="ok"),
        commit_sha="abcdef1234567890",
    )
    body = render_comment(outcome)
    # The fence must be longer than the ``` inside, so the injected heading
    # stays inside the code block (no bare "## injected heading" line).
    assert "````" in body
    assert "\n## injected heading" in body  # present, but inside the fence


def test_render_refused_comment_explains():
    outcome = FixOutcome(
        kind=OutcomeKind.REFUSED,
        summary="genuinely flaky timing failure; no safe fix",
        other_failing_checks=("other test",),
        failing_run_url="https://github.com/o/r/actions/runs/9",
    )
    body = render_comment(outcome)
    assert "did not push" in body.lower()
    assert "flaky" in body
    assert "other test" in body
    assert "actions/runs/9" in body


def test_render_failed_comment():
    outcome = FixOutcome(kind=OutcomeKind.FAILED, summary="could not clone repo")
    body = render_comment(outcome)
    assert "error" in body.lower()
    assert "could not clone repo" in body


# --- pipeline orchestration ---

def _gh_authorized(pr_head_sha="abc123", run_head_sha="abc123"):
    from types import SimpleNamespace
    membership = SimpleNamespace(state="active")
    team = MagicMock()
    team.get_team_membership.return_value = membership
    org = MagicMock()
    org.get_team_by_slug.return_value = team
    pr = SimpleNamespace(head=SimpleNamespace(
        sha=pr_head_sha, ref="agent/backport/sweep/8.0",
        repo=SimpleNamespace(full_name="valkey-io/valkey")))
    run = SimpleNamespace(head_sha=run_head_sha, head_branch="agent/backport/sweep/8.0",
                          status="completed", conclusion="failure")
    run.jobs = lambda: [SimpleNamespace(name="test-ubuntu-latest", conclusion="failure")]
    repo = MagicMock()
    repo.get_pull.return_value = pr
    repo.get_workflow_run.return_value = run
    gh = MagicMock()
    gh.get_organization.return_value = org
    gh.get_repo.return_value = repo
    return gh


def _artifact_client(logs):
    client = MagicMock()
    client.download_run_logs.return_value = logs
    return client


def _run_pipeline(monkeypatch, **overrides):
    from scripts.ci_fix.verify.base import VerifyEnv
    from scripts.ci_fix.verify.workflow_env import JobEnvironment
    monkeypatch.setattr("scripts.ci_fix.pipeline.shallow_clone_at_sha",
                        overrides.get("clone", lambda *a, **k: True))
    monkeypatch.setattr(
        "scripts.ci_fix.pipeline._classify_failing_job",
        overrides.get("classify", lambda *a, **k: JobEnvironment(VerifyEnv.LOCAL)),
    )
    monkeypatch.setattr(
        "scripts.ci_fix.pipeline.discover_port_candidates",
        overrides.get("discover", lambda *a, **k: ()),
    )
    return run_ci_fix(
        _gh_authorized(), command=parse_command(f"@valkeyrie-bot fix {_RUN_URL}"),
        pr_repo_full_name="valkey-io/valkey", pr_number=3988, commenter="alice",
        git_env={}, artifact_client=_artifact_client(overrides.get("logs", {"1.txt": b"err"})),
        diagnose_func=overrides.get("diagnose", lambda *a, **k: _proposal()),
        run_loop_func=overrides.get("loop", lambda *a, **k: _loop_success()),
        push_func=overrides.get("push", lambda *a, **k: "deadbeef" * 5),
        port_push_func=overrides.get("port_push", lambda *a, **k: "deadbeef" * 5),
        verify_runs=overrides.get("verify_runs", 2),
    )


def _loop_success():
    from scripts.ci_fix.review import LoopResult
    return LoopResult(success=True, run_result=_passed_run(),
                      review=ReviewVerdict(True, "ok"), changed_paths=("test.tcl",),
                      attempts=1, detail="ok")


def _loop_failure():
    from scripts.ci_fix.review import LoopResult
    return LoopResult(success=False, run_result=None, review=None,
                      changed_paths=(), attempts=3, detail="test still failing")


def test_pipeline_happy_path(monkeypatch):
    outcome = _run_pipeline(monkeypatch)
    assert outcome.kind is OutcomeKind.PUSHED
    assert outcome.commit_sha.startswith("deadbeef")


def test_pipeline_gate_rejection(monkeypatch):
    # Non-member: get_team_membership returns pending.
    gh = _gh_authorized()
    gh.get_organization.return_value.get_team_by_slug.return_value\
        .get_team_membership.return_value.state = "pending"
    monkeypatch.setattr("scripts.ci_fix.pipeline.shallow_clone_at_sha", lambda *a, **k: True)
    outcome = run_ci_fix(
        gh, command=parse_command(f"@valkeyrie-bot fix {_RUN_URL}"),
        pr_repo_full_name="valkey-io/valkey", pr_number=3988, commenter="stranger",
        git_env={}, artifact_client=_artifact_client({"x": b"y"}),
    )
    assert outcome.kind is OutcomeKind.REFUSED
    assert "not an active member" in outcome.summary


def test_pipeline_no_logs(monkeypatch):
    outcome = _run_pipeline(monkeypatch, logs={})
    assert outcome.kind is OutcomeKind.REFUSED
    assert "expired" in outcome.summary


def test_pipeline_clone_failure(monkeypatch):
    outcome = _run_pipeline(monkeypatch, clone=lambda *a, **k: False)
    assert outcome.kind is OutcomeKind.FAILED
    assert "clone" in outcome.summary


def test_pipeline_diagnose_refuses(monkeypatch):
    outcome = _run_pipeline(monkeypatch, diagnose=lambda *a, **k: _proposal(FixPath.REFUSE))
    assert outcome.kind is OutcomeKind.REFUSED


def test_pipeline_loop_failure(monkeypatch):
    outcome = _run_pipeline(monkeypatch, loop=lambda *a, **k: _loop_failure())
    assert outcome.kind is OutcomeKind.REFUSED
    assert "still failing" in outcome.summary


def test_pipeline_port_cherry_pick_pushes_without_local_verify(monkeypatch):
    from scripts.ci_fix.port_discovery import PortCandidate
    loop = MagicMock(side_effect=AssertionError("PORT must not run local verifier"))
    classify = MagicMock(side_effect=AssertionError("PORT does not need env classification"))
    # The model only sees the short SHA the prompt renders; the discovered
    # candidate carries the full 40-char SHA, as it does in the real flow.
    full_sha = "9f374e15848d7b070cdd58a071a741c0a59a6c75"
    proposal = _proposal(FixPath.PORT).__class__(
        **{**_proposal(FixPath.PORT).__dict__, "unstable_fix_commit": "9f374e15848d"}
    )
    ported = MagicMock(return_value="abc123" * 6)
    candidate = PortCandidate(sha=full_sha, subject="the upstream fix")

    outcome = _run_pipeline(
        monkeypatch, diagnose=lambda *a, **k: proposal,
        loop=loop, classify=classify, port_push=ported,
        discover=lambda *a, **k: (candidate,),
    )

    assert outcome.kind is OutcomeKind.PUSHED
    assert outcome.verify_backend == "upstream-port"
    assert outcome.review is not None
    assert "Ported upstream commit" in outcome.review.reasoning
    # the port push got the full upstream SHA, canonicalized from the short hint
    assert ported.call_args.kwargs["unstable_fix_commit"] == full_sha
    loop.assert_not_called()
    classify.assert_not_called()


def test_pipeline_port_conflict_refuses(monkeypatch):
    from scripts.ci_fix.port_discovery import PortCandidate
    proposal = _proposal(FixPath.PORT).__class__(
        **{**_proposal(FixPath.PORT).__dict__, "unstable_fix_commit": "9f374e15848d"}
    )
    candidate = PortCandidate(
        sha="9f374e15848d7b070cdd58a071a741c0a59a6c75", subject="the upstream fix",
    )

    def refuse(*_a, **_k):
        raise PushRefused("upstream fix did not cherry-pick cleanly: conflict")

    outcome = _run_pipeline(
        monkeypatch, diagnose=lambda *a, **k: proposal, port_push=refuse,
        discover=lambda *a, **k: (candidate,),
    )
    assert outcome.kind is OutcomeKind.REFUSED
    assert "cherry-pick" in outcome.summary


def test_pipeline_port_refuses_sha_not_in_discovered_candidates(monkeypatch):
    """A PORT SHA the model invents that was not surfaced as a candidate for
    this failure is refused, so PORT cannot bypass verification with an
    unrelated default-branch commit."""
    from scripts.ci_fix.port_discovery import PortCandidate
    proposal = _proposal(FixPath.PORT).__class__(
        **{**_proposal(FixPath.PORT).__dict__, "unstable_fix_commit": "9f374e15848d"}
    )
    ported = MagicMock(return_value="abc123" * 6)
    # A different commit was discovered; the model's SHA is not in the set.
    other = PortCandidate(
        sha="1111111111111111111111111111111111111111", subject="unrelated",
    )

    outcome = _run_pipeline(
        monkeypatch, diagnose=lambda *a, **k: proposal, port_push=ported,
        discover=lambda *a, **k: (other,),
    )
    assert outcome.kind is OutcomeKind.REFUSED
    assert "not among the fixes discovered" in outcome.summary
    ported.assert_not_called()


def test_pipeline_port_refuses_ambiguous_short_sha(monkeypatch):
    """A short SHA that prefixes more than one discovered candidate is
    ambiguous; the port is refused rather than guessing a commit."""
    from scripts.ci_fix.port_discovery import PortCandidate
    proposal = _proposal(FixPath.PORT).__class__(
        **{**_proposal(FixPath.PORT).__dict__, "unstable_fix_commit": "9f374e"}
    )
    ported = MagicMock(return_value="abc123" * 6)
    cand_a = PortCandidate(sha="9f374e15848d7b070cdd58a071a741c0a59a6c75", subject="fix a")
    cand_b = PortCandidate(sha="9f374eaa00000000000000000000000000000000", subject="fix b")

    outcome = _run_pipeline(
        monkeypatch, diagnose=lambda *a, **k: proposal, port_push=ported,
        discover=lambda *a, **k: (cand_a, cand_b),
    )
    assert outcome.kind is OutcomeKind.REFUSED
    assert "not among the fixes discovered" in outcome.summary
    ported.assert_not_called()


def test_pipeline_push_refused(monkeypatch):
    def refuse(*a, **k):
        raise PushRefused("bad branch namespace")
    outcome = _run_pipeline(monkeypatch, push=refuse)
    assert outcome.kind is OutcomeKind.REFUSED
    assert "bad branch namespace" in outcome.summary


# --- backend routing ---

def test_pipeline_docker_passes_image_to_loop(monkeypatch):
    from scripts.ci_fix.verify.base import VerifyEnv
    from scripts.ci_fix.verify.workflow_env import JobEnvironment
    seen = {}

    def loop(repo_dir, proposal, **kwargs):
        seen["image"] = kwargs.get("container_image")
        return _loop_success()

    outcome = _run_pipeline(
        monkeypatch,
        classify=lambda *a, **k: JobEnvironment(VerifyEnv.DOCKER, image="almalinux:8"),
        loop=loop,
    )
    assert outcome.kind is OutcomeKind.PUSHED
    assert seen["image"] == "almalinux:8"
    assert outcome.verify_backend == "docker:almalinux:8"


def test_pipeline_passes_verify_runs_to_loop(monkeypatch):
    seen = {}

    def loop(repo_dir, proposal, **kwargs):
        seen["verify_runs"] = kwargs.get("verify_runs")
        return _loop_success()

    outcome = _run_pipeline(monkeypatch, loop=loop, verify_runs=5)
    assert outcome.kind is OutcomeKind.PUSHED
    assert seen["verify_runs"] == 5


def test_pipeline_unsupported_env_refuses(monkeypatch):
    from scripts.ci_fix.verify.base import VerifyEnv
    from scripts.ci_fix.verify.workflow_env import JobEnvironment
    outcome = _run_pipeline(
        monkeypatch,
        classify=lambda *a, **k: JobEnvironment(VerifyEnv.UNSUPPORTED, reason="self-hosted arm"),
    )
    assert outcome.kind is OutcomeKind.REFUSED
    assert "self-hosted arm" in outcome.summary


def test_pipeline_refuses_job_not_in_failed_set(monkeypatch):
    # The AI names a job that did not fail in the linked run; refuse.
    def diagnose(*a, **k):
        p = _proposal()
        return p.__class__(**{**p.__dict__, "failing_job_hint": "some-other-job"})
    outcome = _run_pipeline(monkeypatch, diagnose=diagnose)
    assert outcome.kind is OutcomeKind.REFUSED
    assert "not among the failed jobs" in outcome.summary


def _macos_pipeline(monkeypatch, verifier, *, apply_ok=True, review_ok=True):
    from scripts.ci_fix.verify.base import VerifyEnv
    from scripts.ci_fix.verify.workflow_env import JobEnvironment
    monkeypatch.setattr("scripts.ci_fix.pipeline.shallow_clone_at_sha", lambda *a, **k: True)
    monkeypatch.setattr("scripts.ci_fix.pipeline._classify_failing_job",
                        lambda *a, **k: JobEnvironment(VerifyEnv.MACOS))
    monkeypatch.setattr("scripts.ci_fix.pipeline.apply_fix",
                        lambda *a, **k: (apply_ok, ("test.tcl",) if apply_ok else ()))
    monkeypatch.setattr("scripts.ci_fix.pipeline.build_and_review_patch",
                        lambda *a, **k: _patch_review(review_ok))
    return run_ci_fix(
        _gh_authorized(), command=parse_command(f"@valkeyrie-bot fix {_RUN_URL}"),
        pr_repo_full_name="valkey-io/valkey", pr_number=3988, commenter="alice",
        git_env={}, artifact_client=_artifact_client({"1.txt": b"err"}),
        diagnose_func=lambda *a, **k: _proposal(),
        push_func=lambda *a, **k: "cafe" * 10,
        macos_verifier=verifier,
    )


def _patch_review(ok):
    from scripts.ci_fix.review import PatchReview
    return PatchReview(ok=ok, patch="diff\n",
                       review=ReviewVerdict(ok, "ok" if ok else "weak"),
                       detail="" if ok else "review rejected the fix: weak")


def test_pipeline_macos_green_pushes(monkeypatch):
    from scripts.ci_fix.verify.base import VerificationResult
    verifier = MagicMock()
    verifier.verify.return_value = VerificationResult(verified=True, ran=True, detail="ok", run_url="https://run/9")
    outcome = _macos_pipeline(monkeypatch, verifier)
    assert outcome.kind is OutcomeKind.PUSHED
    assert outcome.verify_backend == "macos"
    assert outcome.macos_run_url == "https://run/9"


def test_pipeline_macos_red_refuses(monkeypatch):
    from scripts.ci_fix.verify.base import VerificationResult
    verifier = MagicMock()
    verifier.verify.return_value = VerificationResult(verified=False, ran=True, detail="did not pass", run_url="https://run/9")
    outcome = _macos_pipeline(monkeypatch, verifier)
    assert outcome.kind is OutcomeKind.REFUSED
    assert "did not pass" in outcome.summary


def test_pipeline_macos_unavailable_refuses(monkeypatch):
    outcome = _macos_pipeline(monkeypatch, None)
    assert outcome.kind is OutcomeKind.REFUSED
    assert "not configured" in outcome.summary


# --- _classify_failing_job over real workflow files ---

def _write_workflows(tmp_path, files):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    for name, body in files.items():
        (wf / name).write_text(body)
    return tmp_path


def test_classify_finds_job_in_workflow(tmp_path):
    from scripts.ci_fix.pipeline import _classify_failing_job
    from scripts.ci_fix.verify.base import VerifyEnv
    repo = _write_workflows(tmp_path, {
        "ci.yml": "jobs:\n  build-mac:\n    runs-on: macos-latest\n    steps:\n      - run: make\n",
    })
    env = _classify_failing_job(repo, "build-mac")
    assert env.env is VerifyEnv.MACOS


def test_classify_ambiguous_cross_workflow_refuses(tmp_path):
    from scripts.ci_fix.pipeline import _classify_failing_job
    from scripts.ci_fix.verify.base import VerifyEnv
    repo = _write_workflows(tmp_path, {
        "a.yml": "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n",
        "b.yml": "jobs:\n  test:\n    runs-on: macos-latest\n    steps:\n      - run: make\n",
    })
    env = _classify_failing_job(repo, "test")
    assert env.env is VerifyEnv.UNSUPPORTED
    assert "multiple workflows" in env.reason


def test_classify_missing_job_refuses(tmp_path):
    from scripts.ci_fix.pipeline import _classify_failing_job
    from scripts.ci_fix.verify.base import VerifyEnv
    repo = _write_workflows(tmp_path, {
        "ci.yml": "jobs:\n  other:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n",
    })
    assert _classify_failing_job(repo, "nope").env is VerifyEnv.UNSUPPORTED


# --- _match_failed_job ambiguity ---

def test_match_failed_job_exact_and_single_base():
    from scripts.ci_fix.pipeline import _match_failed_job
    assert _match_failed_job("build", ("build", "lint")) == "build"
    # matrix suffix: single base-name match resolves
    assert _match_failed_job("test", ("test (clang)",)) == "test (clang)"


def test_match_failed_job_refuses_ambiguous_matrix():
    from scripts.ci_fix.pipeline import _match_failed_job
    # two matrix legs share the base name "test" -> ambiguous -> None
    assert _match_failed_job("test", ("test (a)", "test (b)")) is None


def test_match_failed_job_none_when_not_failed():
    from scripts.ci_fix.pipeline import _match_failed_job
    assert _match_failed_job("other", ("build",)) is None


def test_read_workflow_safely_skips_symlink_and_oversized(tmp_path):
    from scripts.ci_fix.pipeline import _MAX_WORKFLOW_BYTES, _read_workflow_safely

    good = tmp_path / "ok.yml"
    good.write_text("jobs: {}\n")
    assert _read_workflow_safely(good) == "jobs: {}\n"

    big = tmp_path / "big.yml"
    big.write_text("x" * (_MAX_WORKFLOW_BYTES + 1))
    assert _read_workflow_safely(big) is None

    target = tmp_path / "target.yml"
    target.write_text("jobs: {}\n")
    link = tmp_path / "link.yml"
    link.symlink_to(target)
    assert _read_workflow_safely(link) is None

    assert _read_workflow_safely(tmp_path / "missing.yml") is None


def test_pipeline_handoff_when_verify_cannot_run(monkeypatch):
    """A loop that could not verify the fix yields a HANDOFF outcome carrying
    the patch, not a refusal or a push."""
    from scripts.ci_fix.review import LoopResult

    handoff_loop = LoopResult(
        success=False, run_result=None, review=ReviewVerdict(True, "looks sane"),
        changed_paths=("src/x.c",), attempts=1,
        detail="could not verify the fix here (missing dep); handing off",
        handoff=True, handoff_patch="--- a/src/x.c\n+++ b/src/x.c\n",
    )
    outcome = _run_pipeline(monkeypatch, loop=lambda *a, **k: handoff_loop)
    assert outcome.kind is OutcomeKind.HANDOFF
    assert outcome.handoff_patch == "--- a/src/x.c\n+++ b/src/x.c\n"
    assert "could not verify" in outcome.summary
