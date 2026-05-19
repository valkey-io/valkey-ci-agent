"""Fuzzer run analysis: deterministic pattern matching + Claude Code triage."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from scripts.ai.runtime import run_agent
from scripts.common.text_utils import strip_ansi
from scripts.fuzzer.artifacts import ArtifactClient
from scripts.fuzzer.incidents import compute_fingerprint
from scripts.fuzzer.models import FuzzerRunAnalysis, FuzzerRunContext, FuzzerSignal

logger = logging.getLogger(__name__)

# (title, severity, pattern, is_bug_indicator)
# A "bug indicator" upgrades the verdict from possible-core-valkey-bug to
# likely-core-valkey-bug. RDB/AOF failures are anomalous but not necessarily
# bugs (could be disk errors).
_ANOMALY_PATTERNS: list[tuple[str, str, str, bool]] = [
    ("Node crash or assertion", "critical",
     r"ASSERTION FAILED|Assertion failed|BUG REPORT START|STACK TRACE", True),
    ("Sanitizer failure", "critical",
     r"AddressSanitizer|UndefinedBehaviorSanitizer|runtime error:", True),
    ("Segfault", "critical", r"segmentation fault|signal 11", True),
    ("OOM", "critical", r"Out Of Memory|Can't allocate|OOM command not allowed", False),
    ("Failover timeout", "critical",
     r"Failover attempt expired|Manual failover timed out", True),
    ("Split-brain or slot loss", "critical",
     r"split.?brain|slots still assigned to killed nodes", True),
    ("RDB/AOF failure", "warning",
     r"Background saving error|Failed opening.*rdb|AOF rewrite.*failed", False),
]


def _scan_logs(context: FuzzerRunContext) -> tuple[list[FuzzerSignal], list[str]]:
    """Deterministic pattern matching on results.json and node logs."""
    anomalies: list[FuzzerSignal] = []
    normals: list[str] = []

    results = context.results or {}
    if results.get("success") is False:
        anomalies.append(FuzzerSignal(
            "Run failed", "critical",
            str(results.get("error_message") or "reported failure"),
        ))
    validation = results.get("final_validation")
    if isinstance(validation, dict):
        for name, check in (validation.get("checks") or {}).items():
            if not isinstance(check, dict):
                continue
            if check.get("success") is False:
                anomalies.append(FuzzerSignal(
                    f"{name} validation failed", "critical",
                    str(check.get("error") or "failed"),
                ))
            elif check.get("success") is True:
                normals.append(f"{name} passed")

    for name, text in context.node_logs.items():
        cleaned = strip_ansi(text)
        for title, severity, pattern, _ in _ANOMALY_PATTERNS:
            m = re.search(pattern, cleaned, re.I)
            if m:
                anomalies.append(FuzzerSignal(title, severity, f"{name}: {m.group(0)[:200]}"))

    return _dedupe_signals(anomalies), normals


def _dedupe_signals(signals: list[FuzzerSignal]) -> list[FuzzerSignal]:
    seen: set[tuple[str, str]] = set()
    out: list[FuzzerSignal] = []
    for s in signals:
        key = (s.title, s.evidence)
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _load_artifacts(context: FuzzerRunContext, files: dict[str, bytes]) -> None:
    """Parse downloaded artifact files into the context."""
    for path, payload in files.items():
        name = path.rsplit("/", 1)[-1]
        text = payload.decode("utf-8", errors="replace")
        if name == "manifest.json":
            try:
                manifest = json.loads(text)
            except ValueError:
                continue
            context.tested_valkey_sha = manifest.get("valkey_sha") or manifest.get("tested_valkey_sha")
            if manifest.get("scenario_id"):
                context.scenario_id = str(manifest["scenario_id"])
            if manifest.get("seed") is not None:
                context.seed = str(manifest["seed"])
        elif name == "results.json":
            try:
                data = json.loads(text)
            except ValueError:
                continue
            # The fuzzer wraps results as {"results": [...]}. Keep the first entry.
            if isinstance(data, dict) and isinstance(data.get("results"), list):
                context.results = data["results"][0] if data["results"] else None
            elif isinstance(data, dict):
                context.results = data
        elif name.endswith(".log"):
            context.node_logs[name] = text


def _triage(anomalies: list[FuzzerSignal]) -> tuple[str, str]:
    if not anomalies:
        return "normal", "expected-chaos-noise"
    status = "anomalous" if any(s.severity == "critical" for s in anomalies) else "warning"
    bug_titles = {t for t, _, _, is_bug in _ANOMALY_PATTERNS if is_bug}
    if {s.title for s in anomalies} & bug_titles:
        return status, "likely-core-valkey-bug"
    return status, "possible-core-valkey-bug"


_CLAUDE_PROMPT_TEMPLATE = """\
You analyze Valkey fuzzer workflow runs (chaos testing for Redis-compatible clusters).
Distinguish expected chaos behavior from real bugs. Be conservative.

