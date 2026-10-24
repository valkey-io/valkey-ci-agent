"""Tests for the Slack message built from the report data."""

from __future__ import annotations

import json
from typing import Dict, List

from scripts.ci_health import slack_report
from scripts.ci_health.slack_report import (
    INTERMITTENT,
    NEW,
    PERSISTENT,
    FailureLine,
    SlackMessage,
    _chunks,
    _split_name,
    build_message,
)

DASH = "https://valkey-io.github.io/valkey-ci-agent/"
DAYS = [f"2026-10-{d:02d}" for d in range(11, 25)]  # 14 columns, newest last


def _data(failures: List[str], timelines: Dict[str, List[str]], *, status: str = "failure",
          failed_jobs: int = 3) -> Dict:
    """Report data with one newest run; ``timelines`` says which days each test failed before."""
    tests = {}
    for name, days in timelines.items():
        tests[name] = {"timeline": {d: ({"count": 1} if d in days else None) for d in DAYS}}
    return {
        "summary": {"repo": "valkey-io/valkey"},
        "columns": [{"key": d} for d in DAYS],
        "runs": [{"run_id": 40044203787, "day": DAYS[-1], "status": status, "commit_sha": "5c6a0cdcf32",
                  "total_jobs": 56, "failed_jobs": failed_jobs, "failure_names": failures}],
        "tests": tests,
    }


def _card(name: str, *, first_seen: str, streak: int = 0, fingerprint: str = "") -> Dict:
    return {"test_name": name, "first_seen": first_seen, "recent_streak": streak,
            "fingerprint": fingerprint or f"fp-{abs(hash(name)) % 10**6}"}


class TestBuildMessage:
    def test_header_lines_and_footer(self) -> None:
        new = "Active defrag - AOF loading in tests/unit/memefficiency.tcl"
        broken = "spawn timeout in tests/integration/repl-compression.tcl"
        flaky = "test-macos-latest: server crash"
        data = _data([flaky, new, broken], {
            new: [],
            broken: DAYS[3:],
            flaky: [DAYS[5], DAYS[6], DAYS[8]],
        })
        cards = [
            _card(new, first_seen=DAYS[-1], fingerprint="aaa"),
            _card(broken, first_seen=DAYS[3], streak=11, fingerprint="bbb"),
            _card(flaky, first_seen=DAYS[5], streak=1, fingerprint="ccc"),
        ]
        msg = build_message(data, cards, dashboard_url=DASH)
        assert msg.failed
        lines = msg.text.split("\n")
        assert lines[0].startswith("🔴 *Daily CI failed* — <https://github.com/valkey-io/valkey/actions/runs/40044203787|Oct 24>")
        assert "3 of 56 jobs" in lines[0] and "|5c6a0cd>" in lines[0] and f"<{DASH}|dashboard>" in lines[0]
        # new -> persistent -> intermittent, whatever order the run listed them in
        assert [ln[:1] for ln in lines[1:4]] == ["🆕", "🔴", "🟡"]
        assert lines[1] == f"🆕  {'⬜' * 13}🟥   1/14   *Active defrag - AOF loading* — memefficiency.tcl  <{DASH}#test=aaa|↗>"
        assert lines[2].startswith(f"🔴  {'⬜' * 3}{'🟥' * 11}  11/14   *spawn timeout* — repl-compression.tcl")
        assert lines[3].startswith("🟡  ⬜⬜⬜⬜⬜🟥🟥⬜🟥⬜⬜⬜⬜🟥   4/14   *test-macos-latest* — server crashed")
        assert lines[4] == "last 14 daily runs, newest right · 🆕 first failure · 🔴 fails most runs · 🟡 intermittent"
        assert [b["type"] for b in msg.blocks] == ["section", "section", "context"]

    def test_todays_failure_fills_the_newest_cell_even_before_the_store_has_it(self) -> None:
        name = "t in tests/unit/x.tcl"
        data = _data([name], {name: []})
        msg = build_message(data, [], dashboard_url=DASH)
        assert "⬜" * 13 + "🟥   1/14" in msg.text

    def test_classification_rules(self) -> None:
        name = "t in tests/unit/x.tcl"
        data = _data([name], {name: DAYS[-3:]})
        # Failed three runs straight: persistent.
        assert build_message(data, [_card(name, first_seen=DAYS[0], streak=3)], dashboard_url=DASH).text.split("\n")[1][:1] == "🔴"
        # Same history, but a streak below the threshold: intermittent.
        assert build_message(data, [_card(name, first_seen=DAYS[0], streak=2)], dashboard_url=DASH).text.split("\n")[1][:1] == "🟡"
        # First seen today and only today: new, whatever the streak field says.
        fresh = _data([name], {name: []})
        assert build_message(fresh, [_card(name, first_seen=DAYS[-1], streak=1)], dashboard_url=DASH).text.split("\n")[1][:1] == "🆕"
        # No scorecard at all (first failure ever): new.
        assert build_message(fresh, [], dashboard_url=DASH).text.split("\n")[1][:1] == "🆕"

    def test_issue_link_when_the_detector_has_one(self) -> None:
        name = "t in tests/unit/x.tcl"
        msg = build_message(_data([name], {name: []}), [_card(name, first_seen=DAYS[-1], fingerprint="abc")],
                            dashboard_url=DASH, issue_urls={"abc": "https://github.com/valkey-io/valkey/issues/5000"})
        assert "<https://github.com/valkey-io/valkey/issues/5000|issue> · <" in msg.text

    def test_passed_run_is_green_and_not_failed(self) -> None:
        msg = build_message(_data([], {}, status="success", failed_jobs=0), [], dashboard_url=DASH)
        assert not msg.failed
        assert msg.text.startswith("🟢 *Daily CI passed* —") and "56 jobs" in msg.text
        assert "\n" not in msg.text  # no lines, no footer
        assert [b["type"] for b in msg.blocks] == ["section"]

    def test_failed_run_without_test_names_says_so(self) -> None:
        msg = build_message(_data([], {}, status="failure"), [], dashboard_url=DASH)
        assert msg.failed
        assert "jobs failed without a test failure on record" in msg.text

    def test_no_runs_at_all(self) -> None:
        msg = build_message({"summary": {}, "columns": [], "runs": [], "tests": {}}, [], dashboard_url=DASH)
        assert not msg.failed and "no run on record" in msg.text

    def test_slack_markup_in_names_is_escaped(self) -> None:
        name = "SET a <b> & c in tests/unit/type/string.tcl"
        msg = build_message(_data([name], {name: []}), [], dashboard_url=DASH)
        assert "*SET a &lt;b&gt; &amp; c*" in msg.text

    def test_many_lines_split_into_sections_under_slacks_limit(self) -> None:
        names = [f"test number {i} with a reasonably long name to fill the block in tests/unit/file{i}.tcl" for i in range(60)]
        msg = build_message(_data(names, {n: [] for n in names}), [], dashboard_url=DASH)
        sections = [b for b in msg.blocks if b["type"] == "section"][1:]
        assert len(sections) > 1
        assert all(len(s["text"]["text"]) <= 3000 for s in sections)
        assert sum(s["text"]["text"].count("🆕") for s in sections) == 60

    def test_payload_is_json_serialisable(self) -> None:
        name = "t in tests/unit/x.tcl"
        msg = build_message(_data([name], {name: []}), [], dashboard_url=DASH)
        json.dumps(msg.payload())


