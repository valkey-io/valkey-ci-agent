# valkey-ci-agent

An AI-powered CI automation agent for the Valkey project. Uses Claude Code (Anthropic Claude Opus via Bedrock) to perform tasks that require code understanding - conflict resolution, code review, failure analysis, and more.

## Architecture

The agent is structured as a layered framework:

```text
scripts/
  ai/          AI layer: Claude Code subprocess orchestration
  backport/    Automated backports (active)
  fuzzer/      Fuzzer run monitoring (active)
  ci_fix/      On-demand CI test-fix bot (active)
  common/      Shared infrastructure (git auth, GitHub client, safety guards)
.github/actions/setup-agent
              Shared workflow setup for Python deps and optional Claude Code
repos.yml      Central registry of repos, branches, and project boards
```

New workflows are added as sibling directories to `backport/`. Each workflow picks an agent profile (tools, timeout, effort) and writes its own prompt. The AI layer and shared infra stay unchanged.

**Workflows:**

| Workflow | Status | Description |
|----------|--------|-------------|
| Backport | Active | Cherry-picks merged PRs onto release branches with AI conflict resolution |
| Fuzzer Monitor | Active | Analyzes scheduled fuzzer runs and files issues for anomalous failures |
| CI Fix | Active | On-demand `@valkeyrie-bot fix <ci-link>` - diagnoses and fixes a failing test on a backport PR |
| Test Failure Detector | Active | Detects test failures from Daily CI, files/updates GitHub issues |
| PR Reviewer | Planned | Two-stage code review with skeptic pass |
| Additional Daily CI Analysis | Planned | Detects flaky tests, generates fix PRs |

## Backport Workflow

The currently active workflow. Cherry-picks merged PRs onto release branches with AI-powered conflict resolution. Works for any repo defined in `repos.yml` - Valkey core, Valkey modules (bloom, search, json), or anything else following the per-branch project-board pattern.

### How it works

1. **Daily sweep** - every day at 09:00 UTC, the preflight job reads `repos.yml` and generates one matrix leg per `{repo, branch}` pair
2. **Project discovery** - each leg queries the GitHub Project v2 board for PRs marked "To be backported"
3. **Cherry-pick** - attempts `git cherry-pick` for each candidate onto the target release branch
4. **AI conflict resolution** - when cherry-pick conflicts, Claude Code reads both sides and resolves the conflict in place
5. **Validation** - registry-configured build commands run before push; any failure blocks the push
6. **PR creation** - pushes the branch and opens (or updates) a PR with a summary table
7. **Status sync** - after a backport PR is merged into the release branch, the source PR's Project v2 status can be moved from "To be backported" to "Done"

Manual single-PR backports are also supported via `workflow_dispatch`.

### Registry (`repos.yml`)

The registry is the single source of truth. To onboard a new repo, add an entry to `repos.yml`:

```yaml
repos:
  - repo: valkey-io/valkey
    project_owner: valkey-io
    project_owner_type: organization
    language: c                          # used in conflict resolver prompt
    validation_setup_commands:
      - "./ci/setup-backport-validation.sh" # optional; run once in clone
    build_commands:
      - "make -j$(nproc)"                # run before push; empty = skip
    repair_validation_failures: false    # optional; one AI repair attempt on failure
    backport_label: backport
    llm_conflict_label: ai-resolved-conflicts
    max_conflicting_files: 100
    branches:
      - branch: "8.1"
        project_number: 14
      - branch: "9.0"
        project_number: 18
```

By default, agent branches are pushed directly to `repo` under the `agent/backport/...` namespace and PRs are opened in that same upstream repository. `push_repo` is optional and only exists as an escape hatch for a real different-owner fork; same-owner `push_repo` values are rejected so staging repositories do not become the normal model.

The sweep branch is always kept green: a candidate is only kept if the whole branch still validates after the cherry-pick, so one bad commit can never block later candidates. Each scheduled run keeps up to two validated cherry-picks (`--max-candidates 2`) and reports candidates that were skipped or failed validation in the PR's "Needs attention" section without committing them. When `repair_validation_failures` is enabled, Claude Code gets one narrow edit-only attempt to fix a failing cherry-pick before it is dropped.

