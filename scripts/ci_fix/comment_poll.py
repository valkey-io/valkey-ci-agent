"""Comment-triggered entry point for the CI fix bot.

A maintainer comments ``@valkeyrie-ops fix <ci-link>`` on a valkey-io/valkey PR.
This scheduled poller finds that comment and dispatches the existing ``ci-fix``
workflow, which does the actual diagnose/verify/push. The poller is only the
trigger; it owns no fix logic.

Idempotency is a reaction marker on GitHub, not external state. The claim is
atomic: GitHub's create-reaction returns ``201`` when this call added the
reaction and ``200`` when it already existed, so only the run that observes
``201`` dispatches. Two overlapping ticks therefore cannot both fire.

Order per comment: parse, reject bots, confirm it is a PR, authorize the
verified comment author, skip if already marked, claim (atomic), dispatch. Each
step that cannot proceed skips without claiming, so a later tick can retry.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

from github import Auth, Github

from scripts.ci_fix.gate import ParsedCommand, is_authorized, parse_command
from scripts.common.github_client import retry_github_call

logger = logging.getLogger(__name__)

# Reaction used as the claim marker. ``eyes`` reads as "seen, working on it".
_CLAIM_REACTION = "eyes"

# Manual backfill must not page unbounded history on a typo. A week is well past
# any realistic outage while keeping the comment listing cheap.
_MAX_LOOKBACK_MINUTES = 7 * 24 * 60
_DEFAULT_LOOKBACK_MINUTES = 30

DispatchFn = Callable[[str, int, ParsedCommand, str], None]
"""(repo_full_name, pr_number, command, commenter) -> None."""


def poll_once(
    gh: Github,
    *,
    target_repo: str,
    org: str,
    team_slug: str,
    bot_login: str,
    lookback_minutes: int,
    dispatch: DispatchFn,
    claim: Callable[[Any], bool],
) -> int:
    """Scan recent PR comments once and dispatch a fix for each new command.

    Returns the number of comments dispatched. ``bot_login`` is the App's own
    login (``<app-slug>[bot]``) used to recognize our own claim reaction.
    ``claim`` performs the atomic reaction claim and returns True only when this
    run acquired it; ``dispatch`` triggers the ci-fix workflow. Both are injected
    so the orchestration is testable without real GitHub side effects.
    """
    repo = gh.get_repo(target_repo)
    since = time.time() - lookback_minutes * 60
    dispatched = 0

    for comment in _recent_comments(repo, since):
        try:
            if _process_comment(
                gh, repo, comment,
                target_repo=target_repo, org=org, team_slug=team_slug,
                bot_login=bot_login, dispatch=dispatch, claim=claim,
            ):
                dispatched += 1
        except Exception as exc:  # noqa: BLE001 - one bad comment must not abort the tick
            logger.warning("Skipping comment %s after error: %s", getattr(comment, "id", "?"), exc)

    return dispatched


def _process_comment(
    gh: Github,
    repo: Any,
    comment: Any,
    *,
    target_repo: str,
    org: str,
    team_slug: str,
    bot_login: str,
    dispatch: DispatchFn,
    claim: Callable[[Any], bool],
) -> bool:
    """Handle one comment; return True iff it was claimed and dispatched.

    Every guard that cannot proceed returns False without claiming, so a later
    tick can retry. The caller isolates this per comment, so an API error on one
    comment does not stop the rest of the tick.
    """
    command = parse_command(comment.body or "")
    if command is None:
        return False
    if _is_bot(comment):
        return False
    pr_number = _pull_request_number(repo, comment)
    if pr_number is None:
        return False
    commenter = _login(comment)
    if not is_authorized(gh, org, team_slug, commenter):
        logger.info("Skipping comment %s from unauthorized %s", comment.id, commenter)
        return False
    if _already_claimed(comment, bot_login):
        return False
    if not claim(comment):
        # Another concurrent tick won the claim, or the claim call failed.
        return False
    dispatch(target_repo, pr_number, command, commenter)
    logger.info("Dispatched ci-fix for %s#%d (commenter %s)", target_repo, pr_number, commenter)
    return True


def _recent_comments(repo: Any, since_epoch: float) -> list[Any]:
    """List repository issue comments updated since ``since_epoch``.

    Uses the issue-comments listing (not issue search, which returns issues, not
    comment objects). Covers comments on both issues and PRs; the caller filters
    to PRs.
    """
    since = _from_epoch(since_epoch)
    return list(
        retry_github_call(
            lambda: repo.get_issues_comments(sort="updated", direction="desc", since=since),
            retries=3,
            description=f"list issue comments for {repo.full_name}",
        )
    )


def _pull_request_number(repo: Any, comment: Any) -> int | None:
    """Return the PR number this comment belongs to, or None if it is an issue.

    An issue comment carries the parent issue URL (``.../issues/<n>``). We fetch
    that issue from the known repo and require a ``pull_request`` field: the
    ci-fix engine only acts on PRs, so an issue comment must never be claimed.
    The repo is passed in because a listed ``IssueComment`` does not expose its
    repository.
    """
    number = _issue_number_from_url(getattr(comment, "issue_url", "") or "")
    if number is None:
        return None
    issue = retry_github_call(
        lambda: repo.get_issue(number),
        retries=3,
        description=f"get issue {number} for comment {comment.id}",
    )
    return number if getattr(issue, "pull_request", None) is not None else None


def _already_claimed(comment: Any, bot_login: str) -> bool:
    """True if the bot's own App identity already reacted to this comment."""
    reactions = retry_github_call(
        lambda: comment.get_reactions(),
        retries=2,
        description=f"read reactions on comment {comment.id}",
    )
    return any(r.content == _CLAIM_REACTION and r.user.login == bot_login for r in reactions)


