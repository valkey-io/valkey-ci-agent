# valkey-ci-agent

valkey-ci-agent is the automation behind a lot of the Valkey project's routine
maintenance. It backports fixes to release branches, chases down failing CI
jobs, files issues when the nightly tests or the fuzzer find something, cuts
releases and writes their notes, and keeps the published container images free
of known vulnerabilities.

Most of that work is ordinary engineering done in Python: calling the GitHub
API, running git, building and testing. For the handful of steps that need
someone to actually read code, such as resolving a cherry-pick conflict,
working out why a test fails, or describing a change in a release note, the
agent asks Claude, running through AWS Bedrock. Whatever Claude produces is
checked by code before it goes anywhere.

Nothing the agent does takes effect on its own. Every change it proposes shows
up as a pull request or a comment, runs through the target repository's normal
CI, and waits for a maintainer. The bot never merges anything.

## What it does

**Backports.** When a fix on `unstable` should also go to a release line, a
maintainer adds it to that branch's project board. Every day the agent
cherry-picks what is waiting there, resolves conflicts, builds and tests the
result, and keeps one pull request per release branch open for review. It
also comments on newly merged PRs that look like they should be backported.
See [Backports](docs/backport.md).

**Fixing CI failures.** A maintainer can comment `@valkeyrie-ops fix` on a PR
with a failing job. The bot reproduces the failure, asks Claude for a fix,
reruns the failing test to make sure the fix works, has a second Claude session
review it, and then pushes it or posts it as a suggested patch. It does the
same on its own when one of its backport PRs goes red. See
[Fixing CI failures](docs/ci-fix.md).

**Tracking test failures.** Every night, once Valkey's Daily CI has finished,
the Test Failure Detector opens or updates one issue per failing test, so
broken and flaky tests are tracked instead of forgotten. See
[Test Failure Detector](docs/test-failure-detector.md).

**Watching the fuzzer.** Every four hours the agent looks at the latest
valkey-fuzzer run and decides whether a failure is a real bug or the expected
noise of a chaos test. Real bugs get an issue. See
[Fuzzer monitor](docs/fuzzer-monitor.md).

**Releases.** A maintainer can cut a release of Valkey or one of its modules
with one dispatch. The agent gathers every change since the last release,
writes the notes, bumps the version and opens a release PR, and it applies the
inline comments maintainers leave on that PR. For Valkey itself, release
automation then carries the merged PR through to a tagged, published release,
with two human approvals on the way. See [Release notes](docs/release-notes.md)
and [Release automation](docs/release-automation.md).

**Container vulnerabilities.** Once a week the agent scans the published
Valkey container images for known CVEs. When a rebuild would fix one, it builds
the image itself and scans it to prove the fix before asking valkey-container
to rebuild and publish. See [CVE scan](docs/cve-scan.md).

A PR reviewer with a second, skeptical pass is planned. Detection of flaky
tests across Daily runs is in progress: the statistics layer is in, and the
workflow that feeds it follows. See [CI health](docs/ci-health.md).

## Using it

Most of the time you don't need to do anything. The scheduled workflows run on
their own and open PRs or issues when there is something to look at. When you
do want something:

- To get a failing job on a `valkey-io/valkey` PR fixed, start a comment with
  `@valkeyrie-ops fix`. You need to be a member of `valkey-io/contributors`.
- To cut a release, run **Prepare Release** from this repository's Actions
  tab. To generate only the notes, run **Cut Release Notes**.
- To backport one PR right away, run **Manual Backport**.
- Every scheduled workflow can also be started by hand with `gh workflow run`,
  and most of them do a harmless dry run unless you say otherwise. Each page
  under `docs/` ends with the commands for its workflows.

## Why it is safe to leave running

The agent only ever writes to its own branches: `backport/...`,
`agent/backport/...`, `agent/ci-fix/...` and `agent/release-cut/...`. It opens
pull requests from those branches and leaves the merge to a maintainer. It
never pushes to a release branch and never rewrites a backport branch that has
already been published.

Claude never holds a GitHub token. It gets the AWS credentials it needs to
reach the model and nothing else, and every commit and push is made by code
after checking what Claude changed. The CI fix bot goes further and runs Claude
and the tests it starts as a separate system account that cannot see the
workflow's secrets.

Changes are tested before they are published. Backports are built and tested
before they are pushed, and a failure keeps them out. A CI fix has to make the
failing test pass and survive an independent review. A container rebuild is
requested only after the fixed image has been built and scanned. When the
agent can't be sure of something, it holds back: an unreadable configuration
files nothing, a release PR with open questions opens as a draft, and a fix
that couldn't be proven says so.

Each job gets a GitHub token that lasts an hour and only covers the
repositories and permissions that job needs. [Architecture](docs/architecture.md)
covers the design in more depth.

## Finding your way around

```text
scripts/
  backport/               Backports, backport suggestions, CI follow-up
  ci_fix/                 The CI fix bot
  test_failure_detector/  Daily CI failures to issues
  ci_health/              Daily CI history and flakiness statistics
  fuzzer/                 Fuzzer run triage
  release_notes/          Release notes and review comments on release PRs
  release/                Release preparation and publication
  cve_scan/               Container CVE scan and verified rebuilds
  ai/                     Running Claude Code, and what each session may do
  common/                 Shared git, GitHub, artifact and isolation code
.github/workflows/        The workflows themselves
docs/                     One page per workflow, plus setup and design
repos.yml                 The repositories and release branches the agent covers
tests/                    One test module per source module
```

The docs are:

- [Setup and operations](docs/setup.md): secrets, GitHub App permissions,
  environments, and when every workflow runs.
- [The registry](docs/registry.md): how `repos.yml` describes a repository.
- [Architecture](docs/architecture.md): how the pieces fit together.
- [DEVELOPMENT.md](DEVELOPMENT.md): working on the agent itself.

## License

BSD 3-Clause. See [LICENSE](LICENSE).
