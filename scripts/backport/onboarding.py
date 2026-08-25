"""Onboard a newly published Valkey GA branch into backport automation."""

from __future__ import annotations

import argparse
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import yaml  # type: ignore[import-untyped]
from github import Auth, Github, InputGitAuthor
from github.GithubException import GithubException

from scripts.backport.pr_creator import create_pull_from_push_repo
from scripts.backport.registry import _parse_registry
from scripts.backport.sweep_graphql import GitHubGraphQLClient
from scripts.backport.sweep_prs import find_existing_pr
from scripts.common.github_client import retry_github_call

logger = logging.getLogger(__name__)

_REGISTRY_PATH = "repos.yml"
_SOURCE_REPO = "valkey-io/valkey"
_MARKER_NAMESPACE = "valkey-ci-agent:backport-onboarding"
_PR_MARKER_NAMESPACE = "valkey-ci-agent:backport-onboarding-pr"
_ISSUE_GENERATED_START = f"<!-- {_MARKER_NAMESPACE}:generated:start -->"
_ISSUE_GENERATED_END = f"<!-- {_MARKER_NAMESPACE}:generated:end -->"
_PROJECT_STATUS_FIELD = "Status"
_PROJECT_STATUS_OPTION = "To be backported"
_BRANCH_PREFIX = "agent/backport/onboard"
_FIRST_GA_RE = re.compile(r"^(\d+)\.(\d+)\.0$")
_COMMIT_AUTHOR = InputGitAuthor(
    "github-actions[bot]",
    "41898282+github-actions[bot]@users.noreply.github.com",
)


@dataclass(frozen=True)
class ProjectMatch:
    number: int
    title: str
    url: str


@dataclass(frozen=True)
class OnboardingResult:
    action: str
    branch: str = ""
    issue_url: str = ""
    pr_url: str = ""
    detail: str = ""


def first_ga_branch(tag: str) -> str | None:
    """Return ``X.Y`` only for a final first-GA tag ``X.Y.0``."""
    match = _FIRST_GA_RE.fullmatch(tag)
    if match is None:
        return None
    return f"{match.group(1)}.{match.group(2)}"


def discover_project(
    gql: GitHubGraphQLClient,
    *,
    owner: str,
    branch: str,
) -> tuple[list[ProjectMatch], str]:
    """Find open backport Projects whose metadata names *branch* exactly.

    A candidate must also expose the Status/To be backported configuration
    consumed by the sweep. This prevents an unrelated release-planning Project
    with the same version in its title from being selected.
    """
    query = """
    query($owner: String!, $cursor: String) {
      organization(login: $owner) {
        projectsV2(first: 100, after: $cursor, orderBy: {field: NUMBER, direction: ASC}) {
          nodes {
            number
            title
            shortDescription
            url
            closed
            fields(first: 50) {
              nodes {
                __typename
                ... on ProjectV2SingleSelectField {
                  name
                  options { name }
                }
              }
              pageInfo { hasNextPage endCursor }
            }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """
    cursor: str | None = None
    projects: list[dict[str, Any]] = []
    while True:
        data = gql.execute(query, {"owner": owner, "cursor": cursor})
        organization = data.get("organization")
        if not organization:
            raise RuntimeError(f"GitHub organization {owner!r} was not found or is not readable")
        page = organization.get("projectsV2") or {}
        projects.extend(node for node in (page.get("nodes") or []) if isinstance(node, dict))
        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            raise RuntimeError("GitHub Projects pagination omitted endCursor")

    token = re.compile(rf"(?<![0-9.]){re.escape(branch)}(?![0-9.])")
    matches: list[ProjectMatch] = []
    for project in projects:
        if project.get("closed"):
            continue
        searchable = f"{project.get('title') or ''}\n{project.get('shortDescription') or ''}"
        if token.search(searchable) is None or not _has_backport_status(
            gql, owner=owner, project=project,
        ):
            continue
        number = project.get("number")
        if not isinstance(number, int):
            continue
        matches.append(ProjectMatch(
            number=number,
            title=str(project.get("title") or f"Project {number}"),
            url=str(project.get("url") or f"https://github.com/orgs/{owner}/projects/{number}"),
        ))

    detail = _project_discovery_detail(owner, branch, matches)
    return matches, detail


