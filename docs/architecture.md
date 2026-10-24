# Architecture

This page explains how the agent is put together and the ideas that every
workflow shares. Each workflow's own page describes what it does step by
step; this one is about what they have in common.

## The ideas behind it

**Code decides, Claude helps.** The agent is mostly ordinary Python. Code
works out exactly what to act on, down to a specific CI job and commit. Code
checks everything Claude hands back, and code makes every commit and push.
Claude is brought in only where reading and understanding code is the actual
task: resolving conflicts, diagnosing failures, writing release notes,
triaging fuzzer runs, and judging whether a fix should be backported. The CVE
scan doesn't use Claude at all.

**Propose, don't merge.** Every change the agent makes goes on one of its own
branches, `backport/...`, `agent/backport/...`, `agent/ci-fix/...` or
`agent/release-cut/...`, and reaches the project as a pull request. That PR
runs the repository's normal CI and needs a maintainer to merge it. The agent
never pushes to a release branch and never rewrites a backport branch it has
already published.

**Claude never gets a GitHub token.** When the agent starts Claude, it strips
GitHub tokens and other secrets from its environment and gives it only the AWS
credentials it needs to reach the model. Git gets its credentials through a
helper, so tokens never end up in a repository's configuration or in a URL.
The CI fix bot also runs Claude as a separate system account.

**When unsure, stop.** A failed check blocks a push. A configuration that
can't be read means nothing gets filed. A CVE fix that can't be proven isn't
rebuilt. A release PR with open questions opens as a draft.

**Ask for as little as possible.** Each job gets a GitHub token that lasts an
hour and covers only the repositories and permissions that job needs. Jobs
that run Claude don't get a GitHub token at all.

## How the code is laid out

```text
scripts/
  backport/              Backports, suggestions, CI follow-up, onboarding release lines
  ci_fix/                The CI fix bot
  test_failure_detector/ Daily CI failures to issues
  ci_health/             Daily CI history and flakiness statistics
  fuzzer/                Fuzzer run triage
  release_notes/         Release notes and review comments on release PRs
  release/               Release preparation, tracking and publication
  cve_scan/              Container CVE scan, verification and rebuilds
  parsers/               Reading Trivy's output
  ai/                    Starting Claude Code, and what each session may do
  common/                Code shared between workflows
.github/workflows/       The workflows
.github/actions/setup-agent
                         Shared job setup
repos.yml                The repositories and release branches backports cover
release_policy.yml       Who may release, and which branches and checks count
```

Each workflow has its own package under `scripts/`. A new workflow is a new
package next to them that reuses `ai/` and `common/` as they are.

## The shape of a workflow

Most workflows are split into the same three parts.

The first part receives the request. It might be a scheduled job that finds
work, or a workflow someone started by hand. It checks that the request is
allowed and pins it to exact facts, such as a run, a job and a commit, and
produces a description of the task. It doesn't diagnose or publish anything
itself.

The second part does the work. It prepares one result from that task: it
clones, searches, reproduces, calls Claude, and checks what comes back.

The third part publishes. Right before writing, it checks again that the
destination is what it expects, such as the same commit at the head of the
same branch on a PR the bot owns. It writes only to the bot's own branches,
and reports back where the request came from.

The CI fix bot shows this most clearly, with several ways in that all lead to
the same engine and the same publisher. The backport sweep, the release notes
cut and release automation are built the same way.

## Workflows

| Workflow | Page | Starts in |
|---|---|---|
| Backports | [backport.md](backport.md) | `scripts/backport/sweep.py` |
| Fixing CI failures | [ci-fix.md](ci-fix.md) | `scripts/ci_fix/run.py` |
| Test Failure Detector | [test-failure-detector.md](test-failure-detector.md) | `scripts/test_failure_detector/main.py` |
| Fuzzer monitor | [fuzzer-monitor.md](fuzzer-monitor.md) | `scripts/fuzzer/main.py` |
| Release notes | [release-notes.md](release-notes.md) | `scripts/release_notes/main.py` |
| Release automation | [release-automation.md](release-automation.md) | `scripts/release/main.py` |
| CVE scan | [cve-scan.md](cve-scan.md) | `scripts/cve_scan/sweep.py` |
| CI health | [ci-health.md](ci-health.md) | `scripts/ci_health/main.py` |

