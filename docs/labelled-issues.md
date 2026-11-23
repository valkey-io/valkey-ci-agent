# Making the change an issue asks for

A committer can hand a Valkey issue to the bot by putting the
`needs-agent-fix` label on it. The bot writes a test that shows the bug, or
that encodes the behaviour a small feature should have, checks that the test
fails on `unstable`, and then makes the change that passes it. What comes back
is one comment on the issue: the change as a patch, or a draft pull request
that carries it, or the reason there is none. A committer reviews and merges.
The bot never merges.

This is the general case of what the bot does: it makes a change from
evidence, checks it, has it reviewed and proposes it. A [CI fix](ci-fix.md)
is the case where the evidence already exists, as the log of a job that
failed. An issue has no failed job, so the bot makes the evidence first. From
there on both go through the same engine, the same three jobs and the same
publisher. What differs is kept apart: how a failed job is told from a test
bug, and the preference for fixing the test, are for CI fixes only.

## Asking for a change

Put `needs-agent-fix` on the issue. The bot looks for labelled issues every
hour, bugs before feature requests, and works on two at a time.

It is meant for bugs and for small features whose behaviour is agreed in the
issue. A new command, a breaking change, a refactor and anything whose outcome
nobody has settled are not for it, and it says so if it is asked.

Before anything runs, the bot checks the request itself:

- **A committer applied the label.** Someone with triage access can apply
  labels too, so the bot looks up who applied this one and requires write
  access to `valkey-io/valkey`.
- **The issue is nobody else's.** An issue that is assigned, or that an open
  pull request says it fixes, is already being worked on.
- **The text is the one that was labelled.** If the title or the description
  changed after the label went on, a committer reads it again and applies the
  label again.

A request that fails one of these gets a comment that says which, and loses
its label. The same checks run again right before anything is published, so a
person who picks the issue up while the bot works is not raced. Taking the
label off, or closing the issue, calls the request off: the run's result is
then not posted at all, not even its diagnosis.

When the bot is done it comments once, and takes the label off. Applying it
again asks again.

When you apply the label to a crash report, decide first whether the crash
could be a security problem. If it could, follow `SECURITY.md` instead: what
the bot produces is public.

## What the bot does

**It writes the test first.** Claude reads the issue and what committers said
in its comments. Nobody else's comments are read. For a bug it writes a test
that asserts the correct behaviour, and so fails today. For a feature it first
writes down acceptance criteria, from the issue and the committers' comments
alone. If it would have to guess a reply, an error, a default or a limit, it
stops there, and the bot posts the questions a committer has to answer.

**Code checks that the test is evidence.** Only test files may have changed.
The bot builds `unstable` and runs the test three times. It has to fail every
time, on its own assertion: a build that breaks, a missing command or a server
that does not start says nothing about the issue. When Claude wrote several
tests, at most three, each of them has to fail. A test that does not qualify
goes back to Claude with the output, up to five times. After that the bot
reports that it could not show the issue.

**Claude makes the change.** It gets the failing run's output, the test, and
the bot's guide for a change, which ends with Valkey's own guidelines. The
test is frozen: the change may add tests, to the same file too. It may not
remove a line of what code saw fail, or change anything inside that test.

**Code checks the change.** The change is made in Valkey's own pull request
job, `test-ubuntu-latest`, replayed on the bot's runner, so it is built the
way CI builds it, with `-Werror`. If that job cannot be replayed, the bot
falls back to a plain `make`. The test has to pass once and then looped, 40
times, or as many as fit in the time, and at least ten. Every test file the
change touches has to pass in full, and the test has to pass once more in a
build with AddressSanitizer. Code, not Claude, formats the lines the change
touches in C files, with the `clang-format` release Valkey's CI pins, and
regenerates `commands.def` when a command's JSON changed.

Some changes are sent back before they are run:

- a change to CI configuration or to vendored code under `deps/`
- a new command, which is the project's to decide
- more than 300 lines of product source, which is more than a reviewer reads
  in one sitting (tests are not counted)
- a test that sleeps for a fixed time instead of waiting for a condition

