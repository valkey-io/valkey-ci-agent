# Design: PR CI failure tracker

Status: draft, for discussion before implementation.

## The problem

The [Test Failure Detector](../docs/test-failure-detector.md) only reads Daily
CI. When a test that is broken or flaky on `unstable` fails on a
contributor's PR, the author usually reruns the job or ignores it, and the
failure is never recorded anywhere. These are real failures on `unstable`,
and we lose them.

PR CI runs much more often than Daily, so it is the best evidence we have of
how a test behaves. But most red PR CI is the PR's own fault, so we can't
file an issue in `valkey` for every PR failure. A premature public issue also
draws contributors into investigating something that may not be real.

## The idea

There are two tiers and two stages:

1. **Collect, cheaply.** Every failed test in PR CI is recorded on an issue
   in a separate **tracker repository** (`tracker_repo`), one issue per test.
   There is no Claude here, only log parsing. The tracker issue is the state:
   each repeat failure adds a comment, so its comments are the count. Nobody
   watching `valkey` sees it.
2. **Analyse, rarely.** Only when a test reaches the limit (enough failed
   attempts from enough distinct PRs, on a test that already existed on
   `unstable`) does one analysis look at it. Reaching the limit never labels
   or promotes anything by itself. The analysis makes the call, with one of
   three outcomes:
   - **A real `unstable` problem.** The tracker issue is labelled
     `state:ready-to-promote`.
   - **The PRs' own problem.** The tracker issue is closed.
   - **Partly fake.** The failures that aren't the test's fault are
     excluded, and the issue keeps collecting until the limit is reached
     again.

