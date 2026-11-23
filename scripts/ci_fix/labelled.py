"""Make the change an issue asks for, when a committer labels it.

A committer puts ``needs-agent-fix`` on an issue of the target repository. The
label is the queue: an hourly dispatcher starts one run for each labelled
issue (:mod:`scripts.ci_fix.issue_dispatch`), and this module checks the
request, plans it, and reports the result on the issue. The change is made
like every other fix (:mod:`scripts.ci_fix.run`), from a test written first
(:func:`scripts.ci_fix.engine.prepare_change`).

Before anything runs, the labelling itself is checked. Someone with triage
access can apply a label, so the person who applied it must have write
access. An issue that is assigned, or that an open pull request says it
fixes, is somebody's already. And the text has to be the one the committer
labelled: an issue edited since is labelled again. A request that fails one
of these loses its label, with a comment that says why.

The result is one comment on the issue: the change as a patch, or the draft
pull request that carries it when ``AGENT_FIX_PUBLISH`` is ``draft-pr``, or
why there is none, with the questions that are open and the failing test when
there is one. The label then comes off, so applying it again asks again.
Nothing else is kept: what was asked and what was answered are the label
events and the bot's comments on the issue.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import islice
from typing import Any

from github.GithubException import GithubException

from scripts.ci_fix.comment import render_change
from scripts.ci_fix.issue_state import AGENT_LABEL, labels_of, request_rejection
from scripts.ci_fix.models import (
    MAX_FAILED_ATTEMPTS,
    Failure,
    FixPlan,
    FixPolicy,
    FixResult,
    FixTarget,
    FixTask,
    FrontDoor,
    IssuePublication,
    IssueRequest,
    ResultKind,
    TargetKind,
)
from scripts.ci_fix.policy import CHANGE_BRANCH_PREFIX, push_repo
from scripts.ci_fix.publish import reconcile_draft_pr
from scripts.common.git_clone import is_full_sha
from scripts.common.github_client import retry_github_call
from scripts.common.text_utils import compact_text

logger = logging.getLogger(__name__)

# How a labelled issue's change is published (models.IssuePublication): a
# repository variable, so that draft PRs are turned on without a change here.
PUBLICATION_ENV = "AGENT_FIX_PUBLISH"
# The most of the bot's pull requests for labelled issues that are open at once.
MAX_OPEN_PRS = 5
# This many of them closed without merging, in a row, and no more are opened
# until someone has looked into why and said so: the variable below, set to
# the number of the newest pull request that was looked at.
PAUSE_AFTER_CLOSED = 3
REVIEWED_THROUGH_ENV = "AGENT_FIX_REVIEWED_THROUGH"
# The CI job of the target repository a change is made in: Valkey's pull
# request job that builds with -Werror and runs the main test suite. Its
# setup and build are replayed, so the change is built the way CI builds it.
_HOST_WORKFLOW = "ci"
_HOST_JOB = "test-ubuntu-latest"
# What the collaborator permission API calls write access. It reports the
# maintain role as write, and triage, which can apply labels, as read.
_WRITE_ACCESS = frozenset({"admin", "write"})
_MARKER = "<!-- valkey-ci-agent:agent-fix label={label} outcome={outcome} -->"
_GAVE_UP = "No change was prepared: {attempts} attempts stopped with an error. Applying the label again starts another."
_MARKER_RE = re.compile(r"<!-- valkey-ci-agent:agent-fix label=(?P<label>\d+) outcome=(?P<outcome>[a-z]+) -->")
_BRANCH_RE = re.compile(rf"^{re.escape(CHANGE_BRANCH_PREFIX)}issue-(?P<issue>\d+)-(?P<label>\d+)$")
_BUG_TITLE_RE = re.compile(r"^\s*\[(?:BUG|CRASH)\]", re.IGNORECASE)
_CRASH_TITLE_RE = re.compile(r"^\s*\[CRASH\]", re.IGNORECASE)
_MAX_BODY_CHARS = 20_000
_MAX_COMMENT_CHARS = 2_000
_MAX_COMMENTS = 10
_SCANNED_COMMENTS = 100
# Pull requests into the default branch looked through for the bot's own.
_SCANNED_OPEN_PULLS = 2_000
_SCANNED_CLOSED_PULLS = 300

_LAST_EDIT_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { issue(number: $number) { lastEditedAt } }
}
"""


@dataclass(frozen=True)
class Labelling:
    """One application of the label to an issue: its event, who applied it, and when."""

    event: int
    actor: str
    at: datetime


