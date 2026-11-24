"""Poll automated release PRs and dispatch one batch per PR.

Run with ``--repo`` and ``--pr`` it watches one release PR (the per-PR watcher
workflow), and with ``--start`` or ``--stop`` it starts or cancels that PR's
watcher. ``--restart-missing`` starts a watcher for every open release PR
that has none. Without any of these it scans every open PR of every
supported repository once.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from github import Auth, Github

from scripts.agent_fix.gate import is_authorized
from scripts.backport.sweep_graphql import GitHubGraphQLClient
from scripts.common.github_client import retry_github_call
from scripts.common.logging_utils import configure_logging
from scripts.common.polling import env_int, env_seconds, run_poll_loop
from scripts.release_notes.review import (
    REPOSITORIES,
    BatchMarker,
    ReleasePR,
    ReviewRefused,
    list_review_threads,
    parse_status,
    reconcile_review_targets,
    review_batch_id,
    review_comment_sha256,
    review_payload_json,
    review_targets,
    selected_reviews,
    status_body,
    validate_release_pr,
)

logger = logging.getLogger(__name__)

_MAX_LOOP_SECONDS = 55 * 60
_ADDRESSING_LEASE = timedelta(minutes=90)
DispatchFn = Callable[[ReleasePR, int], None]

AGENT_REPO = "valkey-io/valkey-ci-agent"
REVIEW_WORKFLOW = "release-notes-review.yml"
WATCH_WORKFLOW = "release-notes-review-watch.yml"
# A watcher chain stops queueing itself once its PR is this old. Release PRs
# live hours to a couple of days; this bounds the runner a forgotten one
# would otherwise hold around the clock. Start the watcher by hand to go on.
DEFAULT_MAX_AGE_HOURS = 72
# A run waiting on the watcher's per-PR concurrency group, or for a runner.
_WAITING = frozenset({"queued", "pending", "waiting", "requested"})
_LIVE = _WAITING | {"in_progress"}


def _authorizer(
    gh: Github,
    authorize: Callable[[str], bool] | None,
    org: str,
    team_slug: str,
) -> Callable[[str], bool]:
    auth_cache: dict[str, bool] = {}

    def authorized(login: str) -> bool:
        if login not in auth_cache:
            auth_cache[login] = (
                authorize(login)
                if authorize is not None
                else is_authorized(gh, org, team_slug, login)
            )
        return auth_cache[login]

    return authorized


def load_release_pr(gh: Github, repo_full_name: str, number: int) -> tuple[Any, ReleasePR] | None:
    """The PR and its validated identity, or None once it is merged or closed.

    Any other reason the PR is not a release PR raises :class:`ReviewRefused`,
    because a watcher pointed at the wrong PR should fail where someone sees
    it rather than poll for an hour.
    """
    repo = retry_github_call(
        lambda: gh.get_repo(repo_full_name),
        retries=3,
        description=f"get release-review repository {repo_full_name}",
    )
    pr = retry_github_call(
        lambda: repo.get_pull(number),
        retries=3,
        description=f"get {repo_full_name}#{number}",
    )
    if str(getattr(pr, "state", "")).lower() != "open":
        logger.info("%s#%s is %s; the watch ends", repo_full_name, number, getattr(pr, "state", "?"))
        return None
    return pr, validate_release_pr(pr, repo_full_name)


def poll_pr(
    gh: Github,
    gql: GitHubGraphQLClient,
    *,
    repo_full_name: str,
    number: int,
    bot_login: str,
    dispatch: DispatchFn,
    authorize: Callable[[str], bool] | None = None,
    org: str = "valkey-io",
    team_slug: str = "contributors",
) -> tuple[int, bool]:
    """Process one release PR; return (batches dispatched, PR still open)."""
    loaded = load_release_pr(gh, repo_full_name, number)
    if loaded is None:
        return 0, False
    pr, release = loaded
    dispatched = _process_pr(
        pr,
        release,
        gql,
        bot_login=bot_login,
        authorized=_authorizer(gh, authorize, org, team_slug),
        dispatch=dispatch,
    )
    return (1 if dispatched else 0), True


def poll_once(
    gh: Github,
    gql: GitHubGraphQLClient,
    *,
    repositories: tuple[str, ...],
    bot_login: str,
    dispatch: DispatchFn,
    authorize: Callable[[str], bool] | None = None,
    org: str = "valkey-io",
    team_slug: str = "contributors",
) -> int:
    """Scan all open PRs and dispatch every complete actionable batch."""

    authorized = _authorizer(gh, authorize, org, team_slug)
    dispatched = 0
    for repo_full_name, pr, release in _open_release_prs(gh, repositories):
        try:
            if _process_pr(
                pr,
                release,
                gql,
                bot_login=bot_login,
                authorized=authorized,
                dispatch=dispatch,
            ):
                dispatched += 1
        except Exception as exc:  # noqa: BLE001 - continue with sibling PRs
            logger.warning(
                "Could not process %s#%s: %s",
                repo_full_name,
                getattr(pr, "number", "?"),
                exc,
            )
    return dispatched


def _open_release_prs(gh: Github, repositories: tuple[str, ...]) -> list[tuple[str, Any, ReleasePR]]:
    """Every open release PR across *repositories*; an unreadable repository is skipped."""
    found: list[tuple[str, Any, ReleasePR]] = []
    for repo_full_name in repositories:
        try:
            repo = retry_github_call(
                lambda: gh.get_repo(repo_full_name),
                retries=3,
                description=f"get release-review repository {repo_full_name}",
            )
            pulls = retry_github_call(
                lambda: list(repo.get_pulls(state="open")),
                retries=3,
                description=f"list open PRs in {repo_full_name}",
            )
        except Exception as exc:  # noqa: BLE001 - continue with sibling repos
            logger.warning("Could not scan %s: %s", repo_full_name, exc)
            continue
        for pr in pulls:
            try:
                found.append((repo_full_name, pr, validate_release_pr(pr, repo_full_name)))
            except ReviewRefused as exc:
                logger.info("Skipping %s#%s: %s", repo_full_name, getattr(pr, "number", "?"), exc)
            except Exception as exc:  # noqa: BLE001 - continue with sibling PRs
                logger.warning("Could not read %s#%s: %s", repo_full_name, getattr(pr, "number", "?"), exc)
    return found


def _process_pr(
    pr: Any,
    release: ReleasePR,
    gql: GitHubGraphQLClient,
    *,
    bot_login: str,
    authorized: Callable[[str], bool],
    dispatch: DispatchFn,
) -> bool:
    threads = list_review_threads(gql, release.repo_full_name, release.number)
    reviews = selected_reviews(
        threads,
        release.notes_path,
        authorized,
    )
    comments = retry_github_call(
        lambda: list(pr.get_issue_comments()),
        retries=3,
        description=f"list status comments on {release.repo_full_name}#{release.number}",
    )

    reconciled = False
    for comment in comments:
        marker = parse_status(comment, bot_login)
        if (
            marker is None
            or not marker.commit_sha
            or marker.commit_sha != release.head_sha
            or marker.status not in {"addressing", "addressed"}
            or not marker.targets
        ):
            continue
        if marker.status == "addressing":
            recovered_body = status_body(
                "addressed",
                release.head_sha,
                marker.batch_id,
                marker.count,
                commit_sha=marker.commit_sha,
                repo_full_name=release.repo_full_name,
                targets=marker.targets,
            )

            def update_recovered_status() -> Any:
                return comment.edit(recovered_body)

            retry_github_call(
                update_recovered_status,
                retries=3,
                description="recover published release-review status",
            )
        if not _needs_reconciliation(marker, threads):
            continue
        _replied, _resolved, failures = reconcile_review_targets(
            pr,
            gql,
            targets=marker.targets,
            commit_sha=marker.commit_sha,
            bot_login=bot_login,
            repo_full_name=release.repo_full_name,
            load_threads=lambda: list_review_threads(
                gql,
                release.repo_full_name,
                release.number,
            ),
        )
        reconciled = True
        if failures:
            logger.warning(
                "Could not finish release-review bookkeeping for %s#%d: %s",
                release.repo_full_name,
                release.number,
                "; ".join(failures),
            )
            return False

    if reconciled:
        threads = list_review_threads(gql, release.repo_full_name, release.number)
        reviews = selected_reviews(threads, release.notes_path, authorized)
    if not reviews:
        return False
    review_payload_json(reviews)
    batch_id = review_batch_id(reviews)

    for comment in comments:
        marker = parse_status(comment, bot_login)
        if (
            marker is not None
            and marker.head_sha == release.head_sha
            and (
                (marker.status == "addressing" and _lease_is_active(comment))
                or (
                    marker.batch_id == batch_id
                    and marker.status in {"addressed", "refused"}
                )
            )
        ):
            return False

    status = retry_github_call(
        lambda: pr.create_issue_comment(
            status_body(
                "addressing",
                release.head_sha,
                batch_id,
                len(reviews),
                targets=review_targets(reviews),
            )
        ),
        retries=3,
        description=f"post status on {release.repo_full_name}#{release.number}",
    )
    status_id = getattr(status, "id", 0)
    if isinstance(status_id, bool) or not isinstance(status_id, int) or status_id <= 0:
        raise RuntimeError("GitHub created a status comment without an id")
    try:
        dispatch(release, status_id)
    except Exception as exc:
        reason = str(exc)
        retry_github_call(
            lambda: status.edit(
                status_body(
                    "failed",
                    release.head_sha,
                    batch_id,
                    len(reviews),
                    reason=reason,
                    targets=review_targets(reviews),
                )
            ),
            retries=3,
            description="mark failed release-review dispatch",
        )
        raise
    return True


def _lease_is_active(comment: Any, *, now: datetime | None = None) -> bool:
    updated_at = getattr(comment, "updated_at", None)
    if not isinstance(updated_at, datetime):
        return True
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return current - updated_at <= _ADDRESSING_LEASE


def _needs_reconciliation(
    marker: BatchMarker,
    threads: tuple[Any, ...],
) -> bool:
    current = {thread.node_id: thread for thread in threads}
    for target in marker.targets:
        thread = current.get(target.node_id)
        latest = thread.latest_human_comment() if thread is not None else None
        if (
            thread is not None
            and not thread.resolved
            and latest is not None
            and latest.database_id == target.selected_comment_id
            and review_comment_sha256(latest) == target.selected_comment_sha256
        ):
            return True
    return False


def watch_run_title(repo_name: str, number: int) -> str:
    """The watcher workflow's run-name for one PR; the workflow must render the same."""
    return f"Watch {repo_name}#{number} reviews"


