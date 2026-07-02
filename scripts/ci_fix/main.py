"""Entry point for the ``@valkeyrie-bot fix <ci-link>`` workflow.

Driven by ``workflow_dispatch``: a maintainer supplies the PR and failing-run
URL, and we run the pipeline and post the rendered outcome as a PR comment. It
also accepts a raw ``issue_comment`` event payload (``--event-path``) so a
future target-repo wrapper can forward a comment event here without changing
this entry point.

When the input is not an actionable fix command, we exit silently (rc 0). The
pipeline itself never raises for a refusal - it returns a ``FixOutcome`` we
always turn into a comment - so the only error path here is an unexpected
internal failure, which we surface both as a comment and a non-zero exit code.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from github import Auth, Github

from scripts.ci_fix.comment import render_comment
from scripts.ci_fix.gate import ParsedCommand, parse_command
from scripts.ci_fix.models import FixOutcome, OutcomeKind
from scripts.ci_fix.pipeline import run_ci_fix
from scripts.ci_fix.review import DEFAULT_VERIFY_RUNS
from scripts.ci_fix.verify.macos import MacosVerifier
from scripts.common.git_auth import GitAuth
from scripts.common.github_client import retry_github_call
from scripts.common.polling import env_int
from scripts.common.workflow_artifacts import ArtifactClient

logger = logging.getLogger(__name__)

# The team authorization is configurable so the same entry point can run
# against a different org/team in a fork test environment. Defaults to the
# production target; override only via these env vars.
_AUTH_ORG = os.environ.get("CI_FIX_AUTH_ORG", "valkey-io")
_AUTH_TEAM = os.environ.get("CI_FIX_AUTH_TEAM", "contributors")

# The agent repo hosting the verify-macos workflow, and the ref to dispatch it
# on. When unset, macOS verification is unavailable and macOS failures refuse.
_MACOS_AGENT_REPO = os.environ.get("CI_FIX_MACOS_AGENT_REPO", "")
_MACOS_AGENT_REF = os.environ.get("CI_FIX_MACOS_AGENT_REF", "main")
_MACOS_TOKEN = os.environ.get("CI_FIX_MACOS_TOKEN", "")
_MAX_VERIFY_RUNS = 10


def _verify_runs() -> int:
    """Return the local/Docker verification repeat count."""
    return env_int(
        "CI_FIX_VERIFY_RUNS",
        DEFAULT_VERIFY_RUNS,
        minimum=1,
        maximum=_MAX_VERIFY_RUNS,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH", ""),
                        help="Path to the issue_comment event JSON")
    parser.add_argument("--target-token", default=os.environ.get("TARGET_TOKEN", ""),
                        help="GitHub App installation token")
    # Dispatch mode: supply the command pieces directly instead of an event.
    # Used by the workflow_dispatch entry for manual/fork testing.
    parser.add_argument("--repo", default="", help="PR repository (owner/name)")
    parser.add_argument("--pr", type=int, default=0, help="PR number")
    parser.add_argument("--run-url", default="", help="Failing CI run URL")
    parser.add_argument("--commenter", default="", help="Requesting user login")
    parser.add_argument("--hint", default="", help="Optional diagnosis hint")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.target_token:
        parser.error("--target-token/TARGET_TOKEN is required")

    request = _request_from_dispatch(args) if args.run_url else _request_from_event(args)
    if request is None:
        logger.info("No actionable fix command; nothing to do.")
        return 0

    repo_full_name, pr_number, commenter, command = request
    return _run_and_comment(args.target_token, repo_full_name, pr_number, commenter, command)


def _run_and_comment(
    token: str,
    repo_full_name: str,
    pr_number: int,
    commenter: str,
    command: ParsedCommand,
) -> int:
    gh = Github(auth=Auth.Token(token))
    artifact_client = ArtifactClient(gh, token=token)
    macos_verifier = None
    if _MACOS_AGENT_REPO and _MACOS_TOKEN:
        # Dispatching the verify-macos workflow needs actions:write on the agent
        # repo, which the target (valkey-scoped) token does not carry; use the
        # dedicated agent-repo token for that client.
        agent_gh = Github(auth=Auth.Token(_MACOS_TOKEN))
        macos_verifier = MacosVerifier(
            agent_gh, agent_repo_full_name=_MACOS_AGENT_REPO, ref=_MACOS_AGENT_REF,
        )
    try:
        with GitAuth(token=token) as auth:
            outcome = run_ci_fix(
                gh,
                command=command,
                pr_repo_full_name=repo_full_name,
                pr_number=pr_number,
                commenter=commenter,
                git_env=auth.env(),
                artifact_client=artifact_client,
                org=_AUTH_ORG,
                auth_team=_AUTH_TEAM,
                verify_runs=_verify_runs(),
                macos_verifier=macos_verifier,
            )
    except Exception:  # noqa: BLE001 - never crash without telling the PR
        logger.exception("ci_fix pipeline raised unexpectedly")
        outcome = FixOutcome(
            kind=OutcomeKind.FAILED,
            summary="An internal error stopped the run; see the bot run logs for details.",
        )
    try:
        _post_comment(gh, repo_full_name, pr_number, render_comment(outcome))
    except Exception:  # noqa: BLE001 - a failed comment must not mask the outcome
        logger.exception("Failed to post outcome comment on #%s", pr_number)
    logger.info("ci_fix outcome: %s - %s", outcome.kind.value, outcome.summary)
    return 1 if outcome.kind is OutcomeKind.FAILED else 0


def _request_from_event(args: argparse.Namespace) -> tuple[str, int, str, ParsedCommand] | None:
    if not args.event_path:
        return None
    event = json.loads(Path(args.event_path).read_text(encoding="utf-8"))
    parsed = _parse_event(event)
    if parsed is None:
        return None
    repo_full_name, pr_number, commenter, body = parsed
    command = parse_command(body)
    if command is None:
        return None
    return repo_full_name, pr_number, commenter, command


def _request_from_dispatch(args: argparse.Namespace) -> tuple[str, int, str, ParsedCommand] | None:
    if not (args.repo and args.pr and args.commenter):
        return None
    command = parse_command(f"@valkeyrie-bot fix {args.run_url} {args.hint}".strip())
    if command is None:
        return None
    return args.repo, args.pr, args.commenter, command


def _parse_event(event: dict) -> tuple[str, int, str, str] | None:
    """Extract (repo_full_name, pr_number, commenter, body) from the event.

    Returns None when the event is not a created comment on a pull request.
    """
    if event.get("action") != "created":
        return None
    issue = event.get("issue") or {}
    if "pull_request" not in issue:
        return None
    comment = event.get("comment") or {}
    body = comment.get("body") or ""
    commenter = (comment.get("user") or {}).get("login") or ""
    pr_number = issue.get("number")
    repo_full_name = (event.get("repository") or {}).get("full_name") or ""
    if not (body and commenter and isinstance(pr_number, int) and repo_full_name):
        return None
    return repo_full_name, pr_number, commenter, body


def _post_comment(gh: Github, repo_full_name: str, pr_number: int, body: str) -> None:
    def _post() -> None:
        issue = gh.get_repo(repo_full_name).get_issue(pr_number)
        issue.create_comment(body)

    retry_github_call(_post, retries=3, description=f"comment on #{pr_number}")


if __name__ == "__main__":
    raise SystemExit(main())