def _has_backport_status(
    gql: GitHubGraphQLClient,
    *,
    owner: str,
    project: dict[str, Any],
) -> bool:
    fields = project.get("fields") or {}
    nodes = list(fields.get("nodes") or [])
    page_info = fields.get("pageInfo") or {}
    cursor = page_info.get("endCursor")
    query = """
    query($owner: String!, $number: Int!, $cursor: String) {
      organization(login: $owner) {
        projectV2(number: $number) {
          fields(first: 50, after: $cursor) {
            nodes {
              __typename
              ... on ProjectV2SingleSelectField {
                name
                options { name }
              }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
    """
    while page_info.get("hasNextPage"):
        if not cursor:
            raise RuntimeError("GitHub Project fields pagination omitted endCursor")
        data = gql.execute(query, {
            "owner": owner,
            "number": project["number"],
            "cursor": cursor,
        })
        project_data = (data.get("organization") or {}).get("projectV2")
        if not project_data:
            raise RuntimeError(f"GitHub Project {owner}/{project['number']} became unreadable")
        fields = project_data.get("fields") or {}
        nodes.extend(fields.get("nodes") or [])
        page_info = fields.get("pageInfo") or {}
        cursor = page_info.get("endCursor")

    for field in nodes:
        if not isinstance(field, dict) or field.get("__typename") != "ProjectV2SingleSelectField":
            continue
        options = field.get("options") or []
        if field.get("name") == _PROJECT_STATUS_FIELD and any(
            option.get("name") == _PROJECT_STATUS_OPTION
            for option in options if isinstance(option, dict)
        ):
            return True
    return False


def _project_discovery_detail(
    owner: str,
    branch: str,
    matches: list[ProjectMatch],
) -> str:
    rule = (
        f"an open {owner} Project whose title or short description contains the exact "
        f"branch token `{branch}` and whose `{_PROJECT_STATUS_FIELD}` field has a "
        f"`{_PROJECT_STATUS_OPTION}` option"
    )
    if not matches:
        return f"No matching Project was found. Expected {rule}."
    if len(matches) > 1:
        found = ", ".join(f"[{item.title}]({item.url})" for item in matches)
        return f"Found {len(matches)} matching Projects ({found}). Expected exactly one: {rule}."
    return ""


def registry_project_number(text: str, repo_name: str, branch: str) -> int | None:
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError("repos.yml must contain a YAML mapping")
    registry = _parse_registry(raw)
    try:
        _, entry = registry.get_branch(repo_name, branch)
    except KeyError:
        return None
    return entry.project_number


def add_registry_branch(text: str, repo_name: str, branch: str, project_number: int) -> str:
    """Append one branch entry without reformatting unrelated registry content."""
    existing = registry_project_number(text, repo_name, branch)
    if existing is not None:
        if existing != project_number:
            raise RuntimeError(
                f"{repo_name}@{branch} is already registered with Project {existing}, "
                f"not discovered Project {project_number}"
            )
        return text

    lines = text.splitlines(keepends=True)
    repo_pattern = re.compile(rf"^  - repo:\s*[\"']?{re.escape(repo_name)}[\"']?\s*$")
    repo_start = next(
        (index for index, line in enumerate(lines) if repo_pattern.fullmatch(line.rstrip("\r\n"))),
        None,
    )
    if repo_start is None:
        raise RuntimeError(f"Repository {repo_name!r} is not present in repos.yml")
    repo_end = next(
        (index for index in range(repo_start + 1, len(lines)) if lines[index].startswith("  - repo:")),
        len(lines),
    )
    branches_start = next(
        (index for index in range(repo_start + 1, repo_end)
         if lines[index].rstrip("\r\n") == "    branches:"),
        None,
    )
    if branches_start is None:
        raise RuntimeError(f"Repository {repo_name!r} has no branches block in repos.yml")

    insert_at = repo_end
    while insert_at > branches_start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    entry = (
        f'      - branch: "{branch}"\n'
        f"        project_number: {project_number}\n"
    )
    updated = "".join(lines[:insert_at]) + entry + "".join(lines[insert_at:])
    if registry_project_number(updated, repo_name, branch) != project_number:
        raise RuntimeError("generated repos.yml did not validate with the discovered Project number")
    return updated