def chain_allowed(pr: Any, *, max_age_hours: int, now: datetime | None = None) -> bool:
    """Whether a watcher for *pr* may still queue its next hour (see DEFAULT_MAX_AGE_HOURS)."""
    created = getattr(pr, "created_at", None)
    if not isinstance(created, datetime):
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return current - created <= timedelta(hours=max_age_hours)


def _dispatch_workflow(
    agent_gh: Github,
    *,
    agent_repo: str,
    workflow: str,
    ref: str,
    inputs: dict[str, str],
    description: str,
) -> None:
    """Start *workflow* in the agent repository with *agent_gh*, a client that may only do that."""
    target = retry_github_call(
        lambda: agent_gh.get_repo(agent_repo).get_workflow(workflow),
        retries=3,
        description=f"get workflow {workflow}",
    )
    retry_github_call(
        lambda: target.create_dispatch(ref, inputs),
        retries=3,
        description=description,
    )


def dispatch_release_review(
    agent_gh: Github,
    *,
    agent_repo: str = AGENT_REPO,
    workflow: str = REVIEW_WORKFLOW,
    ref: str = "main",
) -> DispatchFn:
    def dispatch(release: ReleasePR, status_comment_id: int) -> None:
        _dispatch_workflow(
            agent_gh,
            agent_repo=agent_repo,
            workflow=workflow,
            ref=ref,
            inputs={
                "repo": release.repo_full_name.rsplit("/", 1)[-1],
                "pr": str(release.number),
                "head_sha": release.head_sha,
                "status_comment_id": str(status_comment_id),
            },
            description=f"dispatch release review for {release.repo_full_name}#{release.number}",
        )

    return dispatch