See [`examples/repos.yml`](examples/repos.yml) for a multi-module example.

### Installation

#### Prerequisites

- A GitHub App with:
  - `contents:write` on each repo in the registry (for pushing branches)
  - `pull-requests:write` on each repo (for opening PRs)
  - `issues:write` on each repo (for backport status comments)
  - `organization_projects:write` on the org (for querying and updating project boards)
- An AWS account with Bedrock access to `us.anthropic.claude-opus-4-8`
- An OIDC trust between GitHub Actions and your AWS account

#### Step 1: Configure secrets and variables

On `valkey-io/valkey-ci-agent`:

| Type | Name | Value |
|------|------|-------|
| Secret | `AWS_ROLE_ARN` | OIDC role ARN with Bedrock `InvokeModel` permission |
| Secret | `VALKEYRIE_BOT_APP_ID` | Valkeyrie GitHub App ID |
| Secret | `VALKEYRIE_BOT_PRIVATE_KEY` | Valkeyrie GitHub App private key |
| Variable | `AWS_REGION` | e.g., `us-east-1` |

The workflows mint a short-lived installation token with `actions/create-github-app-token` and use that token for registry repository reads, branch pushes, PR creation, status comments, and project-board queries.

#### Step 2: Edit `repos.yml`

Add your repo(s) to the registry. No per-repo config files are needed - everything lives in `repos.yml`.

#### Step 3: Enable the workflows

The scheduled sweep runs automatically.

### Usage

#### Daily sweep (automatic)

Runs daily at 09:00 UTC via cron. The preflight job reads `repos.yml` and fans out one job per `{repo, branch}`. Each produces one PR with up to five successfully applied backports for that branch; skipped or unresolved candidates do not count against that cap.

#### Manual backport (on-demand)

```bash
gh workflow run manual-backport.yml \
  --repo valkey-io/valkey-ci-agent \
  --field pr_url=https://github.com/valkey-io/valkey/pull/3601 \
  --field target_branch=9.0
```

Creates one PR named `[Backport 9.0] <original title>`.

#### Mark merged backports done

A scheduled poller reconciles each project board against branch reality and
flips items from `To be backported` to `Done` once the backport actually lands.
It starts hourly via `backport-mark-done-poll.yml`, reconciles immediately, and
reconciles once more 30 minutes later inside the same runner. It covers every
repo and branch in `repos.yml`. Because it reconciles the whole board on every
run, it is self-healing: it picks up backports applied by the sweep, by an
earlier run, or by a manual cherry-pick, without depending on a merge event.

An item is marked `Done` only when the source PR's commit is genuinely on the
target branch - verified by the cherry-pick's trailing `(#N)` subject, or by the
PR appearing in a sweep commit's `## Applied` table. A backport PR body that
merely claims a PR was applied can never mark it `Done` on its own.

Run it manually for a single repo (omit `--target-branch` to reconcile every
configured branch), and add `--dry-run` to report what would change without
mutating the board:

```bash
python -m scripts.backport.mark_done \
  --repo valkey-io/valkey \
  --target-branch 9.1 \
  --target-token "$TOKEN" \
  --dry-run
```

#### Filtering the sweep

To run only for a specific repo or branch:

```bash
gh workflow run backport-sweep.yml \
  --repo valkey-io/valkey-ci-agent \
  --field repo=valkey-io/valkey \
  --field project_number=14
```

## Fuzzer Monitor Workflow

The fuzzer monitor watches scheduled `valkey-io/valkey-fuzzer` workflow runs, analyzes their artifacts, and files issues for runs that look anomalous.

### How it works

