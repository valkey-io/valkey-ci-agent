# valkey-ci-agent

An AI-powered CI automation agent for the Valkey project. Uses Claude Code (Anthropic Claude Fable 5 via Bedrock) to perform tasks that require code understanding - conflict resolution, code review, failure analysis, and more.

## Architecture

The agent is structured as a layered framework:

```text
scripts/
  ai/          AI layer: Claude Code subprocess orchestration
  backport/    Automated backports (active)
  fuzzer/      Fuzzer run monitoring (active)
  ci_fix/      On-demand CI test-fix bot (active)
  release_notes/  Release cutter: AI notes + version bump (active)
  cve_scan/    CVE scanning + verified rebuild dispatch (build-and-prove, active)
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
| Release Notes | Active | Cuts a release for valkey core or a module repo (search/json/bloom): AI-generates notes from `release-notes` PRs plus AI-triaged candidates without that label, promotes them onto a release line branch, bumps the repo's version file, opens a PR (held as a draft when the cut flags issues) |
| CVE Scan | Active | Scans container images for vulnerabilities, builds and scans the candidate image itself to prove the fix, then dispatches a targeted rebuild |
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

### Setup and Usage

See [DEVELOPMENT.md](DEVELOPMENT.md) for local setup, local validation commands,
required GitHub Actions secrets, and manual workflow dispatch examples.

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
6. **Create or update issues** - `IssueDedupPublisher` upserts one issue per fingerprint, matching on a hidden body marker (`<!-- valkey-ci-agent:test-failure:<hex-hash> -->`, where the hash is derived from the test name and file) rather than the title. Per-failure rendering (title, body, recurrence comment, `test-failure` label) lives in `issue_renderer.py`. Each failure resolves to one of four outcomes:
   - **created** - no matching issue exists: opens one titled `[TEST-FAILURE] {test_name} in {test_file}` with the `test-failure` label, error trace, CI links, and environment list
   - **updated** - a matching issue exists: merges any new failing environments into the body and bumps the occurrence counter / adds a recurrence comment
   - **skipped** - the run ID matches the `last-key` marker already recorded on the issue, so a re-triggered sweep over the same CI run does not inflate the occurrence count or post a duplicate comment
   - **skipped-recently-closed** - no open issue matches, but a matching issue (by marker, or by exact title for issues from the older fingerprint scheme) was closed within the past day; creation is suppressed because the failure was likely already fixed. This check is opt-in on the shared publisher and enabled only by the detector, so the fuzzer monitor never suppresses a recurring incident

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

## Release Notes Workflow

Cuts a release in one shot for valkey core or a module repo (`valkey-search`,
`valkey-json`, `valkey-bloom`). A maintainer dispatches the target repo, version,
and urgency, plus an explicit stage for `.0` releases; patch versions infer `ga`.
The agent derives the M.m release line and generates notes from the `release-notes`
PRs plus candidates without that label that AI triage judges user-facing (Claude
via Bedrock). Deterministic release-impact checks keep crash, memory-safety,
corruption, access-control, protocol, compatibility, and similar fixes from being
silently excluded by an AI verdict. The agent renders the result onto the
long-running release line as a dated section, bumps the repo's version file,
refreshes the running contributor list, and opens one PR for review (as a draft,
holding the merge, when the cut flags anything a maintainer should address first;
see [Edge-case handling](#edge-case-handling)).
Nothing accumulates notes on a branch; the notes for a release are generated all
at once. The release line is changed only when a maintainer merges the generated
PR.

Per-repo conventions (changelog heading name, version file layout, note
categories, prompt wording) live in `scripts/release_notes/projects.py`:

| Repo | Version file | Stage recorded |
|---|---|---|
| valkey | `src/version.h` (`VALKEY_VERSION` macros) | yes |
| valkey-search | `src/version.h` (`kModuleVersion` + `MODULE_RELEASE_STAGE`) | yes |
| valkey-json | `CMakeLists.txt` (`project(... VERSION M.m.p)`) | no |
| valkey-bloom | `Cargo.toml` (`[package] version`) | no |

The Valkeyrie GitHub App installation must include every repository that can be
selected: `valkey`, `valkey-search`, `valkey-json`, and `valkey-bloom` (or choose
**All repositories**). Each workflow token is scoped to only the selected
`${{ inputs.repo }}` and requests `contents:write`, `pull-requests:write`, and
`metadata:read`. Advisory-backed cuts additionally require
`repository-advisories:read`; normal cuts do not. Token creation fails when the
installation does not include the selected repository or lacks a requested
permission.

For repos whose version file records no stage, tag-based validation remains the
authoritative check against re-cutting an already-tagged stage. The
valkey-search 1.0 line (version inline in `src/module_loader.cc`) is not
supported and fails with a clear error.

Before their first automated cut, all three module repositories (`valkey-search`,
`valkey-json`, and `valkey-bloom`) need a one-time `00-RELEASENOTES`
normalization. The trailing historical contributor block must become the
canonical cumulative footer used by this workflow: a `### Contributors` heading
followed by one `* Display Name @handle` entry per line. Matching legacy text
inside a dated release section may remain because the renderer carries dated
history forward verbatim; only the trailing footer needs normalization. Legacy
repository-specific footer formats are not carried as permanent parsing rules
here.

