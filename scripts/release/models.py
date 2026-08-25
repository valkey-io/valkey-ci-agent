"""Small immutable values used by the release workflows."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReleaseIntent(str, Enum):
    RC = "rc"
    GA = "ga"
    PATCH = "patch"


@dataclass(frozen=True)
class DerivedRelease:
    version: str
    stage: str

    @property
    def tag(self) -> str:
        return self.version if self.stage == "ga" else f"{self.version}-{self.stage}"


@dataclass(frozen=True)
class ReleasePolicy:
    repo: str
    authorized_team: str
    branches: tuple[str, ...]
    checks_workflow: str
    required_checks: tuple[str, ...]

    @property
    def team_org(self) -> str:
        return self.authorized_team.split("/", 1)[0]

    @property
    def team_slug(self) -> str:
        return self.authorized_team.split("/", 1)[1]


@dataclass(frozen=True)
class PublishPlan:
    branch: str
    tag: str
    version: str
    stage: str
    sha: str
    body: str
    prerelease: bool
    make_latest: str
    tag_protected: bool | None
    tag_bypass_integration_ids: tuple[int, ...] | None = None
    candidate_ci: str = "not checked"
