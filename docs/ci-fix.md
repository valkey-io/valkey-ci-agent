# Fixing CI failures

The CI fix bot takes one failed CI job, works out why it failed, and proposes
a fix. It treats its own fixes with suspicion. Before anyone sees a fix, the
bot reruns the failing test with the fix applied and asks a second,
independent Claude session to review it. Even then the fix is only a
proposal: it arrives as a commit on a pull request, where CI runs as usual, or
as a patch in a comment, and a maintainer decides what happens next. The bot
never merges.

## Asking for a fix

On any `valkey-io/valkey` pull request, members of `valkey-io/contributors`
can ask for a fix by starting a comment with:

```text
@valkeyrie-ops fix
```

You can point it at a particular run or job, and add a hint in your own
words:

```text
@valkeyrie-ops fix https://github.com/valkey-io/valkey/actions/runs/123/job/456 the timeout looks too tight on slow runners
```

Without a link, the bot picks the failure on the PR's latest commit that it is
most likely to be able to fix. It prefers ordinary test jobs, then lint and
build jobs, whose fix often belongs in the CI configuration the bot won't
touch, and leaves sanitizer and valgrind jobs for last. `@valkeyrie-bot fix`
works as well.

The bot looks for new requests every hour. When it picks yours up, it reacts
to your comment, and it replies on the PR when it is done.

What you get back depends on whose PR it is. On a contributor's PR, or a PR
from a fork, the bot never pushes anything. It posts the fix as a patch in a
comment, and the author can apply it or not. On the bot's own PRs, meaning
backport PRs and the PRs it opened to fix something, it pushes the fix as a
new commit.

## Other ways a fix starts

The bot also starts without being asked in two situations.