def labelling(issue: Any) -> Labelling | None:
    """The labelling that stands on ``issue``: the newest time the label was applied, if it is still there."""
    if AGENT_LABEL not in labels_of(issue):
        return None
    events = retry_github_call(
        lambda: list(issue.get_events()), retries=2, description=f"read issue #{issue.number} events",
    )
    applied = [
        event for event in events
        if str(getattr(event, "event", "") or "") == "labeled"
        and str(getattr(getattr(event, "label", None), "name", "") or "") == AGENT_LABEL
    ]
    if not applied:
        return None
    newest = max(applied, key=lambda event: _utc(event.created_at))
    return Labelling(
        event=int(newest.id), actor=str(getattr(getattr(newest, "actor", None), "login", "") or ""),
        at=_utc(newest.created_at),
    )


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def has_write_access(repo: Any, login: str) -> bool:
    """Whether ``login`` is a committer: someone who can push to ``repo``."""
    if not login:
        return False
    try:
        permission = retry_github_call(
            lambda: repo.get_collaborator_permission(login), retries=2, description=f"read {login}'s permission",
        )
    except GithubException as exc:
        if exc.status == 404:
            return False
        raise
    return str(permission) in _WRITE_ACCESS


def answered(issue: Any, label_event: int, bot_login: str) -> bool:
    """Whether the bot has reported on this labelling of ``issue``: a change, a refusal, or its last failure."""
    comments = retry_github_call(
        lambda: list(issue.get_comments()), retries=2, description=f"read issue #{issue.number} comments",
    )
    return any(
        int(match.group("label")) == label_event
        for comment in comments
        if str(getattr(getattr(comment, "user", None), "login", "") or "") == bot_login
        for match in _MARKER_RE.finditer(str(getattr(comment, "body", "") or ""))
    )


def is_crash_report(title: str) -> bool:
    """Whether an issue reports the server crashing: its test may then fail by a crash.

    By its title, as Valkey's crash report template writes it, which the
    committer who labelled the issue saw. Not by its text: a crash log
    pasted into it, or hidden in a comment of it, would turn off the rule
    that stops the run at a crash nobody reported.
    """
    return bool(_CRASH_TITLE_RE.match(title))


def is_bug(issue: Any) -> bool:
    """Whether ``issue`` reports a bug, which is worked on before a feature request."""
    return "bug" in {label.lower() for label in labels_of(issue)} or bool(_BUG_TITLE_RE.match(str(getattr(issue, "title", "") or "")))


def open_requests(repo: Any, bot_login: str, *, limit: int) -> list[tuple[Any, Labelling]]:
    """The labelled issues that wait for an answer, bugs first, with the labelling each run answers."""
    issues = retry_github_call(
        # islice, not a slice: PyGithub's slice raises IndexError when a page comes back short.
        lambda: list(islice(repo.get_issues(state="open", labels=[AGENT_LABEL]), limit)),
        retries=2,
        description="list labelled issues",
    )
    waiting = []
    for issue in issues:
        if getattr(issue, "pull_request", None) is not None:
            continue  # the issues API also returns pull requests
        applied = labelling(issue)
        if applied is not None and not answered(issue, applied.event, bot_login):
            waiting.append((issue, applied))
    return sorted(waiting, key=lambda item: not is_bug(item[0]))


def publication() -> IssuePublication:
    """How a labelled issue's change is published, from the environment; a comment unless told otherwise."""
    return IssuePublication.from_env(PUBLICATION_ENV)


def hold_reason(repo: Any, bot_login: str) -> str:
    """Why no more pull requests are opened for labelled issues now, or "".

    At most ``MAX_OPEN_PRS`` are open at once: review time is what the
    project is short of. And when the last ``PAUSE_AFTER_CLOSED`` were closed
    without merging, the bot is producing changes nobody wants, and stops
    until one merges, or someone has looked into why and sets
    ``REVIEWED_THROUGH_ENV`` to the newest of them: the pull requests up to
    that number no longer count.
    """
    pulls = change_pulls(repo, bot_login, state="open")
    if len(pulls) >= MAX_OPEN_PRS:
        return f"{len(pulls)} of the bot's pull requests for labelled issues are open (the most is {MAX_OPEN_PRS})"
    reviewed = os.environ.get(REVIEWED_THROUGH_ENV, "").strip()
    if reviewed and not reviewed.isdigit():
        raise ValueError(f"{REVIEWED_THROUGH_ENV} must be a pull request number, not {reviewed!r}")
    closed = sorted(
        (pull for pull in change_pulls(repo, bot_login, state="closed") if int(pull.number) > int(reviewed or 0)),
        key=lambda pull: pull.closed_at, reverse=True,
    )
    last = closed[:PAUSE_AFTER_CLOSED]
    if len(last) == PAUSE_AFTER_CLOSED and not any(getattr(pull, "merged_at", None) for pull in last):
        numbers = ", ".join(f"#{pull.number}" for pull in last)
        return (
            f"the last {PAUSE_AFTER_CLOSED} pull requests for labelled issues were closed without merging "
            f"({numbers}); set {REVIEWED_THROUGH_ENV} to the newest once someone has looked into why"
        )
    return ""


