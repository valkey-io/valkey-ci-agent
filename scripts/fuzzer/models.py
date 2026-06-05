"""Data models for the fuzzer analysis pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FuzzerSignal:
    """A single anomaly observed in a fuzzer run.

    `severity` is "critical" or "warning".
    """

    title: str
    severity: str
    evidence: str


@dataclass
class FuzzerRunContext:
    """Mutable evidence bag built up while fetching artifacts and logs."""

    repo: str
    workflow_file: str
    run_id: int
    run_url: str
    conclusion: str
    head_sha: str
    scenario_id: str | None = None
    seed: str | None = None
    tested_valkey_sha: str | None = None
    results: dict[str, Any] | None = None
    node_logs: dict[str, str] = field(default_factory=dict)


@dataclass
class FuzzerRunAnalysis:
    """Final triage output produced by `FuzzerRunAnalyzer.analyze`."""

    repo: str
    workflow_file: str
    run_id: int
    run_url: str
    conclusion: str
    head_sha: str
    overall_status: str  # "normal", "warning", "anomalous"
    triage_verdict: str
    summary: str
    anomalies: list[FuzzerSignal] = field(default_factory=list)
    scenario_id: str | None = None
    seed: str | None = None
    tested_valkey_sha: str | None = None
    root_cause_category: str | None = None
    reproduction_hint: str | None = None
    incident_fingerprint: str | None = None
    suggested_labels: list[str] = field(default_factory=list)
