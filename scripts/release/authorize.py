"""Live authorization against the release policy's GitHub team.

Preparation and the final approved publication verify the acting user's
membership at execution time. Failures fail closed. Publication planning is
non-writing and may be started automatically after the canonical PR merge.
"""

from __future__ import annotations

from typing import Any

from github.GithubException import GithubException

from scripts.common.github_client import retry_github_call
from scripts.release.models import ReleasePolicy


class NotAuthorizedError(Exception):
    """The actor is not a member of the policy's authorized team."""


def ensure_authorized(gh: Any, policy: ReleasePolicy, actor: str) -> None:
    """Raise :class:`NotAuthorizedError` unless *actor* is authorized.

    ``authorized_team`` is ``org/team-slug`` and membership is queried live.

    Lookup failures also refuse (fail closed), with a message naming the
    failed lookup, since "the token cannot read the org's teams" needs a
    different operator response than "not a member".
    """
    actor = actor.strip()
    if not actor:
        raise NotAuthorizedError("no acting user supplied")

    try:
        team = retry_github_call(
            lambda: gh.get_organization(policy.team_org).get_team_by_slug(policy.team_slug),
            retries=2,
            description=f"resolve team {policy.authorized_team}",
        )
        user = retry_github_call(
            lambda: gh.get_user(actor),
            retries=2,
            description=f"resolve user {actor}",
        )
        is_member = retry_github_call(
            lambda: team.has_in_members(user),
            retries=2,
            description=f"check {actor} membership in {policy.authorized_team}",
        )
    except GithubException as exc:
        raise NotAuthorizedError(
            f"could not verify membership of @{actor} in {policy.authorized_team} "
            f"(HTTP {exc.status}); refusing (fail closed). The token must be able "
            f"to read {policy.team_org}'s teams (GitHub App: members:read; a 404 "
            f"can mean either the team does not exist or the token cannot see it)."
        ) from exc
    if not is_member:
        raise NotAuthorizedError(
            f"@{actor} is not a member of {policy.authorized_team}; "
            f"only that team may perform release actions on {policy.repo}"
        )
