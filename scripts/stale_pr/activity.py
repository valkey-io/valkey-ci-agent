"""Work out when a pull request last saw real activity, from its timeline.

The input is the list of events the REST timeline endpoint returns
(``GET /repos/{owner}/{repo}/issues/{number}/timeline``). Three dates come out:

* last code: the newest commit on the PR or force push to its branch. A merge
  commit, such as the one GitHub's "Update branch" button creates, only brings
  the base branch in and is not counted.
* last author: the newest comment or review by the PR's author.
* last maintainer: the newest comment or review by anyone the caller says is a
  maintainer, other than the author.

Comments and reviews by bots never count. Neither do events such as labels,
assignments or cross-references, which move GitHub's ``updated_at`` without
anyone working on the PR.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import datetime
from typing import Any

from scripts.common.identity import APP_LOGIN, BOT_LOGIN
from scripts.stale_pr.models import PRActivity

# The agent's own accounts. GitHub reports their comments with type "Bot",
# but the logins are listed too in case a comment reaches us without a type.
AGENT_LOGINS = frozenset({BOT_LOGIN, f"{BOT_LOGIN}[bot]", APP_LOGIN, f"{APP_LOGIN}[bot]"})

_COMMENT_AND_REVIEW_EVENTS = frozenset({"commented", "reviewed"})


def compute_activity(
    events: Iterable[Mapping[str, Any]],
    *,
    author: str,
    is_maintainer: Callable[[str], bool],
    ignored_logins: Iterable[str] = (),
) -> PRActivity:
    """Return the last code, author and maintainer dates for one PR's timeline."""
    ignored = {login.casefold() for login in (*AGENT_LOGINS, *ignored_logins)}
    author_key = author.casefold()
    last_code: datetime | None = None
    last_author: datetime | None = None
    last_maintainer: datetime | None = None

    for event in events:
        code_at = _code_time(event)
        if code_at is not None:
            last_code = _most_recent(last_code, code_at)
            continue
        for login, user_type, at in _comments_and_reviews(event):
            if user_type == "Bot" or login.casefold() in ignored or login.casefold().endswith("[bot]"):
                continue
            if login.casefold() == author_key:
                last_author = _most_recent(last_author, at)
            elif is_maintainer(login):
                last_maintainer = _most_recent(last_maintainer, at)

    return PRActivity(
        last_code_at=last_code,
        last_author_at=last_author,
        last_maintainer_at=last_maintainer,
    )


def _code_time(event: Mapping[str, Any]) -> datetime | None:
    """The time of a commit or force push, or None for any other event."""
    kind = event.get("event")
    if kind == "committed":
        if len(event.get("parents") or ()) > 1:
            return None
        # The committer date changes on a rebase; the author date does not.
        return _parse_time((event.get("committer") or {}).get("date"))
    if kind == "head_ref_force_pushed":
        return _parse_time(event.get("created_at"))
    return None


def _comments_and_reviews(event: Mapping[str, Any]) -> Iterator[tuple[str, str, datetime]]:
    """Yield (login, user type, time) for each comment or review in *event*."""
    kind = event.get("event")
    if kind in _COMMENT_AND_REVIEW_EVENTS:
        # A review that is still pending has no submitted_at yet; nobody else can see it.
        at = _parse_time(event.get("submitted_at") if kind == "reviewed" else event.get("created_at"))
        user = event.get("user") or event.get("actor") or {}
        if at is not None and user.get("login"):
            yield user["login"], user.get("type") or "", at
    elif kind == "line-commented":
        # Inline review comments arrive grouped under one event per thread.
        for comment in event.get("comments") or ():
            at = _parse_time(comment.get("created_at"))
            user = comment.get("user") or {}
            if at is not None and user.get("login"):
                yield user["login"], user.get("type") or "", at


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _most_recent(current: datetime | None, candidate: datetime) -> datetime:
    return candidate if current is None or candidate > current else current