1. **Cron** - every 4 hours, the monitor checks the latest scheduled fuzzer run
2. **Deterministic scan** - pattern-matches crash/sanitizer/failover/RDB signals against artifact JSON and node logs; ignores chaos-expected noise (CLUSTERDOWN, replication link loss)
3. **Claude Code analysis** - drops the artifacts in a tempdir, shallow-clones `valkey-io/valkey` at the tested commit and `valkey-io/valkey-fuzzer` at the run's HEAD, then asks Claude (with read-only `Read,Grep,Glob` tools) to correlate the failure with source and decide whether the run reflects a real bug or chaos-expected noise. If a clone fails the prompt tells Claude not to cite source line numbers.
4. **Issue upsert** - anomalous runs file (or update) an issue on `valkey-io/valkey-fuzzer`, deduplicated by a stable fingerprint over root cause and anomaly shape
5. **Audit** - per-run JSON results and Claude evidence are uploaded as workflow artifacts

The Claude Code subprocess runs under the `fuzzer_analysis_readonly` agent profile with `Read,Grep,Glob` tools only - no editing, no Bash, no network access beyond the Bedrock call itself.

### Configuration

The monitor reuses the same secrets and OIDC role as the backport workflow (see [Step 1](#step-1-configure-secrets-and-variables) above). The Valkeyrie GitHub App needs `actions:read`, `contents:read`, and `issues:write` on `valkey-io/valkey-fuzzer`; the workflow mints a short-lived installation token scoped to that repository only.

### Manual run

```bash
# Run live against the latest scheduled fuzzer run (default)
gh workflow run monitor-fuzzer.yml --repo valkey-io/valkey-ci-agent

# Probe without invoking Claude or filing issues
gh workflow run monitor-fuzzer.yml \
  --repo valkey-io/valkey-ci-agent \
  --field dry_run=true
```

Scheduled runs always run live.

## Test Failure Detector

Monitors the Daily CI workflow on `valkey-io/valkey`, detects test failures, and automatically creates or updates GitHub issues to track them.

### How it works

The detector is a thin pipeline (`scripts/test_failure_detector/`) layered on shared building blocks in `scripts/common/` - `ArtifactClient` for artifact download and `IssueDedupPublisher` for issue dedup/publishing - so the same primitives back the Fuzzer Monitor.

1. **Daily sweep** - every day at 23:00 UTC the workflow runs on `valkey-io/valkey-ci-agent` and reads from `valkey-io/valkey`. Valkey Daily CI runs daily at 00:00 UTC, so this detector workflow will capture the current day's run. In case of a miss for any reason, manual dispatch functionality is available (more detail in 'Scheduled' section below).
2. **Find the run** - locates the most recent completed, non-cancelled/skipped Daily workflow run on the target branch (`unstable` by default), or uses a manually supplied run ID
3. **Download artifact** - `ArtifactClient` fetches the `all-test-failures` artifact, handling the auth-header-stripping redirect to Azure blob storage, transient-failure retries, and expired-artifact (404) cases
4. **Get job URLs** - fetches job metadata from the run to build CI links for each failure, with normalized name variants for fuzzy matching against artifact names
5. **Parse and deduplicate** - iterates the nested JSON (`{job -> suite -> [failures]}`) and groups by a `{test_name}::{test_file}` fingerprint, so a test failing across multiple jobs becomes one `UniqueFailure` with multiple job references
6. **Create or update issues** - `IssueDedupPublisher` upserts one issue per fingerprint, matching on a hidden body marker (`<!-- valkey-ci-agent:test-failure:{test_name}::{test_file} -->`) rather than the title. Per-failure rendering (title, body, recurrence comment, `test-failure` label) lives in `issue_renderer.py`. Each failure resolves to one of three outcomes:
   - **created** - no matching issue exists: opens one titled `[TEST-FAILURE] {test_name} in {test_file}` with the `test-failure` label, error trace, CI links, and environment list
   - **updated** - a matching issue exists: merges any new failing environments into the body and bumps the occurrence counter / adds a recurrence comment
   - **skipped** - the run ID matches the `last-key` marker already recorded on the issue, so a re-triggered sweep over the same CI run does not inflate the occurrence count or post a duplicate comment

A GitHub Actions job summary is emitted at every exit path with a table of metrics (failures detected, issues created/updated).

#### Prerequisites: Cross-repo Authentication

The workflow generates a GitHub App installation token scoped to the `valkey-io` org using the same App secrets as the backport workflow (`VALKEYRIE_BOT_APP_ID` + `VALKEYRIE_BOT_PRIVATE_KEY`). This token provides `actions:read` (to download artifacts) and `issues:write` (to create/update issues) on `valkey-io/valkey`.

### Usage

#### Scheduled (automatic)

Runs daily at 23:00 UTC via cron. The workflow runs on `valkey-io/valkey-ci-agent` and uses a GitHub App token to read artifacts from the most recent completed, non-cancelled/skipped Daily workflow run on the target branch (`unstable` by default), and write issues to `valkey-io/valkey`. Valkey Daily CI runs daily at 00:00 UTC, with runs typically completing within 4-7 hours, with slight exception (from valkey-io/valkey's history of 411 completed runs, 6 runs exceed 7 hours, with the longest lasting 10h 02m), such that Test Failure Detector should always capture the current day's workflow (workflow completes within seconds). In the event of a missed run, the current detector includes manual dispatch functionality, targeting a given run ID. Manual dispatches can be performed so long as Daily CI artifacts persist, currently set at 30 days.

#### Manual dispatch

```bash
gh workflow run test-failure-detector-sweep.yml \
  --repo valkey-io/valkey-ci-agent \
  --field repo=valkey-io/valkey \
  --field run_id=12345678 \
  --field dry_run=true
```

- `repo` - target repository to scan (default: `valkey-io/valkey`)
- `run_id` - specific workflow run ID to analyze (empty = latest Daily run)
- `dry_run` - parse and report only, don't create/update issues

## CI Fix Workflow

An on-demand workflow that fixes a single failing test on a backport PR when a
maintainer asks for it. From this agent repository, run it explicitly:

```bash
gh workflow run ci-fix.yml \
  --repo valkey-io/valkey-ci-agent \
  --field repo=valkey-io/valkey \
  --field pr=<pr-number> \
  --field run_url=https://github.com/valkey-io/valkey/actions/runs/<run_id>
```

The workflow is scoped to `valkey-io/valkey`, matching the GitHub App token it
mints. Maintainers can dispatch it manually, or comment on a `valkey-io/valkey`
PR and let `ci-fix-comment-poll.yml` dispatch it. The invocation must start the
comment, and the hint is only the rest of that line, so a conversational comment
that merely quotes or mentions the command does not trigger a run. The intended
comment shape is:

```text
@valkeyrie-bot fix https://github.com/valkey-io/valkey/actions/runs/<run_id>
```

Add a free-text hint via the dispatch `hint` input to steer the diagnosis
(e.g. `look at the valgrind timeout`). The bot fixes one test per invocation;
re-run it to address the next failing test in the same run.

### How it works

The division of labor is the whole design: **AI judges, code executes and
owns every verdict.** The AI never runs a command and never pushes.

1. **Gate** (code, fail-closed) - parses the command, verifies the commenter
   is an active member of `valkey-io/contributors`, and binds the failed run
   to the PR head (`head_repo` + `head_branch` + `head_sha`). If the branch
   moved since the run, it refuses - the log no longer describes the code.
2. **Fetch** (code) - downloads the failed run's logs and shallow-clones the
   repo at the exact failed commit.
3. **Diagnose** (AI, read-only) - reads the log and the repo, including the
   project's *own* CI workflow files to learn how it builds and tests, then
   returns a structured proposal: port an existing upstream fix, author a
   test-scaffolding fix, or refuse. Nothing about the test framework is
   hardcoded, so the same engine works for any repo.
4. **Select the verifier** (code) - code, not the AI, decides where the fix is
   verified. It lists the jobs that actually failed in the linked run, requires
   the AI's job hint to match one of them, and classifies that job's runner from
   its workflow definition: an x86 Linux job verifies locally, a container job
   verifies inside that image via Docker, a macOS job verifies on a macOS
   runner. Anything it cannot classify safely (arm, self-hosted, dynamic) is
   refused.
5. **Verify + review** - the verification policy depends on the fix path:
   - PORT: when the fix is an existing default-branch commit that cherry-picks
     cleanly, the bot may push the port and rely on this PR's normal CI as the
     authority. This exception is limited to already-merged upstream fixes.
   - Linux/Docker: first run the AI's targeted build+verify recipe on the clean
     checkout. If it passes before any fix, the bot treats the linked failure
     as flaky or environment-specific and refuses. If the local environment
     cannot establish a baseline because a setup dependency is missing, any
     authored patch is handoff-only. Otherwise, apply the fix and run it in a
     **sanitized subprocess** (scrubbed environment, locked working directory,
     timeout, output cap; Docker adds no-network, dropped capabilities,
     non-root), where the real exit code is the verdict. The build runs once
     and the verify command must pass `CI_FIX_VERIFY_RUNS` times in a row
     (default 2). This path retries on failure.
   - macOS: send the approved patch to a macOS runner the agent controls, which
     checks out the PR head, applies the patch, and runs the command; its CI
     conclusion is the verdict.
   A skeptic review (read-only AI) judges whether the fix addresses the root
   cause rather than silencing the symptom. A push requires both a passing
   verification and an approving review.
6. **Push** (code) - extracts only the approved patch, applies it in a fresh
   trusted clone at the gated SHA, commits authored as the bot (no DCO
   sign-off - a human must certify before merge), and pushes to the PR's own
   `agent/backport/...` branch. The checkout that ran tests never receives
   credentials. The PR's normal CI re-runs as the authoritative check. The bot
   never merges.

This is targeted verification of the one failing check, not a replay of the
whole CI job. Every refusal posts a PR comment explaining why, with evidence,
so when the bot can't safely fix something (a real product bug, a flaky test,
an unverifiable environment), a maintainer can take over immediately.

### Configuration

Reuses the same secrets and OIDC role as the other workflows (see
[Step 1](#step-1-configure-secrets-and-variables)). The workflow mints two
short-lived App tokens:

- On `valkey-io/valkey`: `members:read` (team authorization), `actions:read`
  (run logs and failed-job listing), `contents:write` (push the fix),
  `issues:write` (PR comments), `pull-requests:write` (PR metadata).
- On `valkey-io/valkey-ci-agent`: `actions:write` (dispatch and read the
  macOS verification workflow). Used only for the macOS backend.

`ci-fix-comment-poll.yml` runs hourly and polls twice inside the same runner,
30 minutes apart. The in-run loop is capped below the GitHub App token lifetime,
so the second tick does not depend on GitHub scheduling another workflow exactly
on time. Optional poller tuning lives in `CI_FIX_POLL_INTERVAL_SECONDS` and
`CI_FIX_POLL_DURATION_SECONDS`.

Optional verification tuning: `CI_FIX_VERIFY_RUNS` sets how many times a
Linux/Docker fix must pass the verify command before it is trusted (default 2,
maximum 10). The build runs once regardless, so raising it only repeats the
verify step. macOS verification runs once on its dedicated runner.

## Safety

- **Branch namespace** - the agent writes only `agent/backport/...` branches and opens PRs for maintainer review.
- **Credential isolation** - all GitHub auth uses `GIT_ASKPASS`; tokens never appear in `.git/config` or URLs
- **Claude Code env isolation** - `GITHUB_TOKEN`, `GH_TOKEN`, and `*_SECRET` are stripped from the subprocess environment. Claude cannot see credentials.
- **Deterministic validation** - registry-configured build commands run before push. A validation failure blocks the push.
- **Fork sync** - when a different-owner `push_repo` is configured, the agent fast-forwards that fork's release branch to match upstream before cherry-picking
- **Stale branch pruning** - if a previous backport PR was closed without merging, the agent deletes the orphaned branch before starting fresh
- **DCO** - backport commits are signed off. ci_fix commits are authored by the bot without a sign-off, so a human certifies the change before merge.

## Documentation

- [docs/architecture.md](docs/architecture.md) - full system design including planned workflows
- [CONTRIBUTING.md](CONTRIBUTING.md) - development setup and code structure