It ships in [two phases](#phases). In phase 1, nothing reaches `valkey`
until a maintainer comments `@valkeyrie-ops promote` on a ready tracker
issue. In phase 2, once the phase 1 verdicts have earned trust, ready issues
are promoted automatically, under a daily cap.

Promoted issues are **report-only** in this version. The
[CI fix bot](../docs/ci-fix.md#fixing-detector-issues) only fixes scheduled
Daily runs (`_validate_scheduled_run` in `scripts/ci_fix/issues.py`), and it
plans on the source run's branch, which for a PR run is the contributor's.
So a promoted issue carries no `last-key`, and the dispatcher skips it. When
Daily later fails the same test, the detector updates the same issue and the
fix bot takes over as usual. [Fixing later](#fixing-later) describes how to
make these issues fixable without the bot ever planning from a PR run.

## Pipeline

```text
scheduled sweep (daily)
  → ci.yml pull_request runs created since the last successful sweep (minus 6h)
  → their attempts that completed since then (skip action_required)
  → failed jobs of each attempt (skip carried-over jobs)
  → parse logs into fingerprints (the detector's parser)
  → PR identity and tested base from the checkout log; ignore PRs not into unstable
  → one batched comment per test on its tracker issue
  → for each tracker issue touched this sweep, or labelled state:pending:
        an open valkey issue carries its marker?   → link it, nothing more
        limit not reached (again)?                  → nothing more
        resets → recount → gates still pass?        → analyse
        real unstable problem                       → label ready-to-promote
                                                       (phase 2: promote, under the cap)
        the PRs' own problem, or infra              → close as rejected
        some failures fake, or unsure               → exclude those, keep collecting

command poll (every 30 minutes)
  → @valkeyrie-ops promote on a tracker issue, from an authorized member → promote
```

## Stage 1: collection

### Which runs

Runs come from the workflows in `repos.yml` (`pr_failure_tracker.workflows`,
initially `ci.yml`) with `event == pull_request`.

- **The unit is an attempt, `(run_id, attempt)`.** The run object only
  reports its latest attempt, so its conclusion is never used to pick runs. A
  test that failed on attempt 1 and passed on the rerun is the flaky case we
  care about most, and listing only failed runs misses it.
- **Each sweep reads only what is new since the last successful sweep**,
  normally the last 24 hours. The start is the start time of the last
  successful scheduled run of this workflow, minus an hour of overlap, read
  from the Actions API, so no cursor is stored. The first sweep reads
  `window_days`.
- The sweep lists runs created after the start minus 6 hours, and reads
  their attempts that completed after the start. The 6 hours cover a run
  that was still going during the last sweep: `ci.yml` PR runs take a median
  of 25 minutes, and 99% finish within 2.6 hours.
- **Caveat: late reruns are missed.** A rerun keeps its run's `created_at`,
  and the list API only filters on creation. So a rerun of a run created
  before that listing window is never seen. In 30 days, 12 of 37 real reruns
  came more than a day after the run was created. The earlier attempts are
  still recorded; only the late attempt is lost, along with the flaky signal
  it carries. If it turns out to matter, the sweep can list 30 days of run
  metadata (about 17 pages, no logs) and keep the runs whose `updated_at` is
  after the start.
- Attempts concluded `action_required` are skipped. They are fork runs
  waiting for approval, with no jobs. In 14 days, 11 of 14 green runs with
  `run_attempt > 1` were this, not real reruns.
- **Anything cancelled is ignored, whatever the reason:** a manual cancel,
  `cancel-in-progress` after a new push, or anything else. That covers a job
  that concluded `cancelled`, and every job of an attempt that concluded
  `cancelled`, including jobs that had already failed. This drops a few real
  failures (4 of 40 sampled cancelled runs had one), in exchange for a
  simple rule.
- Otherwise, a job is read when it concluded `failure` or `timed_out`.
- A job whose `started_at` is before its attempt's `run_started_at` was
  carried over from an earlier attempt under a new job ID, and is skipped.
  Otherwise deduplicating on `job_id` double-counts.
- Jobs in `ci_followup_ignored_jobs` are skipped. A failed job with no parsed
  test (build break, lint, a crash with no summary) is listed in the job
  summary only. Job-level failures in PR CI are almost always the PR's own.
- A sweep that couldn't read a log, or stopped on a rate limit, ends as
  failed. The next sweep then starts from the same point and reads that work
  again. The keyed tracker comments make the overlap and any re-read write
  nothing twice. An expired log is recorded as expired, and doesn't fail the
  sweep.

### PR identity

`run.pull_requests` is empty for fork PRs (138 of 155 failed runs in 14
days), so it isn't the source. For each attempt:

1. From a job log, take the first `+<merge>:refs/remotes/pull/<n>/merge`
   fetch line and the matching `HEAD is now at <merge7> Merge <head> into <base>`
   line. Other `HEAD is now at` lines, such as the libbacktrace checkout, are
   ignored. `<head>` must equal the attempt's `head_sha`.
2. A fork controls `ci.yml`, so it can print any number. `GET /pulls/<n>`
   must match the run's head repository and head branch, and have `valkey`
   as its base repository. The PR may have moved on since the attempt, as
   with a daily sweep it often will. So the attempt's `head_sha` must be the
   PR's current head **or one of its commits** (`GET /pulls/<n>/commits`).
   A commit dropped by a force-push can't be checked, and that attempt is
   ignored. Then `pr` and `base_ref` come from that PR object. The head
   repository, which names the fork's owner, is only compared, never stored.
3. For a same-repo run with no such line, a single matching entry in
   `run.pull_requests` is used, with the same checks.
4. Otherwise the PR is unknown. A lookup by fork branch isn't used, because
   one branch can map to several PRs.

### Only PRs into `unstable`

`ci.yml` also runs on PRs into `8.0`, `9.x`, `cluster-v2` and `agent/**`. For
now these are **ignored entirely**. An attempt is collected only if its
verified PR has `base.ref == base_branch` (`unstable`) and its tested base is
known. Anything else gets no sighting, no count and no tracker issue: a PR
into another branch, an unknown PR, or a missing merge line. An unknown PR is
ignored too, because its target can't be confirmed.

The PR is resolved from the first failed job's log, so the other logs of an
ignored attempt are never downloaded. In 14 days, 152 of 222 test sightings
came from PRs into other branches, including `cluster-v2` Raft tests that
don't exist on `unstable`. Ignoring them takes the tracker from about 63
issues a fortnight to about 24.

### Tested base

`tested_base_sha` is the first parent of the merge commit in that line: the
commit the PR was really tested on. The run's `head_sha` is the PR head, and
the merge ref moves on every push, so the job log is the only reliable
source. A rerun replays the same merge commit. If there is no accepted line,
the base is unknown and the attempt is ignored.

The merge line is printed by the fork's own workflow, so `<base>` is checked
too. It must be a commit on `unstable` whose committer date is no later than
the run's `created_at`: GitHub can't have tested a base that didn't exist
yet. Otherwise the attempt is ignored. Without this check, a fork could claim
a base newer than the one it was tested on, so that a pre-fix failure passes
the reset check (E1). The check still lets a fork claim an *older* base.
That only makes its failures look pre-fix, so they are dropped, which is the
safe direction. Verifying the merge commit itself wouldn't help: commits
pushed to a fork can be read through `valkey`'s API, so a fork can create a
matching one. Faked failures in general are bounded by the distinct-PR gate,
not by this check.

### What a sighting is

One record per (test, file, job). A test failing in three jobs of one
attempt is three sightings but one failed attempt. A sighting lives as one
line in a tracker comment ([The tracker issue](#the-tracker-issue)); this is
what that line and the job it points to describe.

```json
{
  "test": "Replica buffer don't induce eviction",
  "file": "tests/unit/maxmemory.tcl",
  "kind": "tcl | gtest",
  "error": "first lines of the error, truncated",
  "pr": 1234,
  "pr_source": "log | run",
  "head_sha": "abc123…",
  "merge_sha": "fedcba…",
  "tested_base_sha": "0123ab…",
  "run_id": 123,
  "attempt": 2,
  "attempt_started_at": "2026-11-19T10:00:00Z",
  "job_id": 456,
  "job_name": "test-ubuntu-latest",
  "passed_in": {"attempt": 3, "job_id": 789}
}
```

`passed_in` is not recorded at collection. A passing rerun often arrives in
a later sweep than the failure, and the sweep doesn't read passing attempts.
Instead it is **computed at analysis time**, when the referenced jobs are
re-read anyway. For each eligible sighting, the run's later attempts are
listed by run ID, and `passed_in` is the first one that really ran a job with
the same name and passed it. Because the lookup is by run ID, it also finds
reruns that came after the collection window. It is a flakiness signal for
the analysis, and **doesn't add a counted attempt**.

### Identity: test name and file

A tracker issue is identified by two things only: **the test name and the
file it's in**. Everything else (runs, attempts, jobs, PRs, errors,
environments) changes from one sweep to the next, so it can't identify
anything. It lives in the issue's body and comments instead.

- **For people:** the title is the detector's, `[TEST-FAILURE] <test> in <file>`,
  and the body starts with `Test:` and `File:` lines. A search for a test
  name or a file path in the tracker repo finds its issue.
- **For the bot:** the marker holds `<fp>`, the detector's existing
  `issue_renderer.fingerprint_for`. That is a 20-hex-character hash of exactly
  those two values (`sha256("valkey-ci-agent:test-failure|<test>|<file>")`),
  with nothing else in it. It is hashed rather than written raw because a
  test name can contain anything, including `-->` or another marker. A raw
  name would break the HTML comment, or forge a marker (see
  [Security](#security)). The bot finds the issue by matching the marker
  exactly, never by title search, which is fuzzy and rate-limited.

Because the hash is the detector's, a PR sighting and a Daily issue for the
same test share it. The tracker issue's marker uses its own namespace,
`valkey-ci-agent:pr-failure:<fp>`, which the detector's and fix bot's regexes
never match. The promoted issue uses the detector's marker, so one test gets
one issue. Log parsing (`job_failures.py`, `valkey_ci.py`) is reused as is;
PR CI has no `all-test-failures` artifact, so only the job-log path is used.

## Where state lives

There is no stats branch, no cursor, no artifact, and no store of the
tracker's own:

| What | Where |
|---|---|
| Sightings, counts, resets, verdicts | issues in `tracker_repo`, one per test and file |
| Where the next sweep starts | the last successful sweep run, from the Actions API |
| Confirmed, analysed failures | the promoted issue in `valkey` |

`tracker_repo` should be an issues-only repository in the org, such as
`valkey-io/valkey-ci-tracker`, rather than a real fork of `valkey`. A fork
has issues disabled by default, joins `valkey`'s fork network, and on a
personal account needs the App installed there. The tracker repository holds
no code and is never a `repos.yml` target, so no other workflow acts on it.

Alternatives considered:

- **A stats branch here:** rejected. An App token with `contents: write` can
  push to any branch, so it would need a ruleset with a force-push exception,
  plus a cursor, push retries and a monthly squash.
- **S3:** rejected. It is an external dependency for self-hosting.
- **Issues in `valkey`:** rejected. Unconfirmed failures would be public and
  would draw contributors in.
- **Stateless recompute only:** viable, but every sweep would re-read the
  whole window, about 630 REST calls and 360 MB of logs for 21 days, and
  maintainers would see nothing below the threshold. With the tracker, a
  sweep reads one day: about 100 calls, and only the logs of that day's
  failed jobs. One recompute can still rebuild the tracker.

**Cost (measured 2026-11-13 to 11-26).** About 700 `ci.yml` PR runs in 14
days, 160 of them failed, about 240 failed jobs across all attempts, about
1 MB per test-job log. That is about 25 sightings and about 17 new failed
jobs a day.

**Burst case.** A broken `unstable` commit can fail every PR at once, around
300 writes in one sweep, more than GitHub's 80 content writes a minute. So
there is one comment per test per sweep, writes are throttled to about
one a second, and a 403 or 429 stops the sweep, which the next one repeats.

## The tracker issue

The title is `[TEST-FAILURE] <test> in <file>`. The body has the `Test:` and
`File:` lines and the marker `<!-- valkey-ci-agent:pr-failure:<fp> -->`.
Each sweep adds at most one bot comment per issue, with one line per new
sighting:

```text
<!-- valkey-ci-agent:pr-failure:sighting run=<id> attempt=<n> job=<id> pr=<n> head=<sha> base=<sha> -->
```

Sightings are deduplicated on `(run, attempt, job)` across all comments.
`IssueDedupPublisher`'s `last-key` remembers only the last key, so
interleaved runs would be double-counted with it.

Only comments by the bot with a valid sighting line count, and issues are
locked on creation. The issue is an index, not evidence: at the gate, the
referenced jobs are re-read from GitHub.

PR numbers are written as plain text in code spans, never as `#n` or a PR
URL. Those would add "mentioned this" events to the contributor's PR, which
is the noise this design avoids. Job URLs are fine. No author is ever
recorded (see [Security](#security)).

| State | GitHub form | Meaning |
|---|---|---|
| collecting | open, `state:collecting` | below the limit, or analysed and kept with some failures excluded |
| pending | open, `state:pending` | the limit was reached; analysis is owed, and every sweep retries it |
| ready | open, `state:ready-to-promote`, `verdict:<v>` | the analysis found a real problem; waiting for the command (phase 1) or the cap (phase 2); resets are re-checked every sweep |
| promoted | open, `state:promoted`, links the `valkey` issue | no analysis while that issue is open |
| has-main-issue | open, `state:has-main-issue` | a Daily issue exists; collect only |
| rejected | closed `not_planned` by the bot, with a verdict marker | the PRs' own problem; its failures no longer count |
| fixed | closed `completed`, with a reset marker | the count restarts at the reset |
| stale | closed `not_planned` by the bot | no eligible sighting in `window_days` |
| muted | closed `not_planned` by a person | keep recording, never analyse |

A new eligible sighting reopens a `fixed`, `stale` or `rejected` issue.
Closed issues are the history, so nothing needs pruning or squashing.

## Clearing and resets

### Expiry

Sightings older than `window_days` (14) don't count, and a tracker issue with
none in the window is closed as `stale`. Resets and exclusions are comments,
so they never expire while a pre-fix base could still produce a sighting.

### Resetting a test when it is fixed

A failure from code that has since been fixed says nothing about the fix, so
it must not count. Resets are worked out **only when a test would
become a candidate**, from the `unstable` commits since the oldest eligible
sighting's base. A fix is:

1. **The test changed.** A commit's diff touches the `test "<name>"` block
   (Tcl) or the `TEST(Suite, Name)` body (gtest). A refactor can trigger
   this; that is the safe mistake, because it only delays an analysis.
2. **A PR closed the `valkey` issue.** A PR merged into `unstable` closes it
   through a closing reference. A PR that only *references* the issue hasn't
   fixed it, and backports reference issues too.
3. **The `valkey` issue was closed as completed.** The anchor is the close
   event's commit, else the closing PR's merge commit, else `unstable` HEAD
   at the time.
4. **A maintainer closed the tracker issue as completed.** The anchor is
   `unstable` HEAD at the time. This replaces v1's `workflow_dispatch` reset,
   which couldn't be rebuilt from GitHub data.

A reset is a bot comment,
`<!-- valkey-ci-agent:pr-failure:reset anchor=<sha> reason=<1-4> source=<…> -->`.
It closes the issue as completed. From then on
a sighting counts only if its `tested_base_sha` contains the newest anchor
(`git merge-base --is-ancestor` against a blobless clone of `unstable`).

If the post-reset sightings pass the gates again, the analysis is told which
fix reset the test, and the promoted issue says that fix didn't hold. This
happened in the sample window: #5515 was closed by 80bb13e, and five later
failures from 4 PRs crossed the gates. Daily filed #5553 about 5 hours
earlier.

## Stage 2: analysis

### When it triggers

The unit is a **failed attempt**: one `(run_id, attempt)` in which the test
failed, however many jobs it failed in. A passing attempt never counts.

A sighting is **eligible** only if:

- **E1:** it contains the newest reset anchor, if any.
- **E2:** for a named test, its file at `tested_base_sha` defines it. `$var`
  and `[...]` in Tcl names are wildcards, since 620 of 5,347 Tcl names are
  interpolated. This drops tests the PR added and names nobody wrote.
- **E3:** its attempt started within `window_days`.

A test becomes a **candidate** when code confirms all of these, before
any Claude call:

| Gate | Default | Counts |
|---|---|---|
| `threshold_failures` | 3 | eligible failed attempts |
| `min_distinct_prs` | 3 | verified PRs with an eligible failure whose own change doesn't touch the test |

"Its own change" is the diff that sighting's attempt really tested,
`tested_base_sha...head_sha` (the compare API, by SHA). It isn't the PR's
current diff: a PR can change the test in one push and revert it in the
next, and each sighting is judged by what it ran.

Excluded sightings (see [Step 2](#step-2-claude-verdict)) never count. The
test must also have no open `valkey` issue with its marker, and must
not already be `ready`. After an analysis that keeps the issue collecting,
the next one waits until the **limit is reached again**: `threshold_failures`
new eligible attempts since that analysis, with both gates still holding.
The days spanned aren't a gate, but go into the facts. A fresh
regression arrives as a same-day burst across PRs, which is the case to
catch fast.

| Case | Result |
|---|---|
| One PR fails test X in 5 attempts | 1 PR: no analysis |
| 2 PRs fail X 4 times between them | 2 PRs: no analysis |
| Attempt 1 fails X, attempt 2 passes | 1 failed attempt, `passed_in=2` |
| The PR added test X | E2 fails: never counts |
| 3 PRs, one failure each | candidate |

Over 14 days of real data, these defaults give 3 candidates (#5215,
#5515/#5553, #5232), all genuine cross-PR failures. The next test down failed
3 times in 2 PRs, so the PR gate held it back.

### Step 1: deterministic facts (no Claude)

Every eligible sighting with its ID (`<run>-<attempt>-<job>`); how many
sightings each of E1 to E3 dropped; for each sighting, whether its own
change (as above) touched the test or a file the error names; per PR, its
eligible failures and whether it merged; `passed_in` for each sighting,
computed as above, and the days spanned; whether Daily failed the test
in the window; the reset, if any; and `unstable` commits in the window that
touched the test or the files the error names. These are evidence for step 2,
and none rejects a candidate on its own.

### Step 2: Claude verdict

The analysis runs in **its own job**, with no GitHub token, only OIDC for
Bedrock. It uses a new profile, `pr_failure_analysis_readonly`, in
`scripts/ai/runtime.py`, modelled on `ci_fix_review`: `Read,Grep`,
`confined=True`, `isolated=True`, no writes. The fuzzer profile isn't
confined, so it isn't reused. The work directory holds `unstable`, the facts
and capped error excerpts and the per-sighting diffs, never PR code.

```json
{"verdict": "unstable_regression|flaky|poorly_isolated|pr_caused|infra|unsure",
 "confidence": "low|medium|high",
 "rationale": "<= 1200 chars",
 "evidence": ["<sighting id>", "…"],
 "exclude": [{"id": "<sighting id>", "reason": "<= 200 chars"}],
 "suspected_commit": "<sha>|null",
 "fix_area": "test|harness|src|deps|unknown"}
```

`exclude` lists the failures that aren't the test's fault, for example one
PR that broke the test while the others hit a real flake. It is parsed with
`extract_json_object(strict=True)`; anything else is `unsure`. Evidence and
exclusion IDs and commits that aren't in the facts are dropped. A separate
publish job validates the verdict again before any write.

Code first applies the exclusions as one bot comment,
`<!-- valkey-ci-agent:pr-failure:excluded ids=<id>,… -->`, with each reason
fenced. Then it recounts, and acts on the verdict:

| Verdict | Gates after exclusions | Action |
|---|---|---|
| `unstable_regression`, `flaky`, `poorly_isolated` | still pass | label `state:ready-to-promote` and `verdict:<v>`, and post the rationale (phase 2: promote, under the cap) |
| `unstable_regression`, `flaky`, `poorly_isolated` | no longer pass | keep collecting until the limit is reached again |
| `pr_caused`, `infra` | any | close as rejected; its sightings no longer count |
| `unsure` | any | keep collecting; the third `unsure` labels it `needs-human-triage` |

The verdict is never the only gate. The deterministic gates decide whether
analysis happens. An exclusion can only lower a count, never raise one. A
person's command, or in phase 2 the cap, decides what reaches `valkey`.

## Promotion: the `valkey` issue

- **Phase 1: by command only.** A member of `valkey-io/contributors`
  comments `@valkeyrie-ops promote` on the tracker issue. Usually the issue
  is `ready`, but the command also works on any open tracker issue, as an
  override of the analysis. A poll every 30 minutes picks it up, the same
  way as `@valkeyrie-ops fix`. It uses `ci_fix/gate.py`'s `parse_command`
  pattern and `is_authorized`, which fails closed. The bot reacts to the
  comment, and replies with the `valkey` issue or the reason it refused.
  Because tracker issues are locked, the team needs write access to
  `tracker_repo`.
- **Phase 2: automatic.** `ready` issues are promoted with no command, at most
  `max_promotions_per_day` (2) in 24 hours, counted from `state:promoted`
  labels. Overflow stays `ready`. The command still works, and isn't capped.
- **Re-checked before every promotion.** A test can be fixed while its
  issue waits as `ready`. So every sweep re-checks resets for `ready`
  issues, and any promotion, by command or automatic, recomputes resets
  right before it creates the issue. Automatic promotion also recomputes the
  gates. If the test was fixed since its counted failures, or the gates no
  longer pass, the issue goes back to collecting and the bot comments why.
  For a command, the reply carries that reason.
- **Creation.** Through the detector's publisher with a new
  `create_only=True`, which never updates an open issue. It uses the
  detector's renderer, title, `test-failure` label and App identity, so a
  later Daily failure finds it, and `parse_issue` and `merge_environments`
  keep working.
- **Body.** The detector's sections, plus **"Seen in PR CI"**: failing
  attempts, distinct PRs (counts only), passes on
  rerun, job URLs, the fenced verdict and rationale, the fix that didn't
  hold if any, and a link to the tracker issue.
- **Markers.** The detector's fingerprint marker, `occurrences:1`,
  `source:pr-ci` and `pr-key:<run>-<attempt>`, and **no `last-key`**, which
  is reserved for the run the fix bot should fix. Without it `parse_issue`
  returns None, and the dispatcher skips the issue without commenting.
- **An existing `valkey` issue** is linked and left alone, and the tracker
  issue becomes `state:has-main-issue`. The tracker never reopens a closed
  `valkey` issue; after a reset it creates a new one that links the old.
- **A later Daily failure** updates the issue through the detector's normal
  upsert, which writes `last-key`, and the fix bot takes over unchanged.

Nothing is ever posted on contributors' PRs.

### Fixing later

The fix bot must never plan from a PR run, whose workflow, logs and branch
the contributor controls. Instead, the tracker can produce a trusted run:

1. On promotion, or on `@valkeyrie-ops reproduce` on the tracker issue, it
   dispatches `daily.yml` on `unstable`, narrowed with `test_args` and
   `skipjobs`, and `use_repo` and `use_git_ref` left at their defaults.
2. If that run fails the test, the publisher records it as `last-key`.
3. `_validate_scheduled_run` also accepts a `workflow_dispatch` run of
   `daily.yml` on the default branch triggered by the App. That is the only
   fix-bot change. The dispatching job needs `actions: write` on `valkey`.

Fixing against the latest Daily instead doesn't work when the test passed
there, because `_failing_jobs` finds no matching job and refuses.

## Security

- **Everything in a fork PR run is untrusted, including its workflow**: job
  names, every log line (the checkout lines too), test names, error text, PR
  diffs and Claude's output. Only API fields are trusted, including the PR
  object fetched in [PR identity](#pr-identity).
- **Deterministic gates before any promotion**, independent of the verdict:
  the failure threshold, distinct verified PRs, and the test existing at the
  tested base. On top of those, phase 1 needs an authorized person's command,
  and phase 2 has the daily cap. Claude can label an issue ready, close it,
  or exclude failures, but it can never add a count or file anything by
  itself.
- **Forged markers.** The detector's `_build_body` puts `test_name` into the
  body raw, and the publisher appends `last-key` after the body. A fork can
  make its CI print a test name containing
  `<!-- valkey-ci-agent:test-failure:last-key:N -->`.
  `parse_detector_occurrence` takes the first match, so the fix bot would
  dispatch on run N. A name containing another test's marker would make
  `_find_existing` attach the wrong issue. So a test or file name with `<`,
  `>`, a backtick, `@` or a newline, or longer than 200 characters, is never
  promoted, not even by command. Error text and the rationale go in fences longer than any
  backtick run inside them.
- **No personal data.** PR author logins aren't stored anywhere: not in
  tracker issues, `valkey` issues, the facts or the prompt. The fork owner
  is only compared during [PR identity](#pr-identity). A PR number is a
  public identifier, and is enough to look up the author when needed.
- **Tokens.** Each job mints its own App token with explicit `permission-*`,
  top-level permissions are `{}`, and no job gets `contents: write`.

| Job | Repository | Permissions |
|---|---|---|
| collect | `valkey` | `actions: read`, `pull-requests: read` |
| record | `tracker_repo` | `issues: write` |
| analyze | none | Bedrock through OIDC only |
| commands | `tracker_repo`; org team membership | `issues: write` (reactions, replies), `members: read` |
| promote | `valkey`, `tracker_repo` | `issues: write` |

The tracker never checks out, builds or runs PR code, downloads PR
artifacts, or reruns a PR job. It runs on `schedule` and `workflow_dispatch`
only, never `pull_request_target` or `workflow_run`. It never writes anything
but a Daily or reproduced run to `last-key`, bumps `occurrences` on a Daily
issue, or edits `valkey` issues it didn't create.

## Code layout

```text
scripts/pr_failure_tracker/
  main.py        the sweep: collect, record, evaluate candidates
  collect.py     runs, attempts and jobs; log parsing; PR identity and tested base
  tracker.py     tracker issues: markers, keyed comments, states, throttling
  gates.py       eligibility, thresholds, exclusions, name checks, the daily cap
  resets.py      lazy fix detection and ancestry checks
  analyze.py     facts and the Claude verdict
  commands.py    @valkeyrie-ops promote and reproduce on tracker issues
  promote.py     the valkey issue, through the detector's publisher
.github/workflows/pr-failure-tracker.yml            collect → analyze (no token) → promote
.github/workflows/pr-failure-tracker-commands.yml   the 30-minute command poll
docs/pr-failure-tracker.md
tests/test_pr_failure_tracker_*.py
```

Shared changes:

- `scripts/ai/runtime.py`: the `pr_failure_analysis_readonly` profile.
- `test_failure_detector/issue_renderer.py`: a "Seen in PR CI" section and
  name checks. The test-level body already says "failing in CI"; only the
  job-level renderers say "Daily", and v2 doesn't use them.
- `common/issue_dedup.py`: `create_only`, which returns `"skipped-exists"` if
  an open issue matches.
- `backport/registry.py`: parse and validate a `pr_failure_tracker` block.
  The loader ignores unknown keys today, so a misspelled block would silently
  do nothing.
- `valkey_ci.Source` doesn't get a PR value, because it drives the fix bot's
  workflow list and the backport CI follow-up.
- Docs: a sentence in `docs/ci-fix.md` that issues from PR CI aren't fixed
  until Daily records the failure, and the new markers in
  `docs/test-failure-detector.md`.

## Configuration (`repos.yml`)

```yaml
pr_failure_tracker:
  enabled: false
  tracker_repo: valkey-io/valkey-ci-tracker   # issues only; never a repos.yml target
  workflows: [ci.yml]
  base_branch: unstable
  window_days: 14
  threshold_failures: 3
  min_distinct_prs: 3
  promote: command        # command (phase 1) | auto (phase 2)
  max_promotions_per_day: 2   # phase 2 only
```

## Phases

**Phase 1: labels, promotion by command** (`promote: command`). Collection,
the limit and the analysis all run as described. The analysis outcome only
labels, closes or trims tracker issues. Nothing reaches `valkey` until a
maintainer comments `@valkeyrie-ops promote`. During this phase,
maintainers check the verdicts and exclusions in the tracker, and the gates
are tuned from real data.

**Phase 2: automatic promotion** (`promote: auto`), once we're happy with
the phase 1 verdicts. `ready` issues are promoted under the daily cap, and
the command still works.

**Later:** reproduction on `unstable` ([Fixing later](#fixing-later)), more
workflows, other repositories.

## Testing

- **Collection:** fixtures for a passed rerun, an `action_required` first
  attempt, a carried-over job, a cancelled job and a failed job in a
  cancelled run (both ignored), a fork PR with empty `pull_requests`, a
  forged `pull/<n>/merge` line, the libbacktrace checkout line, a PR into
  `cluster-v2` and an unknown PR (both ignored, with no other logs read), a
  failed log download, a run still in progress during the previous sweep,
  and a failed sweep whose work the next sweep picks up. PR identity: a PR
  pushed again before the sweep (accepted), a commit force-pushed away
  (ignored), and a merge line whose base is newer than the run or not on
  `unstable` (ignored).
- **Tracker:** keyed comments are idempotent across overlapping sweeps;
  non-bot comments are ignored; one comment per test in a burst; stop
  and resume on 403 or 429; every state transition.
- **Gates:** E1 to E3, including interpolated Tcl names and gtest; both
  thresholds; a PR that changed the test in the tested push and reverted it
  later; no re-analysis until the limit is reached again; a `pending` issue
  retried with no new sighting; `passed_in` found for a rerun in a later
  sweep.
- **Verdicts:** each row of the verdict table; exclusions that drop the
  count below the gates keep the issue collecting; exclusion IDs that aren't
  in the facts are ignored; excluded sightings never count again.
- **Phases:** phase 1 never promotes without the command; a non-member's
  command is refused; a command already reacted to isn't run twice; phase 2
  promotes `ready` issues under the cap, and the command bypasses it.
- **Resets:** a diff inside and outside the test block; a closing versus a
  referencing PR; a close with no commit; a person's close versus the bot's;
  a pre-fix base; a fix that didn't hold; a fix that lands while the issue
  is `ready`, for both a command and automatic promotion.
- **Handoff:** a promoted issue has no `last-key` and is skipped silently; a
  later detector upsert makes it dispatchable; the new markers survive an
  update and never match `_OCCURRENCE_RE` or `_last_key_re`; `create_only`
  never updates a Daily issue.
- **Security:** a name with `<!--`, a backtick, `@` or a newline is never
  promoted; no `#n` or PR URLs in tracker or `valkey` bodies; the analysis job
  has no GitHub token; the workflow has no `pull_request_target` or
  `workflow_run`.

## Open questions

1. **Sweep cadence.** Daily, after the detector, is proposed. Each sweep
   reads only what's new, so a faster sweep costs about the same in total. Is
   a faster signal worth it?
2. **The tracker repository.** A new `valkey-io/valkey-ci-tracker`
   (recommended), or a fork with issues enabled? Who can see and triage it?
3. **Fix-bot tuning** (out of scope). Oversized timeout bumps, and no test
   alongside a `src/` fix, belong in `scripts/ci_fix/guides/valkey-io__valkey.md`
   and are tracked separately.
