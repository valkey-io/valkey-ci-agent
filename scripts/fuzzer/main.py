"""Monitor the latest scheduled Valkey fuzzer workflow run."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from github import Auth, Github

from scripts.common.issue_dedup import IssueDedupPublisher
from scripts.common.workflow_artifacts import ArtifactClient
from scripts.fuzzer import issue_renderer
from scripts.fuzzer.analyzer import FuzzerRunAnalyzer

TARGET_REPO = "valkey-io/valkey-fuzzer"
WORKFLOW_FILE = "fuzzer-run.yml"

# Verdicts that should NOT produce an issue. Everything else does — including
# `needs-human-triage` (Claude failed and there are unresolved signals).
_NO_PUBLISH_VERDICTS = frozenset({"expected-chaos-noise", "environmental-or-infra"})

logger = logging.getLogger(__name__)


def _should_publish(analysis: Any) -> bool:
    """Publish on anomalous status OR any bug-candidate triage verdict."""
    if analysis.overall_status == "anomalous":
        return True
    return analysis.triage_verdict not in _NO_PUBLISH_VERDICTS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-token", default=None,
                        help="GitHub token (falls back to TARGET_TOKEN env var)")
    parser.add_argument("--output",
                        help="Write JSON result to this path instead of stdout")
    parser.add_argument("--dry-run", action="store_true",
                        help="List the latest run without analyzing or filing an issue")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    token = args.target_token or os.environ.get("TARGET_TOKEN", "")
    if not token:
        parser.error("--target-token or TARGET_TOKEN env var is required")

    gh = Github(auth=Auth.Token(token))
    client = ArtifactClient(gh, token=token)
    analyzer = FuzzerRunAnalyzer(gh, github_token=token, artifact_client=client)
    publisher = IssueDedupPublisher(gh, marker_namespace=issue_renderer.MARKER_NAMESPACE)

    runs = client.list_recent_runs(TARGET_REPO, WORKFLOW_FILE, event="schedule", max_runs=1)
    results: list[dict[str, Any]] = []
    for run in runs:
        entry: dict[str, Any] = {
            "run_id": run.id,
            "conclusion": run.conclusion or "",
            "html_url": run.html_url,
        }

        if args.dry_run:
            entry["action"] = "would-analyze"
            results.append(entry)
            continue

        try:
            analysis = analyzer.analyze(TARGET_REPO, run.id, workflow_file=WORKFLOW_FILE)
            entry["action"] = "analyzed"
            entry["status"] = analysis.overall_status
            entry["verdict"] = analysis.triage_verdict
            entry["summary"] = analysis.summary
            if _should_publish(analysis):
                if not analysis.incident_fingerprint:
                    # Refuse to publish without a fingerprint — otherwise
                    # unrelated runs would collide on a single issue.
                    logger.error(
                        "Run %s passed publish gate but has no fingerprint; skipping",
                        run.id,
                    )
                    entry["issue_action"] = "skipped-no-fingerprint"
                else:
                    action, url = publisher.upsert(
                        TARGET_REPO,
                        fingerprint=analysis.incident_fingerprint,
                        render=issue_renderer.render_for(analysis),
                        idempotency_key=str(run.id),
                    )
                    entry["issue_action"] = action
                    entry["issue_url"] = url
        except Exception as exc:
            entry["action"] = "error"
            entry["error"] = str(exc)
            logger.warning("Failed to analyze run %s: %s", run.id, exc, exc_info=True)

        results.append(entry)

    output = {"target_repo": TARGET_REPO, "dry_run": args.dry_run, "runs": results}
    rendered = json.dumps(output, indent=2)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered)
    # Surface monitor errors via the workflow's exit code so a failed run
    # shows ❌ in the Actions tab instead of being hidden in the JSON artifact.
    return 1 if any(r.get("action") == "error" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