Unstable sentinels are repository-specific: valkey core uses
`255.255.255-dev`, valkey-json uses numeric `99.99.99`, and valkey-bloom uses
`99.99.99-dev`. The selected version bumper recognizes only its repository's
sentinel and replaces it with the requested version when a new release line is
cut.

The normal dispatch defaults to a read-only preview. For rc1 of a new minor line:

```bash
gh workflow run release-notes-cut.yml \
  --repo valkey-io/valkey-ci-agent \
  --field repo=valkey \
  --field version=9.1.0 \
  --field stage=rc1 \
  --field urgency=LOW
```

The target branch (`9.1`) and baseline are derived automatically. Other `.0`
stages differ only in `stage`; a patch release leaves it empty:

```bash
# Next RC (the 9.1.0-rc1 tag must exist on the 9.1 branch)
gh workflow run release-notes-cut.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey --field version=9.1.0 --field stage=rc2 --field urgency=LOW

# GA after the final RC (the last rc tag must exist on the 9.1 branch)
gh workflow run release-notes-cut.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey --field version=9.1.0 --field stage=ga --field urgency=LOW

# Patch GA (the 9.1.0 tag must exist on the 9.1 branch)
gh workflow run release-notes-cut.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey --field version=9.1.1 --field urgency=LOW

# Module repo patch GA (the 1.2.1 tag must exist on the 1.2 branch)
gh workflow run release-notes-cut.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey-search --field version=1.2.2 --field urgency=LOW
```

After reviewing the preview, repeat the same dispatch with
`--field dry_run=false` to open or update the release PR. The normal workflow
exposes only `repo`, `version`, `stage`, `urgency`, and `dry_run`; `stage` is
case-insensitive and is required only when the patch component is zero.

If more changes merge into `M.m` while that release PR is open, dispatch the
same `version` and `stage` again with `dry_run=false`. A rerun regenerates the
complete release range through the latest `M.m` tip, rebuilds the same
`agent/release-cut/<version>-<stage>` branch from that tip, and updates the
existing open PR in place. This deliberately re-evaluates earlier entries as
well as processing new ones, so changed labels or PR metadata are picked up.
Because the prep branch is replaced with `--force-with-lease`, manual edits made
directly on that generated branch are not retained.

Use `release-notes-cut-advanced.yml` only for an explicit date/baseline,
contributor override, security entries/advisory lookup, or `force_ready`. It
delegates to the same release workflow as the normal dispatch, so the release
logic cannot drift between the two interfaces.

An omitted date resolves to the current **UTC** date. Use the advanced workflow's
explicit `date` input when the intended release date follows another timezone's
calendar day.

**Branch model** (tag-driven, one M.m branch per minor):

| Dispatch | Target branch | Range baseline |
|----------|---------------|----------------|
| rc1 of M.m.p | `M.m` | Previous release tag (auto-resolved) |
| rcN (N>1) | `M.m` | The `M.m.p-rc(N-1)` tag on M.m |
| ga of M.m.p | `M.m` | The last rc tag on M.m |
| later patches (`stage` omitted -> `ga`) | `M.m` | The previous patch tag (e.g. `M.m.(p-1)`) |

Maintainers create the M.m branch and push release tags before dispatching. The
agent never creates, deletes, or force-pushes a release-line branch; it creates
or updates only its namespaced prep branch.

### Common recipes