def change_pulls(repo: Any, bot_login: str, *, state: str) -> list[Any]:
    """The bot's pull requests for labelled issues into the default branch, newest first.

    Every open one is found, however long it has been open. Of the closed
    ones, the most recently updated are enough to know the last few.
    """
    limit = _SCANNED_OPEN_PULLS if state == "open" else _SCANNED_CLOSED_PULLS
    pulls = retry_github_call(
        lambda: list(islice(
            repo.get_pulls(state=state, base=repo.default_branch, sort="updated", direction="desc"), limit,
        )),
        retries=2,
        description=f"list {state} pull requests",
    )
    return [pull for pull in pulls if is_change_pull(pull, bot_login)]


def is_change_pull(pull: Any, bot_login: str) -> bool:
    """Whether ``pull`` is the bot's pull request for a labelled issue."""
    return (
        str(getattr(getattr(pull, "user", None), "login", "") or "") == bot_login
        and _BRANCH_RE.match(str(getattr(getattr(pull, "head", None), "ref", "") or "")) is not None
    )


def plan_request(
    gh: Any,
    _artifact_client: Any,
    request: dict[str, Any],
    *,
    bot_login: str,
) -> FixPlan | dict[str, Any]:
    """The plan for one labelled issue (``{"repo", "issue", "attempt"}``), or the final result.

    A final result is a skip (not labelled now, or already answered), a
    request turned down with a comment, or an earlier attempt's draft PR
    recovered. With ``replay``, a commit, the issue is planned on that commit
    whatever its state and nothing is ever written: a closed issue replayed
    on the commit before its fix shows what the bot would have done.
    """
    repo_full_name = str(request.get("repo") or "")
    number = int(request.get("issue") or 0)
    replay = str(request.get("replay") or "")
    if number <= 0:
        raise ValueError("an explicit positive issue number is required")
    if replay and not is_full_sha(replay):
        raise ValueError(f"a replay names the full commit to make the change on, not {replay!r}")
    repo = retry_github_call(lambda: gh.get_repo(repo_full_name), retries=2, description=f"get {repo_full_name}")
    issue = retry_github_call(lambda: repo.get_issue(number), retries=2, description=f"get issue #{number}")
    skipped = {"repo": repo_full_name, "issue": number, "action": "skipped"}
    if getattr(issue, "pull_request", None) is not None:
        return {**skipped, "reason": f"#{number} is a pull request"}
    if replay:
        # The job's setup and build replay with the runner's privileges: only a commit the project merged.
        merged = retry_github_call(
            lambda: repo.compare(replay, repo.default_branch), retries=2, description=f"compare {replay[:12]}",
        )
        if str(getattr(merged, "status", "") or "") not in ("ahead", "identical"):
            raise ValueError(f"a replay is made on a commit of {repo.default_branch}, which {replay[:12]} is not")
        return FixPlan(front_door=FrontDoor.LABEL, task=_task(repo, issue, Labelling(0, "", _utc(issue.created_at)), replay))
    applied = labelling(issue)
    if applied is None or str(getattr(issue, "state", "") or "") != "open":
        return {**skipped, "reason": f"issue #{number} is not open with the `{AGENT_LABEL}` label"}
    if answered(issue, applied.event, bot_login):
        return {**skipped, "reason": "the bot already answered this labelling"}
    attempt = int(request.get("attempt") or 0)
    try:
        if publication() is IssuePublication.DRAFT_PR:
            # What an earlier attempt at this labelling got as far as: its pull request, whose answer on the
            # issue was lost, or its branch, pushed before the pull request could be opened. Before the checks
            # below, which would take the bot's own pull request for somebody else's.
            earlier = _task(repo, issue, applied, bot_login=bot_login, details=False)
            recovered = reconcile_draft_pr(gh, earlier)
            if recovered is not None:
                return report_result(gh, FixPlan(front_door=FrontDoor.LABEL, task=earlier, attempt=attempt), recovered)
        rejection = _intake_rejection(repo, issue, applied)
        if rejection:
            _answer(issue, applied.event, ResultKind.REFUSED, f"No change was prepared. {rejection}")
            return {"repo": repo_full_name, "issue": number, "action": ResultKind.REFUSED.value, "summary": rejection}
        return FixPlan(
            front_door=FrontDoor.LABEL, task=_task(repo, issue, applied, bot_login=bot_login), attempt=attempt,
        )
    except Exception:
        # The publish job does not run without a plan. The last attempt says so here, or nobody would.
        if attempt >= MAX_FAILED_ATTEMPTS:
            _answer(issue, applied.event, ResultKind.FAILED, _GAVE_UP.format(attempts=attempt))
        raise


