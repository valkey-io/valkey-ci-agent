"""Fix one current-head CI failure on an open backport sweep PR.

This module is intentionally only a selector and front door. It finds the
bot-owned sweep PR for one registered release branch, waits for its current
head's CI to finish, chooses one unhandled actionable job, and plans its fix,
which runs like every other (:mod:`scripts.ci_fix.run`).

The engine owns diagnosis, editing, and publication. The target PR's normal CI
remains the final verifier, matching the existing backport workflow model. A
result comment carries a hidden marker with its outcome, so the same logical
job is not retried on the same head once it has a result. A failed attempt (an
agent crash, the time running out) is retried, up to ``MAX_FAILED_ATTEMPTS``
in all.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from scripts.backport.registry import RepoEntry, load_registry
from scripts.backport.sweep import _BRANCH_PREFIX
from scripts.backport.sweep_prs import find_existing_pr
from scripts.ci_fix.comment import render_result
from scripts.ci_fix.github_runs import (
    FailedJob,
    most_actionable_failure,
    newest_runs_for_head,
    workflow_file,
    workflow_identity,
)
from scripts.ci_fix.models import (
    MAX_FAILED_ATTEMPTS,
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
from scripts.ci_fix.policy import is_backport_branch, is_writable_branch
from scripts.common.github_client import retry_github_call
from scripts.common.workflow_jobs import job_is_ignored

logger = logging.getLogger(__name__)

_MARKER_RE = re.compile(
    r"<!-- valkey-ci-agent:auto-ci-followup "
    r"head=(?P<head>[0-9a-f]{40}) run=(?P<run>\d+)"
    r"(?: attempt=(?P<attempt>\d+))? job=(?P<job>\d+)"
    r"(?: key=(?P<key>[0-9a-f]{64}))?(?: outcome=(?P<outcome>[a-z]+))? -->"
)
_MAX_ATTEMPTS_PER_HEAD = 3


@dataclass(frozen=True)
class FollowupTarget:
    """One failed job selected from the current sweep PR head."""

    pr: Any
    run: Any
    head_sha: str
    head_branch: str
    job: FailedJob


@dataclass(frozen=True)
class _HandledFailures:
    job_ids: frozenset[int]
    job_keys: frozenset[str]
    attempt_count: int


def find_followup_target(
    gh: Any,
    *,
    repo_entry: RepoEntry,
    target_branch: str,
    bot_login: str,
) -> tuple[FollowupTarget | None, str]:
    """Return one safe, unhandled current-head failure or a skip reason.

    Every open bot-owned PR into ``target_branch`` is considered, not only the
    sweep PR: a pushed CI fix and a draft PR opened for a detector issue start
    the repository's own CI, and nothing else watches whether it goes red.
    """
    if not repo_entry.automatic_ci_followup:
        return None, "disabled"
    if repo_entry.effective_push_repo != repo_entry.repo:
        return None, "fork-head-unsupported"

    prs = _bot_prs_into(gh, repo_entry, target_branch=target_branch, bot_login=bot_login)
    if not prs:
        return None, "no-open-bot-pr"
    reasons: list[str] = []
    for pr in prs:
        target, reason = _followup_for_pr(
            gh, pr, repo_entry=repo_entry, target_branch=target_branch, bot_login=bot_login,
        )
        if target is not None:
            return target, reason
        reasons.append(reason)
    # The sweep PR's reason first: it is the one a maintainer expects.
    return None, reasons[0]


def _bot_prs_into(
    gh: Any, repo_entry: RepoEntry, *, target_branch: str, bot_login: str,
) -> list[Any]:
    """Open bot-owned PRs into ``target_branch`` from a namespace this engine writes.

    The deterministic sweep PR comes first, then any other, newest first, so a
    sweep failure is still handled before a fix PR's.
    """
    sweep_branch = f"{_BRANCH_PREFIX}/{target_branch}"
    sweep = find_existing_pr(gh, repo_entry.repo, repo_entry.effective_push_repo, sweep_branch)
    found = [sweep] if sweep is not None else []
    try:
        others = retry_github_call(
            lambda: list(gh.get_repo(repo_entry.repo).get_pulls(
                state="open", base=target_branch, sort="created", direction="desc",
            )[:_MAX_SCANNED_PRS]),
            retries=2,
            description=f"list open PRs into {target_branch}",
        )
    except Exception as exc:  # noqa: BLE001 - the sweep PR is still followed up
        logger.warning("Could not list open PRs into %s: %s", target_branch, exc)
        return found
    for pr in others:
        head = str(getattr(getattr(pr, "head", None), "ref", "") or "")
        if head == sweep_branch or not is_writable_branch(head):
            continue
        if str(getattr(getattr(pr, "user", None), "login", "") or "") != bot_login:
            continue
        found.append(pr)
    return found


def _followup_for_pr(
    gh: Any,
    pr: Any,
    *,
    repo_entry: RepoEntry,
    target_branch: str,
    bot_login: str,
) -> tuple[FollowupTarget | None, str]:
    """One unhandled failure on ``pr``'s exact current head, or why there is none."""
    head_branch = str(getattr(getattr(pr, "head", None), "ref", "") or "")
    if not is_writable_branch(head_branch):
        return None, "head-branch-not-writable"
    rejection = _validate_sweep_pr(
        pr,
        repo_full_name=repo_entry.repo,
        target_branch=target_branch,
        bot_login=bot_login,
    )
    if rejection:
        return None, rejection

    head_sha = str(getattr(pr.head, "sha", "") or "")
    handled = _handled_failures(pr, head_sha, bot_login)
    if handled.attempt_count >= _MAX_ATTEMPTS_PER_HEAD:
        return None, "attempt-budget-exhausted"

    current_runs = newest_runs_for_head(
        gh,
        repo_entry.repo,
        branch=head_branch,
        head_sha=head_sha,
    )

    if not current_runs:
        return None, "no-current-head-runs"
    if any(
        str(getattr(run, "status", "") or "") != "completed"
        for run in current_runs
    ):
        return None, "current-head-ci-running"

    picked = most_actionable_failure(
        gh, repo_entry.repo, current_runs,
        keep=lambda run, job: bool(job.id)
        and job.id not in handled.job_ids
        and _job_key(run, job.name) not in handled.job_keys
        and not job_is_ignored(job.name, repo_entry.ci_followup_ignored_jobs),
    )
    if picked is None:
        return None, "no-unhandled-actionable-failures"
    run, job, _failed = picked
    return FollowupTarget(pr=pr, run=run, head_sha=head_sha, head_branch=head_branch, job=job), "actionable"