def _issue_number_from_url(url: str) -> int | None:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _is_bot(comment: Any) -> bool:
    return getattr(getattr(comment, "user", None), "type", "") == "Bot"


def _login(comment: Any) -> str:
    return getattr(getattr(comment, "user", None), "login", "") or ""


def _from_epoch(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def claim_via_status(comment: Any) -> bool:
    """Atomically claim ``comment`` by creating the reaction, return True on win.

    The win condition is the raw HTTP status: ``201`` means this call created the
    reaction (we own the claim), ``200`` means it already existed (another tick
    owns it). PyGithub's ``create_reaction`` hides this, so we issue the request
    through the requester, which returns the status. The reactions endpoint is
    derived from the comment's own API URL (``comment.url``), since a listed
    comment does not expose its repository. A failed call is treated as "not
    claimed" so a later tick can retry.
    """
    requester = comment._requester  # noqa: SLF001 - the only status-exposing path
    url = f"{comment.url}/reactions"
    try:
        status, _headers, _data = retry_github_call(
            lambda: requester.requestJson("POST", url, input={"content": _CLAIM_REACTION}),
            retries=2,
            description=f"claim reaction on comment {comment.id}",
        )
    except Exception as exc:  # noqa: BLE001 - a failed claim is a clean skip
        logger.warning("Claim failed for comment %s: %s", comment.id, exc)
        return False
    return status == 201


def dispatch_ci_fix(
    gh: Github,
    *,
    agent_repo: str,
    workflow: str,
    ref: str,
) -> DispatchFn:
    """Build a dispatcher that triggers the ci-fix workflow on the agent repo."""

    def _dispatch(repo_full_name: str, pr_number: int, command: ParsedCommand, commenter: str) -> None:
        run_url = (
            f"https://github.com/{command.run_owner}/{command.run_repo}"
            f"/actions/runs/{command.run_id}"
        )
        inputs = {
            "repo": repo_full_name,
            "pr": str(pr_number),
            "run_url": run_url,
            "hint": command.hint,
            "commenter": commenter,
        }
        wf = gh.get_repo(agent_repo).get_workflow(workflow)
        retry_github_call(
            lambda: wf.create_dispatch(ref, inputs),
            retries=2,
            description=f"dispatch {workflow} for {repo_full_name}#{pr_number}",
        )

    return _dispatch


def _lookback_minutes() -> int:
    raw = os.environ.get("CI_FIX_POLL_LOOKBACK_MINUTES", "")
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_LOOKBACK_MINUTES
    return max(1, min(value, _MAX_LOOKBACK_MINUTES))


def _bot_login() -> str:
    """The login whose claim reaction the poller recognizes as its own.

    Production derives it from the App slug (``<slug>[bot]``). An explicit
    ``CI_FIX_POLL_BOT_LOGIN`` override exists for environments where the actor
    is not an App, such as fork testing with a personal access token.
    """
    override = os.environ.get("CI_FIX_POLL_BOT_LOGIN", "").strip()
    if override:
        return override
    return f"{os.environ['CI_FIX_POLL_APP_SLUG']}[bot]"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    token = os.environ["CI_FIX_POLL_TOKEN"]
    target_repo = os.environ.get("CI_FIX_POLL_TARGET_REPO", "valkey-io/valkey")
    agent_repo = os.environ.get("CI_FIX_POLL_AGENT_REPO", "valkey-io/valkey-ci-agent")
    workflow = os.environ.get("CI_FIX_POLL_WORKFLOW", "ci-fix.yml")
    ref = os.environ.get("CI_FIX_POLL_REF", "main")
    org = os.environ.get("CI_FIX_AUTH_ORG", "valkey-io")
    team_slug = os.environ.get("CI_FIX_AUTH_TEAM", "contributors")
    # The App slug (from the token-mint action) identifies our own claim
    # reaction. The installation token cannot reliably call GET /user, so the
    # bot login is derived from the slug rather than looked up.
    bot_login = _bot_login()

    gh = Github(auth=Auth.Token(token))
    dispatched = poll_once(
        gh,
        target_repo=target_repo,
        org=org,
        team_slug=team_slug,
        bot_login=bot_login,
        lookback_minutes=_lookback_minutes(),
        dispatch=dispatch_ci_fix(gh, agent_repo=agent_repo, workflow=workflow, ref=ref),
        claim=claim_via_status,
    )
    logger.info("CI fix comment poll dispatched %d fix(es)", dispatched)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