def live_watcher_runs(
    agent_gh: Github, repo_name: str, number: int, *, agent_repo: str = AGENT_REPO
) -> list[Any]:
    """Every waiting or running watcher run for this PR, newest first."""
    workflow = retry_github_call(
        lambda: agent_gh.get_repo(agent_repo).get_workflow(WATCH_WORKFLOW),
        retries=3,
        description=f"get workflow {WATCH_WORKFLOW}",
    )
    runs = retry_github_call(
        lambda: list(itertools.islice(workflow.get_runs(), 200)),
        retries=3,
        description="list watcher runs",
    )
    title = watch_run_title(repo_name, number)
    return [
        run
        for run in runs
        if (getattr(run, "display_title", "") or "") == title
        and getattr(run, "status", "") in _LIVE
    ]


def ensure_watcher(
    agent_gh: Github,
    repo_name: str,
    number: int,
    *,
    successor: bool,
    agent_repo: str = AGENT_REPO,
    ref: str = "main",
) -> bool:
    """Start a watcher for this PR unless one is already lined up; True when started.

    *agent_gh* needs only ``actions: write`` on the agent repository. The cut
    and the scheduled restart call this with ``successor=False``: a watcher
    that is running or waiting is enough. A running watcher calls it with
    ``successor=True`` to queue the next hour's run: that run waits on the
    per-PR concurrency group and takes over the moment this one ends, so the
    hour boundary has no gap, and only a run already waiting makes it
    redundant.
    """
    live = live_watcher_runs(agent_gh, repo_name, number, agent_repo=agent_repo)
    blocking = [run for run in live if not successor or getattr(run, "status", "") in _WAITING]
    if blocking:
        logger.info(
            "A watcher for %s#%d is already %s (run %s); not starting another",
            repo_name, number, getattr(blocking[0], "status", "?"), getattr(blocking[0], "id", "?"),
        )
        return False
    _dispatch_workflow(
        agent_gh,
        agent_repo=agent_repo,
        workflow=WATCH_WORKFLOW,
        ref=ref,
        inputs={"repo": repo_name, "pr": str(number)},
        description=f"start the review watcher for {repo_name}#{number}",
    )
    logger.info("Started the review watcher for %s#%d", repo_name, number)
    return True