def plan_request(
    gh: Any,
    _artifact_client: Any,
    request: dict[str, Any],
    *,
    bot_login: str,
    registry: str = "repos.yml",
) -> FixPlan | dict[str, Any]:
    """The fix plan for one failure on ``request``'s branch (``{"repo", "branch"}``), or why there is none."""
    repo_entry, _branch_entry = load_registry(registry).get_branch(str(request["repo"]), str(request["branch"]))
    target_branch = str(request["branch"])
    target, reason = find_followup_target(
        gh,
        repo_entry=repo_entry,
        target_branch=target_branch,
        bot_login=bot_login,
    )
    if target is None:
        return {
            "repo": repo_entry.repo,
            "branch": target_branch,
            "action": "skipped",
            "reason": reason,
        }

    selected_job = target.job
    run_attempt = int(getattr(target.run, "run_attempt", 1) or 1)
    failure = Failure(
        repo_full_name=repo_entry.repo,
        run_id=int(target.run.id),
        run_attempt=run_attempt,
        head_sha=target.head_sha,
        job_id=selected_job.id,
        job_name=selected_job.name,
        base_branch=target_branch,
        description=f"the failure in job `{selected_job.name}`",
        pr_number=int(target.pr.number),
        workflow=workflow_file(target.run),
    )
    task = FixTask(
        failure=failure,
        target=FixTarget(
            kind=TargetKind.UPDATE_PR,
            repo_full_name=repo_entry.repo,
            base_repo_full_name=repo_entry.repo,
            branch=target.head_branch,
            expected_sha=target.head_sha,
        ),
        # A backport branch's failure is usually a missing upstream change; a
        # pushed CI fix or a draft PR has no upstream counterpart to port.
        policy=FixPolicy.BACKPORT if is_backport_branch(target.head_branch) else FixPolicy.GENERAL,
    )
    return FixPlan(
        front_door=FrontDoor.FOLLOW_UP, task=task,
        markers=tuple(_markers(failure, target.run, (selected_job,))),
    )


