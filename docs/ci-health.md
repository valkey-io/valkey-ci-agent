# CI health

Valkey's Daily CI gives every test a verdict every night, and the
[Test Failure Detector](test-failure-detector.md) turns each failing test into
an issue. What neither of them keeps is the history: how often a test has
failed over the last month, whether it is flaky or simply broken, and which
commits turned it from green to red. CI health is that history. It records
every Daily run in a local store and computes per-test statistics over it.

## Status

This is the statistics layer only. It reads and writes a SQLite file and never
calls GitHub, so it is tested without stubs. The workflow that fills the store
from Daily runs, the published dashboard and JSON, and the Slack report follow
as separate changes. The design is in
[issue 115](https://github.com/valkey-io/valkey-ci-agent/issues/115). The
code is ported from the valkey-daily-dashboard prototype, which has run since
April 2026 over Daily history backfilled to March 2025.

## What it computes

The store keeps runs, their jobs, and the failures parsed from each job. The
Daily workflow is read one run per calendar day; the per-commit CI workflow is
read one run per run. The weekly workflow fires once on Sunday and runs
`daily.yml` once per release branch inside a single run, so the store splits
that run into one synthetic run per branch and every consumer sees an ordered
series per branch, the same as for `unstable`.

Some failures never reach the `all-test-failures` artifact: a test that timed
out while spawning a server, or a valgrind or sanitizer job that failed with
no test attributed. The log parser reads the failed job's log for those, and
is the only source for them. It also reads attributed Tcl failures, test
framework exceptions, and server crashes, and keeps a per-job bucket for a
failed job it cannot name at all.

A scorecard per test gives its current streak, failure rate, trend, and a
classification: `persistent` when it fails at least half of its recorded runs
or has failed every run for several consecutive days, `rare` when it has
failed on exactly one day in all recorded history, and `flaky` for everything
in between. The bands are counts rather than a rate threshold because with one
run a day the smallest nonzero rate is one over the number of recorded days.

Blame finds each test's green-to-red onset: the last run it passed and the
first run it failed, and the commits between those two heads. How sure it is
that this is a regression rather than the test's usual flakiness comes from a
Beta-Bernoulli model: the test's baseline failure rate is learned from its own
history before the onset, and the confidence is how surprising the failures
after the onset are under that baseline. Blame takes the commit comparison
through a `CommitComparer` protocol, so the statistics have no GitHub
dependency; the sync change supplies the real comparer.

## Shared failure identity

A scorecard row and the detector's issue for the same test should share one
id, so the dashboard can link to the issue and the issue can link back to the
history. `fingerprint.py` is that contract. The detector keys a
`[TEST-FAILURE]` issue by `(test_name, test_file)` through
`compute_fingerprint` under its marker namespace; `fingerprint.py` maps a
display name onto the same pair and applies the same call. Failures with no
attributed test use `test_file="unknown"`, the fallback
`tests/test_helper.tcl` writes for a line it cannot parse. A parity test pins
the two against each other.

## Where the code lives

```text
scripts/ci_health/
  store.py        SQLite history: runs, jobs, parsed failures
  log_parser.py   job-log parser: attributed Tcl failures, timeouts,
                  exceptions, crashes, and the per-job bucket
  fingerprint.py  the detector's issue key, so a scorecard row and its
                  [TEST-FAILURE] issue share one id
  scorecard.py    per-test streak, failure rate, classification, trend
  blame.py        green-to-red onset per test, commit range, confidence
  stats.py        Beta-Bernoulli regression confidence
  windowing.py    per-day and per-run keying of a workflow's history
  weekly.py       splits the weekly release-branch fan-out into per-branch runs
```

Tests live in `tests/test_ci_health_*.py`, one module per source module, plus
real-world log captures in `test_ci_health_log_parser_realworld.py`.
