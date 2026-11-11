# Backports

Valkey maintains several release lines at once, and most bug fixes that land
on `unstable` need to reach some of them too. Doing that by hand means
cherry-picking, fixing conflicts, rebuilding and retesting, once per branch
for every fix. The backport workflows do that work, and leave maintainers with
the parts that need judgement: deciding what should be backported, and
reviewing the result.

These workflows cover every repository listed in [`repos.yml`](registry.md):
Valkey itself, its modules, and anything else that tracks backports on a
GitHub Project board per release branch.

## How a fix gets backported

It starts with a maintainer. Each release branch has a GitHub Project board.
When a merged PR should be backported, a maintainer adds it to that branch's
board with the status "To be backported". That status is the only thing the
agent acts on.

Every day at 09:00 UTC the sweep goes through each repository and release
branch in `repos.yml`. For each branch it reads the board and cherry-picks the
waiting PRs, one at a time, onto a branch named `agent/backport/sweep/<branch>`.
PRs that were squash-merged or merged with a merge commit are both handled;
PRs that were rebase-merged are not, because there is no single commit to
pick. When a cherry-pick conflicts, Claude reads both sides of the conflict
and resolves it in place. A pick that conflicts in more files than the
repository allows (100 by default) is skipped.

After each cherry-pick, the agent builds and tests the branch with the
commands configured for that repository. A backport that breaks the build or
a test is taken off the branch again, so one bad backport never holds up the
ones behind it. The sweep keeps at most two new backports per run, which keeps
each pull request small enough to review properly.

The agent then pushes the branch and opens a pull request into the release
branch, or updates the one that is already open. The PR description lists
what was applied and has a "Needs attention" section explaining anything that
was skipped or failed. When Claude resolved a conflict, the PR gets the
`ai-resolved-conflicts` label so reviewers know to look closely. Backport
commits carry a DCO sign-off.

Once a maintainer merges the PR, the work is done. Within the hour, another
job notices that the commits are now on the release branch and moves their
cards on the project board from "To be backported" to "Done". It checks the
whole board every time, so it also catches backports that someone
cherry-picked by hand.

## Keeping the next pull request coming

If the sweep only ran once a day, a branch would sit idle from the moment its
backport PR merged until the next morning. To avoid that, a poller runs every
hour, and again half an hour later, and asks a simple question for each
branch: is there an open sweep PR? If there is, a human is still reviewing it
and the poller leaves the branch alone. If there isn't, it runs the sweep
straight away and opens a new one.

That open PR is the only state the system keeps. The sweep, the poller and
the CI follow-up described below all share one lock per branch, so they never
work on the same branch at once.

## What gets checked before a backport is kept

Each repository decides how its backports are validated, in `repos.yml`. The
checks run in this order: one-time setup commands, the build, any commands
tied to the paths the backport changed, a validation profile, and generated
files.

For Valkey, the `valkey-core` profile does most of the work. It looks at
exactly what the backport changed and runs the checks that apply to it:
whitespace, `clang-format` using the release branch's own rules, the Tcl tests
that changed (in TLS mode too, where those tests require it), the C and C++
unit tests, the cluster, sentinel and module suites when those areas changed,
and reply-schema validation for changed tests. It also makes sure that a new
unit test file is actually picked up by that branch's test runner, because
older branches discover unit tests differently and a test that silently
doesn't run would look like a pass.

Some files in Valkey are generated, such as the unit test header. When a
backport touches what they are generated from, the agent runs the generator,
checks that running it a second time gives the same result, and folds the
output into the backport commit. If a generator changes any file it isn't
declared to change, the backport is stopped.

## When a backport fails its checks

A repository can opt in to two kinds of help with
`repair_validation_failures`.

The first is a repair. Claude gets one attempt to fix a backport that failed
validation. It may only edit the files the backport touched, and it is not
allowed to delete tests to make the failure go away.

The second is for tests that can't run on an older branch at all. Sometimes a
fix comes with a new test written for a test harness that the release branch
doesn't have. Rather than quietly dropping the test, Claude ports what the
test checks into an existing test that the branch does support, and the whole
backport is validated again. If that can't be done, the backport fails
instead of going in without its test.