**A second session looks for a counterexample.** Its job is not to approve
the change but to find a case it still gets wrong. If the issue reports a
failure after a restart, a test that only checks behaviour before one shows
little. For a feature, it looks for an acceptance criterion no test checks.
When it finds such a case it writes it as a test, and code runs that test. If
it fails, the change goes back to Claude with the reviewer's test. If it
passes, it showed nothing, and is dropped. The reviewer can also reject a
change that does more than the issue needs, departs from the code around it,
or would be hard to backport. An approval has to say which cases it examined.

A change gets three rounds in all. One that does not pass is reported with
the failing test and the analysis, so that the next person does not start
from nothing.

**A crash nobody reported stops everything.** A crash report's test may fail
by the server crashing. An issue is a crash report by its title, `[CRASH]`,
as Valkey's template writes it and as the committer who labelled it saw it. A
crash log in the text does not make it one, or text hidden in an issue could
switch this rule off. For any other issue, a server that crashes on code the
bot has not changed may be a vulnerability: while the test is first run, or
when a reviewer's counterexample is. The attempt ends there. The sessions it
kept are deleted, the command and its output are not logged, and nothing is
said on the issue: it counts as an attempt that stopped with an error, like
one that ran out of time. A crash with the bot's own change in the tree is
that change failing its check, and goes back to Claude like any other.

## What you get back

By default the bot posts the tested change on the issue as a patch, with the
root cause, the acceptance criteria if there were any, and what it checked.
Nothing is pushed.

If `AGENT_FIX_PUBLISH` is set to `draft-pr`, the bot pushes a branch named
`agent/fix/issue-<issue>-<labelling>` and opens a draft pull request. Its
description starts with a short summary fit for the squash commit and the
line that closes the issue. The root cause, the criteria, the checks and
Claude's own notes follow under "AI generated details". The commit has no DCO
sign-off: the bot cannot certify where code came from, so whoever takes the
change over signs it off.

At most five of these pull requests are open at once. While five are open,
labelled issues wait, and a change that finishes while five are open is
posted on its issue as a patch. And when the last three were closed without merging,
the bot stops opening more until someone has looked into why. It starts again
when one of its pull requests merges, or when `AGENT_FIX_REVIEWED_THROUGH` is
set to the number of the newest of those three. A pull request that was
merged and later reverted is not noticed: turn the label off with
`AGENT_FIX_DISPATCH` while that is looked into.

If `AGENT_FIX_PUSH_REPO` names a fork, the branch is pushed there and the
pull request comes from the fork. Its CI then runs with an outside
contributor's permissions, and the token that publishes the change cannot
write to Valkey's code. The Valkeyrie Bot App has to be installed on the fork,
with write access to its contents and workflows: the branch is built on
Valkey's tip, and a fork that is behind takes Valkey's own workflow files
with it.

An attempt that fails, because the bot crashed or ran out of time, posts
nothing and is tried once more. If that fails too, the bot says so on the
issue and takes the label off.

## After the pull request opens

A committer can ask for a fix to a failed job of the pull request's CI, as on
any pull request:

```text
@valkeyrie-ops fix
```

That fix is prepared on the bot's own runner with a plain `make`, not in the
replayed CI job: the pull request's head is a change nobody has reviewed, and
a replayed job's setup runs with the runner's privileges. When the branch is
in `valkey-io/valkey` the fix is pushed to it. When it is in the bot's fork
the fix is posted as a suggestion.

The bot does not yet look after the pull request by itself. It does not
rebase it, fix its CI unasked, mark it ready for review, or apply review
comments.

## Keeping the bot contained