When one of the bot's own backport PRs fails CI, the backport workflow's
hourly CI follow-up hands one failed job at a time to this bot. That is
described in [Backports](backport.md#when-a-backport-prs-own-ci-fails).

When the [Test Failure Detector](test-failure-detector.md) files an issue for
a test that failed in Daily CI, the bot can try to fix that too. A maintainer
can start this for one issue by hand, and an hourly dispatcher can start it
for every eligible issue once it is switched on. See
[Fixing detector issues](#fixing-detector-issues).

## What the bot does with a failure

**It pins down exactly what failed.** The bot records the run, the attempt,
the job and the commit that failed. For a PR, the failure has to be on the
PR's latest commit, because a fix for an older commit would be fixing code
that is no longer there.

**It recreates the job where it can.** The fix is prepared inside a copy of
the job that failed: the same runner or container, the same setup steps, the
same build flags and the same test command. Many failures only happen on one
platform or with one build configuration, so this matters. When the job can't
be recreated faithfully, for example because it runs in a VM or one of its
steps depends on a secret, the bot works on a plain Ubuntu runner with a
default build instead.
[How the failed job is recreated](#how-the-failed-job-is-recreated) has the
details.

**It looks for a fix that already exists.** Before writing anything, the bot
searches the git history for merged commits that mention the failing test,
change its file or a source file the error points at, or reference its issue.
On a release branch, the answer is often a fix that already landed on
`unstable` and was never backported, and porting it is better than writing a
new one. On `unstable`, a matching commit newer than the failure suggests the
problem may already be fixed.

**It works out how the fix will be checked.** The job's log is the evidence
of what fails. The bot works out the command that will verify a fix (the
job's own build and test command, for the failing test only) and tells
Claude what it is. Some failures have no command the bot can run: a sanitizer
or valgrind report that names no test, a sentinel or legacy-cluster test, a
lint. For those the bot only accepts a change that is already merged upstream,
checks that it builds, and leaves the rest to the PR's own CI. When there is
nothing upstream to port, as on `unstable` itself, Claude may still propose a
patch. That patch is reviewed but not run, and the bot's report says so in
its first line. A backport branch never gets one.

**Claude diagnoses and edits.** Claude gets the job's whole log, whatever the
history search found, the verification command, and Valkey's testing
conventions: the bot's own guide in `scripts/ci_fix/guides/valkey-io__valkey.md`
plus the repository's `tests/README.md` and contributor docs. It then does one
of three things: ports an existing fix (preferred when one fits), writes a
small patch, or declines. It is told to aim for the fix a maintainer would
write. Where the evidence leaves a few possible causes and one change is right
under each of them, it makes that change and says what is not established. It
declines only when it can justify no change. Claude works in a scratch copy of the code. It can
edit files there but cannot commit or push.

On a backport branch Claude may not write a fix of its own. It ports a commit
that is merged upstream, or adapts one and names it. Claude cannot run git, so
the bot gives it every commit the branch lacks with the files each changes,
the diffs of the newest ones to the files the failure names, and the diff of
a port that did not apply. For an adaptation the bot checks that the named
commit is merged, is not on the branch yet, and that the patch is that
commit's change: at least half of the lines it adds are the commit's, or at
least half of the lines it removes are lines the commit removes. An
adaptation often writes something else in their place, such as another
encoding to assert or a wider tolerance. Naming a commit that only touches
the same file is not enough, and the reviewer reads the named commit's diff
next to the patch. One other patch is accepted: one that
changes only files the backport PR itself changes, which repairs what the
backport brought. A test that is flaky upstream too is fixed upstream first. A
backport that broke the build is the exception: that is repaired by hand.

**Code checks what Claude handed back.** The bot rejects anything that doesn't
add up: a commit Claude made itself, a fix with no root cause or reasoning, a
port that isn't a real merged
commit or is already on the branch, a hand-written fix where only an upstream
change is accepted, or a suggested patch that is too large.

## How the fix is tested

A fix for a bot PR or a detector issue has to pass a feedback loop before it
is published. The loop runs at most three rounds within the fix's time
budget. One Claude session gets at most two hours of it, and an hour is kept
back for verification and review.

In each round, a port is first cherry-picked into the code. If it conflicts,
Claude can adapt it into a patch instead. The fix is then built, and a build
that breaks goes straight back to Claude. A fix under `deps/` makes the build
start clean, because Valkey's makefile otherwise keeps the libraries it built
before.

Then the failing test is run with the fix applied, once. When it cannot pass
alone (with `--only`, runtest skips the other tests but still runs the code
between them, which may need what a skipped test set up), it is run in its
own top-level block, then its whole file once. Whichever passed is then run
in a loop: 40 times, or 4 under valgrind, or as many as fit in 45 minutes
when one loop is slow. The test has to run and pass every loop. A whole-file
run is not looped.

The bot does not first reproduce the failure: the failed job is the evidence
that it fails, and a flaky test rarely fails on demand. A passing loop shows
that the test passes with the fix, not that it failed without it.

Finally a second Claude session, which can read the code but not change it,
reviews the fix with the same conventions and the test results in front of
it, and has to approve. It is told to check Claude's diagnosis against the
log rather than take it as given, and to reject a longer timeout or a looser
threshold that comes without a reason, and any change made so that a test can
run alone. A port is reviewed differently: its commit is already merged
upstream, where maintainers reviewed it, so the reviewer only checks that it
addresses this failure and brings nothing else. A reviewer that rejects a fix
says what change it would approve. If the tests fail or the reviewer objects,
the output or the reviewer's reasoning goes back to Claude, along with what
its last session concluded, and it makes the fix that points to in the next
round. If the build or the
tests change any tracked file while the fix is being tested, the fix that was
tested is no longer the fix in the tree, so that also goes back for another
round.

A fix that doesn't pass within three rounds is declined. If the bot runs out
of time or crashes, that isn't treated as declining: the CI follow-up and
detector issues get one more attempt at the same failure, and nothing is
posted about the failed attempt.

Patches suggested on contributor PRs are reviewed but not run.

## Fixing detector issues

When the Test Failure Detector files an issue, the bot can attempt a fix. You
can start it for one issue with the **CI Fix Issues** workflow. An hourly
dispatcher also starts it for every open detector issue that is eligible.
Setting the repository variable `CI_FIX_ISSUES_AUTO_DISPATCH` to `false`
turns the dispatcher off.

An issue is eligible when it is open, unassigned and no open PR says it
closes or addresses it (`Fixes #1234`, also as one of a list; a PR that only
mentions the issue doesn't count),
and when the bot hasn't already posted a fix, declined, or failed twice for
the failure it currently records. A newer failure makes the issue
eligible again. At most two of these fixes run at once, which
`CI_FIX_ISSUES_MAX_RUNS` can change. Right before starting, the bot checks
again that it hasn't already reported on that failure, so a dispatch that is
retried can never post a second patch.

The fix is prepared on the current tip of the branch that Daily CI tested,
normally `unstable`, using the original log and commit as evidence. Only the
issue's own test is checked. Other tests that failed in the
same job have issues of their own.

By default the bot posts the tested patch on the issue, together with the root
cause and what it checked. Nothing is pushed and no PR is opened. A maintainer
can apply the patch or ignore it, and the issue isn't tried again until the
test fails again. If the test fails again while a fix is being prepared, the
fix is still posted, and it covers that newer failure too.

If `CI_FIX_ISSUES_PUBLISH` is set to `draft-pr`, the bot instead pushes a
branch named `agent/ci-fix/issue-<issue>-<run>` and opens a draft PR that says
`Fixes #<issue>`. One test file often has several open issues, so if the patch
touches the test of another open detector issue, the bot runs that test as
well and mentions the issue in the PR, without closing it.

## How the failed job is recreated

To run the fix in the environment that failed, the bot reads the workflow
file that ran the failed job. That is `daily.yml` for a nightly failure, or
the PR's own workflow otherwise, read at the commit the fix will be made on.
It finds the job whose name matches the one that failed, including the
specific matrix entry, and evaluates the `${{ }}` expressions it needs the way
GitHub would have for that run.

The fix job then checks out the code and the job's one other repository
(`libbacktrace`), and replays the job's setup steps exactly as GitHub ran
them. Where the job would have run its tests, the bot prepares the fix
instead, rebuilding with the job's own `make` command and testing with its own
`runtest` line.

This works for jobs on Ubuntu or macOS runners, and for jobs in a container
on an Ubuntu runner. It needs a job with one `make` step, one `runtest`
command, and setup steps that are scripts or checkouts. If the job has
anything the bot can't follow exactly, such as a secret, a step that uses
another step's output, an action other than a checkout, or a setup script
between its test steps, the fix runs on the bot's own Ubuntu runner instead.

## Keeping the bot contained

The job that runs Claude has no GitHub token at all. It can read the code and
reach the model, and that is it. Separate jobs before and after it hold
short-lived tokens for reading the failure and for publishing the result.

Claude, and every build and test command the bot runs, run as a separate
system account, `ci-fix-agent`. That account cannot read the workflow's
environment. Claude is started with only the AWS credentials it needs for the
model, and build and test commands get no credentials at all. Valkey's build
and test dependencies are installed before any credentials exist.

That account owns the code while it works, so the bot is careful when it takes
the code back. It stops if `.git` was replaced, because a planted git
configuration could otherwise run commands as the workflow user. Before
testing a fix, it deletes ignored files that the job's own setup didn't
create, so a prebuilt binary can't stand in for the real build.

The bot pushes a change under `.github/` only to a backport branch it owns,
where a release branch often needs an upstream CI fix; only that push gets a
token with `workflows: write`. Anywhere else it refuses, and its tokens
couldn't push one anyway. The PR's own CI and a maintainer's review always
have the final say. The bot's comment lists everything it checked, including the history
search and the verification.

Each Claude session's full event stream is saved in the fix run's
`ci-fix-result` artifact, next to the outcome, so what the agent read and
concluded can be looked at after the job's log is gone.

## Running it by hand

To ask for a fix on a PR, dispatch **CI Fix Bot**. You are checked the same
way a commenter would be, so you need to be a member of
`valkey-io/contributors`, and `repo` must be `valkey-io/valkey`. Leave
`run_url` empty to let the bot choose the failure.

```bash
gh workflow run ci-fix.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey-io/valkey --field pr=1234 \
  --field run_url=https://github.com/valkey-io/valkey/actions/runs/123456789/job/987654321
```

To pick up fix comments now instead of waiting for the hourly check:

```bash
gh workflow run ci-fix-comment-poll.yml --repo valkey-io/valkey-ci-agent \
  --field lookback_minutes=180
```

To try to fix one detector issue:

```bash
gh workflow run ci-fix-issues.yml --repo valkey-io/valkey-ci-agent --field issue=1234
```

To see which issues the dispatcher would start fixes for, without starting
any:

```bash
gh workflow run ci-fix-issue-dispatch.yml --repo valkey-io/valkey-ci-agent --field dry_run=true
```

## Configuration

The bot uses the shared secrets described in [setup.md](setup.md). Its
behaviour can be adjusted with these repository variables:

| Variable | Default | What it does |
|---|---|---|
| `CI_FIX_ISSUES_AUTO_DISPATCH` | unset | Set to `false` to stop the hourly dispatcher from starting fixes for detector issues. |
| `CI_FIX_ISSUES_MAX_RUNS` | `2` | How many detector issue fixes can run at once. |
| `CI_FIX_ISSUES_PUBLISH` | `comment` | `comment` posts the patch on the issue; `draft-pr` opens a draft PR. |
| `CI_AGENT_CLAUDE_MODEL` | unset | Use a different model, for example `opus`. |

`CI_FIX_AGENT_USER` names the separate account, and is set to `ci-fix-agent`
by the workflow. Which jobs the bot ignores, and whether it follows up on
backport PRs, is set per repository in [`repos.yml`](registry.md).

Comment requests are currently limited to `valkey-io/valkey`. Most of the
engine doesn't care which repository it works on, but recreating a job and
verifying a fix follow Valkey's `make` and `runtest` conventions, and
another repository would need its own tokens and triggers.

## Where the code lives

Every fix runs as the same three jobs in `ci-fix-run.yml`, driven by
`scripts/ci_fix/run.py`: a plan job that pins down the failure and works out
how to recreate the job, a fix job, and a publish job. The fix job is called
`fix-in-job` when it recreates the failed job, `fix-in-container` when that
job ran in a container, and `fix-local` when it falls back to the bot's own
runner. The jobs pass their state to each other as JSON (`state.py`).

Each way of starting a fix has a small front door that checks the request and
turns it into the same `FixTask`: `main.py` for PR requests,
`comment_poll.py` for picking up comments, `issues.py` and
`issue_dispatch.py` for detector issues, and `scripts/backport/ci_followup.py`
for backport PRs. `gate.py` parses commands and checks that the requester is
allowed. `github_runs.py` and `issue_state.py` read the state of runs and
issues.

`ci_job.py` reads the workflow file and writes the script that replays a
job's setup, and `job.sh` runs the fix inside that job. `engine.py` runs the
fix itself. It calls `prior_fixes.py` to search the history, `verification.py`
to plan the command that verifies a fix, and `agent.py` for the Claude
sessions that fix and review. `publish.py` and `push.py` check the destination again and publish.
`comment.py` writes the bot's reply. `policy.py` lists what each kind of
target allows, and `models.py` has the shared data types.
