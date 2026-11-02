"""Run a planned target setup inside the disposable CI-fix account."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from scripts.common.isolation import as_user, handed_to
from scripts.common.proc import PROCESS_BASICS, filter_env

_SETUP_ENV = (
    *PROCESS_BASICS,
    "CI",
    "GITHUB_ACTIONS",
    "RUNNER_ARCH",
    "RUNNER_OS",
)


def run_setup(repo: Path, setup: Path, user: str) -> int:
    """Run ``setup`` in ``repo`` without inheriting workflow credentials."""
    repo = repo.resolve(strict=True)
    setup = setup.resolve(strict=True)
    if repo.parent == repo or not setup.is_file():
        raise ValueError("the CI-fix sandbox paths are invalid")

    with handed_to(user, repo) as home:
        env = filter_env(_SETUP_ENV)
        env["GITHUB_WORKSPACE"] = str(repo)
        env["RUNNER_TEMP"] = home
        invocation = as_user(user, env, ["sh", str(setup)], home=home, cwd=repo)
        result = subprocess.run(
            list(invocation.argv),
            cwd=None,
            env=invocation.env,
            check=False,
        )
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--setup", required=True, type=Path)
    parser.add_argument("--user", required=True)
    args = parser.parse_args(argv)
    return run_setup(args.repo, args.setup, args.user)


if __name__ == "__main__":
    raise SystemExit(main())