Anyone can write an issue, including text meant to steer the model. The
design assumes that will sometimes work, and limits what the model can reach
and what its output can do. Everything in
[Keeping the bot contained](ci-fix.md#keeping-the-bot-contained) applies: the
job that runs Claude holds no GitHub token, Claude cannot run commands, commit
or push, and every build and test runs as a separate account without
credentials. On top of that:

- Tests written from an issue run without network access. They reach the
  servers they start on the loopback interface, and nothing else.
- Text Claude wrote cannot act on GitHub when it is published. A mention, a
  link, an HTML tag and a closing keyword such as "fixes #12" read the same
  but are broken by an invisible character, so GitHub notifies nobody,
  follows no link and closes nothing. A plain `#12` still shows as a
  reference to that issue. That holds for comments, pull request
  descriptions and commit messages, for CI fixes too.
- Nothing is published that looks like a credential: an AWS key, a GitHub
  token, a private key.
- The head of the bot's own pull request is a change nobody has reviewed yet.
  When the bot is asked to fix its CI, it is built only by the separate
  account on the bot's own runner, never by a replayed CI job's setup, which
  runs with the runner's privileges.

The bot keeps no database. The label is the queue. A run is named after its
issue and the labelling it answers, the bot's comment on the issue records
the answer, and a failed attempt is a failed run. Each hour's pass reads that
state again from GitHub.

## Rolling it out

The steps of the rollout map to switches, so each can be measured before the
next is turned on.

1. **Replay.** Run **Agent Fix Labelled** by hand with `replay` set to a commit.
   The change is made on that commit for any issue, open or closed, and
   nothing is written to Valkey. The result is the run's `agent-fix-result`
   artifact. Replaying a closed bug on the commit before its fix shows what
   the bot would have done, to compare with what was merged. A replay reads
   the issue's text and none of its comments, which would say how it was
   fixed. The commit has to be one of `unstable`'s own.
2. **Comments.** Create the `needs-agent-fix` label in `valkey-io/valkey` and
   set `AGENT_FIX_DISPATCH` to `true`.
3. **Pull requests.** Set `AGENT_FIX_PUBLISH` to `draft-pr`, and
   `AGENT_FIX_PUSH_REPO` if the pull requests should come from a fork.
4. **Review loop.** Not built yet.

Setting `AGENT_FIX_DISPATCH` to anything else stops the bot from picking up
labels.

## Running it by hand

To answer one labelled issue now instead of waiting for the next hour:

```bash
gh workflow run agent-fix-labelled.yml --repo valkey-io/valkey-ci-agent --field issue=1234
```

To replay an issue on a commit, writing nothing to Valkey:

```bash
gh workflow run agent-fix-labelled.yml --repo valkey-io/valkey-ci-agent \
  --field issue=1234 --field replay=0123456789abcdef0123456789abcdef01234567
```

To see which issues the dispatcher would start, without starting any:

```bash
gh workflow run agent-fix-labelled-dispatch.yml --repo valkey-io/valkey-ci-agent --field dry_run=true
```

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `AGENT_FIX_DISPATCH` | unset | Set to `true` to pick up labelled issues every hour. |
| `AGENT_FIX_MAX_RUNS` | `2` | How many labelled issues are worked on at once. |
| `AGENT_FIX_PUBLISH` | `comment` | `comment` posts the change on the issue; `draft-pr` opens a draft PR. |
| `AGENT_FIX_PUSH_REPO` | unset | The bot's own fork, `owner/name`, to push branches to. Unset, they are pushed to `valkey-io/valkey`. |
| `AGENT_FIX_REVIEWED_THROUGH` | unset | A pull request number. The bot's pull requests up to it no longer count towards the pause after three closed unmerged. |

The label is currently read on `valkey-io/valkey` only. The engine's checks
follow Valkey's `make`, `runtest`, `clang-format` and `commands.def`
conventions, and another repository would need its own.

## Where the code lives

`scripts/agent_fix/labelled.py` is the front door: it checks the labelling, plans
the change and reports the result on the issue. `issue_dispatch.py` starts
the runs, for this queue and for the detector's.

The change itself is made by `prepare_change` in `engine.py`: the test first
(`_establish_evidence`), then the same loop a CI fix goes through, with the
extra checks (`_change_checks`), the limits (`change_rejection`) and the
counterexample review (`_counterexample_review`). The prompts for the three
sessions are in `agent.py`. `publish.py` rechecks the issue and publishes,
and `comment.py` writes the report.
