"""Keep the pull request the bot opened for a labelled issue ready for its reviewer.

Until a committer reviews it or pushes to it, a pull request the bot opened
(:mod:`scripts.ci_fix.labelled`) is the bot's to look after. Each run looks
at every open one, and plans a change for the first that needs one:

- Its change conflicts with the base branch: the change is made again on the
  branch's tip, test first, and replaces the pull request's head. That is the
  only time it is rebased, because every push runs CI again.
- A job of its CI failed, and the base branch's own CI does not fail it: one
  failure is fixed the way a backport PR's is
  (:mod:`scripts.backport.ci_followup`). A failure the base branch has
  without the change is not this pull request's to fix.

A draft whose CI is green is marked ready for review as the run passes it,
which needs no change.

Once a committer has reviewed it or pushed to it, the bot leaves it alone,
and acts only when asked (``@valkeyrie-ops fix``, ``@valkeyrie-ops address``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any

from scripts.backport import ci_followup
from scripts.backport.registry import load_registry
from scripts.ci_fix import labelled
from scripts.ci_fix.comment import render_change
from scripts.ci_fix.github_runs import FAILED_RUN_CONCLUSIONS, failed_jobs_for_run, newest_runs_for_head, workflow_file
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
from scripts.common.github_client import retry_github_call
from scripts.common.identity import BOT_EMAIL, BOT_NAME

logger = logging.getLogger(__name__)

_REBASE_MARKER = "<!-- valkey-ci-agent:agent-fix-rebase head={head} outcome={outcome} -->"
_REBASE_MARKER_RE = re.compile(r"<!-- valkey-ci-agent:agent-fix-rebase head=(?P<head>[0-9a-f]{40}) outcome=[a-z]+ -->")
_GREEN = frozenset({"success", "skipped", "neutral"})


def plan_request(
    gh: Any,
    _artifact_client: Any,
    request: dict[str, Any],
    *,
    bot_login: str,
    registry: str = "repos.yml",
    prior_plans: tuple[FixPlan, ...] = (),
) -> FixPlan | dict[str, Any]:
    """The plan for the first thing one of ``request``'s (``{"repo"}``) pull requests needs, or that none does."""
    repo_full_name = str(request["repo"])
    entry = load_registry(registry).get_repo(repo_full_name)
    repo = retry_github_call(lambda: gh.get_repo(repo_full_name), retries=2, description=f"get {repo_full_name}")
    branch = str(repo.default_branch)
    reasons: dict[str, str] = {}
    base_failures: frozenset[str] | None = None
    for listed in labelled.change_pulls(repo, bot_login, state="open"):
        # The pull request itself, not its list entry, says whether it merges cleanly.
        pull = retry_github_call(lambda: repo.get_pull(listed.number), retries=2, description=f"get #{listed.number}")
        name = f"#{pull.number}"
        if taken_over(repo, pull, bot_login):
            reasons[name] = "a committer has reviewed it or pushed to it"
            continue
        if pull.mergeable is False:
            plan, reasons[name] = _made_again(repo, pull, bot_login, prior_plans)
            if plan is not None:
                return plan
            continue
        if base_failures is None:
            base_failures = _base_failures(gh, repo, branch)
        target, reasons[name] = ci_followup.followup_for_pr(
            gh, pull, repo_entry=entry, target_branch=branch, bot_login=bot_login,
            prior_plans=prior_plans, left_alone=base_failures,
        )
        if target is not None:
            return _ci_fix(repo_full_name, branch, pull, target)
        if pull.draft and _ci_is_green(gh, repo_full_name, pull):
            retry_github_call(pull.mark_ready_for_review, retries=2, description=f"mark {name} ready for review")
            reasons[name] = "its CI is green: marked ready for review"
    reason = "; ".join(f"{name}: {why}" for name, why in reasons.items())
    return {"repo": repo_full_name, "action": "skipped", "reason": reason or "no open pull request"}


