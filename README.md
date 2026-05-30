# valkey-ci-agent

An AI-powered CI automation agent for the Valkey project. Uses Claude Code (Anthropic Claude Opus via Bedrock) to perform tasks that require code understanding — conflict resolution, code review, failure analysis, and more.

## Architecture

The agent is structured as a layered framework:

```text
scripts/
  ai/          AI layer: Claude Code subprocess orchestration
  backport/    Workflow 1: automated backports (active)
  common/      Shared infrastructure (git auth, GitHub client, safety guards)
repos.yml      Central registry of repos, branches, and project boards
```

New workflows are added as sibling directories to `backport/`. Each workflow picks an agent profile (tools, timeout, effort) and writes its own prompt. The AI layer and shared infra stay unchanged.

**Workflows:**

| Workflow | Status | Description |
|----------|--------|-------------|
| Backport | Active | Cherry-picks merged PRs onto release branches with AI conflict resolution |
| Fuzzer Monitor | Active | Analyzes scheduled fuzzer runs and files issues for anomalous failures |
| PR Reviewer | Planned | Two-stage code review with skeptic pass |
| Daily CI Analysis | Planned | Detects flaky tests, generates fix PRs |

## Backport Workflow

The currently active workflow. Cherry-picks merged PRs onto release branches with AI-powered conflict resolution. Works for any repo defined in `repos.yml` — Valkey core, Valkey modules (bloom, search, json), or anything else following the per-branch project-board pattern.

### How it works

1. **Daily sweep** — every day at 09:00 UTC, the preflight job reads `repos.yml` and generates one matrix leg per `{repo, branch}` pair
2. **Project discovery** — each leg queries the GitHub Project v2 board for PRs marked "To be backported"
3. **Cherry-pick** — attempts `git cherry-pick` for each candidate onto the target release branch
4. **AI conflict resolution** — when cherry-pick conflicts, Claude Code reads both sides and resolves the conflict in place
5. **Validation** — registry-configured build commands run before push; any failure blocks the push
6. **PR creation** — pushes the branch and opens (or updates) a PR with a summary table

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

The sweep branch is always kept green: a candidate is only kept if the whole branch still validates after the cherry-pick, so one bad commit can never block later candidates. Each scheduled run keeps a single validated cherry-pick (`--max-candidates 1`) and reports candidates that were skipped or failed validation in the PR's "Needs attention" section without committing them. When `repair_validation_failures` is enabled, Claude Code gets one narrow edit-only attempt to fix a failing cherry-pick before it is dropped.

See [`examples/repos.yml`](examples/repos.yml) for a multi-module example.

### Installation

#### Prerequisites

- A GitHub App with:
  - `contents:write` on each repo in the registry (for pushing branches)
  - `pull-requests:write` on each repo (for opening PRs)
  - `issues:write` on each repo (for backport status comments)
  - `organization_projects:read` on the org (for querying project boards)
- An AWS account with Bedrock access to `us.anthropic.claude-opus-4-7`
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

Add your repo(s) to the registry. No per-repo config files are needed — everything lives in `repos.yml`.

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

1. **Cron** — every 4 hours, the monitor checks the latest scheduled fuzzer run
2. **Deterministic scan** — pattern-matches crash/sanitizer/failover/RDB signals against artifact JSON and node logs; ignores chaos-expected noise (CLUSTERDOWN, replication link loss)
3. **Claude Code analysis** — drops the artifacts in a tempdir, shallow-clones `valkey-io/valkey` at the tested commit and `valkey-io/valkey-fuzzer` at the run's HEAD, then asks Claude (with read-only `Read,Grep,Glob` tools) to correlate the failure with source and decide whether the run reflects a real bug or chaos-expected noise. If a clone fails the prompt tells Claude not to cite source line numbers.
4. **Issue upsert** — anomalous runs file (or update) an issue on `valkey-io/valkey-fuzzer`, deduplicated by a stable fingerprint over root cause and anomaly shape
5. **Audit** — per-run JSON results and Claude evidence are uploaded as workflow artifacts

The Claude Code subprocess runs under the `fuzzer_analysis_readonly` agent profile with `Read,Grep,Glob` tools only — no editing, no Bash, no network access beyond the Bedrock call itself.

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

## Safety

- **Branch namespace** — the agent writes only `agent/backport/...` branches and opens PRs for maintainer review.
- **Credential isolation** — all GitHub auth uses `GIT_ASKPASS`; tokens never appear in `.git/config` or URLs
- **Claude Code env isolation** — `GITHUB_TOKEN`, `GH_TOKEN`, and `*_SECRET` are stripped from the subprocess environment. Claude cannot see credentials.
- **Deterministic validation** — registry-configured build commands run before push. A validation failure blocks the push.
- **Fork sync** — when a different-owner `push_repo` is configured, the agent fast-forwards that fork's release branch to match upstream before cherry-picking
- **Stale branch pruning** — if a previous backport PR was closed without merging, the agent deletes the orphaned branch before starting fresh
- **DCO** — all agent commits are signed off

## Documentation

- [docs/architecture.md](docs/architecture.md) — full system design including planned workflows
- [CONTRIBUTING.md](CONTRIBUTING.md) — development setup and code structure
