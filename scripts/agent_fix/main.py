"""Plan one maintainer-requested CI fix, and report its result on the pull request.

A maintainer comments ``@valkeyrie-ops fix [<run-or-job-url>] [hint]`` on a
pull request; the comment poller dispatches ``agent-fix.yml`` with the request.
The fix runs like every other (:mod:`scripts.agent_fix.run`). A request the gate
refuses is answered right away. A request it cannot evaluate stays in the
workflow logs.

On a pull request the bot opened for a labelled issue, a committer can also
comment ``@valkeyrie-ops address [hint]``: the unresolved review comments of
committers are applied in one commit, and every check the change passed
runs again (:func:`scripts.agent_fix.engine.prepare_review`).
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from typing import Any

from scripts.agent_fix import labelled, upkeep
from scripts.agent_fix.comment import render_result
from scripts.agent_fix.gate import (
    GateError,
    GateRejection,
    ParsedCommand,
    build_fix_task,
    parse_run_url,
)
from scripts.agent_fix.models import FixPlan, FixResult, FrontDoor, ResultKind
from scripts.agent_fix.policy import is_own_repo
from scripts.backport.sweep_graphql import GitHubGraphQLClient
from scripts.common.github_client import retry_github_call
from scripts.common.logging_utils import log_outcome
from scripts.common.text_utils import compact_text
from scripts.release_notes.review import list_review_threads

logger = logging.getLogger(__name__)

_AUTH_ORG = os.environ.get("AGENT_FIX_AUTH_ORG", "valkey-io")
_AUTH_TEAM = os.environ.get("AGENT_FIX_AUTH_TEAM", "contributors")
# agent-fix.yml's App token covers this repository only.
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
        raise ValueError(f"agent-fix.yml is scoped to {_SCOPE} (got {repo_full_name!r})")
    if not (pr_number > 0 and commenter):
        raise ValueError("an agent-fix request needs a positive PR number and a commenter")
    hint = str(request.get("hint") or "").strip()
    if str(request.get("command") or "fix") == "address":
        return _plan_review(gh, repo_full_name, pr_number, commenter, comment_id, hint, bot_login)
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
        kind, body = ResultKind.FAILED, ""
    elif isinstance(task, GateRejection):
        kind, body = ResultKind.REFUSED, f"No fix prepared: {task.reason}"
    else:
        return FixPlan(front_door=FrontDoor.PR, task=task, comment_id=comment_id)
    return _report(gh, repo_full_name, pr_number, comment_id, body, kind, task.reason)


# How much of a review is handed to the agent: its threads, and each comment.
_MAX_REVIEW_COMMENTS = 40
_MAX_REVIEW_COMMENT_CHARS = 2_000


def _plan_review(
    gh: Any, repo_full_name: str, pr_number: int, commenter: str, comment_id: int, hint: str, bot_login: str,
) -> FixPlan | dict[str, Any]:
    """The plan to apply a committer's review comments to the bot's own pull request, or the answer why not."""
    try:
        repo = retry_github_call(lambda: gh.get_repo(repo_full_name), retries=2, description=f"get {repo_full_name}")
        pull = retry_github_call(lambda: repo.get_pull(pr_number), retries=2, description=f"get PR #{pr_number}")
        task, reason = None, ""
        if not labelled.has_write_access(repo, commenter):
            reason = (
                f"`address` is a committer's command, and {commenter} does not have write access to this "
                "repository."
            )
        elif (
            str(getattr(pull, "state", "") or "") != "open" or not labelled.is_change_pull(pull, bot_login)
            or not is_own_repo(str(pull.head.repo.full_name), repo_full_name)
        ):
            reason = "`address` applies review comments to an open pull request the bot opened for a labelled issue."
        else:
            comments = review_comments(repo, pull, bot_login)
            if hint:
                comments += (f"{commenter}: {hint}",)
            if not comments:
                reason = "There is no unresolved review comment from a committer to apply."
            else:
                task = upkeep.pull_request_task(repo, pull, bot_login, review=comments)
                if task is None:
                    reason = "Its issue is closed, or was edited since it was labelled."
    except Exception as exc:  # noqa: BLE001 - transient reads stay in the workflow logs
        logger.exception("Planning the review of #%s failed", pr_number)
        return _report(gh, repo_full_name, pr_number, comment_id, "", ResultKind.FAILED, str(exc))
    if task is None:
        return _report(
            gh, repo_full_name, pr_number, comment_id, f"No change was pushed: {reason}", ResultKind.REFUSED, reason,
        )
    return FixPlan(
        front_door=FrontDoor.PR, task=replace(task, failure=replace(task.failure, hint=hint)), comment_id=comment_id,
    )


def review_comments(repo: Any, pull: Any, bot_login: str) -> tuple[str, ...]:
    """What committers ask for on ``pull``: their reviews' own text, and their comments in its unresolved threads."""
    is_committer = labelled.committer_check(repo)
    # A thread names the bot without the "[bot]" a review does.
    own = {bot_login, bot_login.removesuffix("[bot]")}

    def committer(login: str) -> bool:
        return login not in own and is_committer(login)

    said = []
    reviews = retry_github_call(
        lambda: list(pull.get_reviews()), retries=2, description=f"list #{pull.number}'s reviews",
    )
    for review in reviews:
        login = str(getattr(getattr(review, "user", None), "login", "") or "")
        body = str(getattr(review, "body", "") or "")
        if body.strip() and committer(login):
            said.append(f"{login}: {compact_text(body, _MAX_REVIEW_COMMENT_CHARS)}")
    threads = list_review_threads(
        GitHubGraphQLClient(os.environ.get("TARGET_TOKEN", "")), str(repo.full_name), int(pull.number),
    )
    for thread in threads:
        if thread.resolved or thread.outdated:
            continue
        where = f"`{thread.path}`" + (f" line {thread.line}" if thread.line else "")
        said += [
            f"{comment.author_login} on {where}: {compact_text(comment.body, _MAX_REVIEW_COMMENT_CHARS)}"
            for comment in thread.comments if committer(comment.author_login)
        ]
    return tuple(said[-_MAX_REVIEW_COMMENTS:])


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
    """Comment a fix or refusal on the PR, and react to the command either way.

    A failed attempt says nothing about the PR: the command gets a -1 reaction,
    and the reason stays in the workflow run.
    """
    try:
        if kind is not ResultKind.FAILED:
            _post_comment(gh, repo_full_name, pr_number, body)
    finally:
        _react_outcome(gh, repo_full_name, comment_id, kind)
    if kind is ResultKind.FAILED:
        level = logging.ERROR
    elif kind.successful:
        level = logging.INFO
    else:
        level = logging.WARNING
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
    content = "+1" if kind.successful else "-1"

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