def taken_over(repo: Any, pull: Any, bot_login: str) -> bool:
    """Whether a committer has reviewed ``pull`` or anyone but the bot has pushed to it."""
    commits = retry_github_call(lambda: list(pull.get_commits()), retries=2, description=f"list #{pull.number}'s commits")
    for commit in commits:
        # GitHub maps the bot's commit identity to no account: its commits have no author login.
        author = str(getattr(getattr(commit, "author", None), "login", "") or "")
        email = str(getattr(getattr(getattr(commit, "commit", None), "author", None), "email", "") or "")
        is_bot = author in (BOT_NAME, bot_login) if author else email == BOT_EMAIL
        if not is_bot:
            return True
    reviews = retry_github_call(lambda: list(pull.get_reviews()), retries=2, description=f"list #{pull.number}'s reviews")
    is_committer = labelled.committer_check(repo)
    return any(is_committer(str(getattr(getattr(review, "user", None), "login", "") or "")) for review in reviews)


def _base_failures(gh: Any, repo: Any, branch: str) -> frozenset[str]:
    """The jobs that fail in the base branch's own CI, on its tip: failures the pull request did not cause."""
    try:
        sha = str(repo.get_branch(branch).commit.sha)
        names: set[str] = set()
        for run in newest_runs_for_head(gh, str(repo.full_name), branch=branch, head_sha=sha):
            if str(getattr(run, "conclusion", "") or "") in FAILED_RUN_CONCLUSIONS:
                names.update(job.name for job in failed_jobs_for_run(
                    gh, str(repo.full_name), int(run.id), run_attempt=int(getattr(run, "run_attempt", 1) or 1),
                ))
        return frozenset(names)
    except Exception as exc:  # noqa: BLE001 - the fix agent is still told to refuse a failure the change did not cause
        logger.warning("Could not read %s's own CI: %s", branch, exc)
        return frozenset()


def _ci_is_green(gh: Any, repo_full_name: str, pull: Any) -> bool:
    runs = newest_runs_for_head(gh, repo_full_name, branch=str(pull.head.ref), head_sha=str(pull.head.sha))
    return bool(runs) and all(
        str(getattr(run, "status", "") or "") == "completed" and str(getattr(run, "conclusion", "") or "") in _GREEN
        for run in runs
    )


def _ci_fix(repo_full_name: str, branch: str, pull: Any, target: ci_followup.FollowupTarget) -> FixPlan:
    """The plan to fix one failed job of the pull request's CI on its branch."""
    issue, _label = labelled.issue_of(pull)
    job = target.job
    failure = Failure(
        repo_full_name=repo_full_name,
        run_id=int(target.run.id),
        run_attempt=int(getattr(target.run, "run_attempt", 1) or 1),
        head_sha=target.head_sha,
        job_id=job.id,
        job_name=job.name,
        base_branch=branch,
        description=(
            f"the failure in job `{job.name}`. The bot opened this pull request for issue #{issue}. Fix the failure "
            f"only if this pull request's change causes it. If `{branch}` has it without the change, as a flaky "
            "or unrelated test does, choose `refuse` and say so: an unrelated fix does not belong here."
        ),
        pr_number=int(pull.number),
        workflow=workflow_file(target.run),
    )
    task = FixTask(
        failure=failure,
        target=FixTarget(
            kind=TargetKind.UPDATE_PR, repo_full_name=str(pull.head.repo.full_name), base_repo_full_name=repo_full_name,
            branch=target.head_branch, expected_sha=target.head_sha,
        ),
        policy=FixPolicy.GENERAL,
    )
    return FixPlan(front_door=FrontDoor.UPKEEP, task=task, markers=tuple(ci_followup.markers(failure, target.run, (job,))))


