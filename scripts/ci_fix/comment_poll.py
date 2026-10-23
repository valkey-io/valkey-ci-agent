"""Comment-triggered entry point for the CI fix bot.

A maintainer comments ``@valkeyrie-ops fix [<ci-link>] [hint]`` on a
valkey-io/valkey PR. This scheduled poller finds that comment and
dispatches the ``ci-fix`` workflow, which does the actual diagnose/edit/publish. The
poller is only the trigger; it owns no fix logic.

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
from scripts.common.logging_utils import configure_logging, log_outcome
from scripts.common.polling import env_int, env_seconds, run_poll_loop

logger = logging.getLogger(__name__)

# Reaction used as the claim marker. ``eyes`` reads as "seen, working on it".
_CLAIM_REACTION = "eyes"

# Manual backfill must not page unbounded history on a typo. A week is well past
# any realistic outage while keeping the comment listing cheap.
_MAX_LOOKBACK_MINUTES = 7 * 24 * 60
# GitHub throttles scheduled workflows on a busy public repo: the cron asks for
# a tick every few minutes but runs land roughly hourly, and have been seen over
# two hours apart under load. The lookback must comfortably exceed that real
# worst-case gap, or a comment posted between two ticks falls out of the window
# and is never seen. The reaction marker prevents reprocessing, so a generous
# window only costs a slightly larger comment listing.
_DEFAULT_LOOKBACK_MINUTES = 180
# GitHub App installation tokens are short-lived. Sustained polling is capped
# below an hour so a job does not keep sleeping past token expiry.
_MAX_LOOP_SECONDS = 55 * 60

class ClaimedDispatchFailed(Exception):
    """Dispatch failed after this run claimed the comment.

    The claim reaction stays (an ambiguous failure may still have started a
    run, and retrying could double-dispatch), so later ticks skip the comment.
    Surfacing it fails the poll run instead of losing the command silently.
    """


DispatchFn = Callable[[str, int, ParsedCommand, str, int], None]
"""(repo_full_name, pr_number, command, commenter, comment_id) -> None."""


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
    errors = 0
    claimed_failures: list[str] = []

    comments = _recent_comments(repo, since)
    for comment in comments:
        try:
            if _process_comment(
                gh, repo, comment,
                target_repo=target_repo, org=org, team_slug=team_slug,
                bot_login=bot_login, dispatch=dispatch, claim=claim,
            ):
                dispatched += 1
        except ClaimedDispatchFailed as exc:
            claimed_failures.append(str(getattr(comment, "id", "?")))
            logger.error("%s", exc)
        except Exception as exc:  # noqa: BLE001 - one bad comment must not abort the tick
            # Nothing was claimed, so a later tick retries this comment.
            errors += 1
            logger.warning(
                "Skipping comment %s after error (not claimed; a later tick retries): %s",
                getattr(comment, "id", "?"), exc,
            )

    logger.info(
        "Scanned %d comment(s) on %s from the last %d minute(s): %d fix(es) dispatched%s%s",
        len(comments), target_repo, lookback_minutes, dispatched,
        f", {errors} will be retried next tick" if errors else "",
        f", {len(claimed_failures)} claimed but not dispatched" if claimed_failures else "",
    )
    if claimed_failures:
        raise RuntimeError(
            f"ci-fix dispatch failed for claimed comment(s) {', '.join(claimed_failures)}"
        )
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
    # Only comments carrying a fix command are logged: everything else on the
    # repository is noise for this poller.
    if _is_bot(comment):
        logger.debug("Skipping fix command in comment %s: posted by a bot", comment.id)
        return False
    pr_number = _pull_request_number(repo, comment)
    if pr_number is None:
        logger.info("Skipping fix command in comment %s: not on a pull request", comment.id)
        return False
    commenter = _login(comment)
    if not is_authorized(gh, org, team_slug, commenter):
        logger.info("Skipping comment %s from unauthorized %s (not in %s/%s)",
                    comment.id, commenter, org, team_slug)
        return False
    if _already_claimed(comment, bot_login):
        logger.debug("Skipping comment %s on %s#%d: already claimed", comment.id,
                     target_repo, pr_number)
        return False
    if not claim(comment):
        # Another concurrent tick won the claim, or the claim call failed.
        logger.info("Skipping comment %s on %s#%d: claim not acquired", comment.id,
                    target_repo, pr_number)
        return False
    try:
        dispatch(target_repo, pr_number, command, commenter, comment.id)
    except Exception as exc:
        raise ClaimedDispatchFailed(
            f"Dispatch failed for comment {comment.id} on {target_repo}#{pr_number} "
            f"(commenter {commenter}, run {command.run_id}) after it was claimed; "
            f"remove the {_CLAIM_REACTION!r} reaction from the comment to let the "
            f"poller retry: {exc}"
        ) from exc
    logger.info("Dispatched ci-fix for %s#%d (commenter %s, comment %s, run %s)",
                target_repo, pr_number, commenter, comment.id, command.run_id)
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

    def _dispatch(
        repo_full_name: str, pr_number: int, command: ParsedCommand, commenter: str,
        comment_id: int = 0,
    ) -> None:
        run_url = (
            f"https://github.com/{command.run_owner}/{command.run_repo}"
            f"/actions/runs/{command.run_id}"
            + (f"/attempts/{command.run_attempt}" if command.run_attempt else "")
            + (f"/job/{command.job_id}" if command.job_id else "")
            if command.run_id else ""
        )
        inputs = {
            "repo": repo_full_name,
            "pr": str(pr_number),
            "run_url": run_url,
            "hint": command.hint,
            "commenter": commenter,
        }
        if comment_id:
            inputs["comment_id"] = str(comment_id)
        wf = gh.get_repo(agent_repo).get_workflow(workflow)
        dispatched = retry_github_call(
            lambda: wf.create_dispatch(ref, inputs),
            retries=2,
            description=f"dispatch {workflow} for {repo_full_name}#{pr_number}",
        )
        if not dispatched:
            raise RuntimeError(f"{agent_repo}/{workflow}@{ref} rejected the dispatch")

    return _dispatch


def _lookback_minutes() -> int:
    return env_int(
        "CI_FIX_POLL_LOOKBACK_MINUTES",
        _DEFAULT_LOOKBACK_MINUTES,
        minimum=1,
        maximum=_MAX_LOOKBACK_MINUTES,
    )


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


def _poll_interval_seconds() -> int:
    return env_seconds("CI_FIX_POLL_INTERVAL_SECONDS", 0, minimum=0)


def _poll_duration_seconds() -> int:
    return env_seconds(
        "CI_FIX_POLL_DURATION_SECONDS",
        0,
        minimum=0,
        maximum=_MAX_LOOP_SECONDS,
    )


def main() -> int:
    configure_logging()

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

    interval = _poll_interval_seconds()
    duration = _poll_duration_seconds()
    logger.info(
        "CI fix comment poll on %s: dispatching %s@%s in %s for members of %s/%s "
        "(bot %s, lookback %dm, %s)",
        target_repo, workflow, ref, agent_repo, org, team_slug, bot_login, _lookback_minutes(),
        f"every {interval}s for {duration}s" if interval and duration else "one pass",
    )

    gh = Github(auth=Auth.Token(token))
    # Dispatch on the agent repository with its own token, so the target
    # repository's token never carries actions:write.
    dispatch_token = os.environ.get("CI_FIX_POLL_DISPATCH_TOKEN", "") or token
    dispatch = dispatch_ci_fix(Github(auth=Auth.Token(dispatch_token)), agent_repo=agent_repo,
                               workflow=workflow, ref=ref)

    def _poll() -> int:
        return poll_once(
            gh,
            target_repo=target_repo,
            org=org,
            team_slug=team_slug,
            bot_login=bot_login,
            lookback_minutes=_lookback_minutes(),
            dispatch=dispatch,
            claim=claim_via_status,
        )

    try:
        results = run_poll_loop(
            _poll,
            interval_seconds=interval,
            duration_seconds=duration,
            logger=logger,
        )
    except Exception as exc:
        log_outcome(logger, logging.ERROR, "CI fix comment poll failed: %s",
                    " ".join(str(exc).split())[:500])
        raise
    log_outcome(logger, logging.INFO, "CI fix comment poll: %d fix(es) dispatched across %d "
                "pass(es)", sum(results), len(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
