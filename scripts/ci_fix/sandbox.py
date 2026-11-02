"""Replay the failed job's setup in the CI-fix checkout, as the job ran it.

The setup runs as the workflow user, with root through sudo as GitHub gives
it: Valkey's jobs install packages and libraries (``sudo apt-get install``,
``sudo make install``), which the unprivileged account cannot. That trusts the
workflow at the checked-out commit as the job itself did, and only a trusted
tree gets here: a target branch tip, or a bot-owned branch of the target
repository (a fork's PR only gets a suggestion, which replays no setup).
The setup runs before the AWS credentials exist and without the workflow's
environment; the reproduction, tests and edit agent that follow still run as
the isolated account.
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from scripts.common.proc import PROCESS_BASICS, filter_env

_SETUP_ENV = (
    *PROCESS_BASICS,
    "CI",
    "GITHUB_ACTIONS",
    "RUNNER_ARCH",
    "RUNNER_OS",
)


def run_setup(repo: Path, setup: Path) -> int:
    """Run ``setup`` in ``repo`` without inheriting workflow credentials."""
    repo = repo.resolve(strict=True)
    setup = setup.resolve(strict=True)
    if repo.parent == repo or not setup.is_file():
        raise ValueError("the CI-fix sandbox paths are invalid")

    env = filter_env(_SETUP_ENV)
    env["GITHUB_WORKSPACE"] = str(repo)
    # The job's own temp directory holds the agent: give the setup its own.
    env["RUNNER_TEMP"] = tempfile.mkdtemp(prefix="ci-fix-setup-", dir=repo.parent)
    return subprocess.run(["sh", str(setup)], cwd=repo, env=env, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--setup", required=True, type=Path)
    args = parser.parse_args(argv)
    return run_setup(args.repo, args.setup)


if __name__ == "__main__":
    raise SystemExit(main())
