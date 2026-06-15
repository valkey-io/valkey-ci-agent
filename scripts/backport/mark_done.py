"""Mark project-board backport items done once the backport actually lands.

Two entry points share the same status-mutation core:

* ``mark_backport_items_done`` — given an explicit set of source PR numbers
  (parsed from a merged backport PR body / head ref), flip the matching board
  items to Done. Used by the merge-triggered workflow.
* ``reconcile_project_board`` — list every board item still in
  "To be backported", verify each source PR actually has a commit on the
  target branch, and flip only the verified ones. Used by the scheduled
  poller, which is self-healing: it does not depend on a merge hook firing.

Both gate Done on the branch genuinely containing the source PR's commit
(the same ``(#<pr>)`` signal the sweep uses to skip already-applied PRs), so a
backport PR body that merely *claims* a PR was applied can never mark it Done
on its own.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.backport.sweep_graphql import GitHubGraphQLClient

logger = logging.getLogger(__name__)

_DEFAULT_STATUS_FIELD = "Status"
_DEFAULT_FROM_STATUS = "To be backported"
_DEFAULT_DONE_STATUS = "Done"

# How many commits of branch history to fetch when verifying presence. A release
# branch accumulates backports steadily; a few thousand commits comfortably
# covers any PR still sitting in "To be backported".
_VERIFY_CLONE_DEPTH = 5000


@dataclass
class BackportStatusUpdateResult:
    requested: list[int]
    updated: list[int] = field(default_factory=list)
    already_done: list[int] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)
    skipped: dict[int, str] = field(default_factory=dict)
    unverified: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "updated": self.updated,
            "already_done": self.already_done,
            "missing": self.missing,
            "skipped": {str(k): v for k, v in sorted(self.skipped.items())},
            "unverified": self.unverified,
        }


def parse_backport_source_pr_numbers(
    body: str,
    *,
    head_ref: str = "",
) -> list[int]:
    """Extract source PR numbers from backport PR body text.

    Sweep PRs may contain failed candidates in a later "Needs attention"
    section, so only the "Applied" section is authoritative for that format.
    Manual single-PR backports use a "Source PR" summary row.
    """
    numbers: set[int] = set()

    applied_section = _markdown_section(body, "Applied")
    if applied_section:
        numbers.update(_pr_numbers_from_table_cells(applied_section))

    numbers.update(
        int(match.group(1))
        for match in re.finditer(
            r"(?im)^\|\s*Source PR\s*\|\s*(?:\[)?#(\d+)(?:\]\([^)]*\))?\s*\|",
            body,
        )
    )

    branch_match = re.search(r"(?:^|/)backport/(\d+)-to-[A-Za-z0-9._/-]+$", head_ref)
    if branch_match:
        numbers.add(int(branch_match.group(1)))

    return sorted(numbers)


def _pr_numbers_on_branch(repo_dir: str, *, max_count: int = _VERIFY_CLONE_DEPTH) -> set[int]:
    """Return source PR numbers referenced by ``(#N)`` in the branch's commits.

    This is the same signal the sweep uses to detect already-applied PRs, so
    mark-done and the sweep agree on what "present on the branch" means. A
    squash-merged backport sweep keeps each cherry-picked commit's original
    ``... (#N)`` subject, and manual cherry-picks keep it via ``-x``/the merge
    title, so the source PR number is recoverable from history.
    """
    import subprocess

    result = subprocess.run(
        ["git", "log", f"--max-count={max_count}", "--format=%s%n%b", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return {int(match.group(1)) for match in re.finditer(r"\(#(\d+)\)", result.stdout)}


def verify_prs_on_branch(
    repo_full_name: str,
    target_branch: str,
    pr_numbers: list[int],
    *,
    git_env: dict[str, str] | None = None,
) -> set[int]:
    """Shallow-clone ``target_branch`` and return which ``pr_numbers`` are present.

    Presence is decided by a ``(#N)`` reference in the branch's commit history.
    Returns the subset of ``pr_numbers`` that are genuinely on the branch.
    """
    wanted = set(pr_numbers)
    if not wanted:
        return set()

    env = dict(os.environ if git_env is None else git_env)
    with tempfile.TemporaryDirectory(prefix="mark-done-verify-") as tmp:
        repo_dir = os.path.join(tmp, "repo")
        _shallow_clone(repo_full_name, target_branch, repo_dir, env)
        present = _pr_numbers_on_branch(repo_dir)
    return wanted & present


def _shallow_clone(
    repo_full_name: str, target_branch: str, dest_dir: str, git_env: dict[str, str]
) -> None:
    import subprocess

    from scripts.common.git_auth import github_https_url

    subprocess.run(
        [
            "git", "clone",
            "--branch", target_branch,
            "--single-branch",
            f"--depth={_VERIFY_CLONE_DEPTH}",
            github_https_url(repo_full_name),
            dest_dir,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=git_env,
    )


def mark_backport_items_done(
    gql: GitHubGraphQLClient,
    *,
    project_owner: str,
    project_number: int,
    source_repo: str,
    source_pr_numbers: list[int],
    project_owner_type: str = "organization",
    status_field: str = _DEFAULT_STATUS_FIELD,
    from_status: str = _DEFAULT_FROM_STATUS,
    done_status: str = _DEFAULT_DONE_STATUS,
    verified_pr_numbers: set[int] | None = None,
) -> BackportStatusUpdateResult:
    """Flip board items for ``source_pr_numbers`` from ``from_status`` to Done.

    When ``verified_pr_numbers`` is provided, only PRs in that set are eligible
    to be marked Done; the rest are recorded as ``unverified`` and left as-is.
    Passing ``None`` keeps the legacy unverified behaviour for callers that have
    already established presence some other way.
    """
    requested = sorted(set(source_pr_numbers))
    result = BackportStatusUpdateResult(requested=requested)
    if not requested:
        return result

    project = _load_project(
        gql,
        project_owner=project_owner,
        project_number=project_number,
        project_owner_type=project_owner_type,
    )
    status_field_id, done_option_id = _find_status_field_and_option(
        project["fields"],
        status_field=status_field,
        done_status=done_status,
    )

    found: set[int] = set()
    requested_set = set(requested)
    for item in project["items"]:
        content = item.get("content") or {}
        if content.get("__typename") != "PullRequest":
            continue
        repo = (content.get("repository") or {}).get("nameWithOwner")
        number = content.get("number")
        if repo != source_repo or number not in requested_set:
            continue

        found.add(number)
        current_status = _item_single_select_value(item, status_field)
        if _normalize(current_status) == _normalize(done_status):
            result.already_done.append(number)
            continue
        if _normalize(current_status) != _normalize(from_status):
            result.skipped[number] = (
                f"{status_field} is {current_status!r}, not {from_status!r}"
            )
            continue
        if verified_pr_numbers is not None and number not in verified_pr_numbers:
            result.unverified.append(number)
            continue

        _set_project_item_status(
            gql,
            project_id=project["id"],
            item_id=item["id"],
            field_id=status_field_id,
            option_id=done_option_id,
        )
        result.updated.append(number)

    result.missing = sorted(requested_set - found)
    result.updated = sorted(set(result.updated))
    result.already_done = sorted(set(result.already_done))
    result.unverified = sorted(set(result.unverified))
    return result


def reconcile_project_board(
    gql: GitHubGraphQLClient,
    *,
    project_owner: str,
    project_number: int,
    source_repo: str,
    target_branch: str,
    project_owner_type: str = "organization",
    status_field: str = _DEFAULT_STATUS_FIELD,
    from_status: str = _DEFAULT_FROM_STATUS,
    done_status: str = _DEFAULT_DONE_STATUS,
    git_env: dict[str, str] | None = None,
) -> BackportStatusUpdateResult:
    """Self-healing reconcile: mark Done every "To be backported" item that is
    genuinely on ``target_branch``.

    Unlike :func:`mark_backport_items_done`, this does not need a merged-PR body
    or a merge hook. It scans the board, clones the branch once, verifies each
    candidate by ``(#N)`` presence, and flips only the verified items. Items not
    yet on the branch are recorded as ``unverified`` and left untouched so a
    later run can pick them up.
    """
    project = _load_project(
        gql,
        project_owner=project_owner,
        project_number=project_number,
        project_owner_type=project_owner_type,
    )

    candidates: list[int] = []
    for item in project["items"]:
        content = item.get("content") or {}
        if content.get("__typename") != "PullRequest":
            continue
        if (content.get("repository") or {}).get("nameWithOwner") != source_repo:
            continue
        if _normalize(_item_single_select_value(item, status_field)) != _normalize(from_status):
            continue
        number = content.get("number")
        if isinstance(number, int):
            candidates.append(number)

    if not candidates:
        return BackportStatusUpdateResult(requested=[])

    verified = verify_prs_on_branch(
        source_repo, target_branch, candidates, git_env=git_env
    )
    logger.info(
        "Branch %s: %d candidate(s) in %r, %d verified present",
        target_branch, len(candidates), from_status, len(verified),
    )

    return mark_backport_items_done(
        gql,
        project_owner=project_owner,
        project_number=project_number,
        source_repo=source_repo,
        source_pr_numbers=candidates,
        project_owner_type=project_owner_type,
        status_field=status_field,
        from_status=from_status,
        done_status=done_status,
        verified_pr_numbers=verified,
    )


def _markdown_section(body: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?ims)^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)"
    )
    match = pattern.search(body)
    return match.group(1) if match else ""


def _pr_numbers_from_table_cells(markdown: str) -> set[int]:
    return {
        int(match.group(1))
        for match in re.finditer(
            r"\|\s*(?:\[)?#(\d+)(?:\]\([^)]*\))?\s*\|",
            markdown,
        )
    }


def _normalize(value: object) -> str:
    return str(value or "").strip().lower()


def _load_project(
    gql: GitHubGraphQLClient,
    *,
    project_owner: str,
    project_number: int,
    project_owner_type: str,
) -> dict[str, Any]:
    owner_field = "user" if project_owner_type == "user" else "organization"
    query = _project_query(owner_field)
    cursor = None
    project_id = ""
    fields: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []

    while True:
        data = gql.execute(
            query,
            {"owner": project_owner, "number": project_number, "cursor": cursor},
        )
        project = (data.get(owner_field) or {}).get("projectV2")
        if not project:
            raise RuntimeError(f"Project {project_owner}/{project_number} not found")

        project_id = project_id or str(project.get("id") or "")
        if not fields:
            fields = (project.get("fields") or {}).get("nodes") or []

        page = project.get("items") or {}
        items.extend(page.get("nodes") or [])
        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    if not project_id:
        raise RuntimeError(f"Project {project_owner}/{project_number} has no id")
    return {"id": project_id, "fields": fields, "items": items}


def _find_status_field_and_option(
    fields: list[dict[str, Any]],
    *,
    status_field: str,
    done_status: str,
) -> tuple[str, str]:
    for field_node in fields:
        if (
            field_node.get("__typename") != "ProjectV2SingleSelectField"
            or _normalize(field_node.get("name")) != _normalize(status_field)
        ):
            continue
        field_id = str(field_node.get("id") or "")
        for option in field_node.get("options") or []:
            if _normalize(option.get("name")) == _normalize(done_status):
                option_id = str(option.get("id") or "")
                if field_id and option_id:
                    return field_id, option_id
        raise RuntimeError(
            f"Project status field {status_field!r} has no {done_status!r} option"
        )
    raise RuntimeError(f"Project has no single-select status field {status_field!r}")


def _item_single_select_value(item: dict[str, Any], field_name: str) -> str:
    for field_value in (item.get("fieldValues") or {}).get("nodes") or []:
        if field_value.get("__typename") != "ProjectV2ItemFieldSingleSelectValue":
            continue
        if _normalize((field_value.get("field") or {}).get("name")) == _normalize(field_name):
            return str(field_value.get("name") or "")
    return ""


def _set_project_item_status(
    gql: GitHubGraphQLClient,
    *,
    project_id: str,
    item_id: str,
    field_id: str,
    option_id: str,
) -> None:
    mutation = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId
    itemId: $itemId
    fieldId: $fieldId
    value: { singleSelectOptionId: $optionId }
  }) {
    projectV2Item { id }
  }
}
"""
    gql.execute(
        mutation,
        {
            "projectId": project_id,
            "itemId": item_id,
            "fieldId": field_id,
            "optionId": option_id,
        },
    )


def _project_query(owner_field: str) -> str:
    return f"""
query($owner: String!, $number: Int!, $cursor: String) {{
  {owner_field}(login: $owner) {{
    projectV2(number: $number) {{
      id
      fields(first: 100) {{
        nodes {{
          __typename
          ... on ProjectV2SingleSelectField {{
            id
            name
            options {{ id name }}
          }}
        }}
      }}
      items(first: 100, after: $cursor) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          id
          content {{
            __typename
            ... on PullRequest {{
              number
              repository {{ nameWithOwner }}
            }}
          }}
          fieldValues(first: 50) {{
            nodes {{
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue {{
                name
                field {{ ... on ProjectV2FieldCommon {{ name }} }}
              }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="repos.yml")
    parser.add_argument("--repo", required=True)
    parser.add_argument(
        "--mode",
        choices=("merge", "poll"),
        default="merge",
        help="merge: mark Done the source PRs from a merged backport PR "
        "(verified against the branch). poll: reconcile every "
        "'To be backported' item against the branch.",
    )
    parser.add_argument(
        "--target-branch",
        help="Release branch. Required for merge mode; in poll mode, omit to "
        "reconcile every branch configured for the repo.",
    )
    parser.add_argument("--target-token", required=True)
    parser.add_argument("--body", default="")
    parser.add_argument("--body-file", default="")
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--source-pr-number", action="append", type=int, default=[])
    parser.add_argument("--status-field", default=_DEFAULT_STATUS_FIELD)
    parser.add_argument("--from-status", default=_DEFAULT_FROM_STATUS)
    parser.add_argument("--done-status", default=_DEFAULT_DONE_STATUS)
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="merge mode only: skip branch-presence verification (legacy "
        "behaviour; trusts the PR body).",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    from scripts.backport.registry import load_registry

    registry = load_registry(args.registry)
    gql = GitHubGraphQLClient(args.target_token)

    if args.mode == "poll":
        results = _run_poll(
            registry, gql, repo=args.repo, target_branch=args.target_branch,
            status_field=args.status_field, from_status=args.from_status,
            done_status=args.done_status,
        )
        print(json.dumps(results, indent=2))
        return

    if not args.target_branch:
        parser.error("--target-branch is required in merge mode")

    body = args.body
    if args.body_file:
        body = sys.stdin.read() if args.body_file == "-" else Path(args.body_file).read_text(encoding="utf-8")

    source_pr_numbers = sorted(
        set(args.source_pr_number)
        | set(parse_backport_source_pr_numbers(body, head_ref=args.head_ref))
    )
    if not source_pr_numbers:
        print(json.dumps(BackportStatusUpdateResult(requested=[]).as_dict(), indent=2))
        return

    repo_entry, branch_entry = registry.get_branch(args.repo, args.target_branch)

    verified: set[int] | None
    if args.no_verify:
        verified = None
    else:
        verified = verify_prs_on_branch(
            repo_entry.repo, branch_entry.branch, source_pr_numbers
        )

    result = mark_backport_items_done(
        gql,
        project_owner=repo_entry.project_owner,
        project_number=branch_entry.project_number,
        source_repo=repo_entry.repo,
        source_pr_numbers=source_pr_numbers,
        project_owner_type=repo_entry.project_owner_type,
        status_field=args.status_field,
        from_status=args.from_status,
        done_status=args.done_status,
        verified_pr_numbers=verified,
    )
    print(json.dumps(result.as_dict(), indent=2))


def _run_poll(
    registry: Any,
    gql: GitHubGraphQLClient,
    *,
    repo: str,
    target_branch: str | None,
    status_field: str,
    from_status: str,
    done_status: str,
) -> dict[str, Any]:
    repo_entry = registry.get_repo(repo)
    if target_branch:
        branches = [registry.get_branch(repo, target_branch)[1]]
    else:
        branches = list(repo_entry.branches)

    out: dict[str, Any] = {}
    for branch_entry in branches:
        result = reconcile_project_board(
            gql,
            project_owner=repo_entry.project_owner,
            project_number=branch_entry.project_number,
            source_repo=repo_entry.repo,
            target_branch=branch_entry.branch,
            project_owner_type=repo_entry.project_owner_type,
            status_field=status_field,
            from_status=from_status,
            done_status=done_status,
        )
        out[branch_entry.branch] = result.as_dict()
    return out


if __name__ == "__main__":
    main()
