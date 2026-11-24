"""Fix a release branch's failure on the default branch first.

A backport sweep's failure that no upstream change fixes, in a test the
default branch shares, is fixed there: after the release branch's fix, a
second fix job, the default branch's own CI job (or the bot's runner, when
it cannot host a fix), makes the same fix for it
(``engine.prepare_upstream``), and :func:`publish` opens it as a pull request
into the default branch. The sweep PR is told to backport it
once it merges. There is one such pull request a failing test, whichever
release branches fail it, and at most ``MAX_OPEN`` of the bot's pull requests
into the default branch are open at once. A fix for a test the default
branch does not have, or that the agent finds the default branch does not
need, is the release branch's own, and goes to its CI-sweep PR as before.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from itertools import islice
from typing import Any

from github.GithubException import GithubException

from scripts.agent_fix.comment import candidate_context, check_items, prose
from scripts.agent_fix.issue_state import open_linked_prs
from scripts.agent_fix.models import (
    FailedTest,
    FixCandidate,
    FixResult,
    FixTarget,
    FixTask,
    OpenFix,
    ResultKind,
    TargetKind,
)
from scripts.agent_fix.policy import FIX_BRANCH_PREFIX
from scripts.agent_fix.push import PushFailed, PushRefused, commit_message, push_candidate
from scripts.backport.pr_creator import create_pull_from_push_repo
from scripts.common.github_client import retry_github_call
from scripts.common.identity import APP_LOGIN

logger = logging.getLogger(__name__)

BRANCH_PREFIX = f"{FIX_BRANCH_PREFIX}upstream/"
# Where these branches were before the package was renamed. A pull request
# from there still answers for its test: open, merged or turned down.
_EARLIER_BRANCH_PREFIX = "agent/ci-fix/upstream/"
MAX_OPEN = 3
# Whose pull requests into the default branch count against ``MAX_OPEN``.
BOT_LOGIN = f"{APP_LOGIN}[bot]"

# Open pull requests the fix agent is shown, and how much of each diff.
_MAX_OPEN_FIXES = 5
_MAX_DIFF_CHARS = 6_000


def branch_for(tests: tuple[FailedTest, ...]) -> str:
    """The one branch for a fix of ``tests``' first test, whichever release branch failed it."""
    test = tests[0]
    return BRANCH_PREFIX + hashlib.sha256(f"{test.file}\0{test.name}".encode()).hexdigest()[:16]


def _author_query(bot_login: str) -> str:
    """The search qualifier for ``bot_login``'s pull requests: an App's is ``app/<slug>``."""
    return f"app/{bot_login.removesuffix('[bot]')}" if bot_login.endswith("[bot]") else bot_login


def open_count(gh: Any, repo_full_name: str, branch: str) -> int:
    """How many of the bot's pull requests into ``branch`` are open."""
    query = f"repo:{repo_full_name} is:pr is:open base:{branch} author:{_author_query(BOT_LOGIN)}"
    return int(retry_github_call(
        lambda: gh.search_issues(query).totalCount, retries=2, description=f"count the bot's PRs into {branch}",
    ))


def cap_reached(gh: Any, repo_full_name: str, branch: str) -> bool:
    return open_count(gh, repo_full_name, branch) >= MAX_OPEN


# --- the open pull requests a fix is shown ----------------------------------


def find_open_fixes(gh: Any, task: FixTask, tests: tuple[FailedTest, ...]) -> tuple[OpenFix, ...]:
    """Open pull requests into the default branch that name a failing test or close its detector issue.

    One of them may already fix the failure, and then the sweep waits for
    it. When they cannot be listed, the agent is shown none.
    """
    repo_full_name, branch = task.failure.repo_full_name, task.target.upstream_branch
    try:
        numbers: dict[int, None] = {}
        for test in tests:
            phrase = test.name or test.unit
            query = f'repo:{repo_full_name} is:pr is:open base:{branch} "{phrase}"'
            # A search is at most 256 characters, and a quote would end the phrase.
            if not phrase or '"' in phrase or len(query) > 256:
                continue
            numbers.update(dict.fromkeys(int(item.number) for item in retry_github_call(
                lambda: list(islice(gh.search_issues(query), _MAX_OPEN_FIXES)),
                retries=2, description=f"search open PRs naming {phrase}",
            )))
        repo = gh.get_repo(repo_full_name)
        for issue, parsed in _open_detector_issues(repo):
            if any(test in parsed.failed_tests for test in tests):
                numbers.update(dict.fromkeys(open_linked_prs(issue)))
        named = {test.file for test in tests}
        fixes = []
        for number in islice(numbers, _MAX_OPEN_FIXES):
            pull = retry_github_call(lambda: repo.get_pull(number), retries=2, description=f"get #{number}")
            if str(getattr(pull, "state", "") or "") != "open" or str(pull.base.ref) != branch:
                continue
            files = retry_github_call(lambda: list(pull.get_files()), retries=2, description=f"list #{number}'s files")
            chosen = [file for file in files if file.filename in named] or files
            diff = "\n".join(f"--- a/{file.filename}\n+++ b/{file.filename}\n{file.patch or ''}" for file in chosen)
            fixes.append(OpenFix(number=number, title=str(pull.title), diff=diff[:_MAX_DIFF_CHARS]))
        return tuple(fixes)
    except Exception as exc:  # noqa: BLE001 - without them the agent simply is not shown any
        logger.warning("Could not list the open pull requests into %s: %s", branch, exc)
        return ()


