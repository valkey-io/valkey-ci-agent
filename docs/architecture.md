# Architecture

The Valkey CI Agent runs workflows that act on Valkey repositories defined in
the central `repos.yml` registry. Five workflows are active today: backports,
fuzzer monitoring, and the test-failure detector (scheduled), and the CI
test-fix bot and release-notes cut (on-demand).

## Layers

```text
scripts/
  backport/    Backport workflow
  fuzzer/      Fuzzer monitor workflow
  test_failure_detector/ Test Failure Detector workflow
  ci_fix/      CI test-fix bot
  release_notes/ Release-notes cutter: AI notes + version bump
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

### Poll

The daily sweep tops a rolling backport PR up to `--max-candidates` validated
cherry-picks and then waits for the next cron tick, so a merged sweep PR is not
topped back up until the following day. The poll workflow (`backport-poll.yml`)
closes that gap by starting hourly and polling immediately, then once more 30
minutes later inside the same runner. For each registered `{repo, branch}` it
runs the same sweep, but only when no sweep PR is currently open for that
branch:

```text
poller.py (short cron or manual dispatch)
  -> reads repos.yml and fans out one job per {repo, branch}
  -> find_existing_pr(...) -> open sweep PR for this branch?
       yes -> skip; a human is reviewing it
       no  -> run_backport_sweep(...) opens a fresh PR
```

The open-PR check is the entire state model: a merge closes the sweep PR, the
next poll finds the gap and tops the board back up, and the new PR locks the
branch again until it too merges. The poll job shares the
`backport-sweep-{repo}-{branch}` concurrency group with the daily sweep so the
two never race for the same branch. Manual dispatches are one-shot; only
scheduled runs use the sustained in-run cadence.

### Entry Points

- `scripts/backport/sweep.py` - daily sweep across registered repos and release branches
- `scripts/backport/poller.py` - short-cron poll that sweeps a branch only when no sweep PR is open
- `scripts/backport/main.py` - single-PR backport (manual dispatch)
- `scripts/backport/matrix.py` - GitHub Actions matrix generation from `repos.yml`
- `scripts/backport/registry.py` - typed registry loader and validation
- `scripts/backport/sweep_*.py` - focused sweep support modules:
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

Claude is given `Read,Grep,Glob` only - no edits, no shell, no network. The
clones give Claude line-level access so it can grep for assertion text or
crash handlers in `valkey/src/` to distinguish known-benign asserts from new
crashes. If a clone fails, the prompt tells Claude not to cite source line
numbers and the analyzer falls back to artifact-only analysis. If Claude
itself fails, the analyzer falls back to deterministic findings and labels
the verdict `needs-human-triage` rather than silently reporting "normal".
If the fuzzer run produced no artifact bundle the analyzer surfaces that
as an error rather than triaging from raw logs.

Unlike the backport flow, the fuzzer monitor never writes to `valkey-io/valkey`
or `valkey-io/valkey-fuzzer` source - its only side effect is creating or
updating issues on `valkey-fuzzer`.

### Entry Points

- `scripts/fuzzer/main.py` - CLI entry point (cron / manual dispatch)
- `scripts/fuzzer/analyzer.py` - orchestration, deterministic scan, Claude Code integration
- `scripts/fuzzer/issue_renderer.py` - fuzzer-specific title/body/comment rendering
- `scripts/fuzzer/models.py` - typed dataclasses for the analysis pipeline

## CI Fix Flow

On-demand, triggered by a maintainer commenting `@valkeyrie-bot fix <ci-link>`
on a backport PR. Decoupled from the backport sweep; it shares only the
common infrastructure.

```text
ci_fix/main.py (workflow_dispatch event)
  -> gate.build_fix_request(...)        fail-closed auth (contributors team)
                                        + SHA-bound run gating
  -> pipeline.run_ci_fix(...)
       verify.github_runs        -> list the jobs that actually failed (code,
                                    not the AI, owns this)
       common.workflow_artifacts -> download the failed run's logs
       common.git_clone          -> shallow-clone the repo at the failed SHA
       port_discovery            -> code-discovers default-branch candidates
                                    for likely missing backports
       diagnose.diagnose_failure -> read-only AI returns a FixProposal
                                    (port | author | refuse) + a failing-job hint,
                                    using the discovered port candidates so
                                    missing backports are preferred over refusals
       pipeline._plan_verification
                                 -> code matches the hint to a real failed job
                                    and classifies its workflow environment
                                    (verify.workflow_env) into a VerificationPlan:
                                    local | docker(image) | macos | refuse
       port:
         apply.apply_port_commit -> cherry-pick the upstream fix commit without
                                    committing; push and rely on this PR's
                                    normal CI as the verification authority
       local/docker:
         review.run_fix_loop     -> reproduce the failure on a clean checkout
                                    -> apply (edit-only AI)
                                    -> runner.run_verification_command (code runs
                                       the AI command in a sanitized subprocess,
                                       inside the job's container for docker;
                                       exit code is the verdict; build once,
                                       verify K times)
                                    -> build_and_review_patch (skeptic AI)
                                    retry on feedback; needs pass AND approve
       macos:
         apply + build_and_review_patch, then
         verify.macos.MacosVerifier -> dispatch the agent's verify-macos job,
                                       wait, conclusion is the verdict
       push.commit_and_push_fix  -> extract approved patch
                                    -> apply in a fresh trusted clone
                                    -> commit (no sign-off), push to the PR's own
                                       agent/backport/... branch (never merge)
  -> comment.render_comment(outcome) -> posted on the PR