## When a backport PR's own CI fails

Validation catches most problems, but the PR's CI runs more jobs on more
platforms. For repositories with `automatic_ci_followup` turned on, which
today is Valkey itself, the agent watches its own open PRs into each release
branch. That means the sweep PR, and also any PR the CI fix bot opened or
pushed a fix to.

Every hour it waits until all the CI runs on a PR's latest commit have
finished, then picks one failed job it hasn't tried yet and hands it to the
[CI fix bot](ci-fix.md). The fix is pushed as a new commit on top, and the
PR's CI runs again. The bot never rewrites the branch and doesn't add a DCO
sign-off to these commits, so a maintainer certifies the change when they
merge it.

Valkey also tests every release branch once a week (`weekly.yml`). When a
sweep PR's own CI has nothing left to fix, the agent takes that branch's
failures from the newest weekly run, one an hour, and fixes them on the sweep
PR under the same rules. The fix is prepared and tested on the sweep PR's
latest commit, not on the commit the weekly run tested, and the comment says
which run it came from. A weekly failure is handled once, however often the
PR's head moves. Without an open sweep PR for the branch there is nowhere to
push, and nothing is done.

A few rules keep this from going in circles. The agent makes at most seven
attempts on any one commit. A run that fails before the agent starts, because
the runner could not install or build what the job needs, is not one of them
and does not count against the job: it is tried again the next hour, up to
seven times. A job it tried and failed to fix gets one more
try; a job it declined to fix is left for a human, and one it declined twice
is left alone on later commits too. Jobs that only report on
other jobs, such as Daily CI's result aggregators and the coverage upload, are
listed in `ci_followup_ignored_jobs` and never touched. Checks that come from
other GitHub Apps, like DCO and Codecov, aren't workflow jobs and are never
considered.

A backport often fails because it depends on an earlier change the release
branch doesn't have, so on a backport branch the fixer first looks for a
change that is already merged on `unstable`: it ports the commit, or adapts
it when it doesn't apply as it is. Where no upstream commit fixes the
failure, such as a test that is flaky on `unstable` too, it writes the fix on
the backport branch, and its comment says that `unstable` does not have it.
Such a fix may change tests and the test harness only: server code changes on
a backport branch only by a commit merged on `unstable`. A fix the bot could
not run is posted as a patch in a comment, never pushed.

## Suggesting backports

Deciding what to backport is still a maintainer's call, but it is easy to miss
a fix. Every day at 00:30 UTC the agent looks at the PRs merged to `unstable`
over the last seven days and comments on the ones that look like backport
candidates. The seven-day window means days that fail, or that GitHub skips,
are covered by a later run. Each PR is judged only once: a run reads the
results earlier runs saved and reuses their decision when the PR's merge
commit and the registered release branches are unchanged. A PR that already
has a comment is skipped, and so is one whose comment a maintainer deleted.

To make the suggestion, the agent first works out which release branches
contain the code the PR changes. It runs `git blame` on the lines the PR
modifies or deletes, and on the lines next to anything it only adds, to find
the commits that wrote them. A release branch is a candidate only if it has
those commits, either directly or because they were backported there. A fix
to a feature that arrived after a release line was cut is never suggested for
that line, and a PR that only adds new files is never suggested at all. Every
registered release branch is considered, including a branch that currently
has only release-candidate tags and has not reached GA.

Claude then reads the diff and the list of candidate branches and decides
whether the change is worth backporting, and to which of those branches. It
can narrow the list but never add to it. The agent posts one comment per PR
naming the branches and their boards, and only when Claude is highly
confident. PRs that change only tests, CI, or docs are never suggested. If it
can't reach a decision about a PR, it posts nothing for that PR and marks the
run as failed, so the next day's run tries again.

Each comment it posts, and each PR that needs a maintainer to look by hand,
also appears as an annotation on the workflow run. The run's job summary
lists every PR with its outcome, including the lower-confidence suggestions
that were not commented.

## Running things by hand

All of these run against `valkey-io/valkey-ci-agent`.