def onboard_first_ga(
    project_gql: GitHubGraphQLClient,
    gh_agent: Any,
    *,
    agent_repo: str,
    tag: str,
    issue_author: str,
    source_repo: str = _SOURCE_REPO,
) -> OnboardingResult:
    """Create the issue and, only after unique discovery, the registry PR."""
    branch = first_ga_branch(tag)
    if branch is None:
        return OnboardingResult(action="not-applicable")
    if gh_agent is None:
        raise RuntimeError(
            f"AGENT_GITHUB_TOKEN is unavailable; manually register {source_repo}@{branch} in "
            f"{agent_repo}/{_REGISTRY_PATH} after identifying its GitHub Project number"
        )

    repo = retry_github_call(
        lambda: gh_agent.get_repo(agent_repo), retries=2, description=f"get {agent_repo}",
    )
    default_branch = repo.default_branch
    base_sha = retry_github_call(
        lambda: repo.get_branch(default_branch).commit.sha,
        retries=2, description=f"resolve {agent_repo} {default_branch} head",
    )
    registry_file = retry_github_call(
        lambda: repo.get_contents(_REGISTRY_PATH, ref=base_sha),
        retries=2, description=f"read {agent_repo}/{_REGISTRY_PATH}",
    )
    registry_text = registry_file.decoded_content.decode("utf-8")
    registered = registry_project_number(registry_text, source_repo, branch)
    if registered is not None:
        return OnboardingResult(
            action="already-registered", branch=branch,
            detail=f"{source_repo}@{branch} already uses Project {registered}",
        )

    project_owner = source_repo.split("/", 1)[0]
    try:
        matches, detail = discover_project(project_gql, owner=project_owner, branch=branch)
    except Exception as exc:  # Project reads must fail closed, but issue creation can continue.
        matches = []
        detail = f"Project discovery failed: {exc}"

    issue_number, issue_url = _upsert_issue(
        repo, source_repo=source_repo, branch=branch, tag=tag,
        project=matches[0] if len(matches) == 1 else None,
        blocker=detail,
        expected_author=issue_author,
    )
    if len(matches) != 1:
        return OnboardingResult(
            action="blocked-project-discovery", branch=branch,
            issue_url=issue_url, detail=detail,
        )

    project = matches[0]
    expected_registry = add_registry_branch(
        registry_text, source_repo, branch, project.number,
    )
    head_branch = f"{_BRANCH_PREFIX}/{branch}"
    validated_head_sha = _ensure_registry_branch(
        repo,
        branch=head_branch,
        base_sha=base_sha,
        expected_registry=expected_registry,
        source_repo=source_repo,
        release_branch=branch,
        project_number=project.number,
    )

    existing_pr = find_existing_pr(gh_agent, agent_repo, agent_repo, head_branch)
    if existing_pr is not None:
        _validate_existing_pr(
            existing_pr,
            default_branch=default_branch,
            head_repo=agent_repo,
            head_branch=head_branch,
            head_sha=validated_head_sha,
            expected_title=f"Enable backports for Valkey {branch}",
            expected_body=_pr_body(source_repo, branch, project, issue_number),
        )
        return OnboardingResult(
            action="pr-already-open", branch=branch, issue_url=issue_url,
            pr_url=existing_pr.html_url,
        )

    try:
        pr = create_pull_from_push_repo(
            repo,
            base_repo=agent_repo,
            push_repo=agent_repo,
            title=f"Enable backports for Valkey {branch}",
            body=_pr_body(source_repo, branch, project, issue_number),
            head_branch=head_branch,
            base_branch=default_branch,
            draft=False,
        )
    except Exception:  # noqa: BLE001 - ambiguous non-idempotent POST outcome
        recovered_pr = find_existing_pr(gh_agent, agent_repo, agent_repo, head_branch)
        if recovered_pr is None:
            raise
        _validate_existing_pr(
            recovered_pr,
            default_branch=default_branch,
            head_repo=agent_repo,
            head_branch=head_branch,
            head_sha=validated_head_sha,
            expected_title=f"Enable backports for Valkey {branch}",
            expected_body=_pr_body(source_repo, branch, project, issue_number),
        )
        return OnboardingResult(
            action="pr-already-open", branch=branch, issue_url=issue_url,
            pr_url=recovered_pr.html_url,
        )
    _validate_existing_pr(
        pr,
        default_branch=default_branch,
        head_repo=agent_repo,
        head_branch=head_branch,
        head_sha=validated_head_sha,
        expected_title=f"Enable backports for Valkey {branch}",
        expected_body=_pr_body(source_repo, branch, project, issue_number),
    )
    return OnboardingResult(
        action="pr-created", branch=branch, issue_url=issue_url, pr_url=pr.html_url,
    )