def _made_again(repo: Any, pull: Any, bot_login: str, prior_plans: tuple[FixPlan, ...]) -> tuple[FixPlan | None, str]:
    """The plan to make a conflicting pull request's change again on its base branch's tip, or why not."""
    head = str(pull.head.sha)
    comments = retry_github_call(
        lambda: list(pull.get_issue_comments()), retries=2, description=f"read #{pull.number}'s comments",
    )
    if any(match.group("head") == head for match in labelled.bot_markers(comments, bot_login, _REBASE_MARKER_RE)):
        return None, "it conflicts, and the bot already answered for this head"
    if sum(plan.task.target.replaces == head and not plan.never_started for plan in prior_plans) >= MAX_FAILED_ATTEMPTS:
        return None, f"it conflicts, and {MAX_FAILED_ATTEMPTS} attempts to make the change again failed"
    task = pull_request_task(repo, pull, bot_login)
    if task is None:
        return None, "it conflicts, and its issue is closed or was edited since it was labelled"
    branch = task.failure.base_branch
    sha = str(retry_github_call(lambda: repo.get_branch(branch), retries=2, description=f"get {branch}").commit.sha)
    assert task.request is not None
    task = labelled.change_task(
        str(repo.full_name), branch, sha, task.request, replace(task.target, expected_sha=sha, replaces=head),
    )
    return FixPlan(front_door=FrontDoor.UPKEEP, task=task), "it conflicts with its base branch"


def pull_request_task(repo: Any, pull: Any, bot_login: str, *, review: tuple[str, ...] = ()) -> FixTask | None:
    """The task of changing the bot's pull request ``pull`` for its issue, on its head, or None.

    None when its issue is closed, or was edited after the labelling the pull
    request answers: the text is then not the one a committer vouched for.
    """
    number, label_event = labelled.issue_of(pull)
    issue = retry_github_call(lambda: repo.get_issue(number), retries=2, description=f"get issue #{number}")
    if str(getattr(issue, "state", "") or "") != "open":
        return None
    events = retry_github_call(lambda: list(issue.get_events()), retries=2, description=f"read issue #{number} events")
    applied = next((event for event in events if int(event.id) == label_event), None)
    if applied is None or labelled.edited_since(repo, issue, applied.created_at):
        return None
    files = retry_github_call(lambda: list(pull.get_files()), retries=2, description=f"read #{pull.number}'s diff")
    request = replace(
        labelled.issue_request(repo, issue, label_event, bot_login),
        pr_number=int(pull.number), review=review,
        earlier="".join(f"--- a/{file.filename}\n+++ b/{file.filename}\n{file.patch or ''}\n" for file in files),
    )
    target = FixTarget(
        kind=TargetKind.UPDATE_PR, repo_full_name=str(pull.head.repo.full_name),
        base_repo_full_name=str(repo.full_name), branch=str(pull.head.ref), expected_sha=str(pull.head.sha),
    )
    return labelled.change_task(str(repo.full_name), str(pull.base.ref), str(pull.head.sha), request, target)


def taken_over_since(gh: Any, plan: FixPlan, bot_login: str) -> str:
    """Why the push ``plan`` prepared is no longer the bot's to make, or "".

    The run took hours. A committer who reviewed the pull request or pushed
    to it meanwhile has taken it over, and the bot acts only when asked.
    """
    failure = plan.task.failure
    repo = retry_github_call(lambda: gh.get_repo(failure.repo_full_name), retries=2, description="get the repository")
    pull = retry_github_call(lambda: repo.get_pull(failure.pr_number), retries=2, description=f"get #{failure.pr_number}")
    return "A committer has reviewed this pull request or pushed to it since." if taken_over(repo, pull, bot_login) else ""


def report_result(gh: Any, plan: FixPlan, result: FixResult) -> dict[str, Any]:
    """Report a CI fix as the follow-up does, and a change made again in a comment of its own."""
    task = plan.task
    if task.request is None:
        return ci_followup.report_result(gh, plan, result)
    failure = task.failure
    if result.kind is not ResultKind.FAILED:
        marker = _REBASE_MARKER.format(head=task.target.replaces, outcome=result.kind.value)
        retry_github_call(
            lambda: gh.get_repo(failure.repo_full_name).get_pull(failure.pr_number)
            .create_issue_comment(f"{render_change(result)}\n\n{marker}"),
            retries=3,
            description=f"report on #{failure.pr_number}",
        )
    return {
        "repo": failure.repo_full_name, "pr": failure.pr_number, "action": result.kind.value,
        "summary": result.summary, "commit": result.commit_sha,
    }