A PR reviewer with a second, skeptical pass is planned. CI health has its
store, statistics, workflow and dashboard; the Slack report follows.

## How Claude is run

Every workflow runs Claude the same way. It calls `run_agent` in
`scripts/ai/runtime.py` with a profile and a prompt, which starts the Claude
Code CLI as a subprocess talking to Bedrock and returns its output.

A profile decides which tools a session gets, how long it may run, and how
hard it should think. Each task gets the narrowest set of tools that works:

| Profile | What Claude can do | Used for |
|---|---|---|
| `conflict_resolve_edit_only` | Read, search, edit and run commands, but not create files | Resolving backport conflicts |
| `validation_repair_edit_only` | Read, search and edit | Repairing a backport that failed its checks |
| `test_adaptation_edit_only` | Read, search and edit | Porting a test an older branch can't run |
| `fuzzer_analysis_readonly` | Read and search | Triaging fuzzer runs |
| `ci_fix_edit` | Read, search, edit, create files and run commands, as a separate account; never commit or push | Fixing CI failures on the bot's own PRs |
| `ci_fix_review` | Read and search, as a separate account | Reviewing a proposed CI fix |
| `ci_fix_suggest` | Read, search, edit and create files, but not run commands | Suggesting fixes on contributors' PRs |
| `release_notes_review_edit_only` | Read, search and edit | Applying review comments to release notes |

Some tasks need no tools at all. When Claude decides which PRs belong in the
release notes, writes them, or judges whether a PR should be backported,
everything it needs is put straight into the prompt. When Claude has to hand
back a structured answer, `scripts/common/ai_output.py` pulls it out of
Claude Code's output.

## Shared code

`scripts/common/` holds the code the workflows share, so each one doesn't
have to solve the same problems again.

For git and GitHub, `git_auth.py` hands git its credentials without writing
them anywhere, and `git_clone.py` clones a repository at an exact commit,
checking its arguments carefully. `github_client.py` retries GitHub API calls
that fail for temporary reasons. `proc.py` runs git commands and builds the
cleaned-up environment that subprocesses run with.

For CI runs, `workflow_artifacts.py` lists runs and downloads their artifacts
and job logs, and `workflow_jobs.py` reads all of a run's jobs at once, so
every decision about a run is based on the same snapshot. `valkey_ci.py`
reads Valkey's `runtest` output. `build_validator.py` runs the build and test
commands from `repos.yml`.

For issues, `incidents.py` turns a failure into a stable fingerprint, and
`issue_dedup.py` uses that fingerprint to open a new issue or update the
existing one. The detector and the fuzzer monitor both use it, each with
their own way of writing the issue, and CI health keys its records with the
same fingerprint so a scorecard row and the detector's issue share an id.

`isolation.py` runs a command, or Claude, as the separate `ci-fix-agent`
account with its own home and temporary directories, and takes the code back
safely afterwards. `polling.py` keeps a job checking at a steady pace for
most of an hour. `identity.py` defines the bot's accounts, and
`job_summary.py`, `logging_utils.py` and `text_utils.py` handle output.

## Where the agent pushes

The agent pushes its branches to the repository it is working on and opens
pull requests there. That keeps `repos.yml` simple and matches the
permissions the GitHub App has. Pushing to a fork owned by someone else is
possible as an escape hatch, but not to a fork with the same owner, so staging
copies don't become the usual way of working. See [the registry](registry.md).

## How the safety measures are tested

The test suite runs on Python 3.9 and 3.11. On 3.11, CI also installs Claude
Code and creates a real `ci-fix-agent` account, so the tests for the separate
account and for limiting Claude's tools
(`tests/test_isolation.py` and `tests/test_claude_code_confinement.py`) run
for real instead of being skipped. Another job runs the isolation tests as
root inside an Alpine container, the way a Valkey container job would run
them. Other tests check that every workflow pins its external actions,
installs Claude Code the same way, and asks only for the permissions it
should.
