# valkey-ci-agent

An AI-powered CI automation agent for the Valkey project. Uses Claude Code (Anthropic Claude Opus via Bedrock) to perform tasks that require code understanding — conflict resolution, code review, failure analysis, and more.

## Architecture

The agent is structured as a layered framework:

```text
scripts/
  ai/          AI layer: Claude Code subprocess orchestration
  backport/    Workflow 1: automated backports (active)
  common/      Shared infrastructure (git auth, GitHub client, safety guards)
repos.yml      Central registry of repos, branches, project boards, validation
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
4. **AI conflict resolution** — when cherry-pick conflicts, Claude Code reads both sides, resolves the conflict, and receives the repo's matching validation commands as guidance
5. **Validation** — registry-configured build commands always run, and path-matched validation rules add targeted tests for touched files; any failure blocks the push
6. **PR creation** — pushes the branch and opens (or updates) a draft PR with a summary table

Manual single-PR backports are also supported via `workflow_dispatch`.

### Registry (`repos.yml`)

The registry is the single source of truth. To onboard a new repo, add an entry to `repos.yml`:

```yaml
repos:
  - repo: valkey-io/valkey
    project_owner: valkey-io
    project_owner_type: organization
    language: c                          # used in conflict resolver prompt
    build_commands:
      - "make -j$(nproc)"                # run before push; empty = skip
    validation_rules:
      - paths:
          - "tests/unit/cluster/cli.tcl"
        commands:
          - "./runtest --clients 1 --single unit/cluster/cli"
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

`validation_rules` are optional. Each rule matches changed paths with shell-style globs and appends the listed commands after `build_commands`. Use them for high-signal tests that catch branch-specific adaptation mistakes without running a full CI matrix locally.

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

## Safety

- **Branch namespace** — the agent writes only `agent/backport/...` branches and opens draft PRs for maintainer review.
- **Credential isolation** — all GitHub auth uses `GIT_ASKPASS`; tokens never appear in `.git/config` or URLs
- **Claude Code env isolation** — `GITHUB_TOKEN`, `GH_TOKEN`, and `*_SECRET` are stripped from the subprocess environment. Claude cannot see credentials.
- **Deterministic validation** — registry-configured build commands and matching path-based tests run before push. A validation failure blocks the push.
- **Fork sync** — when a different-owner `push_repo` is configured, the agent fast-forwards that fork's release branch to match upstream before cherry-picking
- **Stale branch pruning** — if a previous backport PR was closed without merging, the agent deletes the orphaned branch before starting fresh
- **DCO** — all agent commits are signed off

## Documentation

- [docs/architecture.md](docs/architecture.md) — full system design including planned workflows
- [CONTRIBUTING.md](CONTRIBUTING.md) — development setup and code structure
