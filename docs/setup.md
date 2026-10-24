# Setup and operations

This page covers what the agent needs in order to run in GitHub Actions on
`valkey-io/valkey-ci-agent`: its secrets and variables, the GitHub App and
its permissions, the protected environments, and when each workflow runs.

## Secrets and variables

The agent needs three secrets:

| Secret | What it is |
|---|---|
| `AWS_ROLE_ARN` | The AWS role the workflows assume through OIDC to call Claude on Bedrock. It needs permission to invoke the model. |
| `VALKEYRIE_BOT_APP_ID` | The ID of the Valkeyrie Bot GitHub App. |
| `VALKEYRIE_BOT_PRIVATE_KEY` | The App's private key. |

No long-lived credentials are stored. Each job exchanges the App's key for a
GitHub token that lasts an hour and only covers what that job needs, and
exchanges a GitHub OIDC token for short-lived AWS credentials.

These repository variables adjust its behaviour:

| Variable | What it does |
|---|---|
| `AWS_REGION` | The AWS region to use, for example `us-east-1`. |
| `CI_AGENT_CLAUDE_MODEL` | Use a different model, for example `opus`. Unset means the default. |
| `CI_FIX_ISSUES_AUTO_DISPATCH` | Set to `true` to let the CI fix bot start on detector issues by itself. |
| `CI_FIX_ISSUES_MAX_RUNS` | How many detector issue fixes can run at once. Defaults to 2. |
| `CI_FIX_ISSUES_PUBLISH` | How a detector issue's fix is shared: `comment` (the default) or `draft-pr`. |

### The model

By default the agent uses Claude Fable 5 through the
`us.anthropic.claude-fable-5` Bedrock inference profile. The AWS account needs
access to that profile, and its Bedrock data retention must be set to
`provider_data_share`, because Fable 5 isn't available with zero data
retention. If you need to, set `CI_AGENT_CLAUDE_MODEL=opus` to fall back to
Claude Opus 4.8. The workflows install Claude Code CLI version 2.1.175.

## GitHub App permissions

One App, the Valkeyrie Bot, is used by every workflow. Give it only the
permissions below.

| Installed on | Permissions | Needed for |
|---|---|---|
| `valkey` | read administration, actions and checks; write contents, issues and pull requests; read metadata | backports, CI fixes, the detector, CI health, release notes and release automation |
| `valkey-search`, `valkey-json`, `valkey-bloom` | write contents and pull requests; read metadata | backports and release notes |
| repositories whose releases list security advisories | read repository advisories | release notes, when reading advisories |
| `valkey-fuzzer` | read actions and contents; write issues | the fuzzer monitor |
| `valkey-container` | write actions; read contents and metadata | starting CVE rebuilds |
| `valkey-ci-agent` | write actions, contents, issues and pull requests; read metadata | release tracking issues, registry PRs, and starting the release PR review watchers and their edits |
| `valkey-release-automation` | read actions, contents and metadata | release automation |
| the organization | read members; read and write organization projects | checking who may ask for fixes and releases, and reading and updating backport boards |

The release notes workflow can only release repositories the App is installed
on, so make sure all four are included.

## Environments

| Environment | How to configure it | What it protects |
|---|---|---|
| `release-control` | Only the default branch can deploy. | The token release automation uses to prepare and track releases. |
| `release` | Only the default branch can deploy. Require a reviewer, prevent self-review, and don't allow admins to bypass. | Creating the release tag and publishing the release. |
| `cve-rebuild-dispatch` | Only `main` can deploy. No reviewers needed. | The token that can start a container rebuild. |

[Release automation](release-automation.md#setting-it-up-securely) has more on
securing releases, including the tag ruleset.

## When things run

| Workflow | When (UTC) | More |
|---|---|---|
| Backport Sweep | every day at 09:00 | [Backports](backport.md) |
| Backport Poll | every hour, and again half an hour later | [Backports](backport.md#keeping-the-next-pull-request-coming) |
| Backport CI Follow-up | every hour at :20 | [Backports](backport.md#when-a-backport-prs-own-ci-fails) |
| Backport Mark Done Poll | every hour at :30 | [Backports](backport.md#how-a-fix-gets-backported) |
| Backport Eligibility | every day at 00:30 | [Backports](backport.md#suggesting-backports) |
| CI Fix Comment Poll | every hour | [Fixing CI failures](ci-fix.md#asking-for-a-fix) |
| CI Fix Issue Dispatch | every hour at :23, once switched on | [Fixing CI failures](ci-fix.md#fixing-detector-issues) |
| Test Failure Detector | every day at 23:00 | [Test Failure Detector](test-failure-detector.md) |
| CI Health | every day at 06:00 | [CI health](ci-health.md) |
| Monitor Valkey Fuzzer Runs | every four hours at :41 | [Fuzzer monitor](fuzzer-monitor.md) |
| Watch Release PR Reviews | one per release PR, started by the cut; holds a runner for 55 minutes checking every five minutes, with the next hour's run queued, until the PR closes or is three days old | [Release notes](release-notes.md#applying-review-comments) |
| Restart Release PR Watchers | every six hours at :23 | [Release notes](release-notes.md#applying-review-comments) |
| Refresh Release Progress | every hour, checking every five minutes | [Release automation](release-automation.md) |
| CVE Scan | Mondays at 06:00 | [CVE scan](cve-scan.md) |

These only run when started, by a person or by another workflow: Manual
Backport, Manual Revert Commit, CI Fix Bot, CI Fix Issues, Cut Release Notes
and its Advanced version, Address Release Note Reviews, Prepare Release and
Publish Release. Backport PR and CI Fix Run are reusable workflows that other
workflows call.

The workflows that check every few minutes do it inside one long-running
job, because GitHub's scheduler often starts jobs late. The job stays alive
for 55 minutes, which also keeps it inside the lifetime of its one-hour
token. When you start one of the scheduled ones by hand, it checks once and
stops. The review watcher is different: it always watches for the full 55
minutes and then starts itself again, so starting it by hand resumes a watch.

## How every job is set up

Every job checks out this repository and runs the shared setup action in
`.github/actions/setup-agent`. It installs Python and the agent's
dependencies, and Claude Code when the job needs it. Jobs that run inside a
container can ask it to install everything from the container's own packages
instead. Tests in this repository check that every workflow pins its external
actions and installs Claude Code the same way.
