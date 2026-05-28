# Architecture

The Valkey CI Agent automates backport cherry-picks across Valkey repositories
defined in the central `repos.yml` registry.

## Layers

```text
scripts/
  backport/    Backport workflow (active)
  ai/          Claude Code subprocess orchestration
  common/      Shared infrastructure
repos.yml      Registry of repos, release branches, and project boards
```

## Backport Flow

```text
sweep.py (daily cron or manual dispatch)
  -> reads repos.yml and fans out one job per {repo, branch}
  -> discovers PRs from each branch's GitHub Project board
  -> for each registered release branch:
      cherry_pick.py -> git cherry-pick
      conflict_resolver.py -> Claude Code resolves conflicts
      pr_creator.py -> opens/updates PR on the upstream repo
```

### Entry Points

- `scripts/backport/sweep.py` — daily sweep across registered repos and release branches
- `scripts/backport/main.py` — single-PR backport (manual dispatch)
- `scripts/backport/matrix.py` — GitHub Actions matrix generation from `repos.yml`
- `scripts/backport/registry.py` — typed registry loader and validation

### AI Layer

The only AI usage is conflict resolution:

```text
conflict_resolver.py
  → runtime.run_agent("conflict_resolve_edit_only", prompt, cwd=repo)
    → claude_code.run_claude_code(prompt, ...)
      → subprocess: claude --print (Claude Code CLI via Bedrock)
```

Claude gets the repo checkout with conflict markers, reads both sides, and edits
only the conflicted files in place. The prompt is parameterized by the repo
language from `repos.yml`.

Validation first runs the registry's optional `validation_setup_commands`,
then runs `build_commands` before push. Repos can opt into
`validate_each_candidate` to validate after each cherry-pick and isolate the
candidate that broke the branch. When validation fails but the branch has
reviewable changes, the sweep opens or updates a draft PR with failure details
instead of discarding the work. Repos with no `build_commands` configured rely
on upstream CI for verification.

### Common Infrastructure

- `git_auth.py` — GIT_ASKPASS credential helper
- `github_client.py` — retry wrapper for GitHub API

## Repository Model

The standard model is direct upstream branches: the agent pushes
`agent/backport/...` branches to `repo` and opens PRs in that same
repository. This keeps the registry small and matches the GitHub App
permissions used by the workflows.

`push_repo` is optional and exists only as a different-owner fork escape hatch.
Same-owner `push_repo` values are rejected so staging repositories do not become
the normal deployment model.

## Planned Workflows

Future sibling modules to `backport/`:

- **PR Reviewer** — two-stage code review with skeptic pass
- **Fuzzer Monitor** — triage fuzzer failures, file issues
- **Daily CI Analysis** — detect flaky tests, generate fix PRs
