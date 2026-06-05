"""Daily backport sweep across registered release branches."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github import Auth, Github

from scripts.backport.main import _run_git
from scripts.backport.sweep_apply import (
    apply_candidate,
)
from scripts.backport.sweep_git import (
    branch_has_changes,
    clone_target_branch,
    list_already_applied,
    list_applied_prs_on_branch,
    push_backport_branch,
    safe_tmp_component,
    sync_target_branch_to_source,
)
from scripts.backport.sweep_graphql import GitHubGraphQLClient
from scripts.backport.sweep_models import (
    DETAIL_ALREADY_ON_SWEEP_BRANCH,
    BranchSweepResult,
    CandidateResult,
    ProjectBackportCandidate,
)
from scripts.backport.sweep_prs import (
    delete_stale_backport_branch,
    find_existing_pr,
    upsert_pr,
)
from scripts.backport.sweep_reporting import (
    build_summary,
    result_is_on_backport_branch,
    validation_failure_detail,
)
from scripts.backport.sweep_validation import (
    run_test_commands,
    validate_branch_with_optional_repair,
)
from scripts.common.git_auth import GitAuth, github_https_url
from scripts.common.job_summary import emit_job_summary

if TYPE_CHECKING:
    from scripts.backport.registry import BranchEntry, RepoEntry  # noqa: F401

logger = logging.getLogger(__name__)

_DEFAULT_BRANCH_FIELDS = (
    "Backport Branch", "Target Branch", "Release Branch",
    "Branch", "Version", "Release", "Folder",
)
_DEFAULT_STATUS_FIELD = "Status"
_DEFAULT_STATUS_VALUE = "To be backported"
_BRANCH_PREFIX = "agent/backport/sweep"


class ProjectBackportDiscovery:
    def __init__(
        self,
        gql: GitHubGraphQLClient,
        *,
        project_owner: str,
        project_number: int,
        source_repo: str,
        project_owner_type: str = "organization",
        status_field: str = _DEFAULT_STATUS_FIELD,
        status_value: str = _DEFAULT_STATUS_VALUE,
        branch_fields: list[str] | None = None,
        implicit_target_branch: str | None = None,
    ) -> None:
        self._gql = gql
        self._owner = project_owner
        self._number = project_number
        self._owner_type = project_owner_type
        self._source_repo = source_repo
        self._status_field = status_field
        self._status_value = status_value
        self._branch_fields = branch_fields or list(_DEFAULT_BRANCH_FIELDS)
        self._implicit_target = implicit_target_branch

    def discover(
        self,
        release_branches: list[str],
    ) -> dict[str, list[ProjectBackportCandidate]]:
        by_branch: dict[str, list[ProjectBackportCandidate]] = {
            branch: [] for branch in release_branches
        }
        for item in self._iter_items():
            candidate = self._candidate_from_item(item, release_branches)
            if candidate:
                by_branch.setdefault(candidate.target_branch, []).append(candidate)
        return by_branch

    def _iter_items(self) -> list[dict[str, Any]]:
        owner_field = "user" if self._owner_type == "user" else "organization"
        query = _project_items_query(owner_field)
        cursor = None
        items: list[dict[str, Any]] = []
        while True:
            data = self._gql.execute(
                query,
                {"owner": self._owner, "number": self._number, "cursor": cursor},
            )
            project = (data.get(owner_field) or {}).get("projectV2")
            if not project:
                raise RuntimeError(f"Project {self._owner}/{self._number} not found")
            page = project.get("items") or {}
            items.extend(page.get("nodes") or [])
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return items
            cursor = page_info.get("endCursor")

    def _candidate_from_item(
        self,
        item: dict[str, Any],
        branches: list[str],
    ) -> ProjectBackportCandidate | None:
        content = item.get("content") or {}
        if content.get("__typename") != "PullRequest" or not content.get("merged"):
            return None

        item_repo = (content.get("repository") or {}).get("nameWithOwner")
        if item_repo and item_repo != self._source_repo:
            logger.debug(
                "Skipping project item PR #%s from %s (sweep target is %s)",
                content.get("number"),
                item_repo,
                self._source_repo,
            )
            return None

        fields = _extract_field_values(item)
        if not _field_has_value(fields, self._status_field, self._status_value):
            return None

        if self._implicit_target is not None:
            target_branch = self._implicit_target
        else:
            matched_branch = _matching_release_branch(
                fields,
                self._branch_fields,
                branches,
            )
            if not matched_branch:
                return None
            target_branch = matched_branch

        commits = [
            node.get("commit", {}).get("oid", "")
            for node in (content.get("commits", {}).get("nodes") or [])
        ]
        merge_sha = (content.get("mergeCommit") or {}).get("oid")
        return ProjectBackportCandidate(
            source_pr_number=int(content["number"]),
            source_pr_title=str(content.get("title") or ""),
            source_pr_url=str(content.get("url") or ""),
            target_branch=target_branch,
            merge_commit_sha=merge_sha,
            commit_shas=[sha for sha in commits if sha],
            merged_at=str(content.get("mergedAt") or ""),
        )


def run_backport_sweep(
    *,
    repo_entry: "RepoEntry",
    branch_entry: "BranchEntry",
    github_token: str,
    status_field: str = _DEFAULT_STATUS_FIELD,
    status_value: str = _DEFAULT_STATUS_VALUE,
    branch_fields: list[str] | None = None,
    test_commands_override: list[str] | None = None,
    discover_only: bool = False,
    max_candidates: int = 5,
) -> BranchSweepResult:
    repo_full_name = repo_entry.repo
    push_repo = repo_entry.effective_push_repo
    target_branch = branch_entry.branch
    test_commands = (
        test_commands_override
        if test_commands_override is not None
        else list(repo_entry.build_commands)
    )
    validation_setup_commands = (
        [] if test_commands_override is not None
        else list(repo_entry.validation_setup_commands)
    )
    validation_rules = (
        [] if test_commands_override is not None
        else list(repo_entry.validation_rules)
    )

    gh = Github(auth=Auth.Token(github_token))

    discovery = ProjectBackportDiscovery(
        GitHubGraphQLClient(github_token),
        project_owner=repo_entry.project_owner,
        project_number=branch_entry.project_number,
        source_repo=repo_full_name,
        project_owner_type=repo_entry.project_owner_type,
        status_field=status_field,
        status_value=status_value,
        branch_fields=branch_fields,
        implicit_target_branch=target_branch,
    )
    candidates = discovery.discover([target_branch]).get(target_branch, [])
    candidates.sort(key=lambda candidate: candidate.merged_at or "")

    if max_candidates > 0:
        logger.info(
            "Branch %s: %d candidate(s) found, will apply up to %d successful cherry-pick(s)",
            target_branch,
            len(candidates),
            max_candidates,
        )
    else:
        logger.info("Branch %s: %d candidate(s)", target_branch, len(candidates))

    if discover_only:
        for candidate in candidates:
            logger.info(
                "  PR #%d: %s (%s)",
                candidate.source_pr_number,
                candidate.source_pr_title,
                candidate.merge_commit_sha or "no merge sha",
            )
        result = BranchSweepResult(
            target_branch=target_branch,
            candidates_found=len(candidates),
        )
        emit_job_summary(build_summary([result]))
        return result

    if not candidates:
        result = BranchSweepResult(target_branch=target_branch)
        emit_job_summary(build_summary([result]))
        return result

    result = _process_branch(
        gh=gh,
        repo_full_name=repo_full_name,
        github_token=github_token,
        target_branch=target_branch,
        candidates=candidates,
        push_repo=push_repo,
        test_commands=test_commands,
        validation_setup_commands=validation_setup_commands,
        max_applied=max_candidates,
        language=repo_entry.language,
        build_commands=list(repo_entry.build_commands) or None,
        validation_rules=validation_rules,
        repair_validation_failures=repo_entry.repair_validation_failures,
    )
    emit_job_summary(build_summary([result]))
    return result


def _process_branch(
    *,
    gh: Any,
    repo_full_name: str,
    github_token: str,
    target_branch: str,
    candidates: list[ProjectBackportCandidate],
    push_repo: str,
    test_commands: list[str],
    validation_setup_commands: list[str] | None = None,
    max_applied: int = 0,
    language: str = "c",
    build_commands: list[str] | None = None,
    validation_rules: list[Any] | None = None,
    repair_validation_failures: bool = False,
) -> BranchSweepResult:
    result = BranchSweepResult(
        target_branch=target_branch,
        candidates_found=len(candidates),
    )
    tmpdir = tempfile.mkdtemp(prefix=f"backport-{safe_tmp_component(target_branch)}-")

    try:
        with GitAuth(github_token, prefix="backport-sweep-git-askpass-") as git_auth:
            git_env = git_auth.env()
            clone_target_branch(repo_full_name, target_branch, tmpdir, git_env)

            setup_ok, setup_output = run_test_commands(
                tmpdir,
                validation_setup_commands or [],
            )
            if not setup_ok:
                logger.warning(
                    "Validation setup failed for %s.\nOutput (last 4000 chars):\n%s",
                    target_branch,
                    setup_output[-4000:],
                )
                raise RuntimeError(
                    "validation setup failed: "
                    + (setup_output[:500] or "setup command failed")
                )

            if push_repo != repo_full_name:
                sync_target_branch_to_source(
                    gh,
                    push_repo,
                    repo_full_name,
                    target_branch,
                )

            backport_branch = f"{_BRANCH_PREFIX}/{target_branch}"
            existing_pr = find_existing_pr(
                gh,
                repo_full_name,
                push_repo,
                backport_branch,
            )

            if existing_pr:
                logger.info(
                    "Found existing PR #%d for %s, fetching branch...",
                    existing_pr.number,
                    target_branch,
                )
                push_url = github_https_url(push_repo)
                _run_git(tmpdir, "remote", "add", "push_target", push_url, env=git_env)
                _run_git(tmpdir, "fetch", "push_target", backport_branch, env=git_env)
                _run_git(tmpdir, "checkout", f"push_target/{backport_branch}")
                _run_git(tmpdir, "checkout", "-B", backport_branch)
                rebase_result = subprocess.run(
                    ["git", "rebase", f"origin/{target_branch}"],
                    cwd=tmpdir,
                    capture_output=True,
                    text=True,
                )
                if rebase_result.returncode != 0:
                    _run_git(tmpdir, "rebase", "--abort")
                    raise RuntimeError(
                        f"Could not rebase existing backport branch "
                        f"{backport_branch} onto origin/{target_branch}. "
                        f"The existing backport PR #{existing_pr.number} "
                        f"likely has conflicts with the refreshed release "
                        f"branch. Rebase manually or close the PR before "
                        f"the next sweep. Git stderr: "
                        f"{rebase_result.stderr.strip()[:300]}"
                    )
            else:
                delete_stale_backport_branch(gh, push_repo, backport_branch)
                _run_git(tmpdir, "checkout", "-b", backport_branch)
                push_url = github_https_url(push_repo)
                _run_git(tmpdir, "remote", "add", "push_target", push_url, env=git_env)

            already_applied = list_already_applied(
                tmpdir,
                target_branch,
                backport_branch,
            )
            logger.info("Already applied on %s: %s", backport_branch, already_applied)

            applied_count = 0

            for index, candidate in enumerate(candidates):
                if max_applied > 0 and applied_count >= max_applied:
                    logger.info(
                        "Branch %s: reached cap of %d applied backport(s); deferring remaining %d candidate(s) to next sweep",
                        target_branch,
                        max_applied,
                        len(candidates) - index,
                    )
                    break

                if str(candidate.source_pr_number) in already_applied:
                    result.results.append(
                        CandidateResult(
                            source_pr_number=candidate.source_pr_number,
                            source_pr_title=candidate.source_pr_title,
                            outcome="skipped-existing",
                            detail=DETAIL_ALREADY_ON_SWEEP_BRANCH,
                        )
                    )
                    continue

                candidate_result = apply_candidate(
                    tmpdir,
                    candidate,
                    repo_full_name,
                    git_env,
                    language=language,
                    build_commands=build_commands,
                    validation_rules=validation_rules,
                )
                result.results.append(candidate_result)

                if candidate_result.outcome != "applied":
                    continue

                # The sweep branch must stay green: only keep a cherry-pick if
                # the whole branch still validates. A red commit left on the
                # branch would block every later candidate, so we always reset
                # a failure off the branch and move on to the next candidate.
                ok, output = validate_branch_with_optional_repair(
                    tmpdir,
                    target_branch,
                    test_commands,
                    validation_rules or [],
                    repair=repair_validation_failures,
                    run_git=_run_git,
                )
                if not ok:
                    candidate_result.outcome = "skipped-validation-failed"
                    candidate_result.detail = validation_failure_detail(output)
                    _run_git(tmpdir, "reset", "--hard", "HEAD^")
                    logger.warning(
                        "Validation failed for candidate #%d on %s; removed candidate and continuing.",
                        candidate.source_pr_number,
                        target_branch,
                    )
                    continue

                applied_count += 1

            committed = [
                item for item in result.results
                if result_is_on_backport_branch(item)
            ]
            if committed and branch_has_changes(tmpdir, target_branch):
                try:
                    push_backport_branch(
                        tmpdir,
                        backport_branch,
                        git_env,
                        force_with_lease=existing_pr is not None,
                    )
                except Exception as exc:
                    for item in result.results:
                        if item.outcome == "applied":
                            item.outcome = "error"
                            item.detail = f"push failed: {exc}"
                    raise
                logger.info(
                    "Pushed %d commit(s) to %s/%s",
                    len(committed),
                    push_repo,
                    backport_branch,
                )

                result.pr_url = upsert_pr(
                    gh,
                    repo_full_name,
                    push_repo,
                    target_branch,
                    backport_branch,
                    result,
                    existing_pr,
                    gql=GitHubGraphQLClient(github_token),
                    branch_applied=list_applied_prs_on_branch(
                        tmpdir,
                        target_branch,
                        backport_branch,
                    ),
                )

    except Exception as exc:
        logger.exception("Error processing branch %s", target_branch)
        result.error = str(exc)
        result.results.append(
            CandidateResult(
                source_pr_number=0,
                source_pr_title=f"Branch {target_branch}",
                outcome="error",
                detail=str(exc),
            )
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return result


def _normalize(value: object) -> str:
    return str(value or "").strip().lower()


def _project_items_query(owner_field: str) -> str:
    return f"""
