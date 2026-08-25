"""Shared contracts for release-note review polling and handling."""

from __future__ import annotations

import hashlib
import json
import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from scripts.backport.sweep_graphql import GitHubGraphQLClient
from scripts.common.github_client import retry_github_call
from scripts.common.identity import APP_LOGIN
from scripts.release_notes import projects
from scripts.release_notes import release_format as rn

REPOSITORY_NAMES = ("valkey", "valkey-search", "valkey-json", "valkey-bloom")
REPOSITORIES = tuple(f"valkey-io/{name}" for name in REPOSITORY_NAMES)

_BRANCH_RE = re.compile(
    r"^agent/release-cut/"
    r"(?P<version>\d+\.\d+\.\d+)-(?P<stage>ga|rc[1-9]\d*)$"
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MARKER_RE = re.compile(
    r"<!-- valkey-ci-agent:release-review:"
    r"(?P<status>addressing|addressed|refused|failed):"
    r"(?P<head>[0-9a-f]{40}):(?P<batch>[0-9a-f]{16}):"
    r"(?P<count>[1-9]\d*) -->"
)
_STATE_RE = re.compile(
    r"<!-- valkey-ci-agent:release-review-state:(?P<payload>[A-Za-z0-9_-]+) -->"
)
_CONTRIBUTORS_RE = re.compile(r"^###\s+Contributors\s*$", re.MULTILINE)

_THREAD_LIMIT = 100
_COMMENT_LIMIT = 100
_BATCH_LIMIT = 50
_COMMENT_CHARS = 12_000
_CONTEXT_CHARS = 8_000
_DIFF_CHARS = 6_000
_PAYLOAD_CHARS = 100_000
_NOTES_CHARS = 2_000_000


class ReviewRefused(ValueError):
    """The request no longer satisfies the release-review contract."""


@dataclass(frozen=True)
class ReviewRequest:
    repo_name: str
    repo_full_name: str
    pr_number: int
    head_sha: str
    status_comment_id: int


@dataclass(frozen=True)
class ReleasePR:
    repo_full_name: str
    number: int
    head_sha: str
    head_branch: str
    version: str
    stage: str
    notes_path: str
    version_path: str
    profile: projects.ProjectProfile


@dataclass(frozen=True)
class ReviewComment:
    database_id: int
    body: str
    created_at: str
    diff_hunk: str
    author_login: str
    author_type: str

    @property
    def is_human(self) -> bool:
        return (
            self.author_type == "User"
            and bool(self.author_login)
            and not self.author_login.endswith("[bot]")
        )


@dataclass(frozen=True)
class ReviewThread:
    node_id: str
    resolved: bool
    outdated: bool
    path: str
    line: int | None
    comments: tuple[ReviewComment, ...]

    @property
    def root_comment_id(self) -> int:
        return self.comments[0].database_id

    def latest_human_comment(self) -> ReviewComment | None:
        humans = [comment for comment in self.comments if comment.is_human]
        return max(
            humans,
            key=lambda comment: (comment.created_at, comment.database_id),
            default=None,
        )


@dataclass(frozen=True)
class SelectedReview:
    thread: ReviewThread
    comment: ReviewComment


@dataclass(frozen=True)
class ReviewTarget:
    node_id: str
    root_comment_id: int
    selected_comment_id: int
    selected_comment_sha256: str


@dataclass(frozen=True)
class BatchMarker:
    status: str
    head_sha: str
    batch_id: str
    count: int
    commit_sha: str = ""
    targets: tuple[ReviewTarget, ...] = ()


def parse_request(
    repo_name: str,
    pr_number: str,
    head_sha: str,
    status_comment_id: str,
) -> ReviewRequest:
    repo_name = repo_name.strip()
    if repo_name not in REPOSITORY_NAMES:
        raise ReviewRefused(f"unsupported repository: {repo_name!r}")
    if not re.fullmatch(r"[1-9]\d*", pr_number):
        raise ReviewRefused(f"invalid pull request number: {pr_number!r}")
    if not _SHA_RE.fullmatch(head_sha):
        raise ReviewRefused("head SHA must contain 40 lowercase hex characters")
    if not re.fullmatch(r"[1-9]\d*", status_comment_id):
        raise ReviewRefused(f"invalid status comment id: {status_comment_id!r}")
    return ReviewRequest(
        repo_name=repo_name,
        repo_full_name=f"valkey-io/{repo_name}",
        pr_number=int(pr_number),
        head_sha=head_sha,
        status_comment_id=int(status_comment_id),
    )


def validate_release_pr(pr: Any, repo_full_name: str) -> ReleasePR:
    """Validate the small set of facts that identify an automated release PR."""

    if repo_full_name not in REPOSITORIES:
        raise ReviewRefused(f"unsupported repository: {repo_full_name}")
    profile = projects.profile_for(repo_full_name)
    if str(getattr(pr, "state", "")).lower() != "open":
        raise ReviewRefused("release PR is not open")
    if getattr(getattr(pr, "user", None), "login", "") != f"{APP_LOGIN}[bot]":
        raise ReviewRefused("release PR was not opened by the release bot")

    head = getattr(pr, "head", None)
    head_repo = getattr(getattr(head, "repo", None), "full_name", "")
    head_sha = str(getattr(head, "sha", "")).lower()
    head_branch = str(getattr(head, "ref", ""))
    if head_repo != repo_full_name or not _SHA_RE.fullmatch(head_sha):
        raise ReviewRefused("release PR has an unexpected head repository or SHA")
    match = _BRANCH_RE.fullmatch(head_branch)
    if match is None:
        raise ReviewRefused("release PR does not use an agent/release-cut branch")

    version = match.group("version")
    stage = match.group("stage")
    major, minor, _patch = rn.parse_version(version)
    if getattr(getattr(pr, "base", None), "ref", "") != f"{major}.{minor}":
        raise ReviewRefused("release PR targets the wrong release line")

    files = list(
        retry_github_call(
            lambda: pr.get_files(),
            retries=3,
            description=f"list files for {repo_full_name}#{pr.number}",
        )
    )
    paths = [str(getattr(item, "filename", "")) for item in files]
    allowed = {profile.notes_file, profile.bumper.version_file}
    if (
        profile.notes_file not in paths
        or not set(paths).issubset(allowed)
        or len(paths) != len(set(paths))
        or any(getattr(item, "status", "") != "modified" for item in files)
    ):
        raise ReviewRefused("release PR changed unexpected paths")

    number = getattr(pr, "number", 0)
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ReviewRefused("release PR has an invalid number")
    return ReleasePR(
        repo_full_name=repo_full_name,
        number=number,
        head_sha=head_sha,
        head_branch=head_branch,
        version=version,
        stage=stage,
        notes_path=profile.notes_file,
        version_path=profile.bumper.version_file,
        profile=profile,
    )


def status_body(
    status: str,
    head_sha: str,
    batch_id: str,
    count: int,
    *,
    reason: str = "",
    commit_sha: str = "",
    repo_full_name: str = "",
    targets: Iterable[ReviewTarget] = (),
) -> str:
    if status not in {"addressing", "addressed", "refused", "failed"}:
        raise ReviewRefused(f"invalid review status: {status}")
    if (
        not _SHA_RE.fullmatch(head_sha)
        or not re.fullmatch(r"[0-9a-f]{16}", batch_id)
        or count < 1
    ):
        raise ReviewRefused("invalid release-review status values")
    noun = "comment" if count == 1 else "comments"
    if status == "addressing":
        visible = f"Addressing {count} release-note review {noun}."
    elif status == "addressed":
        if not _SHA_RE.fullmatch(commit_sha):
            raise ReviewRefused("addressed status requires a commit SHA")
        url = f"https://github.com/{repo_full_name}/commit/{commit_sha}"
        visible = (
            f"Addressed {count} release-note review {noun} in "
            f"[`{commit_sha[:12]}`]({url})."
        )
    else:
        detail = " ".join((reason or "unknown error").split())[:500].rstrip(".")
        prefix = "Could not safely address" if status == "refused" else "Failed to address"
        visible = f"{prefix} {count} release-note review {noun}: {detail}."
    marker = (
        f"<!-- valkey-ci-agent:release-review:"
        f"{status}:{head_sha}:{batch_id}:{count} -->"
    )
    target_tuple = tuple(targets)
    state = _encode_marker_state(commit_sha, target_tuple)
    return f"{visible}\n\n{marker}\n{state}"


def parse_status(comment: Any, bot_login: str) -> BatchMarker | None:
    if getattr(getattr(comment, "user", None), "login", "") != bot_login:
        return None
    matches = _MARKER_RE.findall(str(getattr(comment, "body", "")))
    if len(matches) != 1:
        return None
    status, head_sha, batch_id, count = matches[0]
    state_matches = _STATE_RE.findall(str(getattr(comment, "body", "")))
    if len(state_matches) > 1:
        return None
    try:
        commit_sha, targets = (
            _decode_marker_state(state_matches[0])
            if state_matches
            else ("", ())
        )
    except (
        BinasciiError,
        ReviewRefused,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        return None
    return BatchMarker(
        status=status,
        head_sha=head_sha,
        batch_id=batch_id,
        count=int(count),
        commit_sha=commit_sha,
        targets=targets,
    )


def _encode_marker_state(
    commit_sha: str,
    targets: tuple[ReviewTarget, ...],
) -> str:
    if commit_sha and not _SHA_RE.fullmatch(commit_sha):
        raise ReviewRefused("invalid release-review commit SHA")
    if len(targets) > _BATCH_LIMIT:
        raise ReviewRefused("release-review marker has too many targets")
    payload = {
        "commit": commit_sha,
        "targets": [
            [
                target.node_id,
                target.root_comment_id,
                target.selected_comment_id,
                target.selected_comment_sha256,
            ]
            for target in targets
        ],
    }
    encoded = urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"<!-- valkey-ci-agent:release-review-state:{encoded} -->"


def _decode_marker_state(payload: str) -> tuple[str, tuple[ReviewTarget, ...]]:
    padding = "=" * (-len(payload) % 4)
    raw = json.loads(urlsafe_b64decode(payload + padding).decode("utf-8"))
    if not isinstance(raw, dict):
        raise ReviewRefused("malformed release-review marker state")
    commit_sha = raw.get("commit", "")
    if not isinstance(commit_sha, str) or (
        commit_sha and not _SHA_RE.fullmatch(commit_sha)
    ):
        raise ReviewRefused("malformed release-review marker commit")
    raw_targets = raw.get("targets", [])
    if not isinstance(raw_targets, list) or len(raw_targets) > _BATCH_LIMIT:
        raise ReviewRefused("malformed release-review marker targets")
    targets: list[ReviewTarget] = []
    for item in raw_targets:
        if (
            not isinstance(item, list)
            or len(item) != 4
            or not isinstance(item[0], str)
            or not item[0]
            or len(item[0]) > 200
            or isinstance(item[1], bool)
            or not isinstance(item[1], int)
            or item[1] <= 0
            or isinstance(item[2], bool)
            or not isinstance(item[2], int)
            or item[2] <= 0
            or not isinstance(item[3], str)
            or not re.fullmatch(r"[0-9a-f]{64}", item[3])
        ):
            raise ReviewRefused("malformed release-review marker target")
        targets.append(ReviewTarget(item[0], item[1], item[2], item[3]))
    return commit_sha, tuple(targets)


_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          id isResolved isOutdated path line
          comments(first: 100) {
            pageInfo { hasNextPage }
            nodes {
              databaseId body createdAt diffHunk
              author { login __typename }
            }
          }
        }
      }
    }
  }
}
"""

_RESOLVE_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


def list_review_threads(
    gql: GitHubGraphQLClient,
    repo_full_name: str,
    pr_number: int,
) -> tuple[ReviewThread, ...]:
    owner, name = repo_full_name.split("/", 1)
    data = gql.execute(
        _THREADS_QUERY,
        {"owner": owner, "name": name, "number": pr_number},
    )
    try:
        connection = data["repository"]["pullRequest"]["reviewThreads"]
        if connection["pageInfo"]["hasNextPage"]:
            raise ReviewRefused(
                f"release PR has more than {_THREAD_LIMIT} review threads"
            )
        raw_threads = connection["nodes"]
    except (KeyError, TypeError) as exc:
        raise ReviewRefused("GitHub returned malformed review-thread data") from exc

    threads: list[ReviewThread] = []
    for raw in raw_threads:
        try:
            comments_connection = raw["comments"]
            if comments_connection["pageInfo"]["hasNextPage"]:
                raise ReviewRefused(
                    f"review thread has more than {_COMMENT_LIMIT} comments"
                )
            comments = tuple(
                sorted(
                    (_parse_comment(item) for item in comments_connection["nodes"]),
                    key=lambda item: (item.created_at, item.database_id),
                )
            )
            if not comments:
                raise ReviewRefused("review thread contains no comments")
            threads.append(
                ReviewThread(
                    node_id=str(raw["id"]),
                    resolved=_required_bool(raw["isResolved"]),
                    outdated=_required_bool(raw["isOutdated"]),
                    path=str(raw["path"]),
                    line=raw["line"] if isinstance(raw["line"], int) else None,
                    comments=comments,
                )
            )
        except (KeyError, TypeError) as exc:
            raise ReviewRefused("GitHub returned malformed review-thread data") from exc
    return tuple(threads)


def _parse_comment(raw: dict[str, Any]) -> ReviewComment:
    author = raw.get("author") or {}
    database_id = raw.get("databaseId")
    if (
        isinstance(database_id, bool)
        or not isinstance(database_id, int)
        or database_id <= 0
    ):
        raise ReviewRefused("review comment has no numeric id")
    return ReviewComment(
        database_id=database_id,
        body=str(raw.get("body", "")),
        created_at=str(raw.get("createdAt", "")),
        diff_hunk=str(raw.get("diffHunk", "")),
        author_login=str(author.get("login", "")),
        author_type=str(author.get("__typename", "")),
    )


def _required_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ReviewRefused("review thread has malformed state")
    return value


def resolve_review_thread(gql: GitHubGraphQLClient, thread_id: str) -> None:
    data = gql.execute(_RESOLVE_MUTATION, {"threadId": thread_id})
    thread = (data.get("resolveReviewThread") or {}).get("thread")
    if not isinstance(thread, dict) or thread.get("id") != thread_id or thread.get("isResolved") is not True:
        raise RuntimeError(f"GitHub did not resolve review thread {thread_id}")


def reconcile_review_targets(
    pr: Any,
    gql: GitHubGraphQLClient,
    *,
    targets: Iterable[ReviewTarget],
    commit_sha: str,
    bot_login: str,
    repo_full_name: str,
    load_threads: Callable[[], tuple[ReviewThread, ...]],
) -> tuple[int, int, list[str]]:
    """Idempotently reply to and resolve a previously published review batch."""

    if not _SHA_RE.fullmatch(commit_sha):
        raise ReviewRefused("review reconciliation requires a commit SHA")
    target_tuple = tuple(targets)
    current = {thread.node_id: thread for thread in load_threads()}
    commit_url = f"https://github.com/{repo_full_name}/commit/{commit_sha}"
    reply_body = f"Addressed in [`{commit_sha[:12]}`]({commit_url})."
    replied = 0
    resolved = 0
    failures: list[str] = []
    pending_resolution: list[ReviewTarget] = []

    for target in target_tuple:
        thread = current.get(target.node_id)
        latest = thread.latest_human_comment() if thread is not None else None
        if thread is None or latest is None:
            continue
        if (
            latest.database_id != target.selected_comment_id
            or review_comment_sha256(latest) != target.selected_comment_sha256
        ):
            continue
        if thread.resolved:
            resolved += 1
            continue
        already_replied = any(
            comment.author_login == bot_login and comment.body == reply_body
            for comment in thread.comments
        )
        if not already_replied:
            try:
                pr.create_review_comment_reply(
                    target.root_comment_id,
                    reply_body,
                )
                replied += 1
            except Exception as exc:  # noqa: BLE001 - continue with sibling threads
                failures.append(f"thread {target.node_id}: {exc}")
                continue
        pending_resolution.append(target)

    if pending_resolution:
        refreshed = {thread.node_id: thread for thread in load_threads()}
        for target in pending_resolution:
            thread = refreshed.get(target.node_id)
            latest = thread.latest_human_comment() if thread is not None else None
            if thread is None or latest is None:
                continue
            if (
                latest.database_id != target.selected_comment_id
                or review_comment_sha256(latest) != target.selected_comment_sha256
            ):
                continue
            if thread.resolved:
                resolved += 1
                continue
            try:
                resolve_review_thread(gql, target.node_id)
                resolved += 1
            except Exception as exc:  # noqa: BLE001 - continue with sibling threads
                failures.append(f"thread {target.node_id}: {exc}")
    return replied, resolved, failures


def selected_reviews(
    threads: Iterable[ReviewThread],
    notes_path: str,
    authorized: Callable[[str], bool],
) -> tuple[SelectedReview, ...]:
    selected: list[SelectedReview] = []
    for thread in sorted(threads, key=lambda item: item.node_id):
        comment = thread.latest_human_comment()
        if (
            thread.resolved
            or thread.outdated
            or thread.path != notes_path
            or comment is None
            or not comment.body.strip()
            or len(comment.body) > _COMMENT_CHARS
            or not authorized(comment.author_login)
        ):
            continue
        selected.append(SelectedReview(thread=thread, comment=comment))
    if len(selected) > _BATCH_LIMIT:
        raise ReviewRefused(f"release-review batch exceeds {_BATCH_LIMIT} comments")
    return tuple(selected)


def review_batch_id(reviews: Iterable[SelectedReview]) -> str:
    review_tuple = tuple(reviews)
    if not review_tuple:
        raise ReviewRefused("release-review batch is empty")
    payload = review_payload_json(review_tuple)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def review_targets(reviews: Iterable[SelectedReview]) -> tuple[ReviewTarget, ...]:
    return tuple(
        ReviewTarget(
            node_id=review.thread.node_id,
            root_comment_id=review.thread.root_comment_id,
            selected_comment_id=review.comment.database_id,
            selected_comment_sha256=review_comment_sha256(review.comment),
        )
        for review in reviews
    )


def review_comment_sha256(comment: ReviewComment) -> str:
    payload = json.dumps(
        {
            "author_login": comment.author_login,
            "author_type": comment.author_type,
            "body": comment.body,
            "created_at": comment.created_at,
            "diff_hunk": comment.diff_hunk,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def review_payload_json(reviews: Iterable[SelectedReview]) -> str:
    payload = []
    for review in reviews:
        payload.append(
            {
                "thread_id": review.thread.node_id,
                "line": review.thread.line,
                "diff_hunk": review.comment.diff_hunk[:_DIFF_CHARS],
                "selected_comment_id": review.comment.database_id,
                "conversation": [
                    {
                        "author": comment.author_login,
                        "body": (
                            comment.body
                            if comment == review.comment
                            else comment.body[:_CONTEXT_CHARS]
                        ),
                    }
                    for comment in review.thread.comments
                ],
            }
        )
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(encoded) > _PAYLOAD_CHARS:
        raise ReviewRefused("release-review batch is too large for one edit pass")
    return encoded


def validate_notes_edit(original: str, candidate: str, release: ReleasePR) -> None:
    """Require an edit confined to the current dated release section."""

    if (
        candidate == original
        or not candidate.endswith("\n")
        or len(candidate) > _NOTES_CHARS
        or "\x00" in candidate
        or "\r" in candidate
    ):
        raise ReviewRefused("release-note edit is empty or malformed")
    before = _split_current_section(original, release)
    after = _split_current_section(candidate, release)
    if before[0] != after[0] or before[2] != after[2]:
        raise ReviewRefused(
            "release-note edit changed the header, dated heading, or older content"
        )


def _split_current_section(
    text: str,
    release: ReleasePR,
) -> tuple[str, str, str]:
    if not text or len(text) > _NOTES_CHARS:
        raise ReviewRefused("release-notes file is empty or too large")
    major, minor, _patch = rn.parse_version(release.version)
    header = f"{rn.render_header(major, minor, release.profile.display_name)}\n\n"
    if not text.startswith(header):
        raise ReviewRefused("release-notes header is not canonical")

    heading_end = text.find("\n", len(header))
    underline_end = text.find("\n", heading_end + 1)
    if heading_end < 0 or underline_end < 0:
        raise ReviewRefused("release-notes current heading is incomplete")
    heading = text[len(header):heading_end]
    expected = (
        f"{rn.stage_heading(release.version, release.stage, release.profile.display_name)}"
        "  -  Released "
    )
    underline = text[heading_end + 1:underline_end]
    if not heading.startswith(expected) or underline != "-" * len(heading):
        raise ReviewRefused("release-notes current heading does not match the PR")

    body_start = underline_end + 1
    rest = text[body_start:]
    boundaries = [len(rest)]
    next_release = rn.dated_section_start(rest, release.profile.display_name)
    if next_release is not None:
        boundaries.append(next_release)
    contributors = _CONTRIBUTORS_RE.search(rest)
    if contributors is not None:
        boundaries.append(contributors.start())
    body_end = body_start + min(boundaries)
    return text[:body_start], text[body_start:body_end], text[body_end:]