Chaos-expected (NOT bugs): CLUSTERDOWN, replication link loss, cluster state FAIL,
slot migration errors during node kills. These are normal side-effects of killing
nodes. Only flag them if they persist after the cluster should have recovered.

Real bugs: crashes/assertions on nodes NOT targeted by chaos, sanitizer errors,
segfaults, permanent slot loss, split-brain, data inconsistency after recovery.

## Run
{run_url} (Valkey SHA {valkey_sha}, scenario {scenario_id}, seed {seed})

## Deterministic findings
{deterministic_summary}

## Task
Read _artifacts/ as needed. Return ONLY a single JSON object:
{{
  "overall_status": "normal|warning|anomalous",
  "triage_verdict": "likely-core-valkey-bug|possible-core-valkey-bug|expected-chaos-noise|environmental-or-infra|needs-human-triage",
  "root_cause_category": "short-label or null",
  "summary": "2-3 sentence maintainer-facing explanation",
  "anomalies": [{{"title": "...", "severity": "warning|critical", "evidence": "..."}}],
  "reproduction_hint": "command or null"
}}
"""


def _invoke_claude(context: FuzzerRunContext, anomalies: list[FuzzerSignal],
                   workdir: Path) -> dict[str, Any]:
    """Drop artifacts in workdir/_artifacts and let Claude grep them."""
    art_dir = workdir / "_artifacts"
    art_dir.mkdir()
    if context.results:
        (art_dir / "results.json").write_text(json.dumps(context.results, indent=2))
    for name, text in context.node_logs.items():
        (art_dir / name).write_text(text)

    det_lines = [f"- [{a.severity}] {a.title}: {a.evidence}" for a in anomalies[:15]] or ["- none"]
    prompt = _CLAUDE_PROMPT_TEMPLATE.format(
        run_url=context.run_url,
        valkey_sha=context.tested_valkey_sha or "unknown",
        scenario_id=context.scenario_id or "unknown",
        seed=context.seed or "unknown",
        deterministic_summary="\n".join(det_lines),
    )

    result = run_agent("fuzzer_analysis_readonly", prompt, cwd=str(workdir))
    if result.returncode != 0:
        raise RuntimeError(f"Claude Code failed (rc={result.returncode}): {result.stderr[:300]}")
    return _parse_claude_response(result.stdout)


def _parse_claude_response(stdout: str) -> dict[str, Any]:
    """Find the last stream-json `result` event, fall back to plain JSON."""
    text = stdout
    for line in stdout.strip().splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result" and "result" in ev:
            text = ev["result"]
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict) and "overall_status" in obj:
            return obj
        start = text.find("{", start + 1)
    raise ValueError("No analysis JSON object in Claude response")


class FuzzerRunAnalyzer:
    """Analyzes fuzzer workflow runs: pattern matching + Claude Code."""

    def __init__(self, github_client: Any, *, github_token: str,
                 artifact_client: ArtifactClient | None = None) -> None:
        self._gh = github_client
        self._client = artifact_client or ArtifactClient(github_client, token=github_token)

    def analyze(self, repo: str, run_id: int, *, workflow_file: str) -> FuzzerRunAnalysis:
        gh_repo = self._gh.get_repo(repo)
        run = gh_repo.get_workflow_run(run_id)
        context = FuzzerRunContext(
            repo=repo, workflow_file=workflow_file, run_id=run_id,
            run_url=run.html_url,
            conclusion=str(run.conclusion or ""),
            head_sha=str(run.head_sha or ""),
        )

        artifacts = self._client.list_run_artifacts(repo, run_id)
        bundle = next(
            (a for a in artifacts if a.name.startswith("fuzzer-run-artifacts") and not a.expired),
            None,
        )
        if bundle is None:
            return _build_error_analysis(context, "no fuzzer artifact bundle found")
        files = self._client.download_artifact(repo, bundle.artifact_id)
        if not files:
            return _build_error_analysis(
                context, "fuzzer artifact bundle was empty or unreadable",
            )
        _load_artifacts(context, files)

        anomalies, normals = _scan_logs(context)

        claude_payload: dict[str, Any] = {}
        claude_error: str | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="fuzzer-") as td:
                claude_payload = _invoke_claude(context, anomalies, Path(td))
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            claude_error = str(exc)
            logger.warning("Claude analysis failed for run %s: %s", run_id, exc, exc_info=True)

        for raw in claude_payload.get("anomalies") or []:
            if isinstance(raw, dict) and raw.get("title"):
                anomalies.append(FuzzerSignal(
                    str(raw["title"]),
                    str(raw.get("severity") or "warning"),
                    str(raw.get("evidence", "")),
                ))
        anomalies = _dedupe_signals(anomalies)

        if claude_payload:
            overall_status = str(claude_payload.get("overall_status") or "warning")
            triage_verdict = str(claude_payload.get("triage_verdict") or "needs-human-triage")
        else:
            overall_status, triage_verdict = _triage(anomalies)
            if claude_error and not anomalies:
                # Couldn't get a verdict from either signal — escalate.
                overall_status, triage_verdict = "warning", "needs-human-triage"

        summary = (claude_payload.get("summary") or "").strip() or (
            f"Run {run_id}: {len(anomalies)} anomalies" if anomalies
            else f"Run {run_id}: see findings"
        )
        root_cause = claude_payload.get("root_cause_category")
        if not isinstance(root_cause, str):
            root_cause = None
        hint = claude_payload.get("reproduction_hint")
        if not isinstance(hint, str) or not hint:
            hint = f"valkey-fuzzer cluster --seed {context.seed}" if context.seed else None

        labels = ["possible-valkey-bug"] if triage_verdict in {
            "likely-core-valkey-bug", "possible-core-valkey-bug",
        } else []

        return FuzzerRunAnalysis(
            repo=repo, workflow_file=workflow_file, run_id=run_id,
            run_url=context.run_url, conclusion=context.conclusion,
            head_sha=context.head_sha, overall_status=overall_status,
            triage_verdict=triage_verdict, summary=summary,
            anomalies=anomalies, normal_signals=normals,
            scenario_id=context.scenario_id, seed=context.seed,
            tested_valkey_sha=context.tested_valkey_sha,
            root_cause_category=root_cause, reproduction_hint=hint,
            incident_fingerprint=compute_fingerprint(
                repo=repo, workflow_file=workflow_file,
                root_cause_category=root_cause, anomalies=anomalies,
            ),
            suggested_labels=labels,
        )


def _build_error_analysis(context: FuzzerRunContext, reason: str) -> FuzzerRunAnalysis:
    """Surface infrastructure failures (e.g. missing artifacts) for human triage."""
    return FuzzerRunAnalysis(
        repo=context.repo, workflow_file=context.workflow_file, run_id=context.run_id,
        run_url=context.run_url, conclusion=context.conclusion, head_sha=context.head_sha,
        overall_status="warning", triage_verdict="needs-human-triage",
        summary=f"Run {context.run_id}: {reason}",
    )