`version` and `urgency` are always required. `stage` is required for `M.m.0`
because the same version can mean rc1, a later RC, or GA; it is omitted for a
patch and inferred as `ga`. The target branch (`M.m`) and baseline resolve from
the version and repository tags.

| Cutting | `version` | `stage` | `base_ref` | Baseline the notes span |
|---------|-----------|---------|-----------|--------------------------|
| First RC of a new minor | `9.2.0` | `rc1` | *(empty)* | Auto: previous release tag (e.g. `9.1.0`) |
| Next RC on the same line | `9.2.0` | `rc2` | *(empty)* | Auto: the `9.2.0-rc1` tag on the `9.2` branch |
| GA after the final RC | `9.2.0` | `ga` | *(empty)* | Auto: the last rc tag (e.g. `9.2.0-rc2`) on `9.2` |
| Patch GA | `9.1.9` | *(empty -> `ga`)* | *(empty)* | Auto: the previous patch tag (`9.1.8`) |
| First RC of a new **major** | `10.0.0` | `rc1` | *(empty)* | Auto: the highest earlier release tag |

Distinctions that are easy to get wrong:

- **`version` is the target you are cutting, always `M.m.p`**: use `9.2.0` for
  every RC of that release (`rc1`, `rc2`, ...) and its GA, not `9.2.0-rc2`. The
  `-rcN` suffix comes from `stage`, not `version`. The M.m branch is derived from
  the version automatically; maintainers must create it and push tags before dispatch.
- **`base_ref` stays empty for normal cuts.** The tool resolves the baseline from
  release tags and shows the exact range in the PR body. Use the advanced
  workflow's override only to correct a range the preview proves is wrong.

Every normal dispatch defaults to `dry_run=true`: it logs the resolved plan and
the exact `base..head` range (both refs and their SHAs) without pushing or
opening a PR.

### How it works

The rendered commit lands on an agent-namespaced `agent/release-cut/...` prep
branch that opens a PR into the release line, so the line only advances when a
human merges.

1. **Resolve the plan** (code) - normalize an explicit stage, or infer `ga` when
   `PATCH > 0`; an omitted stage for `M.m.0` is rejected. Map that
   `(version, stage)` onto the branch model above. The version is canonicalized
   once (`M.m.p`, no leading zeros / stray whitespace) so the repository's
   version file, dated heading, commit title, and branch names all agree. The
   requested state must be newer than both the version file and every existing
   tag on that release line; an already-released stage or downgrade is rejected
   before the AI runs.
2. **Discover the range** (code) - resolve `base..head` and walk it by graph
   reachability, deduplicating to one entry per originating PR number. The M.m
   branch tip is fetched once and pinned to an immutable SHA used by discovery,
   contributors, and the prep worktree. The base is an explicit
   `--base-ref`, else tag resolution on the M.m branch: for rc1 it is the previous
   release tag (resolved from all tags in the repo), for rc2+ it is the prior RC
   tag (e.g. `9.2.0-rc1`), and for a patch GA it is the previous patch tag (e.g.
   `9.1.8`). Tags are created by maintainers before dispatch.
   Per-PR backports are credited to their merged source PR after provenance
   validation; supported evidence includes the structured backport summary,
   cherry-pick/subject/branch metadata, and a standalone
   `backport of <GitHub PR URL>` marker.
3. **Classify** (code) - split PRs by label: `release-notes` PRs are included
   directly, `no-release-notes` PRs are hard-excluded (dropped before triage, never
   noted, and listed in the PR body so a maintainer can catch a mislabel), and
   everything else is a triage candidate. If a PR carries both labels,
   `no-release-notes` wins.
4. **Triage** (AI) - Claude decides, per candidate without `release-notes`, whether the change
   is user-facing enough to note (include) or purely internal (exclude), with a
   short reason for each. Patch-release triage defaults uncertain correctness and
   safety fixes to inclusion. Code independently scans PR-authored title/body
   text for release-impact signals; if AI excludes such a candidate or omits its
   verdict, the guardrail forces it into generation as uncertain. Included
   candidates join the labelled PRs; all decisions and guardrail overrides are
   surfaced in the PR body. A candidate with no verdict and no guardrail signal
   falls back to human triage.
