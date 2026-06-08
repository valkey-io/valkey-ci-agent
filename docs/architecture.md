# Architecture

The Valkey CI Agent runs scheduled workflows that act on Valkey repositories
defined in the central `repos.yml` registry. Two workflows are active today:
backports and fuzzer monitoring.

## Layers

```text
scripts/
  backport/    Backport workflow
  fuzzer/      Fuzzer monitor workflow
  test_failure_detector/ Test Failure Detector workflow
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

Validation first runs the registry's optional `validation_setup_commands`,
then validates the branch after each cherry-pick. The sweep branch is kept
green: a cherry-pick is only kept if the whole branch still validates, and a
failure is reset off the branch so it can never block later candidates. The
run keeps up to two validated cherry-picks (`--max-candidates 2`) and records
skipped or failed candidates in the PR's "Needs attention" section without
committing them. When `repair_validation_failures` is enabled, Claude Code
gets one edit-only repair attempt scoped to the backport diff before a failing
cherry-pick is dropped. Repos with no `build_commands` configured rely on
upstream CI for verification.

### Entry Points

- `scripts/backport/sweep.py` — daily sweep across registered repos and release branches
- `scripts/backport/main.py` — single-PR backport (manual dispatch)
- `scripts/backport/matrix.py` — GitHub Actions matrix generation from `repos.yml`
- `scripts/backport/registry.py` — typed registry loader and validation
- `scripts/backport/sweep_*.py` — focused sweep support modules:
  typed sweep results, Git workspace operations, GitHub PR operations,
  GraphQL access, validation command execution, and Markdown reporting

## Fuzzer Flow

```text
fuzzer/main.py (cron every 4 hours)
  -> common.workflow_artifacts.ArtifactClient.list_recent_runs(...)
  -> FuzzerRunAnalyzer.analyze(run)
       common.workflow_artifacts -> download the artifact bundle
       analyzer._scan_logs() -> deterministic regex pass
       analyzer._invoke_claude() -> drop artifacts in a tempdir,
                                    common.git_clone -> shallow-clone
                                    valkey + valkey-fuzzer at the tested
                                    SHAs, run Claude under
                                    fuzzer_analysis_readonly profile,
                                    parse JSON verdict
       common.incidents.compute_fingerprint() -> stable hash over the
                                    normalized anomaly shapes
  -> common.issue_dedup.IssueDedupPublisher.upsert(...)
       fuzzer.issue_renderer.render_for(analysis) -> title/body/comment
```

Claude is given `Read,Grep,Glob` only — no edits, no shell, no network. The
clones give Claude line-level access so it can grep for assertion text or
crash handlers in `valkey/src/` to distinguish known-benign asserts from new
crashes. If a clone fails, the prompt tells Claude not to cite source line
numbers and the analyzer falls back to artifact-only analysis. If Claude
itself fails, the analyzer falls back to deterministic findings and labels
the verdict `needs-human-triage` rather than silently reporting "normal".
If the fuzzer run produced no artifact bundle the analyzer surfaces that
as an error rather than triaging from raw logs.

Unlike the backport flow, the fuzzer monitor never writes to `valkey-io/valkey`
or `valkey-io/valkey-fuzzer` source — its only side effect is creating or
updating issues on `valkey-fuzzer`.

### Entry Points

- `scripts/fuzzer/main.py` — CLI entry point (cron / manual dispatch)
- `scripts/fuzzer/analyzer.py` — orchestration, deterministic scan, Claude Code integration
- `scripts/fuzzer/issue_renderer.py` — fuzzer-specific title/body/comment rendering
- `scripts/fuzzer/models.py` — typed dataclasses for the analysis pipeline

## AI Layer

```text
runtime.run_agent(profile, prompt, cwd=...)
  -> claude_code.run_claude_code(...)
    -> subprocess: claude --print (Claude Code CLI via Bedrock)
```

Profiles registered today:

- `conflict_resolve_edit_only` — backport conflict resolution (Read/Edit/Bash, writes allowed)
- `fuzzer_analysis_readonly` — fuzzer triage (Read/Grep/Glob only, no writes)

## Common Infrastructure

Workflow-agnostic helpers in `scripts/common/`:

- `git_auth.py` — GIT_ASKPASS credential helper
- `github_client.py` — retry wrapper for GitHub API
- `text_utils.py` — ANSI stripping for log scanning
- `workflow_artifacts.py` — list and download GitHub Actions workflow runs
  and their uploaded artifact bundles. Used by the fuzzer flow today; any
  future workflow that needs to analyze recent runs of a target workflow
  reuses it directly.
- `git_clone.py` — `shallow_clone_at_sha(repo, dest, sha)` — defensive
  shallow clone of a public repo at a specific commit, with input
  validation against argument injection. Used by the fuzzer flow to give
  Claude source access at the tested SHA.
- `incidents.py` — `compute_fingerprint(namespace, shapes)` produces a stable
  hash over normalized anomaly shapes for issue deduplication.
- `issue_dedup.py` — `IssueDedupPublisher` creates or updates a GitHub
  issue keyed by a fingerprint marker. Workflows supply the rendered title,
  body, and comment via a small `render(marker, occurrences) -> IssueContent`
  callback; the publisher owns the dedup machinery.

## Repository Model

The standard model is direct upstream branches: the agent pushes
`agent/backport/...` branches to `repo` and opens PRs in that same
repository. This keeps the registry small and matches the GitHub App
permissions used by the workflows.

`push_repo` is optional and exists only as a different-owner fork escape hatch.
Same-owner `push_repo` values are rejected so staging repositories do not become
the normal deployment model.

## Test Failure Detector Flow

```text
main.py (daily cron or manual dispatch)
  -> get_latest_daily_run() or use provided run_id
  -> download_all_test_failures() from the run's artifacts
  -> get_job_urls() for CI links
  -> parse_and_deduplicate() groups by {test_name, test_file}
  -> process_failures() creates/updates GitHub issues
```

### Entry Points

- `scripts/test_failure_detector/main.py` — CLI entry point and pipeline orchestration
- `scripts/test_failure_detector/download.py` — workflow run discovery and artifact download
- `scripts/test_failure_detector/parse_failures.py` — JSON parsing and deduplication
- `scripts/test_failure_detector/manage_issues.py` — thin orchestration over the shared dedup publisher to create/update issues
- `scripts/test_failure_detector/issue_renderer.py` — test-failure-specific title/body/comment rendering and label assignment

## Planned Workflows

Future sibling modules to `backport/` and `fuzzer/`:

- **PR Reviewer** — two-stage code review with skeptic pass
- **Additional Daily CI Analysis** — detect flaky tests, generate fix PRs
