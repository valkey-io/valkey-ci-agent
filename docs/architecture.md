# Architecture

The Valkey CI Agent automates backport cherry-picks across Valkey repositories
defined in the central `repos.yml` registry.

## Layers

```
scripts/
  backport/    Backport workflow (active)
  ai/          Claude Code subprocess orchestration
  common/      Shared infrastructure
repos.yml      Registry of repos, release branches, project boards, and validation
```

## Backport Flow

```
sweep.py (daily cron or manual dispatch)
  -> reads repos.yml and fans out one job per {repo, branch}
  -> discovers PRs from each branch's GitHub Project board
  -> for each registered release branch:
      cherry_pick.py -> git cherry-pick
      conflict_resolver.py -> Claude Code resolves conflicts
      pr_creator.py -> opens/updates draft PR on the upstream repo
```

### Entry Points

- `scripts/backport/sweep.py` — daily sweep across registered repos and release branches
- `scripts/backport/main.py` — single-PR backport (manual dispatch)
- `scripts/backport/matrix.py` — GitHub Actions matrix generation from `repos.yml`
- `scripts/backport/registry.py` — typed registry loader and validation

### AI Layer

The only AI usage is conflict resolution:

```
conflict_resolver.py
  → runtime.run_agent("conflict_resolve_edit_only", prompt, cwd=repo)
    → claude_code.run_claude_code(prompt, ...)
      → subprocess: claude --print (Claude Code CLI via Bedrock)
```

Claude gets the repo checkout with conflict markers, reads both sides, and edits
only the conflicted files in place. The prompt is parameterized by the repo
language and validation commands from `repos.yml`; validation is skipped when a
repo has no commands configured.

Validation is two-tiered. `build_commands` run for every generated branch, while
optional `validation_rules` append targeted commands when changed paths match a
rule. This lets Valkey core run focused Tcl cluster tests for cluster-related
changes, and lets modules add their own smoke tests without changing agent code.

### Common Infrastructure

- `git_auth.py` — GIT_ASKPASS credential helper
- `github_client.py` — retry wrapper for GitHub API

## Repository Model

The standard model is staging forks: the agent pushes `agent/backport/...`
branches to each repo's configured `push_repo` and opens PRs against the
upstream `repo`. This keeps generated branches out of upstream repositories
while preserving normal review and merge flow on the upstream release branches.

`push_repo` is required for every registry entry. Same-owner staging forks are
valid, but `push_repo` must not be identical to `repo`.

## Planned Workflows

Future sibling modules to `backport/`:

- **PR Reviewer** — two-stage code review with skeptic pass
- **Fuzzer Monitor** — triage fuzzer failures, file issues
- **Daily CI Analysis** — detect flaky tests, generate fix PRs
- **Health Dashboard** — publish CI metrics to GitHub Pages