def _upsert_issue(
    repo: Any,
    *,
    source_repo: str,
    branch: str,
    tag: str,
    project: ProjectMatch | None,
    blocker: str,
    expected_author: str,
) -> tuple[int, str]:
    marker = f"<!-- {_MARKER_NAMESPACE}:{source_repo}:{branch} -->"
    title = f"Enable backport automation for Valkey {branch}"
    generated_body = _issue_body(marker, tag, source_repo, branch, project, blocker)

    def find_owned() -> tuple[Any | None, list[Any]]:
        issues = retry_github_call(
            lambda: list(repo.get_issues(state="open")), retries=2,
            description="list open backport onboarding issues",
        )
        plain_issues = [
            issue for issue in issues if getattr(issue, "pull_request", None) is None
        ]
        marker_issues = [issue for issue in plain_issues if marker in (issue.body or "")]
        untrusted = [
            issue for issue in marker_issues
            if getattr(getattr(issue, "user", None), "login", None) != expected_author
        ]
        if untrusted:
            authors = sorted({
                str(getattr(getattr(issue, "user", None), "login", "unknown"))
                for issue in untrusted
            })
            raise RuntimeError(
                f"onboarding marker {marker} was pre-seeded by an unexpected author "
                f"({', '.join(authors)}); refusing to adopt it"
            )
        owned = [issue for issue in marker_issues if issue not in untrusted]
        if len(owned) > 1:
            raise RuntimeError(
                f"found {len(owned)} open issues with onboarding marker {marker}; "
                "refusing to choose between duplicates"
            )
        return (owned[0] if owned else None), plain_issues

    existing, issues = find_owned()
    if existing is None:
        if any(issue.title == title for issue in issues):
            raise RuntimeError(
                f"open issue title {title!r} exists without the ownership marker; "
                "refusing to overwrite or duplicate it"
            )
        try:
            # Do not blindly retry this non-idempotent POST. If the server
            # created the issue but the response was lost, recover by marker.
            issue = repo.create_issue(title=title, body=generated_body)
        except Exception:  # noqa: BLE001 - ambiguous POST outcome recovery
            recovered, _ = find_owned()
            if recovered is not None:
                if recovered.title != title or (recovered.body or "") != generated_body:
                    raise RuntimeError(
                        "recovered onboarding issue does not match the exact generated title/body; "
                        "refusing to use it"
                    )
                return recovered.number, recovered.html_url
            raise
        return issue.number, issue.html_url

    merged_body = _replace_generated_issue_section(existing.body or "", generated_body)
    if existing.title != title or (existing.body or "") != merged_body:
        retry_github_call(
            lambda: existing.edit(title=title, body=merged_body),
            retries=2, description=f"update backport onboarding issue #{existing.number}",
        )
    return existing.number, existing.html_url


def _replace_generated_issue_section(existing: str, generated: str) -> str:
    old_start = existing.find(_ISSUE_GENERATED_START)
    old_end = existing.find(_ISSUE_GENERATED_END)
    new_start = generated.index(_ISSUE_GENERATED_START)
    new_end = generated.index(_ISSUE_GENERATED_END) + len(_ISSUE_GENERATED_END)
    if old_start < 0 or old_end < old_start:
        raise RuntimeError(
            "marker-owned onboarding issue lacks generated-section boundaries; "
            "refusing to overwrite human-authored content"
        )
    old_end += len(_ISSUE_GENERATED_END)
    return existing[:old_start] + generated[new_start:new_end] + existing[old_end:]