5. **Generate** (AI) - Claude writes one categorized, user-facing bullet per
   included PR (labelled + triaged-in). Category guidance favors the affected
   user-facing surface; code normalizes generic INFO/metrics/ACL LOG/logging
   classifications to `Observability and Logging` and flags that correction for
   review. The model never emits the `(#N)` reference or `by @handle`; code
   removes accidental duplicate markers and terminal punctuation, then appends
   the canonical attribution in `scripts/release_notes/render.py`.
6. **Render + bump** (code) - render the categorized bullets into a new dated
   section prepended before any existing sections on the release line via
   `render_release_notes` (`release_format.py`) / `set_version`
   (`version_bump.py`), append the cumulative contributor list
   (`contributors.py`) deduplicated by case-insensitive display-name/login
   identity (PR-resolved logins give squash-merged authors proper @handles),
   and bump the version file selected by the repository profile. These format
   primitives live
   in-repo rather than being imported from valkey, because upstream
   `valkey-io/valkey` ships no such tooling, so a cut runs against unmodified
   upstream (a plaintext `00-RELEASENOTES` placeholder and a `src/version.h`
   with the `VALKEY_VERSION*` macros).
7. **Open the PR** (code) - commit on the prep branch, push it (force-with-lease),
   and open/update a PR into the release line with a body that explains the cut and
   surfaces any advisories (below). When the cut flags anything a maintainer should
   address first, the PR opens as a draft to hold the merge (see [Edge-case
   handling](#edge-case-handling)). Immediately before changing the prep branch,
   the agent re-fetches M.m and aborts if its tip differs from the pinned SHA.
   Re-dispatching the same version/stage fully regenerates against the latest
   M.m tip, force-updates the deterministic prep branch, and edits the same open
   PR rather than creating another one.

### Edge-case handling

Malformed dispatch inputs fail fast at argparse (exit 2), before the clone + AI
run: a non-`M.m.p` or out-of-range version, an omitted stage for `M.m.0`, a bad
explicit stage, an urgency outside `LOW/MODERATE/HIGH/CRITICAL/SECURITY`, or a
non-ISO date. Repository-state validation runs after the clone: an explicit
`--base-ref` that resolves to nothing aborts with a clear error, and a cut
against a non-existent M.m branch is refused immediately. A target that is equal
to or older than the repository's version file, or at or behind an existing tag on that M.m
line, is also refused before note generation.

When the cut raises anything a maintainer should address before merging, the
release PR opens as a draft (GitHub refuses to merge a draft) so a shipped
change can never be released while a warning goes unread. The body leads with a
banner naming the held items, and each has its own section below. Resolve them and
click **Ready for review** to release, re-dispatch after resolving the signals,
or use `force_ready` from the advanced workflow to open ready anyway (the banner
then records that N items were overridden). `--dry-run` prints the same hold
decision without opening a PR. The signals that hold:

- **RC out of sequence** - a re-cut rc or a skipped rc number.
- **Unanchored baseline** - rc1 of `M.0.0` with no `--base-ref` fell back to the
  nearest tag, which may over-broaden the range.
- **Empty release notes** - no PRs in range, or no PR carried `release-notes` and AI triage
  included none of the remaining candidates; the body says which, so an empty dated
  section is not mistaken for a generation miss.
- **Duplicate / declined / low-confidence** - a PR credited in more than one
  bullet, any included PR for which the model produced no bullet, or a note the model flagged
  as low-confidence.
- **AI triage** - the model decided inclusion for PRs without `release-notes` (a call that used
  to require a human label), so the PR holds until a maintainer confirms the
  include/exclude table; a low-confidence triage call also holds.
- **Release-impact review** - a deterministic guardrail overrode an AI exclusion
  or missing verdict, or release-impact signals were detected while the requested
  urgency is `LOW`/`MODERATE`. The body lists every signal so release/security
  maintainers can choose urgency and any hand-authored Security Fixes entries;
  code does not assign severity automatically.
- **Security** - `SECURITY` urgency with no security fixes, or advisories that
  could not be read (a clean advisory match and normal-note deduplication for a
  supplied `--security-fix` are informational and do not hold).
- **Unresolved changes** - a shipped change that would otherwise slip past
  valkey's label-only gate: a range commit with no resolvable PR (absent from the
  notes entirely), a commit whose resolved PR could not be fetched, or a note
  credited to a backport PR because the original author's PR could not be recovered.
- **Triage** - PRs without `release-notes` that AI triage could not decide (no verdict returned), so
  a maintainer must decide whether to include them.

The body always shows the resolved notes range so an over-broad baseline is
visible: the resolved mode (e.g. `rc2`), the source and target branches, and both
ends as `ref @ <sha>`, so a reviewer can audit the exact commits the notes were
computed over, not just the branch-model names. `--dry-run` prints the same range
and advisories to the log.

### Configuration

Reuses the same secrets and OIDC role as the other workflows (see
[DEVELOPMENT.md](DEVELOPMENT.md)). The workflow mints one
short-lived App token on `valkey-io/valkey` with `contents:write` (push the prep
branch), `pull-requests:write` (open the release PR),
and `metadata:read`. The advanced workflow exposes
`security_from_advisories`; when set, it mints a token that additionally holds
`repository-advisories:read`. That permission is requested only for an advisory
cut, so an ordinary cut is never blocked when the App installation lacks it.
The App installation must hold `repository-advisories:read` for an advisory cut
to read the advisories.

## CVE Scan Workflow

Verification instead of prediction. The scan finds candidate CVEs in the published container images; we then build the candidate image ourselves, scan the real artifact to prove the targeted CVEs are gone, and only then dispatch valkey-container's plain build-and-publish workflow. We never predict whether a rebuild would help by inspecting base package databases or simulating installs; we build and check. All findings are reported in the GitHub Actions job summary. No GitHub issues are created.

This is Phase 1: a concrete implementation targeting `valkey-io/valkey-container`. Phase 2 (reusable `workflow_call` extraction for other repos) is planned.

### How it works

A single workflow (`.github/workflows/cve-scan.yml`) with four jobs, in order: `scan`, `verify`, `collect`, and `rebuild`.

**Job 1: `scan`** (weekly cron + manual `workflow_dispatch`; `needs: none`, `timeout-minutes: 30`)

1. **Install scanner**: sets up Trivy on the runner.
2. **Resolve image matrix**: `image_matrix.py` fetches the upstream `versions.json` manifest and derives the full set of image tags to scan.
3. **Scan (multi-arch)**: `sweep.py` runs Trivy per image per platform via `scanner.py`. Each image is scanned on all 4 published platforms (linux/amd64, linux/arm64, linux/arm/v7, linux/ppc64le). Findings are deduplicated by (image, package, cve_id, installed_version, platform): exact duplicates within a platform collapse, while the same CVE on different platforms stays distinct.
4. **Classify**: findings with a published fix (a `fixed_version` from the distro) become candidates; findings with no fix are reported as not-fixable. This is a candidacy signal, not a prediction that a rebuild will resolve the CVE. The proof happens in the `verify` job.
5. **Report findings**: all findings (candidates and not-fixable) are rendered in the GitHub Actions job summary as grouped markdown tables. No GitHub issues are created.
6. **Emit outputs**: writes `fixable` (true/false), `versions` (space-separated version lines, e.g. `8.0 9.1`), `targets` (a base64 JSON contract listing the candidate `{image, line, variant, platform, cve, package, fixed_version}` tuples), and `matrix` (the expected verification legs, consumed directly by the `verify` job as `fromJSON(needs.scan.outputs.matrix)`) to `GITHUB_OUTPUT`. `versions`/`targets`/`matrix` are empty when there are no candidates.

**Job 2: `verify`** (`needs: scan`, `timeout-minutes: 180`, one matrix leg per candidate)

The matrix is `fromJSON(needs.scan.outputs.matrix)` (`max-parallel: 8`, `fail-fast: false`), so there is one leg per affected `(line, variant, platform)`. Runs only on the canonical repository's `main` branch, only when the scan found candidates (`fixable == 'true'` and `versions != ''`), and never on a dry run. It holds no credentials and pushes nothing.

1. **Build the candidate ourselves**: checks out `valkey-io/valkey-container` at `mainline` and invokes its canonical `.github/actions/build-image` action for ONE platform per leg. The action is also used by valkey-container's publishing workflow, so QEMU, Buildx, context, Dockerfile handling, and provenance have one definition. The agent supplies the explicit `./container` path context with `push: false`, `load: true`, and a local `cve-candidate:<line>-<variant>-<platform-slug>` tag. There are no registry credentials anywhere in this job: the candidate never leaves the runner.
2. **Prove the fix**: `verify_candidate.py` scans the just-built image and records the leg's outcome (verified, survivors, or error) as a marker. Exit code 2 (a verification error) fails the leg loudly; exit 1 (survivors) does not fail the leg. The dispatch decision is made later in `collect`, on the any-architecture policy described in the tradeoffs below: a surviving CVE on one architecture no longer blocks a line proven fixed on another.

Because the matrix has one leg per affected architecture, it can reach 5 lines x 2 variants x 4 platforms = 40 legs. Matrix parallelism is bounded (`max-parallel: 8`) and each leg carries a defensible `timeout-minutes` (180, an estimate to be calibrated on the first live run) because a single-platform source compile is heavy, and two of the four platforms (`linux/arm/v7`, `linux/ppc64le`) are QEMU-emulated compiles that are far slower than native amd64.

**Job 3: `collect`** (`needs: [scan, verify]`, `timeout-minutes: 10`)

Downloads every leg's marker and reconciles them against the expected matrix from the scan: a leg that produced no marker is recorded as missing, and an unexpected leg is rejected. From the reconciled markers it decides which version lines to dispatch and emits `verified_versions`, `fixable`, and `arch_report` (the per-architecture proof status the rebuild report uses). A line qualifies when at least one affected architecture was proven fixed; the only fail-closed no-dispatch is a run that proved nothing at all (no architecture verified anywhere and at least one leg errored or went missing, so "not fixable" cannot be told apart from "we failed to look").

**Job 4: `rebuild`** (`needs: [scan, collect]`, `timeout-minutes: 240`, conditional, automatic)

1. **Condition**: it dispatches `collect`'s `verified_versions` (the version lines proven fixed on at least one affected architecture; the any-architecture gate, see the tradeoffs below). It also restates the guards explicitly: only the canonical repository's `main` branch (`github.repository == 'valkey-io/valkey-ci-agent'` and `github.ref == 'refs/heads/main'`), only when there is at least one dispatchable line, and not on a dry run. It runs in the `cve-rebuild-dispatch` protected Environment, a credential boundary (not an approval gate) that scopes the App credentials and dispatch permission to `main`.
2. **Dispatch with a correlation id**: this step mints a scoped Valkeyrie Bot App token (`actions:write` on valkey-container) - the only step that needs it - generates `correlation_id` as `${{ github.run_id }}-${{ github.run_attempt }}`, and dispatches (`gh workflow run ci.yml --repo valkey-io/valkey-container --field version="<versions>" --field correlation_id="<id>"`). The companion valkey-container change echoes the id into its run name (`CVE rebuild <id>`) alongside adding the shared build action. The dispatch targets only the affected version lines.
3. **Locate by exact run name and wait**: the locate, watch, and conclusion steps use the built-in `GITHUB_TOKEN` rather than the App token, because App installation tokens expire after one hour while the watched build runs 130 to 150 minutes; `GITHUB_TOKEN` stays valid for the whole job, valkey-container is public so its Actions runs are readable, and no privileged App credential is held for the full wait. The job polls `gh run list --repo valkey-io/valkey-container --workflow ci.yml --branch mainline` for the run whose name is exactly `CVE rebuild <correlation_id>`, retrying a few times for it to appear. Exact-name correlation supersedes the previous timestamp-window and actor-filter heuristics, which are removed. If no matching run surfaces it fails loudly (a green rebuild is never reported on a run that cannot be seen). Once found, it waits with `gh run watch --exit-status`, which fails this job when the downstream build fails, and captures the run's `conclusion`.
4. **Report the build result**: the job summary and the Slack notification report the actual downstream build conclusion (not just that the dispatch was accepted), the correlation id, and the valkey-container run URL. The Slack status is success only when the dispatch succeeded, the run was located, and the build concluded success; every other case (including cancelled, timed-out, or unknown) normalizes to failure, so a non-success rebuild always notifies instead of going silent. The job carries a generous `timeout-minutes` (240) because observed full-matrix `ci.yml` builds run about 130 to 150 minutes; the runner is billed while it watches, which is the deliberate cost of verifying the build rather than only the dispatch.

### Tradeoffs (stated honestly)

- **The image is built twice, by design.** We build the candidate in the `verify` job to prove the fix, and valkey-container builds it again to publish. This is deliberate: it keeps their `ci.yml` a plain build-and-publish with no CVE logic. The cost is one extra single-platform compile per affected architecture (up to four per line and variant, two of them emulated).
- **We verify OUR build, not the exact published digest.** The `verify` build and valkey-container's publish build run minutes apart from the same `mainline` Dockerfiles, and OS package repositories only move forward, so a package present in our build is present in theirs. This is strong evidence, but it is not digest-identical: we prove the fix on an artifact we built, not on the exact bytes valkey-container ships.
- **Every affected architecture is verified.** The verify matrix emits one build per distinct `(line, variant, platform)` in the findings, so a CVE flagged on amd64 and arm64 is proven on both. A distro can publish a package fix for one architecture before another, so per-architecture verification is what lets the report state honestly which architectures are fixed. The honest cost: this multiplies the verification build count by up to fourfold per line and variant (four published platforms), and two of the four (`linux/arm/v7`, `linux/ppc64le`) are QEMU-emulated source compiles that are far slower than the native amd64 build. Verifying every architecture is not the same as gating dispatch on all of them (see the next point).
- **Dispatch is any-architecture, and a dispatched line may still be vulnerable on some of them.** A version line is dispatched when AT LEAST ONE affected architecture is proven fixed, not when all of them are. valkey-container's `ci.yml` takes a single `version` input and rebuilds all four platforms in one multi-platform push; there is no way to ask it to rebuild a single architecture. So withholding a dispatch because one architecture could not be proven fixed is strictly harmful: dispatching fixes the architectures whose fix is already live, while a lagging architecture is simply rebuilt and stays exactly as vulnerable as it already was. Nobody is worse off, and amd64 users no longer wait another week because ppc64le lags. The consequence is that a dispatched line may still be vulnerable on an architecture whose fix has not landed, which is why reporting is per architecture: the `collect` job emits an `arch_report`, and the rebuild job's summary and Slack notification state, for every dispatched line, which architectures were proven fixed and which remain vulnerable, never implying a line is fully fixed when only some architectures were proven. Errored or missing legs are surfaced loudly but do not block a line that verified on another architecture; the only fail-closed no-dispatch is a run that proved nothing at all (no architecture verified anywhere and at least one leg errored or went missing, so "not fixable" cannot be told apart from "we failed to look").
- **The build definition is shared, not mirrored.** valkey-container owns `.github/actions/build-image`; both its publishing workflow and this verification workflow call that action. BuildKit, QEMU, context, Dockerfile handling, and provenance changes therefore apply to both paths without a separate drift checker.

Concurrency is declared per job: the scan and verify jobs cancel/redo cheaply when superseded, while the rebuild job uses a separate group with cancel-in-progress false so an in-flight rebuild watcher is never cancelled while the container build continues.

### Installation

#### Prerequisites

- The **Valkeyrie Bot GitHub App** installed on the target repository with:
  - `actions: write` (dispatch the rebuild workflow)
  - `contents: read`, `metadata: read`
- Org-level secrets: `VALKEYRIE_BOT_APP_ID` and `VALKEYRIE_BOT_PRIVATE_KEY`

#### Step 1: Configure secrets

On the repo hosting the agent workflows:

| Type | Name | Value |
|------|------|-------|
| Secret | `VALKEYRIE_BOT_APP_ID` | Valkeyrie Bot GitHub App ID |
| Secret | `VALKEYRIE_BOT_PRIVATE_KEY` | App private key |

Forks are scan/dry-run only: there is no PAT fallback, so without the org App secrets the rebuild job's guards keep it from running and no token is minted.

#### Step 2: Create the protected Environment

Create a `cve-rebuild-dispatch` Environment in repo Settings with a deployment-branch rule limited to `main`. It is a credential boundary (not an approval gate): it scopes the App credentials and dispatch permission so no other ref can access them.

### Configuration

Settings are loaded from `CVE_SCAN_*` environment variables with sensible defaults
targeting `valkey-io/valkey-container`. The workflow pins all values explicitly in
its `env:` block (house style: visible-in-workflow configuration). Override any
variable to change behavior for forks or testing.

| Variable | Default | Description |
|----------|---------|-------------|
| `CVE_SCAN_VERSIONS_URL` | `https://raw.githubusercontent.com/valkey-io/valkey-container/mainline/versions.json` | URL to the versions.json manifest for dynamic image resolution |
| `CVE_SCAN_REPOSITORY` | `valkey/valkey` | Docker Hub repository prefix for derived image tags |
| `CVE_SCAN_INCLUDE_UNSTABLE` | `false` | Include the `unstable` version line (truthy: `1`, `true`, `yes`, `on`; falsy: `0`, `false`, `no`, `off`, empty) |
| `CVE_SCAN_SCANNER` | `trivy` | Vulnerability scanner (trivy only; env var kept for forward compatibility) |
| `CVE_SCAN_SEVERITY_THRESHOLD` | `HIGH` | Ignore findings below this severity (`UNKNOWN`, `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`). |
| `CVE_SCAN_IMAGES` | *(empty)* | Optional static image list (comma-separated). When set, overrides dynamic resolution from versions.json. Testing/escape hatch. |
| `CVE_SCAN_PLATFORMS` | `linux/amd64,linux/arm64,linux/arm/v7,linux/ppc64le` | Comma-separated platforms to scan per image. Defaults to the verified published set for valkey images. |

Invalid values (unknown scanner, bad severity, empty labels) raise immediately:
a typo must not silently scan nothing.

### Usage

#### Weekly scan (automatic)

Runs weekly on the configured schedule (default: Monday 06:00 UTC). Scans all images in the matrix on all configured platforms, reports findings in the job summary, builds and scans each candidate to prove the fix, and dispatches a targeted rebuild automatically for the candidates that pass verification.

#### Manual scan

```bash
gh workflow run cve-scan.yml --repo <agent-repo>
```

Supports a `dry_run` input that prints findings without dispatching a rebuild. It defaults to `true`: a manual run never triggers a real rebuild unless you explicitly set `dry_run=false`. Supports a `severity_threshold` input for ad-hoc investigation; it affects classification and therefore rebuild dispatch, not just reporting.

#### Reviewing results

After each scan, check the workflow run's job summary in GitHub Actions. The summary lists all findings (candidates and not-fixable) as grouped markdown tables. Candidates flow into the `verify` job, which builds and scans the real artifact to prove the fix; only proven candidates reach the `rebuild` job, which then waits for the downstream valkey-container build and reports its actual conclusion and run URL in the job summary and Slack, so any non-success outcome (failed, cancelled, or timed-out) always notifies and is visible rather than masked by a successful dispatch.

## Safety

- **Branch namespace** - the agent writes only `agent/backport/...` (backports) and `agent/release-cut/...` (release cuts) branches and opens PRs for maintainer review. It never force-pushes a release line directly.
- **Credential isolation** - all GitHub auth uses `GIT_ASKPASS`; tokens never appear in `.git/config` or URLs
- **Claude Code env isolation** - `GITHUB_TOKEN`, `GH_TOKEN`, and `*_SECRET` are stripped from the subprocess environment. Claude cannot see credentials.
- **Deterministic validation** - registry-configured build commands run before push. A validation failure blocks the push.
- **CVE scan: proof, not prediction** - a rebuild is dispatched only after the `verify` job builds the candidate image itself and scans the real artifact, and only for the version lines `collect` finds proven fixed on at least one affected architecture. A run that proves nothing at all (no architecture verified anywhere, with an errored or missing leg) fails closed and dispatches nothing. The workflow run log is the audit record for every automatic dispatch.
- **CVE scan: targeted dispatch** - rebuilds are dispatched with `--field version="<versions>"` for only the affected version lines (e.g. `8.0 9.1`), not a rebuild-all. This minimizes the blast radius of automatic rebuilds.
- **CVE scan: no registry credentials in verify** - the `verify` job builds with `push: false` / `load: true` and holds no registry credentials, so the candidate image never leaves the runner.
- **CVE scan: no AI in the pipeline** - the entire scan-verify-dispatch path is deterministic code (scanner, shared candidate build action, `verify_candidate`, `gh workflow run`). No AI layer participates in any decision or dispatch step.
- **Fork sync** - when a different-owner `push_repo` is configured, the agent fast-forwards that fork's release branch to match upstream before cherry-picking
- **Stale branch pruning** - if a previous backport PR was closed without merging, the agent deletes the orphaned branch before starting fresh
- **DCO** - backport commits are signed off. ci_fix commits are authored by the bot without a sign-off, so a human certifies the change before merge.

## Documentation

- [docs/architecture.md](docs/architecture.md) - full system design including planned workflows
- [DEVELOPMENT.md](DEVELOPMENT.md) - local setup, testing, and GitHub Actions usage
