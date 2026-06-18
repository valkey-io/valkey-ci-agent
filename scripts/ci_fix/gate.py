"""Authorization and integrity gate for ``@valkeyrie-bot fix <ci-link>``.

This is the security boundary. Nothing downstream runs until every check here
passes, and every check fails closed.

The command shape check requires the comment to be the fix command with a CI
run link on the same repository as the PR. The authorization check requires the
commenter to be an active member of a configured GitHub team
(``valkey-io/contributors``); a failed or negative membership read is a refusal,
never a fallback to a looser check. The SHA-bound run gating check requires the
failed run's ``head_repo``/``head_branch`` to match the PR head, and the PR head
SHA to still equal the SHA the run was built from. If the branch moved, the log
no longer describes the code, so we refuse rather than fix a stale failure.

The gate returns a ``FixRequest`` only when all three hold; otherwise a
``GateRejection`` explaining why, which the caller turns into a PR comment.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from scripts.ci_fix.models import FixRequest
from scripts.common.github_client import retry_github_call

logger = logging.getLogger(__name__)

# The comment must *begin* with the invocation (after optional leading
# whitespace) so quoting or mentioning the command mid-discussion does not
# trigger a fix. Either bot identity may drive it: @valkeyrie-bot (manual
# dispatch) or @valkeyrie-ops (the App that opens the PRs, used by the comment
# poller). The hint is only the remainder of the invocation line, not the whole
# comment, so a multi-line conversational reply is not folded into the hint.
_COMMAND_RE = re.compile(
    r"^\s*@valkeyrie-(?:bot|ops)\s+fix\s+(?P<url>\S+)(?:[^\S\n]+(?P<hint>[^\n]*))?",
    re.IGNORECASE,
)
# Actions run URL: .../<owner>/<repo>/actions/runs/<run_id>
_RUN_URL_RE = re.compile(
    r"github\.com/(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+)/actions/runs/(?P<run_id>\d+)",
)

_DEFAULT_AUTH_TEAM = "contributors"


@dataclass(frozen=True)
class ParsedCommand:
    run_owner: str
    run_repo: str
    run_id: int
    hint: str


@dataclass(frozen=True)
class GateRejection:
    """A refusal with a human-readable reason for the PR comment."""

    reason: str


def parse_command(body: str) -> ParsedCommand | None:
    """Parse a comment body into a command, or None if it isn't one."""
    if not body:
        return None
    match = _COMMAND_RE.search(body)
    if not match:
        return None
    url_match = _RUN_URL_RE.search(match.group("url"))
    if not url_match:
        return None
    hint = (match.group("hint") or "").strip()
    return ParsedCommand(
        run_owner=url_match.group("owner"),
        run_repo=url_match.group("repo"),
        run_id=int(url_match.group("run_id")),
        hint=hint,
    )


