"""The Slack message for a night's Daily run: one line per failing test, with history.

The message replaces the one ``valkey-io/valkey``'s ``daily.yml`` posts
today, which names the failed jobs and links the run. This one names the
tests, says how each has behaved over the last two weeks, and links each
line to its row on the dashboard and, when the Test Failure Detector has
filed one, to its issue.

The message is rendered from the same report data as the dashboard, so the
two always agree on which failures a run had and what they are called.
Everything here is a pure function of that data; posting is the caller's.

Slack's text format has no colour and no table, so the per-run history is a
row of coloured squares and the alignment comes from a fixed line shape::

    🔴  ⬜⬜⬜🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥  11/14   *spawn timeout* — repl-compression.tcl  ↗
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.request import Request, urlopen

from scripts.ci_health.fingerprint import fingerprint_for_name
from scripts.common.valkey_ci import test_in_file

# How many of the newest runs the bar shows.
BAR_RUNS = 14

# A test that has failed this many of its recent runs in a row reads as broken
# rather than flaky.
PERSISTENT_STREAK = 3

# Slack renders at most this many characters per ``section`` block.
_SECTION_LIMIT = 3000

NEW, PERSISTENT, INTERMITTENT = "new", "persistent", "intermittent"
_INDICATOR = {NEW: "🆕", PERSISTENT: "🔴", INTERMITTENT: "🟡"}
_ORDER = {NEW: 0, PERSISTENT: 1, INTERMITTENT: 2}
_FAILED, _PASSED = "🟥", "⬜"


@dataclass
class FailureLine:
    """One failing test of the run, with what the message says about it."""

    display_name: str
    fingerprint: str
    classification: str
    bar: str
    failed_runs: int
    shown_runs: int
    title: str
    detail: str
    dashboard_url: str
    issue_url: Optional[str] = None

    def mrkdwn(self) -> str:
        link = f"<{self.dashboard_url}|↗>"
        if self.issue_url:
            link = f"<{self.issue_url}|{_issue_label(self.issue_url)}> · " + link
        return (
            f"{_INDICATOR[self.classification]}  {self.bar}  "
            f"{self.failed_runs:>2}/{self.shown_runs}   "
            f"*{_escape(self.title)}* — {_escape(self.detail)}  {link}"
        )


@dataclass
class SlackMessage:
    """The message as the text Slack falls back to and as Block Kit blocks."""

    text: str
    blocks: List[Dict] = field(default_factory=list)
    failed: bool = False

    def payload(self) -> Dict:
        return {"text": self.text, "blocks": self.blocks}


def build_message(
    data: Dict,
    scorecards: Sequence[Dict],
    *,
    dashboard_url: str,
    issue_urls: Optional[Dict[str, str]] = None,
    bar_runs: int = BAR_RUNS,
) -> SlackMessage:
    """The message for the newest run in ``data``.

    ``data`` is :func:`scripts.ci_health.report.generate_report_data` for the
    Daily workflow, whose newest run is the one reported. ``scorecards`` is
    the ``scorecards`` list of :func:`scripts.ci_health.scorecard.compute_scorecards`,
    which supplies each test's first-seen date and recent streak.
    ``issue_urls`` maps a fingerprint to the detector's issue for it.
    """
    runs = data.get("runs") or []
    repo = data.get("summary", {}).get("repo", "valkey-io/valkey")
    if not runs:
        text = f"⚪ *Daily CI*: no run on record · <{dashboard_url}|dashboard>"
        return SlackMessage(text=text, blocks=[_section(text)], failed=False)

    run = runs[-1]
    columns = [c["key"] for c in data.get("columns") or []][-bar_runs:]
    by_name = {sc["test_name"]: sc for sc in scorecards}
    issue_urls = issue_urls or {}
    lines = [
        _line(name, data["tests"].get(name, {}), by_name.get(name), columns, run["day"], dashboard_url, issue_urls)
        for name in run.get("failure_names") or []
    ]
    lines.sort(key=lambda ln: (_ORDER[ln.classification], -ln.failed_runs, ln.title.lower()))

    run_url = f"https://github.com/{repo}/actions/runs/{run['run_id']}"
    sha = run.get("commit_sha") or ""
    commit = f"<https://github.com/{repo}/commit/{sha}|{sha[:7]}>" if sha else "no commit"
    day = _pretty_day(run["day"])
    failed = run.get("status") == "failure" or bool(lines)
    if failed:
        header = (
            f"🔴 *Daily CI failed* — <{run_url}|{day}> · "
            f"{run.get('failed_jobs', 0)} of {run.get('total_jobs', 0)} jobs · {commit} · "
            f"<{dashboard_url}|dashboard>"
        )
    else:
        header = (
            f"🟢 *Daily CI passed* — <{run_url}|{day}> · {run.get('total_jobs', 0)} jobs · {commit} · "
            f"<{dashboard_url}|dashboard>"
        )
    footer = (
        f"last {len(columns)} daily runs, newest right · "
        f"{_INDICATOR[NEW]} first failure · {_INDICATOR[PERSISTENT]} fails most runs · "
        f"{_INDICATOR[INTERMITTENT]} intermittent"
    )
    body = [ln.mrkdwn() for ln in lines]
    if failed and not body:
        body = ["_jobs failed without a test failure on record; see the run_"]

    blocks = [_section(header)]
    blocks += [_section(chunk) for chunk in _chunks(body)]
    if lines:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    text = "\n".join([header, *body] + ([footer] if lines else []))
    return SlackMessage(text=text, blocks=blocks, failed=failed)


def _line(
    name: str,
    info: Dict,
    scorecard: Optional[Dict],
    columns: List[str],
    today: str,
    dashboard_url: str,
    issue_urls: Dict[str, str],
) -> FailureLine:
    timeline = info.get("timeline") or {}
    cells = [bool(timeline.get(key)) for key in columns]
    if columns and not cells[-1]:
        # The run's own failure list is authoritative for today.
        cells[-1] = True
    failed_runs = sum(cells)
    streak = scorecard.get("recent_streak", 0) if scorecard else 0
    first_seen = scorecard.get("first_seen") if scorecard else None
    if (first_seen or today) >= today and failed_runs <= 1:
        classification = NEW
    elif streak >= PERSISTENT_STREAK:
        classification = PERSISTENT
    else:
        classification = INTERMITTENT
    fingerprint = (scorecard or {}).get("fingerprint") or fingerprint_for_name(name)
    title, detail = _split_name(name)
    return FailureLine(
        display_name=name,
        fingerprint=fingerprint,
        classification=classification,
        bar="".join(_FAILED if c else _PASSED for c in cells),
        failed_runs=failed_runs,
        shown_runs=len(columns),
        title=title,
        detail=detail,
        dashboard_url=f"{dashboard_url}#test={fingerprint}",
        issue_url=issue_urls.get(fingerprint),
    )


def _split_name(name: str) -> Tuple[str, str]:
    """``(title, detail)`` for a display name: the test and its file, or the job and why."""
    pair = test_in_file(name)
    if pair is not None:
        return pair[0], pair[1].rsplit("/", 1)[-1]
    if name.endswith(": unattributed failure"):
        return name[: -len(": unattributed failure")], "job failed, no test attributed"
    if name.endswith(": server crash"):
        return name[: -len(": server crash")], "server crashed"
    if name.startswith("Exception: "):
        return name[len("Exception: "):], "test-client exception"
    if name.endswith(" (exception)"):
        return name[: -len(" (exception)")], "test-client exception"
    return name, "see dashboard"


def _pretty_day(day: str) -> str:
    try:
        when = datetime.strptime(day[:10], "%Y-%m-%d")
    except ValueError:
        return day
    return f"{when:%b} {when.day}"


def _issue_label(url: str) -> str:
    """``#N`` for a GitHub issue or pull URL, which is what people search Slack for."""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return f"#{tail}" if tail.isdigit() else "issue"


def _escape(text: str) -> str:
    """Slack mrkdwn treats ``&``, ``<`` and ``>`` as markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section(text: str) -> Dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _chunks(lines: List[str], limit: int = _SECTION_LIMIT) -> List[str]:
    """Join lines into as few sections as Slack's size limit allows."""
    chunks: List[str] = []
    current = ""
    for line in lines:
        candidate = line if not current else current + "\n" + line
        if current and len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def post(message: SlackMessage, webhook_url: str, *, timeout: int = 30) -> None:
    """POST the message to an incoming webhook; raises on a non-2xx answer."""
    body = json.dumps(message.payload()).encode("utf-8")
    req = Request(webhook_url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - the URL is the operator's secret
        if not 200 <= resp.status < 300:
            raise RuntimeError(f"Slack answered {resp.status}")
