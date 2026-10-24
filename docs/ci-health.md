# CI health

Valkey's Daily CI gives every test a verdict every night, and the
[Test Failure Detector](test-failure-detector.md) turns each failing test into
an issue. What neither of them keeps is the history: how often a test has
failed over the last month, whether it is flaky or simply broken, and which
commits turned it from green to red. CI health is that history. It records
every Daily run in a local store and computes per-test statistics over it.

## Status

The store, the statistics and the workflow that fills the store are in. The
workflow uploads its results as JSON; the published dashboard and the Slack
report follow as separate changes. The design is in
[issue 115](https://github.com/valkey-io/valkey-ci-agent/issues/115). The
code is ported from the
[valkey-daily-dashboard](https://github.com/rainsupreme/valkey-daily-dashboard)
prototype ([live dashboard](https://rainsupreme.github.io/valkey-daily-dashboard/)),
running since April 2026 over Daily history backfilled to March 2025.

## When it runs

Daily CI starts at midnight UTC and usually finishes within four to seven
hours. CI health runs at 06:00 UTC, and a run that comes too early for that
night's Daily simply records it the next morning: the sync lists every run
GitHub has and stores the ones it has not seen, so a late Daily is never
skipped. The weekly release-branch run is split and ingested the same way.

## What it does each run

The workflow carries the history store from run to run as an artifact of
its own: each run downloads the newest one an earlier run uploaded, adds to
it, and uploads it again. With no earlier history it starts from what GitHub
still has.

The sync lists the Daily, weekly and post-merge CI runs of `valkey-io/valkey`
on `unstable` and stores the ones it has not seen. (Daily also runs on pull
requests; those runs are not reported on, so the workflow does not store
them.) For each new failed run it fetches the
jobs, and for each failed job it downloads the log once, parses it, and stores
the failures. GitHub keeps job logs for about 90 days; a job whose log has
expired is recorded as such and never asked for again, and its conclusion
still counts in the run's history.

The weekly workflow's runs are split into one synthetic run per release
branch. That backlog is worked through within a budget of API calls per run,
newest first, so steady state costs only the newest week.

Then the scorecards and blame ranges are computed over the whole store and
written as JSON. Nothing is written to any repository, issue or comment, and
the only GitHub token the job holds can read Actions and contents, not write
them.

## What it computes

The store keeps runs, their jobs, and the failures parsed from each job. The
Daily workflow is read one run per calendar day; the per-commit CI workflow is
read one run per run. The weekly workflow fires once on Sunday and runs
`daily.yml` once per release branch inside a single run, so the store splits
that run into one synthetic run per branch and every consumer sees an ordered
series per branch, the same as for `unstable`.

Some failures never reach the `all-test-failures` artifact: a test that timed
out while spawning a server, or a valgrind or sanitizer job that failed with
no test attributed. The log parser reads the failed job's log for those, as
the detector's `job_failures.py` does for its `[JOB-FAILURE]` causes. It also
reads attributed Tcl failures, test framework exceptions, and server crashes,
and keeps a per-job bucket for a failed job it cannot name at all.

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

A scorecard row and the detector's issue for the same failure should share one
id, so the dashboard can link to the issue and the issue can link back to the
history. `fingerprint.py` is that contract, and it reaches the detector's keys
through the detector's own functions rather than a copied formula.

The detector files two kinds of issue. A `[TEST-FAILURE]` is keyed by the
`(test_name, test_file)` pair runtest prints as `<name> in tests/<file>.tcl`;
an attributed display name is split with the detector's `test_in_file`, which
folds a test client's `pid:NNN` and `port NNN` state the same way, and hashed
with `issue_renderer.fingerprint_for`. A `[JOB-FAILURE]` is keyed by its
cause, the runtest summary line `[<status>]: <data>` that names no test, with
the job and step left out so one cause failing in several jobs is one issue;
a cause line is hashed with `issue_renderer.job_fingerprint_for`.

Any other display name, a per-job `unattributed failure` or `server crash`
bucket or an exception the parser could name only by message, gets a
ci_health-local id under its own namespace element. It is stable for deep
links and can never collide with a detector id, but it is not one: the
`[JOB-FAILURE]` key is the cause line, which a display name does not carry.
Looking that issue up takes the cause itself, which the sync layer reads from
the detector's `job_failures` reader.

The parity tests run the detector's `find_unrecorded_failures` on a valgrind
log so the cause under test is the one the detector builds, and pin that the
local id equals neither detector key.

## Where the code lives

```text
scripts/ci_health/
  store.py        SQLite history: runs, jobs, parsed failures
  log_parser.py   job-log parser: attributed Tcl failures, timeouts,
                  exceptions, crashes, and the per-job bucket
  fingerprint.py  the detector's two issue keys, reached through the
                  detector's own functions, so a scorecard row and its
                  [TEST-FAILURE] or [JOB-FAILURE] issue share one id
  scorecard.py    per-test streak, failure rate, classification, trend
  blame.py        green-to-red onset per test, commit range, confidence
  stats.py        Beta-Bernoulli regression confidence
  windowing.py    per-day and per-run keying of a workflow's history
  weekly.py       splits the weekly release-branch fan-out into per-branch runs
  sync.py         fills the store from GitHub Actions: runs, jobs, logs,
                  the weekly split; the only module that calls GitHub
  main.py         the command line: sync, scorecard, blame
```

`sync.py` reads GitHub through a small `RunsClient` protocol (list runs, list
jobs, one job log, compare two commits) so the sync and the statistics are
tested against an in-memory fake. The real client is PyGithub plus the shared
`ArtifactClient` for logs, which keeps the token off GitHub's redirect to
storage and answers an expired log with nothing rather than an error.

Tests live in `tests/test_ci_health_*.py`, one module per source module, plus
real-world log captures in `test_ci_health_log_parser_realworld.py`.

## Running it by hand

```bash
gh workflow run ci-health.yml --repo valkey-io/valkey-ci-agent
gh workflow run ci-health.yml --repo valkey-io/valkey-ci-agent --field since=2026-09-01T00:00:00Z
```

`since` also lists runs created from that time on, for filling a gap. The
`ci-health-history` artifact of the run is the store; the
`ci-health-report-<run id>` artifact holds `scorecards.json`, `blame.json`
and the sync summary.

Locally, with a token that can read Actions on `valkey-io/valkey`:

```bash
python -m scripts.ci_health.main --db history.sqlite --token "$GITHUB_TOKEN" sync --workflow daily
python -m scripts.ci_health.main --db history.sqlite scorecard --days 30
python -m scripts.ci_health.main --db history.sqlite --token "$GITHUB_TOKEN" blame --days 30
```

`scorecard` needs no token. `blame` without one still reports every onset,
with an empty commit range.

## Configuration

The workflow uses the shared GitHub App described in [setup.md](setup.md),
with `actions: read`, `contents: read` and `metadata: read` on
`valkey-io/valkey`. Downloading the earlier history uses the workflow's own
token with `actions: read` on this repository.