```

The defining invariant is the AI/code split plus a hard checkout boundary: the
AI proposes (which check failed, how to fix, a targeted command, a job hint,
whether the fix is sound) and code disposes (selects the verifier environment
from the real failed job, runs the command, owns pass/fail, performs the push).
The AI never selects where verification runs, never executes a command, and
never touches the remote. The checkout that runs untrusted test code never
receives push credentials; publishing applies the approved patch in a fresh
clone at the gated SHA. This is targeted verification of the one failing check,
not a replay of the whole CI job.

Nothing about the test framework is hardcoded. The diagnosis reads the target
repo's own CI workflow files to learn how it builds and runs tests, and the
port-discovery and verification logic resolve the default branch from the
clone (so a repo whose default is `main`, like Valkey Search, works the same as
Valkey core's `unstable`). The engine is repo-agnostic in principle.

The deployment is not yet fully registry-driven. `ci-fix.yml` and
`ci-fix-comment-poll.yml` are operationally scoped to `valkey-io/valkey`: the
workflow guards on the repo, mints a token for it, and the poller watches only
it. The comment poller runs hourly but uses a shared in-run polling loop to scan
immediately and again 30 minutes later, avoiding dependence on GitHub's
scheduled-workflow queue for every half-hour tick. The loop is capped below the
GitHub App token lifetime. Onboarding another repo (e.g. Valkey Search) still
needs a registry-driven token/poll/dispatch path in the workflows; the Python
engine being repo-agnostic is a precondition, not the whole job.

Every failure mode - un-runnable variant, a real product bug, a flaky test, a
moved branch, a non-member commenter - returns a `FixOutcome` that becomes an
explanatory PR comment rather than a silent failure or an unsafe push.

For local and Docker verification, a push requires a failing baseline first. If
the command passes before the fix, the agent treats the CI failure as flaky or
environment-specific and refuses. If the baseline cannot be established because
the local verifier is missing setup dependencies, the agent may still author and
skeptically review a patch, but it is returned as a handoff rather than pushed.

### Entry Points

- `scripts/ci_fix/main.py` - workflow_dispatch entry point; mints the target and agent-repo tokens
- `scripts/ci_fix/gate.py` - command parsing, fail-closed team auth, SHA-bound run gating
- `scripts/ci_fix/diagnose.py` - read-only AI diagnosis into a structured proposal (fix + job hint)
- `scripts/ci_fix/apply.py` - edit-only AI fix application
- `scripts/ci_fix/runner.py` - sanitized local/Docker command execution that owns the verdict
- `scripts/ci_fix/review.py` - skeptic review, the apply/run/review loop, and the shared patch helpers
- `scripts/ci_fix/push.py` - patch handoff, commit (no sign-off), namespace-restricted push
- `scripts/ci_fix/comment.py` - render the outcome into a PR comment
- `scripts/ci_fix/pipeline.py` - top-level orchestration; code-owned verifier selection
- `scripts/ci_fix/models.py` - typed dataclasses for the pipeline
- `scripts/ci_fix/verify/` - the verifier layer:
  - `base.py` - VerifyEnv, FailedJob, VerificationPlan, VerificationResult, the VerifyBackend protocol
  - `workflow_env.py` - classify a failed job's runner (x86 Linux / Docker / macOS / unsupported)
  - `github_runs.py` - list the jobs that actually failed in a run (code-owned)
  - `macos.py` - the macOS verifier: dispatch the verify-macos job and wait
- `.github/workflows/ci-fix-verify-macos.yml` - the macOS verification job

## AI Layer

```text
runtime.run_agent(profile, prompt, cwd=...)
  -> claude_code.run_claude_code(...)
    -> subprocess: claude --print (Claude Code CLI via Bedrock)