# --- publication --------------------------------------------------------------


def open_fix_result(task: FixTask, candidate: FixCandidate) -> FixResult:
    """A refusal because an open pull request into the default branch fixes it: that pull request, to backport."""
    return FixResult(
        kind=ResultKind.SUGGESTED,
        summary=f"#{candidate.open_fix}, open on {task.target.upstream_branch}, fixes it.",
        task=task, candidate=candidate, pr_number=candidate.open_fix, open_upstream=True,
    )


def publish(gh: Any, task: FixTask, candidate: FixCandidate, *, git_env: dict[str, str]) -> FixResult:
    """A sweep's own fix that was run on the default branch: a pull request into it.

    The bot's pull request for the same test is that fix, whether it is open
    or merged (it is to be backported); one that was closed unmerged turned
    the fix down.
    """
    failure, target, branch = task.failure, task.target, task.target.upstream_branch
    if candidate.upstream_refusal:
        return FixResult(kind=ResultKind.REFUSED, summary=candidate.upstream_refusal, task=task, candidate=candidate)
    run = candidate.upstream
    if run is None:
        raise ValueError("only a fix run on the default branch is opened into it")
    head = branch_for(run.tests)
    try:
        repo = gh.get_repo(failure.repo_full_name)
        owner = target.repo_full_name.split("/", 1)[0]
        for name in (head, _EARLIER_BRANCH_PREFIX + head.removeprefix(BRANCH_PREFIX)):
            earlier = _pull_from(repo, f"{owner}:{name}")
            if earlier is not None:
                return _earlier(task, candidate, earlier)
        if open_count(gh, failure.repo_full_name, branch) >= MAX_OPEN:
            return FixResult(kind=ResultKind.FAILED, task=task, candidate=candidate, summary=(
                f"{MAX_OPEN} of the bot's pull requests into {branch} are open: the fix waits for one of them."
            ))
        _delete_branch(repo, head)
    except Exception as exc:  # noqa: BLE001 - retry can recover GitHub API failures
        return FixResult(kind=ResultKind.FAILED, task=task, candidate=candidate,
                         summary=f"The pull request into {branch} could not be prepared: {exc}")
    pushed = FixTarget(
        kind=TargetKind.DRAFT_PR, repo_full_name=target.repo_full_name,
        base_repo_full_name=failure.repo_full_name, branch=head, expected_sha=run.sha,
    )
    try:
        commit_sha = push_candidate(on_upstream(candidate), pushed, git_env=git_env)
    except PushRefused as exc:
        return FixResult(kind=ResultKind.REFUSED, summary=str(exc), task=task, candidate=candidate)
    except PushFailed as exc:
        return FixResult(kind=ResultKind.FAILED, summary=str(exc), task=task, candidate=candidate)
    try:
        pull = retry_github_call(
            lambda: create_pull_from_push_repo(
                repo, base_repo=failure.repo_full_name, push_repo=target.repo_full_name,
                title=commit_message(on_upstream(candidate)).splitlines()[0], body=_body(task, candidate),
                head_branch=head, base_branch=branch, draft=False,
            ),
            retries=2, description=f"open the PR from {head}",
        )
    except Exception as exc:  # noqa: BLE001 - the next attempt pushes the branch again
        return FixResult(kind=ResultKind.FAILED, task=task, candidate=candidate, commit_sha=commit_sha,
                         summary=f"The fix is on {head} ({commit_sha[:12]}), but its PR could not be opened: {exc}")
    return FixResult(
        kind=ResultKind.OPENED, summary=f"Opened #{int(pull.number)} into {branch} with the fix.",
        task=task, candidate=candidate, commit_sha=commit_sha, pr_number=int(pull.number),
        pr_url=str(getattr(pull, "html_url", "") or ""), open_upstream=True,
    )


