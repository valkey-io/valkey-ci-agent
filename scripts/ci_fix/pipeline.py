"""Top-level orchestration for ``@valkeyrie-bot fix <ci-link>``.

Thin wiring over a clean data flow; every refusal returns a ``FixOutcome`` so
the workflow always posts an explanatory comment:

    gate                         -> FixRequest | refuse
    failed_jobs_for_run (code)   -> the jobs that actually failed
    download logs, clone at SHA
    diagnose (read-only AI)      -> FixProposal (a fix + a job *hint*)
    plan_verification (code)     -> VerificationPlan (code-selected backend) | refuse
    apply + review               -> approved PatchReview
    backend.verify(plan)         -> VerificationResult (real verdict)
    commit + namespace push      -> FixOutcome
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from scripts.ci_fix.apply import apply_fix
from scripts.ci_fix.diagnose import diagnose_failure, write_logs_to_workspace
from scripts.ci_fix.gate import GateRejection, ParsedCommand, build_fix_request
from scripts.ci_fix.models import (
    FixOutcome,
    FixPath,
    FixProposal,
    FixRequest,
    OutcomeKind,
    ReviewVerdict,
)
from scripts.ci_fix.port_discovery import PortCandidate, discover_port_candidates
from scripts.ci_fix.push import PushRefused, commit_and_push_fix, commit_and_push_port
from scripts.ci_fix.review import (
    DEFAULT_VERIFY_RUNS,
    LoopResult,
    build_and_review_patch,
    combined_command,
    precheck_command,
    run_fix_loop,
)
from scripts.ci_fix.verify.base import (
    VerificationPlan,
    VerificationResult,
    VerifyBackend,
    VerifyEnv,
    backend_label,
)
from scripts.ci_fix.verify.github_runs import failed_jobs_for_run
from scripts.ci_fix.verify.workflow_env import JobEnvironment, classify_job_environment
from scripts.common.git_clone import shallow_clone_at_sha
from scripts.common.workflow_artifacts import ArtifactClient

logger = logging.getLogger(__name__)

Diagnose = Callable[..., FixProposal]
RunLoop = Callable[..., LoopResult]
Push = Callable[..., str]
PortPush = Callable[..., str]


def run_ci_fix(
    gh: Any,
    *,
    command: ParsedCommand,
    pr_repo_full_name: str,
    pr_number: int,
    commenter: str,
    git_env: dict[str, str],
    artifact_client: ArtifactClient,
    org: str = "valkey-io",
    auth_team: str = "contributors",
    verify_runs: int = DEFAULT_VERIFY_RUNS,
    diagnose_func: Diagnose = diagnose_failure,
    run_loop_func: RunLoop = run_fix_loop,
    push_func: Push = commit_and_push_fix,
    port_push_func: PortPush = commit_and_push_port,
    macos_verifier: VerifyBackend | None = None,
) -> FixOutcome:
    """Run the whole pipeline and return a terminal ``FixOutcome``."""
    request = build_fix_request(
        gh, command=command, pr_repo_full_name=pr_repo_full_name,
        pr_number=pr_number, commenter=commenter, org=org, auth_team=auth_team,
    )
    if isinstance(request, GateRejection):
        return FixOutcome(kind=OutcomeKind.REFUSED, summary=request.reason)

    failed_jobs = tuple(j.name for j in failed_jobs_for_run(gh, request.repo_full_name, request.run_id))

    with tempfile.TemporaryDirectory(prefix="ci-fix-") as workdir_str:
        outcome = _run_in_workspace(
            Path(workdir_str), request, failed_jobs,
            artifact_client=artifact_client, git_env=git_env,
            diagnose_func=diagnose_func, run_loop_func=run_loop_func, push_func=push_func,
            port_push_func=port_push_func, macos_verifier=macos_verifier,
            verify_runs=verify_runs,
        )
    run_url = f"https://github.com/{request.repo_full_name}/actions/runs/{request.run_id}"
    return replace(outcome, failing_run_url=run_url)


def _run_in_workspace(
    workdir: Path,
    request: FixRequest,
    failed_jobs: tuple[str, ...],
    *,
    artifact_client: ArtifactClient,
    git_env: dict[str, str],
    diagnose_func: Diagnose,
    run_loop_func: RunLoop,
    push_func: Push,
    port_push_func: PortPush,
    macos_verifier: VerifyBackend | None,
    verify_runs: int,
) -> FixOutcome:
    logs = artifact_client.download_run_logs(request.repo_full_name, request.run_id)
    if not logs:
        return FixOutcome(
            kind=OutcomeKind.REFUSED,
            summary="The run's logs have expired and can no longer be downloaded; cannot diagnose.",
        )
    logs_dir = write_logs_to_workspace(logs, workdir)

    repo_dir = workdir / "repo"
    if not shallow_clone_at_sha(request.repo_full_name, repo_dir, request.head_sha):
        return FixOutcome(
            kind=OutcomeKind.FAILED,
            summary=f"Could not clone {request.repo_full_name} at {request.head_sha[:12]}.",
        )

    port_candidates = discover_port_candidates(str(repo_dir), str(logs_dir))
    proposal = diagnose_func(
        str(logs_dir), str(repo_dir), hint=request.hint,
        port_candidates=port_candidates,
    )
    if proposal.path is FixPath.REFUSE:
        return _refuse(proposal, proposal.reasoning or "No safe fix found.")

    if proposal.path is FixPath.PORT:
        return _port_and_push(
            repo_dir, request, proposal, failed_jobs, git_env=git_env,
            port_push_func=port_push_func, port_candidates=port_candidates,
        )

    plan = _plan_verification(repo_dir, request, proposal, failed_jobs)
    if isinstance(plan, str):  # a refusal reason
        return _refuse(proposal, plan)

    if plan.env is VerifyEnv.MACOS:
        return _verify_once_and_push(
            repo_dir, request, proposal, plan,
            verifier=macos_verifier, git_env=git_env, push_func=push_func,
        )
    return _loop_and_push(
        repo_dir, request, proposal, plan,
        run_loop_func=run_loop_func, git_env=git_env, push_func=push_func,
        verify_runs=verify_runs,
    )


def _plan_verification(
    repo_dir: Path, request: FixRequest, proposal: FixProposal, failed_jobs: tuple[str, ...],
) -> VerificationPlan | str:
    """Select the verification backend from the real failed job, or return a refusal reason.

    The AI's ``failing_job_hint`` must match a job that actually failed in the
    linked run; code then classifies that job's workflow environment. The AI
    never selects the environment.
    """
    job = _match_failed_job(proposal.failing_job_hint, failed_jobs)
    if job is None:
        return (
            f"The named job {proposal.failing_job_hint or '(none)'!r} is not among the failed "
            f"jobs of the linked run ({', '.join(failed_jobs) or 'none found'}); "
            "refusing rather than verifying a job that did not fail."
        )
    env = _classify_failing_job(repo_dir, job)
    if env.env is VerifyEnv.UNSUPPORTED:
        return (
            f"Cannot verify the {job!r} job in a controlled environment "
            f"({env.reason}); refusing rather than pushing an unverified fix."
        )
    return VerificationPlan(
        env=env.env,
        command=combined_command(proposal),
        workdir=proposal.workdir,
        image=env.image,
        job_name=job,
        head_sha=request.head_sha,
        target_repo=request.head_repo_full_name,
    )


def _loop_and_push(
    repo_dir: Path, request: FixRequest, proposal: FixProposal, plan: VerificationPlan,
    *, run_loop_func: RunLoop, git_env: dict[str, str], push_func: Push,
    verify_runs: int,
) -> FixOutcome:
    """Local/Docker: apply, verify in-loop (retry on fail), review, push on green."""
    loop = run_loop_func(
        str(repo_dir), proposal, container_image=plan.image, verify_runs=verify_runs,
    )
    if loop.handoff:
        return FixOutcome(
            kind=OutcomeKind.HANDOFF, summary=loop.detail, proposal=proposal,
            run_result=loop.run_result, review=loop.review,
            handoff_patch=loop.handoff_patch,
            other_failing_checks=proposal.other_failing_checks,
        )
    if not loop.success:
        return FixOutcome(
            kind=OutcomeKind.REFUSED, summary=loop.detail, proposal=proposal,
            run_result=loop.run_result, review=loop.review,
            other_failing_checks=proposal.other_failing_checks,
        )
    backend = backend_label(plan.env, plan.image)
    return _push(
        repo_dir, request, proposal, loop.changed_paths,
        review=loop.review, run_result=loop.run_result,
        verify_backend=backend, git_env=git_env, push_func=push_func,
    )


def _port_and_push(
    repo_dir: Path, request: FixRequest, proposal: FixProposal, failed_jobs: tuple[str, ...],
    *, git_env: dict[str, str], port_push_func: PortPush = commit_and_push_port,
    port_candidates: tuple[PortCandidate, ...] = (),
) -> FixOutcome:
    """Apply an already-merged upstream fix and publish it.

    PORT is the only path allowed to rely on the PR's normal CI as the
    authoritative verifier. That trust is bounded by code: the SHA must be one
    of the candidates discovered from this failure's logs (so the model cannot
    point at an arbitrary default-branch commit), the hinted job must be a real
    failed job, and the push path independently re-verifies the commit's
    ancestry. Original authorship is preserved.
    """
    job = _match_failed_job(proposal.failing_job_hint, failed_jobs)
    if job is None:
        return _refuse(
            proposal,
            f"The named job {proposal.failing_job_hint or '(none)'!r} is not among the failed "
            f"jobs of the linked run ({', '.join(failed_jobs) or 'none found'}); "
            "refusing rather than porting a fix for a job that did not fail.",
        )
    if not proposal.unstable_fix_commit.strip():
        return _refuse(proposal, "diagnosis chose PORT but did not name an upstream fix commit")

    chosen = proposal.unstable_fix_commit.strip()
    full_sha = _canonical_candidate_sha(chosen, port_candidates)
    if full_sha is None:
        return _refuse(
            proposal,
            f"The chosen commit {chosen[:12]} is not among the "
            "fixes discovered for this failure; refusing to port a commit the code "
            "did not surface as a candidate.",
        )

    try:
        commit_sha = port_push_func(
            str(repo_dir),
            head_repo_full_name=request.head_repo_full_name,
            head_branch=request.head_branch,
            head_sha=request.head_sha,
            unstable_fix_commit=full_sha,
            git_env=git_env,
        )
    except PushRefused as exc:
        return _refuse(proposal, str(exc))

    review = ReviewVerdict(
        approved=True,
        reasoning=(
            f"Ported upstream commit {full_sha[:12]} with its original "
            "authorship; this PORT path relies on the PR's normal CI as the verification authority."
        ),
    )
    return FixOutcome(
        kind=OutcomeKind.PUSHED,
        summary=f"Ported upstream fix for {proposal.failing_check}",
        proposal=proposal, review=review, commit_sha=commit_sha,
        verify_backend="upstream-port",
        other_failing_checks=proposal.other_failing_checks,
    )


def _verify_once_and_push(
    repo_dir: Path, request: FixRequest, proposal: FixProposal, plan: VerificationPlan,
    *, verifier: VerifyBackend | None, git_env: dict[str, str], push_func: Push,
) -> FixOutcome:
    """macOS: no local build loop. Apply once, review the patch, verify remotely, push on green.

    Any unexpected error becomes a FAILED outcome so the invocation always
    produces a PR comment.
    """
    if verifier is None:
        return _refuse(proposal, "macOS verification is not configured for this run; refusing.")
    try:
        precheck = precheck_command(proposal)
        if precheck:
            return _refuse(proposal, precheck)

        applied, changed = apply_fix(str(repo_dir), proposal)
        if not applied:
            return _refuse(proposal, "fix not applied (agent declined or made no edits)")

        reviewed = build_and_review_patch(str(repo_dir), changed, proposal)
        if not reviewed.ok:
            return FixOutcome(
                kind=OutcomeKind.REFUSED, summary=reviewed.detail,
                proposal=proposal, review=reviewed.review,
                other_failing_checks=proposal.other_failing_checks,
            )

        result: VerificationResult = verifier.verify(str(repo_dir), plan, reviewed.patch)
        if not result.verified:
            return FixOutcome(
                kind=OutcomeKind.REFUSED, summary=result.detail,
                proposal=proposal, review=reviewed.review, macos_run_url=result.run_url,
                other_failing_checks=proposal.other_failing_checks,
            )
        return _push(
            repo_dir, request, proposal, changed,
            review=reviewed.review, verify_backend=backend_label(VerifyEnv.MACOS),
            macos_run_url=result.run_url, git_env=git_env, push_func=push_func,
        )
    except Exception:  # noqa: BLE001 - every outcome must become a comment
        logger.exception("macOS verification raised unexpectedly")
        return FixOutcome(
            kind=OutcomeKind.FAILED,
            summary=(
                "An internal error stopped macOS verification before a fix could "
                "be confirmed; see the bot run logs for details."
            ),
            proposal=proposal, other_failing_checks=proposal.other_failing_checks,
        )


def _push(
    repo_dir: Path, request: FixRequest, proposal: FixProposal, changed_paths: tuple[str, ...],
    *, review: Any = None, run_result: Any = None, verify_backend: str = "",
    macos_run_url: str = "", git_env: dict[str, str], push_func: Push,
) -> FixOutcome:
    try:
        commit_sha = push_func(
            str(repo_dir),
            head_repo_full_name=request.head_repo_full_name,
            head_branch=request.head_branch,
            head_sha=request.head_sha,
            proposal=proposal,
            changed_paths=changed_paths,
            git_env=git_env,
        )
    except PushRefused as exc:
        return FixOutcome(
            kind=OutcomeKind.REFUSED, summary=str(exc),
            proposal=proposal, run_result=run_result, review=review, macos_run_url=macos_run_url,
            other_failing_checks=proposal.other_failing_checks,
        )
    return FixOutcome(
        kind=OutcomeKind.PUSHED,
        summary=f"Pushed fix for {proposal.failing_check}",
        proposal=proposal, run_result=run_result, review=review, commit_sha=commit_sha,
        verify_backend=verify_backend, macos_run_url=macos_run_url,
        other_failing_checks=proposal.other_failing_checks,
    )


def _refuse(proposal: FixProposal, summary: str) -> FixOutcome:
    return FixOutcome(
        kind=OutcomeKind.REFUSED, summary=summary, proposal=proposal,
        other_failing_checks=proposal.other_failing_checks,
    )


def _match_failed_job(hint: str, failed_jobs: tuple[str, ...]) -> str | None:
    """Return the single failed job the AI's ``hint`` refers to, or None.

    Requires the hint to correspond to a job that actually failed in the linked
    run, so the AI cannot pick an arbitrary or safer job. Matches exactly, or on
    the base name before a matrix suffix (GitHub names matrix legs like
    ``test-sanitizer (clang)``). If more than one failed job shares that base
    name (e.g. ``test (a)`` and ``test (b)`` both failed and the hint is
    ``test``), the target is ambiguous and we return None rather than guess.
    """
    if not hint or not failed_jobs:
        return None
    exact = [j for j in failed_jobs if j == hint]
    if exact:
        return exact[0]
    hint_base = hint.split(" (")[0]
    base_matches = [j for j in failed_jobs if j.split(" (")[0] == hint_base]
    if len(base_matches) == 1:
        return base_matches[0]
    return None  # zero matches, or ambiguous (multiple matrix legs)


def _canonical_candidate_sha(
    chosen: str, candidates: tuple[PortCandidate, ...],
) -> str | None:
    """Resolve the model's chosen commit to a discovered candidate's full SHA.

    The diagnosis prompt renders candidates as short (12-char) SHAs, so the
    model echoes back a short SHA while the candidate set carries full 40-char
    SHAs. Match by prefix in either direction and return the candidate's full
    SHA, which the push path then ancestry-verifies. Returns None when the
    chosen SHA matches no candidate, or when a short prefix is ambiguous across
    more than one candidate (refuse rather than guess).
    """
    chosen = chosen.strip().lower()
    if not chosen:
        return None
    matches = {
        c.sha
        for c in candidates
        if c.sha.lower().startswith(chosen) or chosen.startswith(c.sha.lower())
    }
    if len(matches) == 1:
        return next(iter(matches))
    return None  # zero matches, or an ambiguous prefix


_MAX_WORKFLOW_BYTES = 1024 * 1024  # workflow YAML over 1 MiB is not a real workflow


def _read_workflow_safely(path: Path) -> str | None:
    """Read a workflow file from an untrusted checkout, or return ``None``.

    The checkout is PR-controlled, so skip symlinks (which could point outside
    the tree), cap the size, and swallow ``OSError`` rather than letting a
    crafted entry abort classification.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > _MAX_WORKFLOW_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _classify_failing_job(repo_dir: Path, failing_job: str) -> JobEnvironment:
    """Classify the failed job's environment from the repo's own workflows.

    Code (not the AI) decides the environment. Job names are not unique across
    workflow files: if the same name appears in more than one workflow with
    different environments, we cannot tell which produced the failure, so we
    refuse rather than guess. If all matches agree, that environment is used.
    """
    base = failing_job.split(" (")[0]  # strip matrix suffix; jobs: key is the base name
    workflows = repo_dir / ".github" / "workflows"
    if not workflows.is_dir():
        return JobEnvironment(VerifyEnv.UNSUPPORTED, reason="no .github/workflows in the repo")

    matches: list[JobEnvironment] = []
    for path in sorted(workflows.glob("*.y*ml")):
        content = _read_workflow_safely(path)
        if content is None:
            continue
        env = classify_job_environment(content, base)
        if env.env is not VerifyEnv.UNSUPPORTED:
            matches.append(env)
    if not matches:
        return JobEnvironment(
            VerifyEnv.UNSUPPORTED,
            reason=f"job {base!r} not found in any workflow, or its environment is unsupported",
        )
    if len({(m.env, m.image) for m in matches}) > 1:
        return JobEnvironment(
            VerifyEnv.UNSUPPORTED,
            reason=(
                f"job {base!r} appears in multiple workflows with different "
                "environments; cannot determine which failed, refusing"
            ),
        )
    return matches[0]
