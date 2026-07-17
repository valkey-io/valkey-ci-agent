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
| Release Notes | Active | Cuts a release: AI-generates notes from labelled PRs plus AI-triaged label-less ones, promotes them onto a release line branch, bumps `src/version.h`, opens a PR (held as a draft when the cut flags issues) |
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

## Release Notes Workflow

Cuts a Valkey release in one shot. A maintainer dispatches the source branch, the
target version, stage, and urgency; the agent generates the release notes from the
labelled PRs plus the label-less ones AI triage judges user-facing (Claude via
Bedrock), renders them onto the long-running
release line as a dated section, bumps `src/version.h`, refreshes the running
contributor list, and opens one PR for review (as a draft, holding the merge, when
the cut flags anything a maintainer should address first; see [Edge-case
handling](#edge-case-handling)). Nothing accumulates notes on a branch; the notes
for a release are generated all at once. The source branch is never modified.

Dispatch it from this agent repository (rc1 of a new minor line):

```bash
gh workflow run release-notes-cut.yml \
  --repo valkey-io/valkey-ci-agent \
  --field version=9.1.0 \
  --field stage=rc1 \
  --field urgency=LOW
```

The target branch (`9.1`) is derived automatically from the version. The other
stages differ only in `stage`; see [Common recipes](#common-recipes) for how each
baseline resolves:

```bash
# Next RC (the 9.1.0-rc1 tag must exist on the 9.1 branch)
gh workflow run release-notes-cut.yml --repo valkey-io/valkey-ci-agent \
  --field version=9.1.0 --field stage=rc2 --field urgency=LOW

# GA after the final RC (the last rc tag must exist on the 9.1 branch)
gh workflow run release-notes-cut.yml --repo valkey-io/valkey-ci-agent \
  --field version=9.1.0 --field stage=ga --field urgency=LOW

# Patch GA (the 9.1.0 tag must exist on the 9.1 branch)
gh workflow run release-notes-cut.yml --repo valkey-io/valkey-ci-agent \
  --field version=9.1.1 --field stage=ga --field urgency=LOW
```

Optional inputs: `date` (defaults to today), `base_ref` (explicit baseline ref
overriding tag resolution), `contrib_base_ref` (contributor range start),
`security_fixes` (manual Security Fixes bullets, one `--security-fix` per entry),
`security_from_advisories` (also render published GitHub security advisories
fixed by this version into Security Fixes), `force_ready` (open the PR ready even
when the cut flags issues, instead of holding it as a draft; see [Edge-case
handling](#edge-case-handling)), and `dry_run` (compute and log the cut without
pushing or opening a PR). Stage is `rc1..rcN` or `ga`, case-insensitive.

**Branch model** (tag-driven, one M.m branch per minor):

| Dispatch | Target branch | Range baseline |
|----------|---------------|----------------|
| rc1 of M.m.p | `M.m` | Previous release tag (auto-resolved) |
| rcN (N>1) | `M.m` | The `M.m.p-rc(N-1)` tag on M.m |
| ga of M.m.p | `M.m` | The last rc tag on M.m |
| later patches | `M.m` | The previous patch tag (e.g. `M.m.(p-1)`) |

Maintainers create the M.m branch and push tags before dispatching. The agent
never creates or deletes branches.

### Common recipes

Only `version`, `stage`, and `urgency` are required. The target branch (`M.m`) is
derived from the version. The baseline resolves itself for every case except rc1 of
an `M.0.0` (first release of a new major), so leave `base_ref` empty unless a recipe
below sets it.

| Cutting | `version` | `stage` | `base_ref` | Baseline the notes span |
|---------|-----------|---------|-----------|--------------------------|
| First RC of a new minor | `9.2.0` | `rc1` | *(empty)* | Auto: previous release tag (e.g. `9.1.0`) |
| Next RC on the same line | `9.2.0` | `rc2` | *(empty)* | Auto: the `9.2.0-rc1` tag on the `9.2` branch |
| GA after the final RC | `9.2.0` | `ga` | *(empty)* | Auto: the last rc tag (e.g. `9.2.0-rc2`) on `9.2` |
| Patch GA | `9.1.9` | `ga` | *(empty)* | Auto: the previous patch tag (`9.1.8`) |
| First RC of a new **major** | `9.0.0` | `rc1` | *(set it)* | You must pass the prior major's final release |

Distinctions that are easy to get wrong:

- **`version` is the target you are cutting, always `M.m.p`**: use `9.2.0` for
  every RC of that release (`rc1`, `rc2`, ...) and its GA, not `9.2.0-rc2`. The
  `-rcN` suffix comes from `stage`, not `version`. The M.m branch is derived from
  the version automatically; maintainers must create it and push tags before dispatch.
- **`base_ref` stays empty except for rc1 of `M.0.0`.** For every other case, the
  tool resolves the baseline from tags on the target branch (and shows the resolved
  range in the PR body, so you can confirm it). Setting `base_ref` by hand on those
  cases is how a wrong range gets cut; only use it when a cut's PR body flags an
  unanchored or over-broad baseline.

When unsure, dispatch with `dry_run` first: it logs the resolved plan and the exact
`base..head` range (both refs and their SHAs) without pushing or opening a PR.

### How it works

The rendered commit lands on an agent-namespaced `agent/release-cut/...` prep
branch that opens a PR into the release line, so the line only advances when a
human merges.

1. **Resolve the plan** (code) - map `(version, stage)` onto the branch model
   above. The version is canonicalized once (`M.m.p`, no leading zeros / stray
   whitespace) so `version.h`, the dated heading, the commit title, and the branch
   names all agree.
2. **Discover the range** (code) - resolve `base..head` and walk it by graph
   reachability, deduplicating to one entry per originating PR number. The head is
   always the M.m branch tip (derived from the version). The base is an explicit
   `--base-ref`, else tag resolution on the M.m branch: for rc1 it is the previous
   release tag (resolved from all tags in the repo), for rc2+ it is the prior RC
   tag (e.g. `9.2.0-rc1`), and for a patch GA it is the previous patch tag (e.g.
   `9.1.8`). Tags are created by maintainers before dispatch.
3. **Classify** (code) - split PRs by the `release-notes` label: labelled PRs are
   included directly, everything else is a triage candidate.
4. **Triage** (AI) - Claude decides, per label-less candidate, whether the change
   is user-facing enough to note (include) or purely internal (exclude), with a
   short reason for each. Included candidates join the labelled PRs; the model's
   include/exclude table is surfaced in the PR body for a maintainer to confirm.
   A candidate the model returns no verdict for falls back to human triage.
5. **Generate** (AI) - Claude writes one categorized, user-facing bullet per
   included PR (labelled + triaged-in). The model never emits the `(#N)` reference
   or `by @handle` - code appends those in `scripts/release_notes/render.py`
   (`format_bullet`), so the bullet format stays fixed in one place.
6. **Render + bump** (code) - render the categorized bullets into a new dated
   section prepended before any existing sections on the release line via
   `render_release_notes` (`release_format.py`) / `set_version`
   (`version_bump.py`), append the cumulative contributor list
   (`contributors.py`), and bump `src/version.h`. These format primitives live
   in-repo rather than being imported from valkey, because upstream
   `valkey-io/valkey` ships no such tooling, so a cut runs against unmodified
   upstream (a plaintext `00-RELEASENOTES` placeholder and a `src/version.h`
   with the `VALKEY_VERSION*` macros).
7. **Open the PR** (code) - commit on the prep branch, push it (force-with-lease),
   and open/update a PR into the release line with a body that explains the cut and
   surfaces any advisories (below). When the cut flags anything a maintainer should
   address first, the PR opens as a draft to hold the merge (see [Edge-case
   handling](#edge-case-handling)).

### Edge-case handling

Malformed dispatch inputs fail fast at argparse (exit 2), before the clone + AI
run: a non-`M.m.p` or out-of-range version, a bad stage, an urgency outside
`LOW/MODERATE/HIGH/CRITICAL/SECURITY`, or a non-ISO date. An explicit `--base-ref`
that resolves to nothing aborts right after the clone with a clear error. A cut
dispatched against a non-existent M.m branch is refused immediately.

When the cut raises anything a maintainer should address before merging, the
release PR opens as a draft (GitHub refuses to merge a draft) so a shipped
change can never be released while a warning goes unread. The body leads with a
banner naming the held items, and each has its own section below. Resolve them and
click **Ready for review** to release, re-dispatch with the flag cleared (a re-cut
with no flags flips the draft ready on its own), or dispatch `force_ready` to open
ready anyway (the banner then records that N items were overridden). `--dry-run`
prints the same hold decision without opening a PR. The signals that hold:

- **RC out of sequence** - a re-cut rc or a skipped rc number.
- **Unanchored baseline** - rc1 of `M.0.0` with no `--base-ref` fell back to the
  nearest tag, which may over-broaden the range.
- **Empty release notes** - no PRs in range, or no PR was labelled and AI triage
  included none of the label-less ones; the body says which, so an empty dated
  section is not mistaken for a generation miss.
- **Duplicate / declined / low-confidence** - a PR credited in more than one
  bullet, a labelled PR the model declined to note, or a note the model flagged
  as low-confidence.
- **AI triage** - the model decided inclusion for label-less PRs (a call that used
  to require a human label), so the PR holds until a maintainer confirms the
  include/exclude table; a low-confidence triage call also holds.
- **Security** - a `--security-fix` also noted as a normal bullet, `SECURITY`
  urgency with no security fixes, or advisories that could not be read (a clean
  advisory match is informational and does not hold).
- **Unresolved changes** - a shipped change that would otherwise slip past
  valkey's label-only gate: a range commit with no resolvable PR (absent from the
  notes entirely), a commit whose resolved PR could not be fetched, or a note
  credited to a backport PR because the original author's PR could not be recovered.
- **Triage** - label-less PRs AI triage could not decide (no verdict returned), so
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
and `metadata:read`. When `security_from_advisories` is set, it mints a token
that additionally holds `repository-advisories:read`; that permission is
requested only for an advisory cut, so an ordinary cut is never blocked when the
App installation lacks it. The App installation must hold
`repository-advisories:read` for an advisory cut to read the advisories.

## Safety

- **Branch namespace** - the agent writes only `agent/backport/...` (backports) and `agent/release-cut/...` (release cuts) branches and opens PRs for maintainer review. It never force-pushes a release line directly.
- **Credential isolation** - all GitHub auth uses `GIT_ASKPASS`; tokens never appear in `.git/config` or URLs
- **Claude Code env isolation** - `GITHUB_TOKEN`, `GH_TOKEN`, and `*_SECRET` are stripped from the subprocess environment. Claude cannot see credentials.
- **Deterministic validation** - registry-configured build commands run before push. A validation failure blocks the push.
- **Fork sync** - when a different-owner `push_repo` is configured, the agent fast-forwards that fork's release branch to match upstream before cherry-picking
- **Stale branch pruning** - if a previous backport PR was closed without merging, the agent deletes the orphaned branch before starting fresh
- **DCO** - backport commits are signed off. ci_fix commits are authored by the bot without a sign-off, so a human certifies the change before merge.

## Documentation

- [docs/architecture.md](docs/architecture.md) - full system design including planned workflows
- [DEVELOPMENT.md](DEVELOPMENT.md) - local setup, testing, and GitHub Actions usage
