"""Plan one maintainer-requested CI fix, and report its result on the pull request.

A maintainer comments ``@valkeyrie-ops fix [<run-or-job-url>] [hint]`` on a
pull request; the comment poller dispatches ``ci-fix.yml`` with the request.
The fix runs like every other (:mod:`scripts.ci_fix.run`). A request the gate
refuses, or cannot evaluate, is answered right away.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from scripts.ci_fix.comment import render_result
from scripts.ci_fix.gate import (
    GateError,
    GateRejection,
    ParsedCommand,
    build_fix_task,
    parse_run_url,
)
from scripts.ci_fix.models import FixPlan, FixResult, FrontDoor, ResultKind
from scripts.common.github_client import retry_github_call
from scripts.common.logging_utils import log_outcome

logger = logging.getLogger(__name__)

_AUTH_ORG = os.environ.get("CI_FIX_AUTH_ORG", "valkey-io")
_AUTH_TEAM = os.environ.get("CI_FIX_AUTH_TEAM", "contributors")
# ci-fix.yml's App token covers this repository only.
_SCOPE = "valkey-io/valkey"


def plan_request(
    gh: Any,
    _artifact_client: Any,
    request: dict[str, Any],
    *,
    bot_login: str,
) -> FixPlan | dict[str, Any]:
    """The fix plan for one command, or the gate's answer, already posted.

    ``request`` holds ``repo``, ``pr``, and optionally ``run_url``, ``hint``,
    ``commenter`` and ``comment_id``. The commenter is whoever started the
    run, unless the poller started it for them (the bot itself). Raises
    ``ValueError`` for a malformed request.
    """
    repo_full_name = str(request.get("repo") or "")
    pr_number = int(request.get("pr") or 0)
    actor = os.environ.get("GITHUB_ACTOR", "")
    commenter = str(request.get("commenter") or "") if actor in ("", bot_login) else actor
    comment_id = int(request.get("comment_id") or 0)
    if repo_full_name != _SCOPE:
        raise ValueError(f"ci-fix.yml is scoped to {_SCOPE} (got {repo_full_name!r})")
    if not (pr_number > 0 and commenter):
        raise ValueError("a CI-fix request needs a positive PR number and a commenter")
    hint = str(request.get("hint") or "").strip()
    command = ParsedCommand("", "", 0, hint)
    if request.get("run_url"):
        parsed = parse_run_url(str(request["run_url"]))
        if parsed is None:
            raise ValueError(f"not a run or job URL: {request['run_url']}")
        command = ParsedCommand(
            run_owner=parsed.run_owner, run_repo=parsed.run_repo, run_id=parsed.run_id, hint=hint,
            job_id=parsed.job_id, run_attempt=parsed.run_attempt,
        )
    task = build_fix_task(
        gh, command=command, pr_repo_full_name=repo_full_name, pr_number=pr_number,
        commenter=commenter, org=_AUTH_ORG, auth_team=_AUTH_TEAM,
    )
    if isinstance(task, GateError):
        kind, body = ResultKind.FAILED, f"CI-fix could not evaluate this request: {task.reason}"
    elif isinstance(task, GateRejection):
        kind, body = ResultKind.REFUSED, f"No fix prepared: {task.reason}"
    else:
        return FixPlan(front_door=FrontDoor.PR, task=task, comment_id=comment_id)
    return _report(gh, repo_full_name, pr_number, comment_id, body, kind, task.reason)


def report_result(gh: Any, plan: FixPlan, result: FixResult) -> dict[str, Any]:
    failure = plan.task.failure
    return _report(
        gh, failure.repo_full_name, failure.pr_number, plan.comment_id,
        render_result(result), result.kind, result.summary,
    )


def _report(
    gh: Any,
    repo_full_name: str,
    pr_number: int,
    comment_id: int,
    body: str,
    kind: ResultKind,
    summary: str,
) -> dict[str, Any]:
    """Comment ``body`` on the PR and react to the command; a comment that fails fails the run."""
    try:
        _post_comment(gh, repo_full_name, pr_number, body)
    finally:
        _react_outcome(gh, repo_full_name, comment_id, kind)
    level = {
        ResultKind.PUSHED: logging.INFO,
        ResultKind.OPENED: logging.INFO,
        ResultKind.FAILED: logging.ERROR,
    }.get(kind, logging.WARNING)
    log_outcome(
        logger, level, "CI fix for %s#%s %s: %s", repo_full_name, pr_number, kind.value, " ".join(summary.split()),
    )
    return {"repo": repo_full_name, "pr": pr_number, "action": kind.value, "summary": summary}


def _post_comment(gh: Any, repo_full_name: str, pr_number: int, body: str) -> None:
    retry_github_call(
        lambda: gh.get_repo(repo_full_name).get_issue(pr_number).create_comment(body),
        retries=3,
        description=f"comment on PR #{pr_number}",
    )


def _react_outcome(
    gh: Any,
    repo_full_name: str,
    comment_id: int,
    kind: ResultKind,
) -> None:
    if not comment_id:
        return
    content = "+1" if kind in (ResultKind.PUSHED, ResultKind.SUGGESTED, ResultKind.OPENED) else "-1"

    def react() -> None:
        requester = gh.get_repo(repo_full_name)._requester  # noqa: SLF001 - raw reactions endpoint
        requester.requestJsonAndCheck(
            "POST",
            f"/repos/{repo_full_name}/issues/comments/{comment_id}/reactions",
            input={"content": content},
        )

    try:
        retry_github_call(
            react,
            retries=2,
            description=f"react to comment {comment_id}",
        )
    except Exception:  # noqa: BLE001 - reaction is best effort
        logger.warning("Could not react to triggering comment %s", comment_id, exc_info=True)