```

Profiles registered today:

- `conflict_resolve_edit_only` - backport conflict resolution (Read/Edit/Bash, writes allowed)
- `fuzzer_analysis_readonly` - fuzzer triage (Read/Grep/Glob only, no writes)
- `ci_fix_diagnose_readonly` - CI-fix diagnosis and skeptic review (Read/Grep/Glob only, no writes)

## Common Infrastructure

Workflow-agnostic helpers in `scripts/common/`:

- `git_auth.py` - GIT_ASKPASS credential helper
- `github_client.py` - retry wrapper for GitHub API
- `text_utils.py` - ANSI stripping for log scanning
- `workflow_artifacts.py` - list and download GitHub Actions workflow runs
  and their uploaded artifact bundles, plus `download_run_logs(...)` for a
  run's raw console logs. Used by the fuzzer flow (artifacts) and the CI-fix
  flow (run logs).
- `git_clone.py` - `shallow_clone_at_sha(repo, dest, sha)` - defensive
  shallow clone of a public repo at a specific commit, with input
  validation against argument injection. Gives the AI source access at the
  tested SHA in both the fuzzer and CI-fix flows.
- `polling.py` - shared one-shot-or-sustained poll loop helpers for scheduled
  pollers that need a predictable in-run cadence.
- `proc.py` - `git_output(...)` (run a git command and return stdout) and
  `filter_env(allowlist)` (the single place that turns an env allowlist into
  a concrete, scrubbed subprocess environment).
- `ai_output.py` - `extract_json_object(stdout, required_key=...)` parses a
  structured verdict out of Claude Code's stream-json output. Shared by the
  fuzzer and CI-fix flows.
- `incidents.py` - `compute_fingerprint(namespace, shapes)` produces a stable
  hash over normalized anomaly shapes for issue deduplication.
- `issue_dedup.py` - `IssueDedupPublisher` creates or updates a GitHub
  issue keyed by a fingerprint marker. Workflows supply the rendered title,
  body, and comment via a small `render(marker, occurrences) -> IssueContent`
  callback; the publisher owns the dedup machinery. When a publisher opts in
  via `closed_lookback` (off by default), it checks for a recently closed
  issue with the same marker (or exact legacy title) before creating a new
  one, avoiding duplicates for failures that were already fixed.

Workflow setup is centralized in `.github/actions/setup-agent`. Jobs still
check out the agent repository explicitly, then use that local composite action
to install the pinned Python toolchain, project dependencies, and optionally
the pinned Claude Code CLI. The workflow standards tests scan both workflows
and local action metadata so external actions and Claude installs do not drift.

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

- `scripts/test_failure_detector/main.py` - CLI entry point and pipeline orchestration
- `scripts/test_failure_detector/download.py` - workflow run discovery and artifact download
- `scripts/test_failure_detector/parse_failures.py` - JSON parsing and deduplication
- `scripts/test_failure_detector/manage_issues.py` - orchestration over the shared dedup publisher to create/update issues
- `scripts/test_failure_detector/issue_renderer.py` - test-failure-specific title/body/comment rendering and label assignment

## Release Notes Flow

```text
main.py (manual dispatch: version, optional stage, urgency, dry_run)
  -> validate + canonicalize inputs; infer patch GA only when PATCH > 0
  -> dry_run selects preview (true) or PR-opening execution (false)
  -> clone valkey (full depth + tags), validate --base-ref
  -> release_cut.cut()
       -> collect_advisory_fixes()   (if --security-from-advisories)
       -> resolve_branch_plan()      verify M.m branch exists, derive target
       -> pipeline.regenerate_unreleased()
            -> discover()  PRs over base..HEAD, deduped by PR number
            -> classify()  include, AI candidate, or no-release-notes hard exclusion
            -> PRDiffCollector  bounded, attributable commit diffs cached once
            -> triage()    completeness-first AI decision + deterministic
                            release-impact override for risky exclusions/no verdicts
            -> generate()  AI: one categorized bullet per included PR
            -> normalize operator-output categories and canonical bullet format
            -> dedup bullets by PR number (surfaces duplicate_prs)
            -> group_bullets()  {category: [canonical bullet line, ...]}
       -> _drop_already_credited()   dedup against PRs the line already ships
       -> promote_and_bump()         dated section + version.h bump + contributors
       -> _commit_push_release_pr()  prep branch (force-with-lease) + PR into the line