def _earlier(task: FixTask, candidate: FixCandidate, pull: Any) -> FixResult:
    """The result when the bot already has a pull request for this test."""
    number, branch = int(pull.number), task.target.upstream_branch
    if getattr(pull, "merged_at", None):
        return FixResult(kind=ResultKind.SUGGESTED, task=task, candidate=candidate, pr_number=number,
                         summary=f"#{number} fixes it on {branch}; it is to be backported to {task.failure.base_branch}.")
    if str(getattr(pull, "state", "") or "") != "open":
        return FixResult(kind=ResultKind.REFUSED, task=task, candidate=candidate, summary=(
            f"#{number}, the bot's fix for this test on {branch}, was closed without merging."
        ))
    return FixResult(kind=ResultKind.SUGGESTED, task=task, candidate=candidate, pr_number=number,
                     summary=f"#{number}, open on {branch}, fixes it.", open_upstream=True)


def _pull_from(repo: Any, head: str) -> Any | None:
    """The pull request from ``head`` (``owner:branch``), whatever its state, or None."""
    found = retry_github_call(
        lambda: list(islice(repo.get_pulls(state="all", head=head), 1)),
        retries=2, description=f"find the PR from {head}",
    )
    return found[0] if found else None


def _delete_branch(repo: Any, branch: str) -> None:
    """Delete ``branch``, an earlier attempt's push whose pull request was never opened."""
    try:
        ref = repo.get_git_ref(f"heads/{branch}")
    except GithubException as exc:
        if exc.status == 404:
            return
        raise
    logger.info("Deleting %s, pushed by an earlier attempt that opened no PR", branch)
    ref.delete()


def _open_detector_issues(repo: Any) -> list[tuple[Any, Any]]:
    # Imported here: the issues front door imports publish, which imports this module.
    from scripts.agent_fix.issues import open_detector_issues

    return open_detector_issues(repo, BOT_LOGIN, limit=100)


def on_upstream(candidate: FixCandidate) -> FixCandidate:
    """``candidate`` as it is on the default branch: its patch there, and what was checked there."""
    run = candidate.upstream
    if run is None:
        raise ValueError("the fix was not made for the default branch")
    return replace(
        candidate, patch=run.patch, changed_paths=run.changed_paths, title=run.title or candidate.title,
        reasoning=run.reasoning or candidate.reasoning, verification=run.verification,
        # The agent's notes are on the release branch's tree (an adaptation's session's are not kept).
        validation="",
    )


def _body(task: FixTask, candidate: FixCandidate) -> str:
    failure, run, branch = task.failure, candidate.upstream, task.target.upstream_branch
    assert run is not None
    release = failure.base_branch
    tests = " and ".join(f"`{test.name}`" if test.name else f"`{test.file}`" for test in run.tests)
    how = (
        f"The fix made on `{release}` does not apply here as it is, so this is the same fix written for this "
        "branch's code" if run.adapted else "The fix made there applies here as it is"
    )
    lines = [
        f"{tests} failed in `{prose(failure.job_name, 300)}` on `{release}` ([run]({failure.run_url})). "
        f"No change on `{branch}` fixes it. {how}: it is made here first, and backported to `{release}` once "
        "it merges.",
        "",
        *candidate_context(on_upstream(candidate), include_reasoning=True),
    ]
    if run.adapted:
        lines += [f"<details><summary>The fix made on <code>{release}</code></summary>", "",
                  "```diff", candidate.patch.rstrip("\n"), "```", "", "</details>", ""]
    lines += [
        f"<details><summary>What the bot checked on <code>{release}</code></summary>", "",
        *check_items(candidate.verification), "", "</details>", "",
    ]
    if candidate.validation:
        lines += [f"<details><summary>The agent's notes on <code>{release}</code></summary>", "",
                  prose(candidate.validation, 2_000), "", "</details>", ""]
    lines += [
        "---",
        "",
        "<sub>Prepared by the CI agent, which never merges. This PR's CI and a maintainer's review decide. "
        "Its commit has no DCO sign-off: whoever takes the change over signs it off.</sub>",
    ]
    return "\n".join(lines)

