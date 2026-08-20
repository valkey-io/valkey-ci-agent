"""CLI used by release preparation and protected publication workflows."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from github import Auth, Github

from scripts.common.job_summary import emit_job_summary
from scripts.release.authorize import NotAuthorizedError
from scripts.release.models import ReleaseIntent
from scripts.release.policy import load_policy
from scripts.release.publish import (
    ReleaseError,
    plan_digest,
    plan_publication,
    prepare_release,
    publish_release,
    render_plan,
)

_ROOT = Path(__file__).resolve().parents[2]


def _token() -> str:
    return os.environ.get("RELEASE_GITHUB_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")


def _write_outputs(values: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT", "")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as output:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError(f"multiline workflow output refused for {key}")
            output.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", default=_token())
    parser.add_argument("--policy", default=str(_ROOT / "release_policy.yml"))
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="derive the next release identity")
    prepare.add_argument("--branch", required=True)
    prepare.add_argument("--intent", required=True, choices=[item.value for item in ReleaseIntent])
    prepare.add_argument("--actor", required=True)

    plan = sub.add_parser("plan", help="validate and render a publication plan")
    plan.add_argument("--branch", required=True)
    plan.add_argument("--candidate-sha", required=True)

    publish = sub.add_parser("publish", help="revalidate and publish an approved plan")
    publish.add_argument("--branch", required=True)
    publish.add_argument("--candidate-sha", required=True)
    publish.add_argument("--actor", required=True)
    publish.add_argument("--expected-digest", required=True)
    publish.add_argument("--expected-bypass-integration-id", required=True, type=int)

    args = parser.parse_args(argv)
    if not args.token:
        parser.error("a GitHub token is required")
    try:
        policy = load_policy(args.policy)
    except (OSError, ValueError) as exc:
        parser.error(f"cannot load release policy: {exc}")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    gh = Github(auth=Auth.Token(args.token))
    try:
        if args.command == "prepare":
            release = prepare_release(
                gh,
                policy,
                branch=args.branch,
                intent=ReleaseIntent(args.intent),
                actor=args.actor,
            )
            _write_outputs({"version": release.version, "stage": release.stage, "tag": release.tag})
            print(f"Prepared {release.tag} on {args.branch}")
            return 0
        if args.command == "plan":
            publication = plan_publication(
                gh,
                policy,
                branch=args.branch,
                candidate_sha=args.candidate_sha,
            )
            summary = render_plan(publication)
            emit_job_summary(summary)
            print(summary)
            _write_outputs(
                {
                    "version": publication.tag,
                    "tag": publication.tag,
                    "sha": publication.sha,
                    "plan_digest": plan_digest(publication),
                }
            )
            return 0
        if args.command == "publish":
            url = publish_release(
                gh,
                policy,
                branch=args.branch,
                candidate_sha=args.candidate_sha,
                actor=args.actor,
                expected_digest=args.expected_digest,
                expected_bypass_integration_id=args.expected_bypass_integration_id,
            )
            _write_outputs({"release_url": url})
            print(f"Published {url}")
            return 0
        raise AssertionError(args.command)
    except (ReleaseError, NotAuthorizedError, ValueError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