```

The branch model is tag-driven: all stages (rc1, rc2, ..., ga) target the existing
M.m branch (e.g. `9.1`). Maintainers create the branch and push tags before
dispatching. Tags determine the discovery range (rc1 uses the previous release tag,
rc2+ finds the prior rc tag, ga finds the last rc/patch tag). The cut lands on an
`agent/release-cut/...` prep branch and opens a PR into M.m, so the line only
advances when a human merges. The normal dispatch has four inputs and defaults to
a dry run; patch versions may omit stage and infer `ga`, while `M.m.0` always
requires an explicit stage. The advanced dispatch is a thin wrapper around the
same reusable workflow.

The prep branch name is deterministic for a version and stage. Re-dispatching
while its PR is open regenerates the full tagged range through the latest M.m
tip, rebuilds the prep commit on that tip, pushes it with force-with-lease, and
updates the existing PR. Full regeneration is intentional: it includes new
merges and also re-evaluates changed labels and PR metadata without treating a
previous AI-generated branch as durable input.

Signals fall into two tiers. Malformed inputs, a missing target branch, an
already-released/backward target, or a target branch that advances during
generation are hard errors that abort before any PR. Warnings (out-of-sequence
stages, unresolved PRs, empty notes, security mismatches, AI-triage decisions,
deterministic triage overrides, and `LOW`/`MODERATE` urgency paired with
release-impact signals) hold the PR as a draft with a banner naming them. Impact
detection is a review trigger, not an automatic security or severity
classification. Re-dispatch reconciles draft state automatically. `force_ready`,
available only on the advanced dispatch, bypasses holds. An omitted release date
is explicitly the current UTC date; the advanced dispatch accepts an explicit
calendar date.

### Entry Points

- `scripts/release_notes/main.py` - CLI entry point, input validation, clone
- `.github/workflows/release-notes-cut.yml` - simple dispatch and shared reusable release job
- `.github/workflows/release-notes-cut-advanced.yml` - advanced-input wrapper around the shared job
- `scripts/release_notes/release_cut.py` - branch-plan resolution, notes rendering, PR body + `_hold_reasons` (draft-hold decision)
- `scripts/release_notes/pipeline.py` - discover -> classify -> triage -> generate -> render orchestration
- `scripts/release_notes/discover.py` - range resolution and PR discovery by graph reachability
- `scripts/release_notes/backport_refs.py` - recover the original PR of a backported commit (verified sweep Applied table, standalone backport-of URL, subject, -x trailer, branch name)
- `scripts/release_notes/classify.py` - label split: release-notes -> include, no-release-notes -> exclude, else -> triage
- `scripts/release_notes/ai_inputs.py` - shared, bounded PR prompt payloads and SHA-cached diffs; combined sweep diffs are omitted when they cannot be attributed to one source PR
- `scripts/release_notes/triage.py` - completeness-first Claude include/exclude plus deterministic release-impact guardrail for PRs without `release-notes` (no tools; PR data inlined in prompt)
- `scripts/release_notes/generate.py` - Claude bullet generation (no tools; PR data inlined in prompt)
- `scripts/release_notes/models.py` - typed dataclasses for the pipeline
- `scripts/release_notes/security.py` - Security Fixes from published GitHub advisories (never AI-authored)
- `scripts/release_notes/render.py` - canonical `00-RELEASENOTES` rendering
- `scripts/release_notes/publish.py` - find/open/update the release PR; `_reconcile_draft` flips draft state on re-dispatch
- `scripts/release_notes/release_format.py` - `00-RELEASENOTES` dated-section rendering
- `scripts/release_notes/version_bump.py` - `src/version.h` macro rewriting
- `scripts/release_notes/contributors.py` - contributor discovery; reconciles display-name, login, co-author trailers, and PR-resolved logins

## Planned Workflows

Future sibling modules and extensions:

- **PR Reviewer** - two-stage code review with skeptic pass
- **Autonomous CI-fix poller** - the CI-fix engine, driven by a poller that
  detects red backport PRs (or test-failure issues) instead of a maintainer
  `@`-mention. Same pipeline, a different front door.
- **Additional Daily CI Analysis** - detect flaky tests, generate fix PRs