query($owner: String!, $number: Int!, $cursor: String) {{
  {owner_field}(login: $owner) {{
    projectV2(number: $number) {{
      items(first: 100, after: $cursor) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          content {{
            __typename
            ... on PullRequest {{
              number title url merged mergedAt
              repository {{ nameWithOwner }}
              mergeCommit {{ oid }}
              commits(first: 100) {{ nodes {{ commit {{ oid }} }} }}
            }}
          }}
          fieldValues(first: 50) {{
            nodes {{
              __typename
              ... on ProjectV2ItemFieldTextValue {{ text field {{ ... on ProjectV2FieldCommon {{ name }} }} }}
              ... on ProjectV2ItemFieldSingleSelectValue {{ name field {{ ... on ProjectV2FieldCommon {{ name }} }} }}
              ... on ProjectV2ItemFieldNumberValue {{ number field {{ ... on ProjectV2FieldCommon {{ name }} }} }}
              ... on ProjectV2ItemFieldIterationValue {{ title field {{ ... on ProjectV2FieldCommon {{ name }} }} }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""


def _extract_field_values(item: dict[str, Any]) -> dict[str, list[str]]:
    values: dict[str, list[str]] = defaultdict(list)
    for field_value in (item.get("fieldValues") or {}).get("nodes") or []:
        name = (field_value.get("field") or {}).get("name")
        if not name:
            continue
        values[_normalize(name)].extend(_field_value_strings(field_value))
    return dict(values)


def _field_value_strings(field_value: dict[str, Any]) -> list[str]:
    type_name = field_value.get("__typename")
    if type_name == "ProjectV2ItemFieldTextValue":
        return [str(field_value.get("text") or "")]
    if type_name == "ProjectV2ItemFieldSingleSelectValue":
        return [str(field_value.get("name") or "")]
    if type_name == "ProjectV2ItemFieldNumberValue":
        number = field_value.get("number")
        return [] if number is None else [str(number)]
    if type_name == "ProjectV2ItemFieldIterationValue":
        return [str(field_value.get("title") or "")]
    return []


def _field_has_value(
    fields: dict[str, list[str]],
    field_name: str,
    expected: str,
) -> bool:
    return any(
        _normalize(value) == _normalize(expected)
        for value in fields.get(_normalize(field_name), [])
    )


def _matching_release_branch(
    fields: dict[str, list[str]],
    branch_fields: list[str],
    branches: list[str],
) -> str | None:
    for field_name in branch_fields:
        values = fields.get(_normalize(field_name), [])
        for branch in branches:
            normalized_branch = _normalize(branch)
            if any(
                _normalize(value) == normalized_branch
                or _normalize(value) == f"backport {normalized_branch}"
                for value in values
            ):
                return branch
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        default="repos.yml",
        help="Path to registry YAML (default: repos.yml)",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Repository full name (must exist in registry)",
    )
    parser.add_argument(
        "--branch",
        required=True,
        help="Target branch (must exist in registry for this repo)",
    )
    parser.add_argument("--target-token", required=True)
    parser.add_argument("--status-field", default=_DEFAULT_STATUS_FIELD)
    parser.add_argument("--status-value", default=_DEFAULT_STATUS_VALUE)
    parser.add_argument("--branch-fields", default=",".join(_DEFAULT_BRANCH_FIELDS))
    parser.add_argument(
        "--test-commands",
        default="",
        help="Override test commands (newline-separated). Empty = use registry.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=5,
        help="Cap the number of applied cherry-picks per branch (0 = unlimited)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    from scripts.backport.registry import load_registry

    registry = load_registry(args.registry)
    repo_entry, branch_entry = registry.get_branch(args.repo, args.branch)

    test_commands_override = None
    if args.test_commands:
        test_commands_override = [
            command.strip()
            for command in args.test_commands.split("\n")
            if command.strip()
        ]

    result = run_backport_sweep(
        repo_entry=repo_entry,
        branch_entry=branch_entry,
        github_token=args.target_token,
        status_field=args.status_field,
        status_value=args.status_value,
        branch_fields=[
            field.strip()
            for field in args.branch_fields.split(",")
            if field.strip()
        ] or None,
        test_commands_override=test_commands_override,
        discover_only=args.discover_only or args.dry_run,
        max_candidates=args.max_candidates,
    )

    print(json.dumps({
        "branch": result.target_branch,
        "found": result.candidates_found,
        "applied": sum(1 for item in result.results if item.outcome == "applied"),
        "pr": result.pr_url,
    }, indent=2))

    if args.discover_only or args.dry_run:
        return

    if result.error:
        logger.error(
            "Backport sweep failure: %s: %s",
            result.target_branch,
            result.error,
        )
        sys.exit(1)

    if result.candidates_found > 0 and result.results:
        errored = [item for item in result.results if item.outcome == "error"]
        if len(errored) == len(result.results):
            logger.error(
                "Backport sweep failure: %s: all %d candidates errored",
                result.target_branch,
                len(errored),
            )
            sys.exit(1)


if __name__ == "__main__":
    main()