def report_result(gh: Any, plan: FixPlan, result: FixResult) -> dict[str, Any]:
    """Comment the result on the PR, with the markers that record its outcome.

    The outcome tells a refusal, which retires the job but changed nothing,
    and a failure, which is retried a bounded number of times, from an
    attempt that counts against the head's budget.
    """
    failure = plan.task.failure
    what = "backport" if is_backport_branch(plan.task.target.branch) else "pull request"
    body = f"Automatic follow-up on this {what}'s CI.\n\n" + render_result(result)
    body += "\n\n" + "\n".join(
        marker.removesuffix(" -->") + f" outcome={result.kind.value} -->" for marker in plan.markers
    )
    retry_github_call(
        lambda: gh.get_repo(failure.repo_full_name).get_pull(failure.pr_number).create_issue_comment(body),
        retries=3,
        description=f"post automatic CI follow-up on #{failure.pr_number}",
    )
    response: dict[str, Any] = {
        "repo": failure.repo_full_name,
        "branch": failure.base_branch,
        "action": result.kind.value,
        "pr": failure.pr_number,
        "head": failure.head_sha,
        "run": failure.run_id,
        "run_attempt": failure.run_attempt,
        "job": {
            "id": failure.job_id,
            "name": failure.job_name,
        },
        "summary": result.summary,
    }
    if result.commit_sha:
        response["commit"] = result.commit_sha
    return response


# How many open PRs into one branch are inspected for a bot-owned fix PR.
_MAX_SCANNED_PRS = 50


def _validate_sweep_pr(
    pr: Any,
    *,
    repo_full_name: str,
    target_branch: str,
    bot_login: str,
) -> str:
    """Return ``""`` only for the expected open, bot-owned sweep PR."""
    if str(getattr(pr, "state", "open") or "") != "open":
        return "pr-not-open"
    if str(getattr(pr.base, "ref", "") or "") != target_branch:
        return "base-branch-mismatch"
    head_repo = str(
        getattr(getattr(pr.head, "repo", None), "full_name", "") or ""
    )
    if head_repo != repo_full_name:
        return "head-repository-mismatch"
    if not re.fullmatch(
        r"[0-9a-f]{40}",
        str(getattr(pr.head, "sha", "") or ""),
    ):
        return "invalid-head-sha"
    author = str(getattr(getattr(pr, "user", None), "login", "") or "")
    if author != bot_login:
        return "pr-not-bot-owned"
    return ""


def _handled_failures(
    pr: Any,
    head_sha: str,
    bot_login: str,
) -> _HandledFailures:
    """Return the attempts and handled jobs for ``head_sha`` only.

    A refusal changed nothing on the branch: it retires its job, but is not
    one of the head's attempts. A failure is one, and retires its job only
    after ``MAX_FAILED_ATTEMPTS`` of them, since it says nothing about the job.
    """
    job_ids: set[int] = set()
    job_keys: set[str] = set()
    failed_ids: Counter[int] = Counter()
    failed_keys: Counter[str] = Counter()
    attempt_count = 0
    comments = retry_github_call(
        lambda: list(pr.get_issue_comments()),
        retries=2,
        description=f"list follow-up markers on #{pr.number}",
    )
    for comment in comments:
        author = str(
            getattr(getattr(comment, "user", None), "login", "") or ""
        )
        if author != bot_login:
            continue
        matches = tuple(
            match
            for match in _MARKER_RE.finditer(
                str(getattr(comment, "body", "") or "")
            )
            if match.group("head") == head_sha
        )
        if any(match.group("outcome") != ResultKind.REFUSED.value for match in matches):
            attempt_count += 1
        for match in matches:
            job, key = int(match.group("job")), match.group("key")
            if match.group("outcome") == ResultKind.FAILED.value:
                failed_ids[job] += 1
                if key:
                    failed_keys[key] += 1
                continue
            job_ids.add(job)
            if key:
                job_keys.add(key)
    job_ids.update(job for job, count in failed_ids.items() if count >= MAX_FAILED_ATTEMPTS)
    job_keys.update(key for key, count in failed_keys.items() if count >= MAX_FAILED_ATTEMPTS)
    return _HandledFailures(
        job_ids=frozenset(job_ids),
        job_keys=frozenset(job_keys),
        attempt_count=attempt_count,
    )


def _job_key(run: Any, job_name: str) -> str:
    """Identify one logical job across twin events for the same workflow/head."""
    normalized_name = " ".join(job_name.casefold().split())
    return hashlib.sha256(
        f"{workflow_identity(run)}\0{normalized_name}".encode()
    ).hexdigest()


def _markers(
    failure: Failure,
    run: Any,
    jobs: tuple[FailedJob, ...],
) -> list[str]:
    """Render terminal exact/logical markers consumed by ``_handled_failures``."""
    return [
        f"<!-- valkey-ci-agent:auto-ci-followup head={failure.head_sha} "
        f"run={failure.run_id} attempt={failure.run_attempt} job={job.id} "
        f"key={_job_key(run, job.name)} -->"
        for job in jobs
    ]