def stop_watcher(agent_gh: Github, repo_name: str, number: int, *, agent_repo: str = AGENT_REPO) -> int:
    """Cancel every waiting or running watcher run for this PR; return how many."""
    live = live_watcher_runs(agent_gh, repo_name, number, agent_repo=agent_repo)
    for run in live:
        retry_github_call(
            run.cancel,
            retries=3,
            description=f"cancel watcher run {getattr(run, 'id', '?')}",
        )
        logger.info("Cancelled watcher run %s for %s#%d", getattr(run, "id", "?"), repo_name, number)
    if not live:
        logger.info("No watcher is running for %s#%d", repo_name, number)
    return len(live)


def restart_missing_watchers(
    gh: Github,
    *,
    agent_gh: Github | None = None,
    repositories: tuple[str, ...] = REPOSITORIES,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    agent_repo: str = AGENT_REPO,
) -> int:
    """Start a watcher for every open release PR that has none; return how many.

    The safety net under the per-PR chains: a chain that broke on an API
    error, or a PR that was already open when the watcher workflow landed,
    gets its watcher back on the next pass. A PR past the chain age is left
    alone, like a chain that reached it; start its watcher by hand. *gh*
    only reads public PRs; *agent_gh* (default *gh*) starts the workflow.
    """
    agent_gh = agent_gh or gh
    started = 0
    for repo_full_name, pr, release in _open_release_prs(gh, repositories):
        if not chain_allowed(pr, max_age_hours=max_age_hours):
            logger.info(
                "%s#%d is older than %d hours; not starting a watcher (start one by hand if needed)",
                repo_full_name, release.number, max_age_hours,
            )
            continue
        try:
            if ensure_watcher(
                agent_gh, repo_full_name.rsplit("/", 1)[-1], release.number,
                successor=False, agent_repo=agent_repo,
            ):
                started += 1
        except Exception as exc:  # noqa: BLE001 - continue with sibling PRs
            logger.warning("Could not start a watcher for %s#%d: %s", repo_full_name, release.number, exc)
    logger.info("Started %d watcher(s) for release PRs that had none", started)
    return started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("RELEASE_NOTES_REVIEW_REPO", ""),
                        help="Repository of the release PR (name or owner/name); needs --pr")
    parser.add_argument("--pr", type=int, default=0, help="Number of the release PR")
    parser.add_argument("--max-age-hours", type=int,
                        default=env_int("RELEASE_NOTES_REVIEW_MAX_AGE_HOURS", DEFAULT_MAX_AGE_HOURS, minimum=1),
                        help="Stop queueing a PR's next hour, and stop restarting it, once the PR is this old")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--start", action="store_true",
                      help="Start the PR's watcher workflow unless one is live, then exit")
    mode.add_argument("--stop", action="store_true",
                      help="Cancel the PR's watcher workflow runs, then exit")
    mode.add_argument("--restart-missing", action="store_true",
                      help="Start a watcher for every open release PR that has none, then exit")
    args = parser.parse_args(argv)

    configure_logging()
    # Two credentials: RELEASE_NOTES_REVIEW_TOKEN reads the release PR and
    # posts on it; RELEASE_NOTES_REVIEW_AGENT_TOKEN may only start and cancel
    # workflows in the agent repository. One token serves both roles when the
    # second is unset (the start, stop and restart jobs use the repository's
    # own token, which reads public PRs and dispatches at home).
    token = os.environ["RELEASE_NOTES_REVIEW_TOKEN"]
    gh = Github(auth=Auth.Token(token))
    agent_token = os.environ.get("RELEASE_NOTES_REVIEW_AGENT_TOKEN", "") or token
    agent_gh = gh if agent_token == token else Github(auth=Auth.Token(agent_token))
    interval = env_seconds("RELEASE_NOTES_REVIEW_POLL_INTERVAL_SECONDS", 0, minimum=0)
    duration = env_seconds(
        "RELEASE_NOTES_REVIEW_POLL_DURATION_SECONDS", 0, minimum=0, maximum=_MAX_LOOP_SECONDS
    )

    if args.restart_missing:
        if args.repo or args.pr:
            parser.error("--restart-missing covers every repository; drop --repo and --pr")
        restart_missing_watchers(gh, agent_gh=agent_gh, max_age_hours=args.max_age_hours)
        return 0

    if args.repo or args.pr or args.start or args.stop:
        if not (args.repo and args.pr > 0):
            parser.error("--repo and --pr go together")
        repo_full_name = args.repo if "/" in args.repo else f"valkey-io/{args.repo}"
        if repo_full_name not in REPOSITORIES:
            parser.error(f"unsupported repository: {args.repo}")
        repo_name = repo_full_name.rsplit("/", 1)[-1]
        if args.start:
            ensure_watcher(agent_gh, repo_name, args.pr, successor=False)
            return 0
        if args.stop:
            stop_watcher(agent_gh, repo_name, args.pr)
            return 0

        # Confirm the PR is an open release PR before anything else, so a
        # watcher pointed at the wrong PR fails here with the reason.
        try:
            loaded = load_release_pr(gh, repo_full_name, args.pr)
        except ReviewRefused as exc:
            logger.error("%s#%d cannot be watched: %s", repo_full_name, args.pr, exc)
            return 1
        if loaded is None:
            return 0
        sustained = interval > 0 and duration > 0
        if sustained:
            # Queue the next hour's run before this one does any work: it
            # waits on the per-PR concurrency group and takes over the moment
            # this run ends, and a failure in this run cannot break the chain.
            if chain_allowed(loaded[0], max_age_hours=args.max_age_hours):
                ensure_watcher(agent_gh, repo_name, args.pr, successor=True)
            else:
                logger.warning(
                    "%s#%d is older than %d hours; the next hour is not queued. "
                    "Start the watcher by hand to keep watching.",
                    repo_full_name, args.pr, args.max_age_hours,
                )

        gql = GitHubGraphQLClient(token)
        bot_login = f"{os.environ['RELEASE_NOTES_REVIEW_APP_SLUG']}[bot]"
        dispatch = dispatch_release_review(agent_gh)
        state = {"open": True}

        def watch_once() -> int:
            dispatched, still_open = poll_pr(
                gh, gql, repo_full_name=repo_full_name, number=args.pr,
                bot_login=bot_login, dispatch=dispatch,
            )
            state["open"] = still_open
            return dispatched

        results = (
            run_poll_loop(
                watch_once,
                interval_seconds=interval,
                duration_seconds=duration,
                until=lambda _dispatched: not state["open"],
                logger=logger,
            )
            if sustained
            else [watch_once()]
        )
        logger.info(
            "%s#%d: dispatched %d release-review batch(es) across %d pass(es); %s",
            repo_full_name, args.pr, sum(results), len(results),
            "still open, the next watcher takes over" if state["open"] else "closed, the watch ends",
        )
        return 0

    gql = GitHubGraphQLClient(token)
    bot_login = f"{os.environ['RELEASE_NOTES_REVIEW_APP_SLUG']}[bot]"
    results = run_poll_loop(
        lambda: poll_once(
            gh,
            gql,
            repositories=REPOSITORIES,
            bot_login=bot_login,
            dispatch=dispatch_release_review(agent_gh),
        ),
        interval_seconds=interval,
        duration_seconds=duration,
        logger=logger,
    )
    logger.info(
        "Dispatched %d release-review batch(es) across %d iteration(s)",
        sum(results),
        len(results),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
