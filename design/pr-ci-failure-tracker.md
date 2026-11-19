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
file an issue for every PR failure.

## The idea

There are two stages with very different costs:

1. **Collect, cheaply and often.** Every new failure in PR CI is recorded as
   a sighting with a reference to its job and commit. There is no Claude and
   no judgement in this stage, only log parsing.
2. **Analyse, rarely.** When a test has failed in enough CI runs and has
   no issue yet, one analysis decides whether the problem belongs to
   `unstable` rather than to the PRs. If it does, an issue is filed.

Once an issue exists, the [CI fix bot's](../docs/ci-fix.md#fixing-detector-issues)
hourly dispatcher picks it up, exactly as it does for Daily issues.

## Pipeline

```text
scheduled sweep
  → completed PR CI runs updated since the cursor
  → failed jobs of every attempt
  → parse logs into fingerprints (the detector's parser)
  → look for fixes merged to unstable since the last sweep → reset those fingerprints
  → append sightings to the stats branch, prune what has expired
  → for each fingerprint touched this sweep:
        has an open issue?                       → nothing more
        failed CI runs since its last reset < threshold? → nothing more
        already analysed at this level?          → nothing more
        otherwise                                → analyse → file issue or record verdict
```

## Stage 1: collection

### Which runs

- Repository: `valkey-io/valkey`. The repository and workflow list are
  configured in `repos.yml`, so other repositories can be added later.
- Workflow: the main PR CI workflow (`ci.yml`). We start with that one.
- Event: `pull_request`, base branch `unstable`._
- Runs that finished with a failure since the last sweep, **including
  reruns**. A rerun keeps its run ID and bumps the attempt, so the cursor is
  based on `updated_at` with a small overlap. Each job is deduplicated on
  `(run_id, attempt, job_id)`.
- Jobs matching `ci_followup_ignored_jobs` and failures in setup steps are
  skipped, as the detector already does.

### What a sighting is

Every failed test in every failed job of every attempt becomes one record.
A PR's own failures are recorded too: a test that breaks under many
unrelated PRs' changes is often a badly isolated test, and that is worth
knowing.

```json
{
  "fingerprint": "…",
  "test": "Replica buffer don't induce eviction",
  "file": "tests/unit/maxmemory.tcl",
  "kind": "tcl | gtest | exception | sanitizer | valgrind | …",
  "error": "first lines of the error, truncated",
  "repo": "valkey-io/valkey",
  "pr": 1234,
  "pr_author": "someone",
  "head_sha": "abc123…",
  "base_sha": "def456…",
  "tested_base_sha": "0123ab…",
  "run_id": 123,
  "attempt": 2,
  "job_id": 456,
  "job_name": "test-ubuntu-latest",
  "job_url": "https://github.com/…/job/456",
  "passed_on_rerun": false,
  "seen_at": "2026-11-19T10:00:00Z"
}
```

`tested_base_sha` is the `unstable` commit the PR was actually tested on, which
is the first parent of the run's merge commit. It can differ from `base_sha`
because PR CI tests the PR merged into `unstable` as it was when the run
started. The reset logic needs it (see [Clearing and resets](#clearing-and-resets)).

`passed_on_rerun` is filled in later, once a newer attempt of the same run
has passed that job. That is strong evidence the test is flaky.

### Fingerprints

Fingerprints must be the same ones the detector uses (`incidents.py` and the
detector's marker namespace). That way a PR sighting and a Daily issue for
the same test match, and the "has an open issue" check is one marker lookup
through `issue_dedup.py`.

The detector's job-log parsing (`job_failures.py`, `valkey_ci.py`) is reused
as is. The only Daily-specific parts are the run lookup (`download.py`) and
the "in Daily" wording in `issue_renderer.py`. Both get a source parameter.

PR CI doesn't upload the `all-test-failures` artifact that Daily does, so the
collector works from job logs alone. That is the same path the detector
already uses for failures the artifact misses.

### Where stats live

The stats live on an orphan branch, `data/test-stats`, in this repository:

```text
sightings.jsonl   one sighting per line, pruned to the window on every write
verdicts.json     fingerprint → last analysis verdict, its level, and when
resets.json       fingerprint → the fix that reset it: commit, PR, merged_at, reason
cursor.json       the updated_at the last sweep finished at, and the last
                  unstable commit scanned for fixes
```

- There is one commit per sweep, made by code, never by Claude.
- There is only one writer at a time: a workflow `concurrency` group with
  `cancel-in-progress: false`. If a push is rejected anyway, the sweep
  refetches, reapplies its records and retries once.
- The sweep needs `contents: write` on `valkey-ci-agent`, but only for that
  branch. This is a new kind of push target, so
  [Where the agent pushes](../docs/architecture.md#where-the-agent-pushes) and
  the push-safety tests get updated to allow exactly `data/test-stats`.
- [Clearing and resets](#clearing-and-resets) keeps the files small.

## Clearing and resets

The stats get cleared in two ways: on a schedule, so nothing grows forever,
and per test, when a fix lands, so the test's count starts again.

### Expiry, on every write

- **Sightings** older than `window_days` (default 14) are dropped.
- **Verdicts** older than `verdict_ttl_days` (default 30) are dropped, and
  so is a verdict with no sightings left. A test that comes back after that
  is analysed from scratch, because the code around it has changed and an
  old `pr_caused` shouldn't keep suppressing it.
- **Resets** are dropped once no sighting for their fingerprint predates
  them, since they no longer filter anything.

### History, monthly

A commit every hour adds up. On the first sweep of each month, the branch is
replaced with a single commit holding the current files, and that commit is
force-pushed. Only `data/test-stats` is ever force-pushed. It holds derived
data that can be rebuilt, not code, so losing its history costs nothing. The
previous month's tip is uploaded as a workflow artifact before the
force-push, in case someone wants to look back.

### Resetting a test when it is fixed

When a contributor fixes a test, its old sightings are evidence about code
that no longer exists. Leaving them counted would push a fixed test over
the threshold on stale data, and analyse it, as soon as it fails once more.

**What counts as a fix.** Each sweep scans the commits that landed on
`unstable` since the last scanned commit. A commit resets a fingerprint when
any of these is true:

1. **It changes the test itself.** For Tcl, that means the diff touches the
   lines of the `test "<name>"` block in the fingerprint's file. For gtest,
   the `TEST(Suite, Name)` body. Changes elsewhere in the same file don't
   count.
2. **It closes the test's issue.** The commit's PR closes or references an
   issue carrying the fingerprint's marker. This also covers fixes in source
   files rather than the test, as long as someone linked the issue.
3. **The issue was closed as completed.** The fix commit is taken from the
   issue's timeline: the `closed` event's `commit_id`, or else the merge
   commit of the PR that closed it. If neither exists, the anchor is the
   `unstable` HEAD at the time of the close. Any fix that landed before
   someone closed the issue is an ancestor of that commit, so it works with
   the same ancestry check as every other reset.
4. **A maintainer resets it by hand.** A `workflow_dispatch` input,
   `reset=<fingerprint>|all`, with an optional commit.

Rule 1 can be triggered by a refactor that isn't a fix. That is the safe
mistake to make, because a reset only delays an analysis; it never files or
closes anything.

**What a reset does.**

- It records `{commit, pr, merged_at, reason}` in `resets.json`.
- It drops the fingerprint's verdict.
- From then on, only sightings whose `tested_base_sha` contains the fix
  commit count towards the threshold. The check is
  `git merge-base --is-ancestor`, run against a treeless clone of `unstable`
  that is kept for the whole sweep. A PR tested on an `unstable` from before
  the fix is still running the broken test, so its failure says nothing
  about whether the fix worked. Those sightings stay stored but don't count.
  Every reset has a commit anchor, so there is no time-based grace period.

**When the fix didn't work.** If a fingerprint crosses the threshold again
using only post-reset sightings, the analysis is told which fix reset it.
The issue it files (or the comment, if the old issue is reopened) says that
the fix in commit X didn't hold, with the post-fix failures linked.

## Stage 2: analysis

### When it triggers

A fingerprint becomes a **candidate** when all of these hold:

- No open issue carries its marker.
- If its issue was closed, there are enough sightings from CI runs that
  already contained the fix. Closing the issue resets the fingerprint (rule
  3 under [Resetting a test when it is fixed](#resetting-a-test-when-it-is-fixed)),
  so only failures whose `tested_base_sha` has the fix commit as an ancestor
  count. The check is one `git merge-base --is-ancestor` per sighting,
  against the clone the sweep already keeps, so it is cheap. Failures from
  PRs that predate the fix are ignored however recent they are. A failure on
  a PR that has the fix counts straight away; the detector's one-day grace
  isn't used here. Such a failure means the fix didn't hold (see below).
- Within the window, it has failed in at least `threshold_runs` CI runs
  (default 5). A run is one `(run_id, attempt)`, so a rerun is a new run, and
  a test failing in several jobs of the same run counts once. Only sightings
  since the fingerprint's last reset count (see
  [Resetting a test when it is fixed](#resetting-a-test-when-it-is-fixed)).
  How many distinct PRs and authors those runs came from isn't part of the
  trigger. It is handed to the analysis as evidence: many runs from one PR
  point to `pr_caused`, and many runs across unrelated PRs point to
  `unstable`.
- It hasn't already been analysed at this level. A rejected candidate is
  analysed again only when its run count reaches double what it was at the
  last analysis (5 → 10 → 20). This stops the same candidate being
  re-analysed on every sweep.

A run in which the test failed and then passed on a rerun of the same commit
counts as two runs towards the threshold.

### Step 1: deterministic facts (no Claude)

For each PR in the candidate's sightings, the analysis gathers:

- how many of the candidate's runs it accounts for, and the totals: distinct
  PRs, distinct authors, and runs per PR
- the diff stat, and whether the PR touched the test file or a source file
  the error names
- whether the PR was later merged, and whether the test kept failing on
  other PRs afterwards
- whether any sighting passed on a rerun
- whether the test failed in Daily during the window (from the detector's
  issues and runs)
- the fix that last reset it, if any
- the `unstable` commits merged during the window that touched the test or
  the files the error names

None of these facts rejects a candidate on its own. For example, "every PR
touched the test file" can mean the test is fragile rather than that the PRs
were wrong. The facts are evidence for step 2.

### Step 2: Claude verdict

This follows the fuzzer monitor's pattern. It runs under a new read-only
profile, `pr_failure_analysis_readonly`, which can read and search, but has
no edits, no commands, no network and no GitHub token. Its working directory
holds:

- a copy of Valkey at current `unstable` HEAD
- the facts from step 1, as a JSON file
- the error excerpt for each sighting (truncated), and each PR's diff,
  capped in size

Claude returns one structured verdict through `ai_output.py`:

| Verdict | Meaning | Action |
|---|---|---|
| `unstable_regression` | `unstable` itself is broken, often since a specific merge | File an issue |
| `flaky` | Fails nondeterministically regardless of the change | File an issue |
| `poorly_isolated` | Breaks under unrelated changes: timing, ordering, shared state, overly tight assertions | File an issue |
| `pr_caused` | The PRs that failed it really broke it, independently | Record the verdict, no issue |
| `infra` | Runner, network or mirror problems | Record the verdict, no issue |
| `unsure` | Not enough evidence | Record the verdict, analyse again at the next level |

The verdict includes a short rationale and, for `unstable_regression`, the
suspected commit if one is identifiable. Code checks the verdict against the
schema. An unparseable answer counts as `unsure`.

### Step 3 (later, optional): reproduce on unstable

For `unstable_regression` and `flaky`, we could rerun the test on
`unstable` HEAD inside the recreated job, reusing the CI fix bot's job
recreation and sandbox. A failure there confirms the verdict and goes in the
issue. A pass doesn't refute `flaky`, so it never blocks filing. This step
is left out of the first version because of the runner cost.

### The issue

The issue is filed through the detector's `issue_dedup.py` publisher with
the same marker namespace, label `test-failure`, and the detector's renderer
with source "PR CI". It holds:

- the test, file and error
- the verdict and Claude's rationale
- the PRs, jobs and commits it failed on, with links, plus the raw count and
  the rerun-pass count
- the environments (job names) it failed in
- the suspected commit, if any

Because the marker is shared, a later Daily failure of the same test updates
this issue instead of opening a second one.

There are no comments on contributors' PRs in this design.

## Security

- **PR logs and diffs are untrusted.** Any fork can put anything into a job
  log, including text aimed at Claude. That is why the analysis profile is
  read-only, with no network and no token, why its answer is a fixed schema
  parsed by code, and why the issue body is rendered by code with escaping.
  Claude's rationale is quoted, never interpreted.
- **Issue spam.** A crafted test name could be used to open issues. Because
  the trigger counts runs, not PRs, one fork can reach it on its own by
  pushing or rerunning repeatedly. What stops that is the analysis, which
  sees that every run came from one PR and returns `pr_caused`, and the
  doubling rule, which caps how often a rejected candidate is analysed
  again. Titles are built by code from parsed fields and truncated.
- **Tokens.** The collector gets a one-hour App token with `actions: read`
  on `valkey`. Filing gets `issues: write` on `valkey`. The stats push gets
  `contents: write` on `valkey-ci-agent`, limited to `data/test-stats`.

## Code layout

```text
scripts/pr_failure_tracker/
  main.py        the sweep: collect, then evaluate candidates
  collect.py     list runs and jobs, parse logs, build sightings
  store.py       read, append, expire, squash and push the stats branch
  resets.py      find fixes on unstable, record resets, ancestry checks
  candidates.py  threshold, open-issue check, verdict memory
  analyze.py     deterministic facts and the Claude verdict
  publish.py     file the issue through the detector's renderer
.github/workflows/pr-failure-tracker.yml
docs/pr-failure-tracker.md
tests/test_pr_failure_tracker_*.py
```

Shared changes:

- `test_failure_detector/issue_renderer.py`: a source parameter instead of
  the hard-coded "Daily".
- `ai/claude_code.py`: the new read-only profile.
- `repos.yml`: a `pr_failure_tracker` block per repository with workflows,
  window, thresholds and an `enabled` / `file_issues` switch.

## Configuration (`repos.yml`)

```yaml
pr_failure_tracker:
  enabled: true
  file_issues: false        # phase 1: collect only
  workflows: [ci.yml]
  base_branch: unstable
  window_days: 14
  verdict_ttl_days: 30
  threshold_runs: 5
```

## Rollout

1. **Collect only.** Turn on the sweep with `file_issues: false`. It writes
   sightings and a job summary listing the would-be candidates. We let it run
   for a week or two, then pick the window and thresholds from real data.
2. **Analyse in dry run.** Run the analysis on candidates and put the
   verdicts in the job summary, without filing anything. We check the
   verdicts by hand.
3. **File issues.** Set `file_issues: true`.
4. **Maybe later:** reproduction on `unstable` (step 3), more workflows, and
   other repositories.

## Testing

- Unit tests for each module. They use recorded run, job and log fixtures,
  including a rerun that passes, a PR that fails the same test repeatedly,
  and a fork PR.
- Store tests: append, prune, reapply after a rejected push, and a corrupt
  or missing file. A corrupt file makes the sweep fail loudly; it is never
  overwritten.
- Candidate tests: threshold edges, run counting (reruns, several jobs in
  one run, a passed rerun counting twice), the open and
  closed-issue checks (a sighting tested before the fix is ignored, one tested
  after it counts, and the fix is found from the close commit, from the
  closing PR, or from `unstable` HEAD at the time of the close), and
  re-analysis levels.
- Expiry tests: sightings past the window, verdicts past their TTL or with no
  sightings left, resets that no longer filter anything, and the monthly
  squash (the artifact is uploaded before the force-push, and only
  `data/test-stats` is ever force-pushed).
- Reset tests: a diff inside the test block (resets) and outside it (doesn't),
  a gtest body, a linked issue, a close with no commit, the manual reset, a
  sighting tested on a pre-fix base (doesn't count), and a fix that didn't
  hold.
- Analysis tests: schema validation and an unparseable answer becoming
  `unsure`. Hostile log content ends up quoted in the prompt and escaped in
  the issue.
- The workflow is covered by the existing checks: pinned actions,
  least-privilege permissions, and allowed push targets.

## Open questions

1. **Sweep cadence.** The proposal is hourly. Is the extra log-reading load
   on the GitHub API acceptable? Phase 1 will show the real volume.
2. **Is `unsure` filed after N analyses?** For example, as a
   `needs-human-triage` issue once it reaches the third level.
3. **Should a fingerprint the detector already tracks from Daily skip
   collection entirely,** or keep collecting for the stats? The proposal
   keeps collecting, because it costs nothing extra and the stats branch is
   useful on its own.