def _intake_rejection(repo: Any, issue: Any, applied: Labelling) -> str:
    """Why this labelling is not acted on, or ""."""
    if not has_write_access(repo, applied.actor):
        return (
            f"The `{AGENT_LABEL}` label was applied by {applied.actor or 'someone'}, who does not have write "
            "access to this repository. A committer applies it."
        )
    title, body = str(getattr(issue, "title", "") or ""), str(getattr(issue, "body", "") or "")
    if len(body) > _MAX_BODY_CHARS:
        return f"The issue is longer than the {_MAX_BODY_CHARS:,} characters the bot reads."
    # Somebody's already (assigned, or an open pull request says it fixes it): checked again before publishing.
    taken = request_rejection(issue, IssueRequest(number=int(issue.number), title=title, body=body))
    if taken:
        return taken
    if edited_since(repo, issue, applied.at):
        return (
            "The issue was edited after it was labelled. A committer reads it as it is now and applies the "
            "label again."
        )
    return ""


def edited_since(repo: Any, issue: Any, moment: datetime) -> bool:
    """Whether the issue's title or text changed after ``moment``."""
    events = retry_github_call(
        lambda: list(issue.get_events()), retries=2, description=f"read issue #{issue.number} events",
    )
    moment = _utc(moment)
    if any(str(getattr(event, "event", "") or "") == "renamed" and _utc(event.created_at) > moment for event in events):
        return True
    owner, name = str(repo.full_name).split("/", 1)
    _headers, answer = retry_github_call(
        lambda: repo.requester.graphql_query(
            _LAST_EDIT_QUERY, {"owner": owner, "name": name, "number": int(issue.number)},
        ),
        retries=2,
        description=f"read when issue #{issue.number} was last edited",
    )
    # Read as it is: an answer without it must not pass for an issue nobody edited.
    edited = answer["data"]["repository"]["issue"]["lastEditedAt"]
    return bool(edited) and datetime.fromisoformat(str(edited).replace("Z", "+00:00")) > moment


def _task(
    repo: Any, issue: Any, applied: Labelling, replay: str = "", *, bot_login: str = "", details: bool = True,
) -> FixTask:
    """The change to make for ``issue``, on the default branch's tip or on the commit a replay names.

    Without ``details`` it names the issue and where its change goes, which
    is all that finding an earlier attempt's branch needs.
    """
    repo_full_name, number = str(repo.full_name), int(issue.number)
    branch = str(repo.default_branch)
    sha = replay or str(retry_github_call(
        lambda: repo.get_branch(branch), retries=2, description=f"get {repo_full_name}:{branch}",
    ).commit.sha)
    # A replayed issue's comments say how it was fixed.
    request = issue_request(
        repo, issue, applied.event, bot_login, comments=details and not replay, replay=bool(replay),
    )
    target = FixTarget(
        kind=TargetKind.DRAFT_PR, repo_full_name=push_repo() or repo_full_name, base_repo_full_name=repo_full_name,
        branch=f"{CHANGE_BRANCH_PREFIX}issue-{number}-{applied.event}", expected_sha=sha,
    )
    return change_task(repo_full_name, branch, sha, request, target)


def issue_request(
    repo: Any, issue: Any, label_event: int, bot_login: str, *, comments: bool = True, replay: bool = False,
) -> IssueRequest:
    """What the agent is given of ``issue``: its text, and with ``comments`` what committers said on it."""
    title = str(getattr(issue, "title", "") or "")
    return IssueRequest(
        number=int(issue.number), title=title, body=str(getattr(issue, "body", "") or "")[:_MAX_BODY_CHARS],
        label_event=label_event, crash_report=is_crash_report(title),
        comments=committer_comments(repo, issue, bot_login) if comments else (), replay=replay,
    )


