"""Authorize a CI-fix request and bind it to one failed job.

The gate keeps only the trust checks that matter operationally:

* the requester is an authorized maintainer;
* the Actions run belongs to the PR and tested its current head;
* the selected job actually failed; and
* only a bot-authored branch in the CI agent's namespace may be pushed.

Every other PR receives a patch suggestion instead.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from github.GithubException import GithubException

from scripts.ci_fix.github_runs import (
    FailedJob,
    failed_jobs_for_run,
    most_actionable_failure,
    newest_runs_for_head,
    select_job,
    workflow_file,
)
from scripts.ci_fix.models import (
    Failure,
    FixPolicy,
    FixTarget,
    FixTask,
    TargetKind,
)
from scripts.ci_fix.policy import can_update_branch, is_backport_branch
from scripts.common.github_client import retry_github_call
from scripts.common.identity import APP_LOGIN, BOT_LOGIN

logger = logging.getLogger(__name__)

_COMMAND_RE = re.compile(
    r"^\s*@(?:" + "|".join(re.escape(login) for login in (BOT_LOGIN, APP_LOGIN))
    + r")\s+fix\b[^\S\n]*(?P<rest>[^\n]*)",
    re.IGNORECASE,
)
_RUN_PATH_RE = re.compile(
    r"/(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+)/actions/runs/(?P<run_id>\d+)"
    r"(?:/attempts/(?P<run_attempt>\d+))?"
    r"(?:/jobs?/(?P<job_id>\d+))?/?",
)
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]\n]*\]\((?P<url>[^)\s]+)\)")
_ANGLE_LINK_RE = re.compile(r"<(?P<url>[^>\s]+)>")
_BARE_LINK_RE = re.compile(r"\S*(?:://|github\.com/)\S*", re.IGNORECASE)
_DEFAULT_AUTH_TEAM = "contributors"


@dataclass(frozen=True)
class ParsedCommand:
    run_owner: str
    run_repo: str
    run_id: int
    hint: str
    job_id: int = 0
    run_attempt: int = 0


@dataclass(frozen=True)
class GateRejection:
    reason: str


@dataclass(frozen=True)
class GateError:
    """A retryable failure while reading the state needed by the gate."""

    reason: str


def parse_command(body: str) -> ParsedCommand | None:
    """Parse ``@bot fix [run-or-job-url] [hint]`` from one comment line."""
    if not body:
        return None
    match = _COMMAND_RE.search(body)
    if match is None:
        return None
    links: list[str] = []

    def take(found: re.Match[str]) -> str:
        link = found.group("url") if "url" in found.groupdict() else found.group(0)
        # A bare link ends a sentence like any word: ``... /runs/1.``
        links.append(link.rstrip(".,;:!?)"))
        return " "

    rest = match.group("rest")
    for pattern in (_MARKDOWN_LINK_RE, _ANGLE_LINK_RE, _BARE_LINK_RE):
        rest = pattern.sub(take, rest)
    hint = " ".join(rest.split())
    if not links:
        return ParsedCommand("", "", 0, hint)
    if len(links) != 1:
        return None
    parsed = parse_run_url(links[0])
    return replace(parsed, hint=hint) if parsed is not None else None


def parse_run_url(run_url: str) -> ParsedCommand | None:
    """Parse one github.com Actions run or job URL."""
    url = (run_url or "").strip()
    if "://" not in url:
        url = f"https://{url}"
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in _GITHUB_HOSTS:
        return None
    if parts.username or parts.password or parts.port:
        return None
    match = _RUN_PATH_RE.fullmatch(parts.path)
    if match is None:
        return None
    return ParsedCommand(
        run_owner=match.group("owner"),
        run_repo=match.group("repo"),
        run_id=int(match.group("run_id")),
        hint="",
        job_id=int(match.group("job_id") or 0),
        run_attempt=int(match.group("run_attempt") or 0),
    )


def is_authorized(
    gh: Any,
    org: str,
    team_slug: str,
    username: str,
    *,
    retries: int = 2,
    raise_on_error: bool = False,
) -> bool:
    """Return True only if ``username`` is allowed to drive the bot.

    ``username`` must be a GitHub-verified principal (``github.actor`` for the
    current dispatch). A future comment trigger must pass ``comment.user.login``
    from the event payload, never a value forwarded through a dispatch input,
    which any opener of the wrapper could set.

    The primary source is active membership of ``org/team_slug``. Fails closed:
    any error reading membership (permission, network, missing team) returns
    False, and a ``pending`` invitation does not authorize.

    ``raise_on_error`` raises a read error other than "not a member" instead,
    for a caller that reports it as retryable.

    An explicit allowlist may be supplied via ``CI_FIX_AUTH_ALLOWLIST`` (a
    comma-separated list of logins). It is empty by default - in production the
    team membership check is the only path. It exists so the same gate can be
    exercised end-to-end in a fork environment where the production team is not
    readable, without weakening the default behavior.
    """
    if not username:
        return False
    if username in _auth_allowlist():
        logger.info("Authorizing %s via CI_FIX_AUTH_ALLOWLIST", username)
        return True
    try:
        team = retry_github_call(
            lambda: gh.get_organization(org).get_team_by_slug(team_slug),
            retries=retries, description=f"get team {org}/{team_slug}",
        )
        membership = retry_github_call(
            lambda: team.get_team_membership(username),
            retries=retries, description=f"team membership {username}",
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on any read error
        if raise_on_error and not (isinstance(exc, GithubException) and exc.status == 404):
            # The caller tells "could not check" (retryable) apart from "not a member".
            raise
        logger.warning("Authorization check failed closed for %s: %s", username, exc)
        return False
    state = getattr(membership, "state", None)
    return state == "active"


def _auth_allowlist() -> frozenset[str]:
    raw = os.environ.get("CI_FIX_AUTH_ALLOWLIST", "")
    return frozenset(login.strip() for login in raw.split(",") if login.strip())


def build_fix_task(
    gh: Any,
    *,
    command: ParsedCommand,
    pr_repo_full_name: str,
    pr_number: int,
    commenter: str,
    org: str = "valkey-io",
    auth_team: str = _DEFAULT_AUTH_TEAM,
    retries: int = 2,
) -> FixTask | GateRejection | GateError:
    """Return a fully bound task or a reason the request is not actionable."""
    try:
        authorized = is_authorized(gh, org, auth_team, commenter, retries=retries, raise_on_error=True)
    except Exception as exc:  # noqa: BLE001 - transient reads must remain retryable
        return GateError(f"Could not authorize @{commenter}: {exc}")
    if not authorized:
        return GateRejection(
            f"@{commenter} is not an active member of {org}/{auth_team}; refusing."
        )
    if command.run_id and f"{command.run_owner}/{command.run_repo}" != pr_repo_full_name:
        return GateRejection(
            f"The linked run belongs to {command.run_owner}/{command.run_repo}, "
            f"not {pr_repo_full_name}; refusing."
        )

    try:
        repo = retry_github_call(
            lambda: gh.get_repo(pr_repo_full_name),
            retries=retries,
            description=f"get {pr_repo_full_name}",
        )
        pr = retry_github_call(
            lambda: repo.get_pull(pr_number),
            retries=retries,
            description=f"get PR #{pr_number}",
        )
    except Exception as exc:  # noqa: BLE001
        return GateError(f"Could not load PR #{pr_number}: {exc}")

    head = getattr(pr, "head", None)
    head_sha = str(getattr(head, "sha", "") or "")
    head_branch = str(getattr(head, "ref", "") or "")
    head_repo = str(getattr(getattr(head, "repo", None), "full_name", "") or "")
    base_branch = str(getattr(getattr(pr, "base", None), "ref", "") or "")
    author = str(getattr(getattr(pr, "user", None), "login", "") or "")
    if str(getattr(pr, "state", "") or "") != "open":
        return GateRejection(f"PR #{pr_number} is not open; refusing.")
    if not all((head_sha, head_branch, head_repo, base_branch)):
        return GateRejection("Could not determine the PR head and base; refusing.")

    selected: FailedJob
    if command.run_id:
        try:
            run = retry_github_call(
                lambda: repo.get_workflow_run(command.run_id),
                retries=retries,
                description=f"get run {command.run_id}",
            )
            current_attempt = int(getattr(run, "run_attempt", 1) or 1)
            failed = failed_jobs_for_run(
                gh,
                pr_repo_full_name,
                command.run_id,
                run_attempt=current_attempt,
                retries=retries,
            )
        except Exception as exc:  # noqa: BLE001
            return GateError(f"Could not load run {command.run_id}: {exc}")
        if not failed:
            return GateRejection(f"Run {command.run_id} has no failed job to fix.")
        if command.run_attempt and command.run_attempt != current_attempt:
            return GateRejection(
                f"The linked URL names run attempt {command.run_attempt}, but "
                f"run {command.run_id} is currently attempt {current_attempt}."
            )
        if command.job_id:
            matched = next((job for job in failed if job.id == command.job_id), None)
            if matched is None:
                return GateRejection(
                    f"Job {command.job_id} is not a failed job of run {command.run_id}; refusing."
                )
            selected = matched
        else:
            selected = select_job(failed)
    else:
        try:
            runs = newest_runs_for_head(
                gh, pr_repo_full_name, branch=head_branch, head_sha=head_sha, retries=retries,
            )
            picked = most_actionable_failure(gh, pr_repo_full_name, runs)
        except Exception as exc:  # noqa: BLE001
            return GateError(f"Could not inspect CI runs for this PR: {exc}")
        if picked is None:
            return GateRejection(
                f"No completed failed run was found for the current head {head_sha[:12]}."
            )
        run, selected, failed = picked

    status = str(getattr(run, "status", "") or "")
    if status != "completed":
        return GateRejection(
            f"The linked run is not complete yet (status: {status or 'unknown'})."
        )
    run_sha = str(getattr(run, "head_sha", "") or "")
    if run_sha != head_sha:
        return GateRejection(
            f"The PR moved after this run (run: {run_sha[:12]}, PR: {head_sha[:12]})."
        )
    run_branch = str(getattr(run, "head_branch", "") or "")
    if run_branch and run_branch != head_branch:
        return GateRejection(
            f"The run tested branch {run_branch}, not the current PR branch {head_branch}."
        )

    same_repo = head_repo == pr_repo_full_name
    bot_owned = can_update_branch(
        same_repo=same_repo,
        branch=head_branch,
        author=author,
    )
    target_kind = TargetKind.UPDATE_PR if bot_owned else TargetKind.SUGGEST
    target = FixTarget(
        kind=target_kind,
        repo_full_name=head_repo,
        base_repo_full_name=pr_repo_full_name,
        branch=head_branch,
        expected_sha=head_sha,
    )
    failure = Failure(
        repo_full_name=pr_repo_full_name,
        run_id=int(getattr(run, "id", 0) or command.run_id),
        run_attempt=int(getattr(run, "run_attempt", 1) or 1),
        head_sha=head_sha,
        job_id=selected.id,
        job_name=selected.name,
        base_branch=base_branch,
        description=f"Targeting the failure in `{selected.name}`.",
        hint=command.hint,
        pr_number=pr_number,
        other_failed_jobs=tuple(job.name for job in failed if job.id != selected.id),
        workflow=workflow_file(run),
    )
    policy = (
        FixPolicy.BACKPORT
        if bot_owned and is_backport_branch(head_branch)
        else FixPolicy.GENERAL
    )
    return FixTask(
        failure=failure,
        target=target,
        policy=policy,
    )
