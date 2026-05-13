# valkey-ci-agent

An AI-powered CI automation agent for the Valkey project. Uses Claude Code (Anthropic Claude Opus via Bedrock) to perform tasks that require code understanding — conflict resolution, code review, failure analysis, and more.

## Architecture

The agent is structured as a layered framework:

```
scripts/
  ai/          AI layer: Claude Code subprocess orchestration
  backport/    Workflow 1: automated backports (active)
  common/      Shared infrastructure (git auth, GitHub client, safety guards)
repos.yml      Central registry of repos, branches, project boards, build commands
```

New workflows are added as sibling directories to `backport/`. Each workflow picks an agent profile (tools, timeout, effort) and writes its own prompt. The AI layer and shared infra stay unchanged.

**Workflows:**

| Workflow | Status | Description |
|----------|--------|-------------|
| Backport | Active | Cherry-picks merged PRs onto release branches with AI conflict resolution |
| PR Reviewer | Planned | Two-stage code review with skeptic pass |
| Fuzzer Monitor | Planned | Analyzes fuzzer runs, triages failures, files issues |
| Daily CI Analysis | Planned | Detects flaky tests, generates fix PRs |
| Health Dashboard | Planned | Publishes CI health metrics to GitHub Pages |

## Backport Workflow

The currently active workflow. Cherry-picks merged PRs onto release branches with AI-powered conflict resolution. Works for any repo defined in `repos.yml` — Valkey core, Valkey modules (bloom, search, json), or anything else following the per-branch project-board pattern.

### How it works

1. **Daily sweep** — every day at 09:00 UTC, the preflight job reads `repos.yml` and generates one matrix leg per `{repo, branch}` pair
2. **Project discovery** — each leg queries the GitHub Project v2 board for PRs marked "To be backported"
3. **Cherry-pick** — attempts `git cherry-pick` for each candidate onto the target release branch
4. **AI conflict resolution** — when cherry-pick conflicts, Claude Code reads both sides, resolves the conflict, and (if configured) runs the repo's build commands to verify compilation
5. **Build validation** — registry-configured build commands run deterministically after resolution; failure blocks the push
6. **PR creation** — pushes the branch and opens (or updates) a draft PR with a summary table

Manual single-PR backports are also supported via `workflow_dispatch`.

### Registry (`repos.yml`)

The registry is the single source of truth. To onboard a new repo, add an entry to `repos.yml`:

```yaml
publish_guard:
  protected_repos:
    - valkey-io/valkey

repos:
  - repo: valkey-io/valkey
    project_owner: valkey-io
    project_owner_type: organization
    language: c                          # used in conflict resolver prompt
    build_commands:
      - "make -j$(nproc)"                # run after conflict resolution; empty = skip
    backport_label: backport
    llm_conflict_label: ai-resolved-conflicts
    max_conflicting_files: 100
    branches:
      - branch: "8.1"
        project_number: 14
      - branch: "9.0"
        project_number: 18
```

Optional `push_repo` field: if set to a different repo (e.g., a fork), branches are pushed there and PRs open cross-repo. If omitted, branches are pushed directly to the upstream repo (same model as OpenSearch's backport bot).

See [`examples/repos.yml`](examples/repos.yml) for a multi-module example.

### Installation

#### Prerequisites

- A GitHub App with:
  - `contents:write` on each repo in the registry (for pushing branches)
  - `pull-requests:write` on each repo (for opening PRs)
  - `organization_projects:read` on the org (for querying project boards)
- An AWS account with Bedrock access to `us.anthropic.claude-opus-4-7`
- An OIDC trust between GitHub Actions and your AWS account

#### Step 1: Configure secrets and variables

On `valkey-io/valkey-ci-agent`:

| Type | Name | Value |
|------|------|-------|
| Secret | `AWS_ROLE_ARN` | OIDC role ARN with Bedrock `InvokeModel` permission |
| Secret | `VALKEY_GITHUB_TOKEN` | App installation token or PAT |
| Variable | `AWS_REGION` | e.g., `us-east-1` |
| Variable | `CI_BOT_COMMIT_NAME` | e.g., `valkey-ci-agent` |
| Variable | `CI_BOT_COMMIT_EMAIL` | e.g., `ci-agent@valkey.io` |
| Variable | `CI_BOT_REQUIRE_DCO_SIGNOFF` | `true` |
| Variable | `VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH` | `1` (to allow writes to protected repos) |

#### Step 2: Edit `repos.yml`

Add your repo(s) to the registry. No per-repo config files are needed — everything lives in `repos.yml`.

#### Step 3: Enable the workflows

The scheduled sweep runs automatically. For event-driven single-PR backports, copy [`examples/backport-caller-workflow.yml`](examples/backport-caller-workflow.yml) into the consumer repo.

### Usage

#### Daily sweep (automatic)

Runs daily at 09:00 UTC via cron. The preflight job reads `repos.yml` and fans out one job per `{repo, branch}`. Each produces one PR bundling all pending backports for that branch.

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

## Safety

- **Publish guard** — blocks writes to protected repos (configured in `repos.yml`) unless `VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH=1` is set. Fails closed if not configured at startup.
- **Credential isolation** — all GitHub auth uses `GIT_ASKPASS`; tokens never appear in `.git/config` or URLs
- **Claude Code env isolation** — `GITHUB_TOKEN`, `GH_TOKEN`, and `*_SECRET` are stripped from the subprocess environment. Claude cannot see credentials.
- **Deterministic build validation** — registry-configured build commands run after conflict resolution. A build failure blocks the push.
- **Fork sync** — when using a fork push target, the agent fast-forwards the fork's release branch to match upstream before cherry-picking
- **Stale branch pruning** — if a previous backport PR was closed without merging, the agent deletes the orphaned branch before starting fresh
- **DCO** — all agent commits are signed off

## Documentation

- [docs/architecture.md](docs/architecture.md) — full system design including planned workflows
- [CONTRIBUTING.md](CONTRIBUTING.md) — development setup and code structure