class TestNames:
    def test_split_name(self) -> None:
        assert _split_name("spawn timeout in tests/integration/repl-compression.tcl") == ("spawn timeout", "repl-compression.tcl")
        assert _split_name("test-valgrind-test (unit): unattributed failure") == ("test-valgrind-test (unit)", "job failed, no test attributed")
        assert _split_name("test-macos-latest: server crash") == ("test-macos-latest", "server crashed")
        assert _split_name("Exception: CLUSTERDOWN The cluster is down") == ("CLUSTERDOWN The cluster is down", "test-client exception")
        assert _split_name("something else") == ("something else", "see dashboard")

    def test_chunks(self) -> None:
        assert _chunks([]) == []
        assert _chunks(["a", "b"], limit=10) == ["a\nb"]
        assert _chunks(["aaaa", "bbbb", "cc"], limit=9) == ["aaaa\nbbbb", "cc"]
        assert _chunks(["x" * 20], limit=9) == ["x" * 20]  # one oversize line is still one chunk

    def test_line_mrkdwn_shape(self) -> None:
        line = FailureLine("n", "fp", NEW, "⬜🟥", 1, 2, "title", "detail", "u#test=fp")
        assert line.mrkdwn() == "🆕  ⬜🟥   1/2   *title* — detail  <u#test=fp|↗>"
        assert NEW != PERSISTENT != INTERMITTENT


class TestPost:
    def test_post_sends_the_payload(self, monkeypatch) -> None:
        sent = {}

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout):
            sent["url"] = req.full_url
            sent["body"] = json.loads(req.data)
            sent["content_type"] = req.get_header("Content-type")
            return _Resp()

        monkeypatch.setattr(slack_report, "urlopen", fake_urlopen)
        slack_report.post(SlackMessage(text="hi", blocks=[{"type": "section"}]), "https://hooks.slack.test/abc")
        assert sent == {"url": "https://hooks.slack.test/abc", "body": {"text": "hi", "blocks": [{"type": "section"}]},
                        "content_type": "application/json"}
