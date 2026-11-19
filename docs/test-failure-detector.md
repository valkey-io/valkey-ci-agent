# Test Failure Detector

Valkey runs a large Daily CI workflow every night, across many platforms and
build configurations. When a test fails there, someone has to notice, and
the same flaky test failing on three nights in a row should be one problem,
not three. The Test Failure Detector reads each night's results and keeps one
GitHub issue per failing test on `valkey-io/valkey`, adding to it every time
the test fails again.

Once an issue exists, the [CI fix bot](ci-fix.md#fixing-detector-issues) can
try to fix it.

## When it runs

Daily CI starts at midnight UTC. The detector runs at 06:00 UTC, allowing a
two-hour buffer after recent Daily runs, which finish by about 04:00 UTC.
If a night is ever missed, you can run the detector by hand for any run whose
artifacts are still around, which is 30 days.

## What it does

The detector finds the most recent finished Daily run on `unstable` (or the
run you name) and downloads its `all-test-failures` artifact. That artifact
lists every failing Tcl test by job and suite. The detector groups the
failures by test name and file, so a test that failed in five jobs becomes
one failure with five links to CI.

Some failures never make it into that artifact: a test that timed out, a
valgrind or sanitizer report, the leak check at shutdown, an exception in the
test framework itself, or a failed C unit test. So the detector also reads the
log of every failed job, looking at the summary `runtest` prints at the end
and at any `[exception]:` line.

- If the log names a test and its file, the failure joins that test's issue.
- A failed C unit test gets an issue named after it, such as
  `[JOB-FAILURE] Unit test Suite.Test failed in test_x.cpp in Daily`.
- Anything else gets an issue named after its cause, such as
  `[JOB-FAILURE] <cause> in Daily`. For an exception, the cause names the test
  file the stack trace points at. For valgrind or a sanitizer, it is the first
  error and the frames under it. One cause gets one issue, however many jobs
  it broke.
- A job that failed without saying why, such as a broken build, a crash, or a
  package mirror being down, isn't filed, because there is nothing to tell one
  such failure from another. The run summary lists it instead.

Jobs that only report on other jobs are skipped, using the
`ci_followup_ignored_jobs` list in [`repos.yml`](registry.md), and so are
failures in setup steps. If the detector can't read a failed job's log, it
lists the job under "Unread job logs" in its summary and marks the run as
failed. If it can't read `repos.yml`, it files nothing at all and fails,
because it would no longer know which jobs to ignore.

## How issues are kept up to date

Each issue carries a hidden marker built from the test's name and file, and
the detector matches on that marker rather than the title. For every failure,
one of four things happens:

- If there is no issue yet, the detector opens one titled
  `[TEST-FAILURE] <test> in <file>`, labelled `test-failure`, with the error,
  links to the failing jobs, and the environments it failed in.
- If an issue is already open, the detector adds any new environments and a
  comment noting that it failed again, and bumps the count.
- If the issue already records this exact run, nothing happens. Running the
  detector twice on the same night doesn't double-count.
- If there's no open issue but a matching one was closed within the last day,
  the detector doesn't open a new one, because the failure has most likely
  just been fixed and the fix hasn't reached that night's run yet.

At the end of every run, including runs that stop early, the detector writes a
summary to the workflow run with how many failures it found and how many
issues it opened and updated.

## Running it by hand

```bash
gh workflow run test-failure-detector-sweep.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey-io/valkey --field run_id=12345678 --field dry_run=true
```

`repo` defaults to `valkey-io/valkey`. Leave `run_id` empty to use the latest
Daily run. With `dry_run=true`, the detector reports what it found without
opening or changing any issue.

## Configuration

The detector uses the shared GitHub App described in [setup.md](setup.md). It
needs to read Actions artifacts and logs and write issues on
`valkey-io/valkey`.

## Where the code lives

`scripts/test_failure_detector/main.py` runs the steps above in order.
`download.py` finds the run and downloads the artifact, `parse_failures.py`
reads and groups the failures, `job_failures.py` reads the logs of failed jobs,
and `manage_issues.py` and `issue_renderer.py` open and update the issues.

Most of the heavy lifting is shared with other workflows through
`scripts/common/`. Artifact downloads use `workflow_artifacts.py`, which deals
with GitHub's redirects, retries and expired artifacts. Issue matching and
updating use `issue_dedup.py`, which the fuzzer monitor uses too; only the
detector turns on the check for recently closed issues. Reading a run's jobs
(`workflow_jobs.py`) and parsing `runtest` output (`valkey_ci.py`) are shared
with the CI fix bot.