def is_authorized(
    gh: Any,
    org: str,
    team_slug: str,
    username: str,
    *,
    retries: int = 2,
) -> bool:
    """Return True only if ``username`` is allowed to drive the bot.

    ``username`` must be a GitHub-verified principal (``github.actor`` for the
    current dispatch). A future comment trigger must pass ``comment.user.login``
    from the event payload, never a value forwarded through a dispatch input,
    which any opener of the wrapper could set.

    The primary source is active membership of ``org/team_slug``. Fails closed:
    any error reading membership (permission, network, missing team) returns
    False, and a ``pending`` invitation does not authorize.

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
        logger.warning("Authorization check failed closed for %s: %s", username, exc)
        return False
    state = getattr(membership, "state", None)
    return state == "active"


def _auth_allowlist() -> frozenset[str]:
    raw = os.environ.get("CI_FIX_AUTH_ALLOWLIST", "")
    return frozenset(login.strip() for login in raw.split(",") if login.strip())


def build_fix_request(
    gh: Any,
    *,
    command: ParsedCommand,
    pr_repo_full_name: str,
    pr_number: int,
    commenter: str,
    org: str = "valkey-io",
    auth_team: str = _DEFAULT_AUTH_TEAM,
    retries: int = 2,
) -> FixRequest | GateRejection:
    """Run all gate checks and return a FixRequest or a GateRejection.

    Assumes the comment was already confirmed to be on a pull request by the
    caller (the GitHub Actions event guarantees this for ``issue_comment`` on
    a PR); this function enforces authorization, run ownership, and SHA
    binding.
    """
    if not is_authorized(gh, org, auth_team, commenter, retries=retries):
        return GateRejection(
            reason=f"@{commenter} is not an active member of {org}/{auth_team}; refusing."
        )

    run_repo_full_name = f"{command.run_owner}/{command.run_repo}"
    if run_repo_full_name != pr_repo_full_name:
        return GateRejection(
            reason=(
                f"The linked run belongs to {run_repo_full_name}, not this PR's "
                f"repository {pr_repo_full_name}; refusing."
            )
        )

    try:
        pr = retry_github_call(
            lambda: gh.get_repo(pr_repo_full_name).get_pull(pr_number),
            retries=retries, description=f"get PR #{pr_number}",
        )
        run = retry_github_call(
            lambda: gh.get_repo(pr_repo_full_name).get_workflow_run(command.run_id),
            retries=retries, description=f"get run {command.run_id}",
        )
    except Exception as exc:  # noqa: BLE001 - fail closed
        return GateRejection(reason=f"Could not load PR or run: {exc}")

    run_status = str(getattr(run, "status", "") or "")
    if run_status != "completed":
        return GateRejection(
            reason=(
                f"The linked run is not finished yet (status: {run_status or 'unknown'}). "
                "Its logs are only available once it completes - re-run me when it has."
            )
        )
    # We deliberately do NOT gate on the run's overall conclusion. A run can be
    # "cancelled" overall (a manual stop, or fail-fast after one job failed) yet
    # still contain genuine job failures worth fixing. Whether there is a real
    # failure to act on is decided per-job downstream: the pipeline lists the
    # jobs that actually failed and requires the AI's hinted job to be one of
    # them, refusing otherwise. Run-level conclusion is noise; the job is truth.

    pr_head_sha = str(getattr(pr.head, "sha", "") or "")
    pr_head_ref = str(getattr(pr.head, "ref", "") or "")
    pr_head_repo = str(getattr(getattr(pr.head, "repo", None), "full_name", "") or "")
    run_head_sha = str(getattr(run, "head_sha", "") or "")
    run_head_branch = str(getattr(run, "head_branch", "") or "")

    if not pr_head_sha or not run_head_sha:
        return GateRejection(
            reason="Could not determine the PR or run head commit; refusing."
        )
    if run_head_sha != pr_head_sha:
        return GateRejection(
            reason=(
                "The PR branch has moved since this run "
                f"(run built {run_head_sha[:12] or 'unknown'}, PR head is "
                f"{pr_head_sha[:12] or 'unknown'}); re-run CI and try again."
            )
        )
    if run_head_branch and pr_head_ref and run_head_branch != pr_head_ref:
        return GateRejection(
            reason=(
                f"The run's branch ({run_head_branch}) does not match the PR head "
                f"branch ({pr_head_ref}); refusing."
            )
        )
    if not pr_head_repo:
        return GateRejection(
            reason="Could not determine the PR head repository; refusing.",
        )
    if pr_head_repo != pr_repo_full_name:
        return GateRejection(
            reason=(
                f"The PR head is on {pr_head_repo}, not {pr_repo_full_name}; "
                "ci_fix only pushes to branches on the PR's own repository."
            )
        )

    return FixRequest(
        repo_full_name=pr_repo_full_name,
        pr_number=pr_number,
        head_repo_full_name=pr_repo_full_name,
        head_branch=pr_head_ref,
        head_sha=pr_head_sha,
        run_id=command.run_id,
        requested_by=commenter,
        hint=command.hint,
    )
