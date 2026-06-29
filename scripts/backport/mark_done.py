"""Mark project-board backport items done once the backport actually lands.

``reconcile_project_board`` lists every board item still in "To be backported",
verifies each source PR actually has a commit on the target branch, and flips
only the verified ones. It runs from the scheduled poller and is self-healing:
it reconciles the whole board against branch reality on every run, so it does
not depend on any merge hook firing.

Done is gated on the branch genuinely containing the source PR's commit (the
same ``(#<pr>)`` signal the sweep uses to skip already-applied PRs), so a
backport PR body that merely *claims* a PR was applied can never mark it Done
on its own.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any

from scripts.backport.sweep_graphql import GitHubGraphQLClient
from scripts.backport.utils import pr_numbers_from_commit_subjects
from scripts.common.git_auth import GitAuth, github_https_url
from scripts.common.polling import (
    add_poll_loop_args,
    format_poll_results,
    run_poll_loop_from_args,
)

logger = logging.getLogger(__name__)

_DEFAULT_STATUS_FIELD = "Status"
_DEFAULT_FROM_STATUS = "To be backported"
_DEFAULT_DONE_STATUS = "Done"

# Depth of the shallow verification clone. A release branch accumulates backports
# steadily, so a few thousand commits comfortably covers any PR still sitting in
# "To be backported" since the branch was cut.
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


def verify_prs_on_branch(
    repo_full_name: str,
    target_branch: str,
    pr_numbers: set[int],
    *,
    token: str = "",
    git_env: dict[str, str] | None = None,
) -> set[int]:
    """Return which of ``pr_numbers`` actually landed on ``target_branch``.

    A PR is considered present if either:

    * a commit on the branch carries the PR's trailing ``(#N)`` in its subject
      (a cherry-pick that kept the source PR's title), or
    * a backport commit on the branch lists the PR in an ``## Applied`` table
      in its body. The sweep squash-merges a batch of cherry-picks into one
      commit whose subject is the *backport* PR; the source PRs it carried are
      only recoverable from that ``## Applied`` table.

    Subject matching uses the trailing ``(#N)`` only; body matching reads only
    the structured ``## Applied`` section, so a stray ``(#N)`` reference in a
    ``## Needs attention`` row or in prose never counts.

    ``token`` authenticates the clone for private/auth-required repos.
    """
    if not pr_numbers:
        return set()

    env = dict(os.environ if git_env is None else git_env)
    with GitAuth(token, prefix="mark-done-git-askpass-") as git_auth:
        env = git_auth.env(env)
        with tempfile.TemporaryDirectory(prefix="mark-done-verify-") as tmp:
            repo_dir = os.path.join(tmp, "repo")
            _shallow_clone(repo_full_name, target_branch, repo_dir, env)

            applied = pr_numbers_from_commit_subjects(_branch_commit_subjects(repo_dir))
            applied |= _applied_prs_from_commit_bodies(repo_dir)

    return pr_numbers & applied


def _branch_commit_subjects(repo_dir: str) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%s", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


# git log -z NUL-separates commit records, letting us split multi-line bodies.
_COMMIT_RECORD_DELIM = "\x00"


def _applied_prs_from_commit_bodies(repo_dir: str) -> set[int]:
    """Source PR numbers listed in ``## Applied`` tables of backport commits.

    Squash-merged backport sweeps record the cherry-picked source PRs only in
    the commit body's ``## Applied`` section. Only that section's table cells
    are read, so a ``(#N)`` in a later ``## Needs attention`` row or in prose is
    never treated as applied.
    """
    result = subprocess.run(
        ["git", "log", "-z", "--format=%B", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    numbers: set[int] = set()
    for message in result.stdout.split(_COMMIT_RECORD_DELIM):
        applied_section = _markdown_section(message, "Applied")
        if applied_section:
            numbers.update(_pr_numbers_from_table_cells(applied_section))
    return numbers


def _shallow_clone(
    repo_full_name: str, target_branch: str, dest_dir: str, git_env: dict[str, str]
) -> None:
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
    verified_pr_numbers: set[int],
    project: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> BackportStatusUpdateResult:
    """Flip board items for ``source_pr_numbers`` from ``from_status`` to Done.

    Only PRs in ``verified_pr_numbers`` are eligible to be marked Done; the rest
    are recorded as ``unverified`` and left as-is.

    ``project`` may be a board already loaded by the caller (e.g. the poller),
    to avoid re-fetching it.

    When ``dry_run`` is true, no mutation is sent; ``updated`` lists the items
    that *would* be marked Done.
    """
    requested = sorted(set(source_pr_numbers))
    result = BackportStatusUpdateResult(requested=requested)
    if not requested:
        return result

    if project is None:
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
        if number not in verified_pr_numbers:
            result.unverified.append(number)
            continue

        if not dry_run:
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
    token: str = "",
    git_env: dict[str, str] | None = None,
    dry_run: bool = False,
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

    candidate_pr_numbers: set[int] = set()
    for item in project["items"]:
        content = item.get("content") or {}
        if content.get("__typename") != "PullRequest":
            continue
        if (content.get("repository") or {}).get("nameWithOwner") != source_repo:
            continue
        if _normalize(_item_single_select_value(item, status_field)) != _normalize(from_status):
            continue
        number = content.get("number")
        if not isinstance(number, int):
            continue
        candidate_pr_numbers.add(number)

    if not candidate_pr_numbers:
        return BackportStatusUpdateResult(requested=[])

    verified = verify_prs_on_branch(
        source_repo, target_branch, candidate_pr_numbers, token=token, git_env=git_env
    )
    logger.info(
        "Branch %s: %d candidate(s) in %r, %d verified present",
        target_branch, len(candidate_pr_numbers), from_status, len(verified),
    )

    return mark_backport_items_done(
        gql,
        project_owner=project_owner,
        project_number=project_number,
        source_repo=source_repo,
        source_pr_numbers=sorted(candidate_pr_numbers),
        project_owner_type=project_owner_type,
        status_field=status_field,
        from_status=from_status,
        done_status=done_status,
        verified_pr_numbers=verified,
        project=project,
        dry_run=dry_run,
    )


def _markdown_section(body: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?ims)^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)"
    )
    match = pattern.search(body)
    return match.group(1) if match else ""


def _pr_numbers_from_table_cells(markdown: str) -> set[int]:
    """Source PR numbers from the ``Source PR`` column of a markdown table.

    Only the ``Source PR`` column is read, so a ``#N`` appearing in a Title or
    Detail cell (e.g. a revert subject, or a "depends on #N" note) is never
    counted. Cells whose text wraps across newlines are reassembled first: a
    logical row begins at a line starting with ``|`` and absorbs the lines that
    follow until the next row. When no ``Source PR`` header is present the first
    column is used, since the sweep always lists the source PR first.
    """
    rows: list[str] = []
    for line in markdown.splitlines():
        if line.lstrip().startswith("|"):
            rows.append(line)
        elif rows:
            rows[-1] += " " + line.strip()

    pr_cell = re.compile(r"^(?:\[)?#(\d+)(?:\]\([^)]*\))?$")
    column: int | None = None
    numbers: set[int] = set()
    for row in rows:
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        if column is None:
            for index, cell in enumerate(cells):
                if _normalize(cell) == "source pr":
                    column = index
                    break
            else:
                column = 0  # no header row; sweep lists the source PR first
            if any(_normalize(cell) == "source pr" for cell in cells):
                continue  # consumed the header row itself
        if all(set(cell) <= set("-: ") for cell in cells if cell):
            continue  # separator row (|---|---|)
        if column < len(cells):
            match = pr_cell.match(cells[column])
            if match:
                numbers.add(int(match.group(1)))
    return numbers


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
        "--target-branch",
        help="Release branch to reconcile. Omit to reconcile every branch "
        "configured for the repo.",
    )
    parser.add_argument("--target-token", required=True)
    parser.add_argument("--status-field", default=_DEFAULT_STATUS_FIELD)
    parser.add_argument("--from-status", default=_DEFAULT_FROM_STATUS)
    parser.add_argument("--done-status", default=_DEFAULT_DONE_STATUS)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be marked Done without mutating the board.",
    )
    parser.add_argument("--verbose", action="store_true")
    add_poll_loop_args(parser)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    from scripts.backport.registry import load_registry

    registry = load_registry(args.registry)
    gql = GitHubGraphQLClient(args.target_token)

    def _poll() -> dict[str, Any]:
        return _run_poll(
            registry, gql, repo=args.repo, target_branch=args.target_branch,
            status_field=args.status_field, from_status=args.from_status,
            done_status=args.done_status, token=args.target_token, dry_run=args.dry_run,
        )

    results = run_poll_loop_from_args(
        _poll,
        args,
        logger=logger,
    )
    print(json.dumps(format_poll_results(results), indent=2))


def _run_poll(
    registry: Any,
    gql: GitHubGraphQLClient,
    *,
    repo: str,
    target_branch: str | None,
    status_field: str,
    from_status: str,
    done_status: str,
    token: str = "",
    dry_run: bool = False,
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
            token=token,
            dry_run=dry_run,
        )
        out[branch_entry.branch] = result.as_dict()
    return out


if __name__ == "__main__":
    main()