def _issue_body(
    marker: str,
    tag: str,
    source_repo: str,
    branch: str,
    project: ProjectMatch | None,
    blocker: str,
) -> str:
    project_line = (
        f"- [x] GitHub Project discovered: [{project.title}]({project.url})"
        if project is not None else
        "- [ ] GitHub Project discovered"
    )
    lines = [
        marker,
        _ISSUE_GENERATED_START,
        "",
        f"Valkey {tag} GA has been published.",
        "",
        f"- [x] Release branch `{branch}` exists",
        project_line,
        f"- [ ] Add `{branch}` and its Project number to `{_REGISTRY_PATH}`",
        f"- [ ] Verify the generated backport matrix includes `{source_repo}@{branch}`",
    ]
    if project is None:
        lines.extend([
            "",
            "## Blocker",
            "",
            blocker,
            "",
            "No Project number was guessed and no pull request was opened.",
        ])
    lines.extend(["", _ISSUE_GENERATED_END])
    return "\n".join(lines)


def _ensure_registry_branch(
    repo: Any,
    *,
    branch: str,
    base_sha: str,
    expected_registry: str,
    source_repo: str,
    release_branch: str,
    project_number: int,
) -> str:
    created = False
    try:
        retry_github_call(
            lambda: repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha),
            retries=2, description=f"create branch {branch}",
        )
        created = True
    except GithubException as exc:
        if exc.status != 422:
            raise

    if not created:
        return _validate_existing_registry_branch(
            repo, branch, base_sha, source_repo, release_branch, project_number,
        )

    base_file = retry_github_call(
        lambda: repo.get_contents(_REGISTRY_PATH, ref=branch),
        retries=2, description=f"read {_REGISTRY_PATH} on {branch}",
    )
    try:
        update = retry_github_call(
            lambda: repo.update_file(
                _REGISTRY_PATH,
                f"Enable backports for Valkey {release_branch}\n\n"
                "Signed-off-by: github-actions[bot] "
                "<41898282+github-actions[bot]@users.noreply.github.com>",
                expected_registry,
                base_file.sha,
                branch=branch,
                author=_COMMIT_AUTHOR,
                committer=_COMMIT_AUTHOR,
            ),
            retries=2, description=f"update {_REGISTRY_PATH} on {branch}",
        )
    except GithubException:
        # A concurrent idempotent run may have won the update race. Accept it
        # only if the resulting branch is exactly the one-commit registry edit.
        return _validate_existing_registry_branch(
            repo, branch, base_sha, source_repo, release_branch, project_number,
        )

    commit = update.get("commit") if isinstance(update, dict) else None
    commit_sha = (
        commit.get("sha") if isinstance(commit, dict)
        else getattr(commit, "sha", None)
    )
    if not isinstance(commit_sha, str) or not commit_sha:
        raise RuntimeError("GitHub did not return the commit SHA for the repos.yml update")
    return _validate_existing_registry_branch(
        repo, branch, base_sha, source_repo, release_branch, project_number,
        expected_head_sha=commit_sha,
    )


def _validate_existing_registry_branch(
    repo: Any,
    branch: str,
    base_sha: str,
    source_repo: str,
    release_branch: str,
    project_number: int,
    *,
    expected_head_sha: str | None = None,
) -> str:
    head_sha = retry_github_call(
        lambda: repo.get_branch(branch).commit.sha,
        retries=2, description=f"resolve {branch} head",
    )
    if expected_head_sha is not None and head_sha != expected_head_sha:
        raise RuntimeError(
            f"deterministic branch {branch!r} moved after the generated commit was created; "
            "refusing to open a PR from an unvalidated head"
        )
    commit = retry_github_call(
        lambda: repo.get_commit(head_sha), retries=2, description=f"inspect branch {branch}",
    )
    parents = list(commit.parents)
    files = list(commit.files)
    if len(parents) != 1 or [file.filename for file in files] != [_REGISTRY_PATH]:
        raise RuntimeError(
            f"existing deterministic branch {branch!r} contains changes other than the expected "
            f"single {_REGISTRY_PATH} edit; refusing to overwrite it"
        )
    if parents[0].sha != base_sha:
        raise RuntimeError(
            f"existing deterministic branch {branch!r} is based on {parents[0].sha}, not the "
            f"current default head {base_sha}; refusing a stale repos.yml snapshot"
        )
    parent_file = retry_github_call(
        lambda: repo.get_contents(_REGISTRY_PATH, ref=parents[0].sha),
        retries=2, description=f"read parent {_REGISTRY_PATH} for {branch}",
    )
    expected = add_registry_branch(
        parent_file.decoded_content.decode("utf-8"),
        source_repo,
        release_branch,
        project_number,
    )
    branch_file = retry_github_call(
        lambda: repo.get_contents(_REGISTRY_PATH, ref=head_sha),
        retries=2, description=f"read branch {_REGISTRY_PATH} for {branch}",
    )
    if branch_file.decoded_content.decode("utf-8") != expected:
        raise RuntimeError(
            f"existing deterministic branch {branch!r} does not contain the exact expected "
            f"{_REGISTRY_PATH} change; refusing to overwrite it"
        )
    final_head_sha = retry_github_call(
        lambda: repo.get_branch(branch).commit.sha,
        retries=2, description=f"recheck {branch} head",
    )
    if final_head_sha != head_sha:
        raise RuntimeError(
            f"deterministic branch {branch!r} moved during validation; refusing to open a PR"
        )
    return head_sha


