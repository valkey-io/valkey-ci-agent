# CI health

Valkey's Daily CI gives every test a verdict every night, and the
[Test Failure Detector](test-failure-detector.md) turns each failing test into
an issue. What neither of them keeps is the history: how often a test has
failed over the last month, whether it is flaky or simply broken, and which
commits turned it from green to red. CI health is that history. It records
every Daily run in a local store and computes per-test statistics over it.

## Status

The store, the statistics, the workflow that fills the store, the dashboard
it renders and the Slack message are in. The dashboard is published to
GitHub Pages once the repository enables it; until then every run uploads it
as an artifact. The Slack message can be rendered and posted by hand; the
workflow does not post it yet, and the link from each dashboard row to the
Test Failure Detector's issue follows with that integration. The design is in
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
them.) A workflow the store already knows is listed from a few days before
its newest stored run rather than from the beginning, and a run first seen
while it was still running is refreshed once it has a conclusion. For each
new failed run it fetches the jobs, and for each failed job it downloads the
log once, parses it, and stores the failures. GitHub keeps job logs for about 90 days; a job whose log has
expired is recorded as such and never asked for again, and its conclusion
still counts in the run's history.

The weekly workflow's runs are split into one synthetic run per release
branch. That backlog is worked through within a budget of API calls per run,
newest first, so steady state costs only the newest week.

Then the site is rendered from the whole store: `index.html`, the dashboard;
`releases.html`, the same views per release branch from the weekly runs;
`report.md`, a Markdown summary; and the JSON exports `report.json`,
`report-ci.json`, `report-releases.json`, `scorecards.json` and `blame.json`.
A newest Daily run older than two days fails the run instead of publishing,
since a regular nightly cadence makes that the sign of a dead token or a
stalled sync; the per-commit CI and weekly views only warn, because merges are
bursty and the weekly backfill may still be in progress. Nothing is written to
any repository, issue or comment, and the only GitHub token the job holds can
read Actions and contents, not write them.

## The dashboard

The front page shows the `unstable` branch. A strip of badges gives each
release branch's health from the latest weekly run, then four tabs:

- **Heatmap**: one row per failing test, one column per run, the per-commit
  CI runs on top and the nightly Daily below. A red run with no test cells
  under it failed in build, setup or a sanitizer rather than in a test. Rows
  with identical failure days are folded into one expandable cluster row,
  since one regression breaking many tests at once would otherwise fill the
  table with identical lines. A ⚠️ marks a test the statistics judge a likely
  regression.
- **Regressions**: each green-to-red onset with the commits between the last
  pass and the first failure, the test's baseline rate before the onset and
  how surprising the failures since are under it; onsets that have since gone
  quiet are folded away as likely fixed.
- **Run Details**: each run with its failed jobs and the commits it added.
- **Flakiness Scorecard**: every test with failures on record, with its
  class, rate, trend and streak; tests quiet for long enough are folded away
  as resolved.

Both pages warn about their own age. The page is static, so whether its data
is current depends on when it is read, not when it was rendered: a script
compares the newest run's date with the reader's clock and shows a banner
when a Daily run is more than two days old or a weekly run more than nine.
The same thresholds fail or warn the workflow, but a page that keeps being
served after the workflow has stopped has only the banner.

The page is one self-contained HTML file. Its styles and script live in
`scripts/ci_health/assets/` and are inlined at render time. A link of the
form `index.html#test=<fingerprint>` opens the scorecard and highlights that
test's row; the Slack message links each line that way.

## The Slack message

`valkey-io/valkey`'s `daily.yml` posts to Slack when the Daily run fails,
naming the failed jobs and linking the run. The message rendered here names
the tests instead, one line per unique failure, however many jobs it broke:

```
🔴 Daily CI failed — Oct 24 · 3 of 56 jobs · 5c6a0cd · dashboard
🆕  ⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜🟥   1/14   Active defrag - AOF loading — memefficiency.tcl  ↗
🔴  ⬜⬜⬜🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥  11/14   spawn timeout — repl-compression.tcl  ↗
🟡  ⬜⬜⬜⬜⬜🟥🟥⬜🟥🟥🟥⬜🟥🟥   7/14   test-macos-latest — server crashed  ↗
last 14 daily runs, newest right · 🆕 first failure · 🔴 fails most runs · 🟡 intermittent
```

The bar is the test's last 14 Daily runs, newest on the right, a filled cell
for each run it failed in. The leading mark is its class: first failure on
record, failed at least three runs straight, or intermittent. Lines are
sorted in that order. Each `↗` opens the test's row on the dashboard, and a
line whose failure the detector has filed also links the issue as `#N`. A run with
nothing failed renders a one-line green message that is only posted on
request. The message is built from the same report data as the dashboard, in
`slack_report.py`, so the two agree on what a run's failures are called.

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
  report.py       the dashboard: heatmap, regressions, run details and
                  scorecard tabs, as HTML and Markdown
  releases.py     the same views per release branch from the weekly runs
  assets/         the dashboard's stylesheet and script, inlined at render
  slack_report.py the Slack message: one line per failing test, with history
  main.py         the command line: sync, scorecard, blame, report, slack
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
`ci-health-site-<run id>` artifact holds the rendered site and the sync
summary.

Locally, with a token that can read Actions on `valkey-io/valkey`:

```bash
python -m scripts.ci_health.main --db history.sqlite --token "$GITHUB_TOKEN" sync --branch unstable
python -m scripts.ci_health.main --db history.sqlite --token "$GITHUB_TOKEN" report --site site
python -m scripts.ci_health.main --db history.sqlite scorecard --days 30
python -m scripts.ci_health.main --db history.sqlite --token "$GITHUB_TOKEN" blame --days 30
```

`report` writes the whole site to a directory and exits non-zero when the
Daily data is stale. `scorecard` needs no token. `blame` and `report` without
one still report every onset, with an empty commit range.

```bash
python -m scripts.ci_health.main --db history.sqlite slack --dry-run
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/... python -m scripts.ci_health.main --db history.sqlite slack
```

`slack` prints the message for the newest Daily run and, without
`--dry-run`, posts it to the webhook when that run failed (`--always` posts
a passing one too). `--issues` names a JSON file mapping a failure's
fingerprint to its issue URL, so lines can link their issues.

## Configuration

The workflow uses the shared GitHub App described in [setup.md](setup.md),
with `actions: read`, `contents: read` and `metadata: read` on
`valkey-io/valkey`. Downloading the earlier history uses the workflow's own
token with `actions: read` on this repository.

Publishing needs GitHub Pages enabled for this repository with GitHub Actions
as its source, then the `CI_HEALTH_PAGES` variable set to `true`. Until then
the publish job does not run and the site is only an artifact of each run.