def change_task(repo_full_name: str, branch: str, sha: str, request: IssueRequest, target: FixTarget) -> FixTask:
    """The task of making ``request``'s change on ``branch`` at ``sha``, in the CI job that hosts it."""
    return FixTask(
        failure=Failure(
            repo_full_name=repo_full_name, run_id=0, head_sha=sha, job_id=0, job_name=_HOST_JOB, base_branch=branch,
            description=f"Issue #{request.number}: {compact_text(request.title, 300)}",
            issue_number=request.number, workflow=_HOST_WORKFLOW,
        ),
        target=target,
        policy=FixPolicy.GENERAL,
        request=request,
    )


def committer_comments(repo: Any, issue: Any, bot_login: str) -> tuple[str, ...]:
    """What committers said on ``issue``, oldest first: the agent reads nobody else's comments.

    A feature's acceptance criteria may come from them. Anyone else's comment
    is a stranger's text under an issue a committer vouched for.
    """
    comments = retry_github_call(
        lambda: list(islice(issue.get_comments(), _SCANNED_COMMENTS)), retries=2,
        description=f"read issue #{issue.number} comments",
    )
    committers: dict[str, bool] = {}
    said = []
    for comment in comments:
        login = str(getattr(getattr(comment, "user", None), "login", "") or "")
        if not login or login == bot_login or login.endswith("[bot]"):
            continue
        if login not in committers:
            committers[login] = has_write_access(repo, login)
        if committers[login]:
            said.append(f"{login}: {compact_text(str(getattr(comment, 'body', '') or ''), _MAX_COMMENT_CHARS)}")
    return tuple(said[-_MAX_COMMENTS:])


def report_result(gh: Any, plan: FixPlan, result: FixResult) -> dict[str, Any]:
    """Comment the result on the issue and take its label off; return it as a result record.

    A failed attempt (a crash, the time running out) says nothing about the
    issue. It is left to the dispatcher to try again, and only the last one
    is reported. A replay writes nothing.
    """
    task, request = plan.task, plan.task.request
    if request is None:
        raise ValueError("a labelled issue's plan names its issue")
    record = {
        "repo": task.failure.repo_full_name, "issue": request.number, "action": result.kind.value,
        # A feature's results are counted apart from a bug's.
        "feature": bool(result.candidate and result.candidate.feature),
        "summary": result.summary, "branch": task.target.branch, "pr": result.pr_number, "pr_url": result.pr_url,
    }
    if request.replay:
        return record
    if result.kind is ResultKind.FAILED and plan.attempt < MAX_FAILED_ATTEMPTS:
        logger.info("Issue #%s: the attempt failed; nothing is posted: %s", request.number, result.summary)
        return record
    issue = retry_github_call(
        lambda: gh.get_repo(task.failure.repo_full_name).get_issue(request.number), retries=2,
        description=f"get issue #{request.number}",
    )
    if result.kind is not ResultKind.OPENED and (
        str(getattr(issue, "state", "") or "") != "open" or AGENT_LABEL not in labels_of(issue)
    ):
        # Taking the label off, or closing the issue, calls the request off. Whoever did may have found
        # the issue is not one to discuss in public: nothing the run found is posted.
        logger.info("Issue #%s was closed or lost its label during the run; nothing is posted", request.number)
        return record
    body = _GAVE_UP.format(attempts=plan.attempt) if result.kind is ResultKind.FAILED else render_change(result)
    _answer(issue, request.label_event, result.kind, body)
    return record


def _answer(issue: Any, label_event: int, kind: ResultKind, body: str) -> None:
    """Post the bot's answer to one labelling, and take the label off: the request is done."""
    marker = _MARKER.format(label=label_event, outcome=kind.value)
    retry_github_call(
        lambda: issue.create_comment(f"{body}\n\n{marker}"), retries=2,
        description=f"answer the labelling of issue #{issue.number}",
    )
    try:
        retry_github_call(
            lambda: issue.remove_from_labels(AGENT_LABEL), retries=2,
            description=f"remove the label from issue #{issue.number}",
        )
    except Exception as exc:  # noqa: BLE001 - the comment's marker already keeps the labelling from another run
        logger.warning("Could not remove `%s` from issue #%s: %s", AGENT_LABEL, issue.number, exc)