def _validate_existing_pr(
    pr: Any,
    *,
    default_branch: str,
    head_repo: str,
    head_branch: str,
    head_sha: str,
    expected_title: str,
    expected_body: str,
) -> None:
    base_ref = getattr(getattr(pr, "base", None), "ref", None)
    head = getattr(pr, "head", None)
    actual_head_repo = getattr(getattr(head, "repo", None), "full_name", None)
    if (
        base_ref != default_branch
        or actual_head_repo != head_repo
        or getattr(head, "ref", None) != head_branch
        or getattr(head, "sha", None) != head_sha
        or pr.title != expected_title
        or (pr.body or "") != expected_body
    ):
        raise RuntimeError(
            f"open PR {pr.html_url} from deterministic branch {head_branch!r} "
            "is not the exact agent-owned onboarding PR; refusing to reuse or overwrite it"
        )


def _pr_body(
    source_repo: str,
    branch: str,
    project: ProjectMatch,
    issue_number: int,
) -> str:
    return "\n".join([
        f"<!-- {_PR_MARKER_NAMESPACE}:{source_repo}:{branch} -->",
        "",
        f"Enable backport automation for `{source_repo}@{branch}` after the first GA release.",
        "",
        f"- GitHub Project: [{project.title}]({project.url}) (`{project.number}`)",
        f"- Registry entry: `{source_repo}@{branch}`",
        f"- Generated backport matrix entry: `{source_repo}@{branch}`",
        "",
        f"Closes #{issue_number}",
    ])


def _actions_warning(message: str) -> None:
    annotation = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::warning title=Post-GA backport onboarding failed::{annotation}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Published release tag")
    parser.add_argument("--agent-repo", required=True, help="Agent repository receiving issue/PR")
    parser.add_argument(
        "--token",
        default=os.environ.get("ONBOARDING_GITHUB_TOKEN", ""),
        help="Post-publication App token",
    )
    parser.add_argument(
        "--issue-author",
        default=os.environ.get("ONBOARDING_BOT_LOGIN", ""),
        help="Expected login of the App/bot filing the issue",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    branch = first_ga_branch(args.tag)
    if branch is None:
        logger.info("Post-GA onboarding does not apply to tag %s", args.tag)
        return 0
    manual = (
        f"Valkey {args.tag} remains published. Manually open an issue in {args.agent_repo} "
        f"and register {_SOURCE_REPO}@{branch} in {_REGISTRY_PATH} after identifying the "
        "exact Project number."
    )
    if not args.token or not args.issue_author:
        _actions_warning(
            "The post-publication onboarding App token or bot login is unavailable. " + manual
        )
        return 0

    try:
        gh = Github(auth=Auth.Token(args.token))
        result = onboard_first_ga(
            GitHubGraphQLClient(args.token),
            gh,
            agent_repo=args.agent_repo,
            tag=args.tag,
            issue_author=args.issue_author,
        )
        logger.info(
            "Post-GA backport onboarding: %s%s",
            result.action,
            f" ({result.detail})" if result.detail else "",
        )
    except Exception as exc:  # noqa: BLE001 - this step cannot revoke publication
        logger.warning("Post-GA backport onboarding failed: %s", exc, exc_info=True)
        _actions_warning(f"Backport onboarding failed: {exc}. {manual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