To run the sweep for one board without changing anything, leave `dry_run` on.
Set it to `false` when you want it to cherry-pick, push and update the PR.

```bash
gh workflow run backport-sweep.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey-io/valkey --field project_number=14 --field dry_run=true
```

To backport a single PR to a single branch:

```bash
gh workflow run manual-backport.yml --repo valkey-io/valkey-ci-agent \
  --field pr_url=https://github.com/valkey-io/valkey/pull/3601 --field target_branch=9.0
```

To revert one commit on a sweep branch, for example a backport you don't want
in the PR:

```bash
gh workflow run manual-revert-commit.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey-io/valkey --field branch=agent/backport/sweep/9.0 --field commit_sha=<sha>
```

To update the project boards now rather than waiting for the hourly run (this
always changes the live boards):

```bash
gh workflow run backport-mark-done-poll.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey-io/valkey
```

To run the CI follow-up now:

```bash
gh workflow run backport-ci-followup.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey-io/valkey --field project_number=14
```

To look at backport suggestions for an earlier window, give the last day and
how many days to cover. A manual run is a dry run unless you set
`dry_run=false`: the verdicts go to the job summary and no comments are
posted. PRs named with `prs` are always judged afresh:

```bash
gh workflow run backport-eligibility.yml --repo valkey-io/valkey-ci-agent \
  --field date=2026-09-16 --field days=7 --field dry_run=true
```

The sweep and single-PR workflows upload their results and Claude's evidence
as workflow artifacts.

### On your own machine

These are useful when you are changing the backport code. See
[DEVELOPMENT.md](../DEVELOPMENT.md) for setup.

See which branches the sweep would cover:

```bash
python -m scripts.backport.matrix --registry repos.yml --repo valkey-io/valkey
```

See what the sweep would pick up for a branch, without cherry-picking or
pushing anything:

```bash
python -m scripts.backport.sweep --registry repos.yml --repo valkey-io/valkey \
  --branch 9.0 --target-token "$GITHUB_TOKEN" --discover-only --verbose
```

Preview which board cards would move to "Done". Drop `--dry-run` to move
them, or `--target-branch` to cover every branch:

```bash
python -m scripts.backport.mark_done --registry repos.yml --repo valkey-io/valkey \
  --target-branch 9.0 --target-token "$GITHUB_TOKEN" --dry-run --verbose
```

Ask for backport suggestions for one day. This calls Claude, so it needs
Bedrock credentials:

```bash
TARGET_TOKEN=$(gh auth token) python -m scripts.backport.eligibility \
  --repo valkey-io/valkey --date 2026-09-16 --days 1 --dry-run
```

The single-PR backport will push and open a PR if it gets that far, so only
point it at a repository and token you intend to write to:

```bash
BACKPORT_GITHUB_TOKEN="$GITHUB_TOKEN" python -m scripts.backport.main \
  --registry repos.yml --repo valkey-io/valkey --pr-number 3601 --target-branch 9.0 --verbose
```

## Configuration

Everything that differs between repositories, from build commands to the
release branches and their boards, lives in [`repos.yml`](registry.md). The
secrets and permissions the workflows need are in [setup.md](setup.md).

## Where the code lives

The sweep starts in `scripts/backport/sweep.py`, which reads the registry and
works through each branch. For each PR, `source_plan.py` works out which
commits to pick, `candidate_apply.py` applies them and calls Claude on
conflicts, the `sweep_validation.py` module runs the checks along with the
repair and test-adaptation passes, and `pr_creator.py` opens or updates the
pull request. The remaining `sweep_*.py` modules handle git, the GitHub API
and the PR description.

The other workflows each have their own entry point: `poller.py` for the
hourly poll, `mark_done.py` for the boards, `ci_followup.py` for the CI
follow-up, `eligibility.py` and `eligibility_evidence.py` for suggestions,
`main.py` for a single-PR backport, and `revert_commit.py` for reverts.
`registry.py` loads and validates `repos.yml`, and `matrix.py` turns it into
the workflow's job matrix. `onboarding.py` adds a new release line to the
registry after its first GA release, and is called from
[release automation](release-automation.md).
