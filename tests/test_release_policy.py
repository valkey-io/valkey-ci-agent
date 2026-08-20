from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release.policy import load_policy, validate_branch


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "policy.yml"
    path.write_text(body, encoding="utf-8")
    return path


VALID = """\
schema_version: 1
repo: valkey-io/valkey
authorized_team: valkey-io/core-team
checks_workflow: ci.yml
branches: ['9.1']
required_checks: [test]
"""


def test_loads_small_policy(tmp_path: Path) -> None:
    policy = load_policy(_write(tmp_path, VALID))
    assert policy.repo == "valkey-io/valkey"
    assert policy.team_slug == "core-team"
    assert validate_branch(policy, " 9.1 ") == "9.1"


@pytest.mark.parametrize(
    "replacement, message",
    [
        ("schema_version: 2", "schema_version"),
        ("authorized_team: core-team", "org/team-slug"),
        ("branches: []", "non-empty list"),
        ("required_checks: [test, test]", "duplicates"),
        ("checks_workflow: .github/workflows/ci.yml", "filename"),
    ],
)
def test_invalid_policy_fails_closed(tmp_path: Path, replacement: str, message: str) -> None:
    key = replacement.split(":", 1)[0]
    body = "\n".join(
        replacement if line.startswith(f"{key}:") else line
        for line in VALID.splitlines()
    )
    with pytest.raises(ValueError, match=message):
        load_policy(_write(tmp_path, body))


def test_unknown_key_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown"):
        load_policy(_write(tmp_path, VALID + "surprise: true\n"))


def test_unlisted_branch_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not releasable"):
        validate_branch(load_policy(_write(tmp_path, VALID)), "unstable")
