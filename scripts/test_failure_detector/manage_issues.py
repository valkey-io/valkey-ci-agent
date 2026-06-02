"""Create or update GitHub issues for detected test failures"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from github import Github
from github.GithubException import GithubException
from github.Issue import Issue
from github.Repository import Repository

from scripts.common.github_client import retry_github_call
from scripts.test_failure_detector.parse_failures import UniqueFailure

logger = logging.getLogger(__name__)

_LABEL_NAME = "test-failure"
_LABEL_COLOR = "e11d48"
_LABEL_DESCRIPTION = "Test failure detected by CI"

def ensure_label_exists(repo: Repository) -> None:
    """Ensure the test-failure label exists on the repository."""
    try:
        retry_github_call(
            lambda: repo.get_label(_LABEL_NAME),
            retries=3,
            description=f"get label {_LABEL_NAME}",
        )
    except GithubException as e:
        if e.status == 404:
            logger.info("Creating label %r on %s", _LABEL_NAME, repo.full_name)
            retry_github_call(
                lambda: repo.create_label(
                    name=_LABEL_NAME,
                    color=_LABEL_COLOR,
                    description=_LABEL_DESCRIPTION,
                ),
                retries=3,
                description=f"create label {_LABEL_NAME}",
            )
        else:
            raise

def get_open_test_failure_issues(repo: Repository) -> list[Issue]:
    """Get all open issues with the test-failure label."""
    issues = retry_github_call(
        lambda: list(repo.get_issues(state="open", labels=[_LABEL_NAME])),
        retries=3,
        description="list open test-failure issues",
    )
    return issues

def _build_issue_title(failure: UniqueFailure) -> str:
    return f"[TEST-FAILURE] {failure.test_name} in {failure.test_file}"

def _build_issue_body(failure: UniqueFailure) -> str:
    """Build the initial issue body for a new test failure."""
    ci_links = "\n".join(
        f"- `{j.job}`: [CI link]({j.url})" for j in failure.jobs
    )
    env_list = ", ".join(f"`{j.job}`" for j in failure.jobs)

    return "\n".join([
        "**Summary**",
        "",
        f"`{failure.test_name}` in `{failure.test_file}` is failing in CI.",
        "",
        "**Failing test(s)**",
        "",
        f"- Test name: `{failure.test_name}`",
        f"- Test file: `{failure.test_file}`",
        "- CI link(s):",
        ci_links,
        "",
        "**Error stack trace**",
        "",
        "```",
        failure.error or "N/A",
        "```",
        "",
        f"**Environments:** {env_list}",
        "",
        "---",
        "*Auto-created by Test Failure Detector*",
    ])

def _build_comment_body(failure: UniqueFailure) -> str:
    """Build a comment for an existing issue that failed again."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ci_links = "\n".join(
        f"- `{j.job}`: [CI link]({j.url})" for j in failure.jobs
    )
    return f"Test failed again on {today}.\n\n**Failed in:**\n{ci_links}"

def _extract_environments_from_body(body: str) -> list[str]:
    """Extract existing environment names from an issue body."""
    env_match = re.search(r"\*\*Environments:\*\*\s*(.+)", body)
    if not env_match:
        return []
    env_inner = re.findall(r"`([^`]+)`", env_match.group(1))
    return env_inner

def _update_environments_in_body(body: str, all_envs: list[str]) -> str:
    """Replace the Environments line in the issue body with updated list."""
    new_env_line = f"**Environments:** {', '.join(f'`{e}`' for e in all_envs)}"
    return re.sub(r"\*\*Environments:\*\*\s*.+", new_env_line, body)

def process_failures(
    gh: Github,
    repo_full_name: str,
    failures: list[UniqueFailure],
) -> dict[str, int]:
    """Create or update GitHub issues for each unique failure.

    Returns a summary dict with counts: {created: N, updated: N, unchanged: N}.
    """
    repo = retry_github_call(
        lambda: gh.get_repo(repo_full_name),
        retries=3,
        description=f"get repo {repo_full_name}",
    )

    ensure_label_exists(repo)
    existing_issues = get_open_test_failure_issues(repo)

    summary = {"created": 0, "updated": 0, "unchanged": 0}

    for failure in failures:
        title = _build_issue_title(failure)
        env_list = [j.job for j in failure.jobs]

        # Check if an issue already exists with this title
        existing = next((i for i in existing_issues if i.title == title), None)

        if existing:
            logger.info("Found existing issue #%d for %s", existing.number, failure.display_name)

            # Check if there are new environments to add
            existing_envs = _extract_environments_from_body(existing.body or "")
            new_envs = [e for e in env_list if e not in existing_envs]

            if new_envs:
                logger.info("  New environments: %s", ", ".join(new_envs))
                all_envs = existing_envs + new_envs
                updated_body = _update_environments_in_body(existing.body or "", all_envs)
                retry_github_call(
                    lambda: existing.edit(body=updated_body),
                    retries=3,
                    description=f"update issue #{existing.number} body",
                )

            # Always add a comment noting the recurrence
            comment_body = _build_comment_body(failure)
            retry_github_call(
                lambda: existing.create_comment(comment_body),
                retries=3,
                description=f"comment on issue #{existing.number}",
            )
            summary["updated"] += 1

        else:
            logger.info("Creating issue for %s", failure.display_name)
            body = _build_issue_body(failure)
            retry_github_call(
                lambda: repo.create_issue(
                    title=title,
                    body=body,
                    labels=[_LABEL_NAME],
                ),
                retries=3,
                description=f"create issue for {failure.test_name}",
            )
            summary["created"] += 1

    logger.info(
        "Done. Created %d, updated %d, unchanged %d issue(s).",
        summary["created"], summary["updated"], summary["unchanged"],
    )
    return summary