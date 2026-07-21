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
| Release Notes | Active | Cuts a release: AI-generates notes from `release-notes` PRs plus AI-triaged candidates without that label, promotes them onto a release line branch, bumps `src/version.h`, opens a PR (held as a draft when the cut flags issues) |
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

Cuts a Valkey release in one shot. A maintainer dispatches the target version and
urgency, plus an explicit stage for `.0` releases; patch versions infer `ga`. The
agent derives the M.m release line and generates notes from the `release-notes`
PRs plus candidates without that label that AI triage judges user-facing (Claude
via Bedrock). Deterministic release-impact checks keep crash, memory-safety,
corruption, access-control, protocol, compatibility, and similar fixes from being
silently excluded by an AI verdict. The agent renders the result onto the
long-running release line as a dated section, bumps `src/version.h`, refreshes
the running contributor list, and opens one PR for review (as a draft, holding
the merge, when the cut flags anything a maintainer should address first; see
[Edge-case handling](#edge-case-handling)).
Nothing accumulates notes on a branch; the notes for a release are generated all
at once. The release line is changed only when a maintainer merges the generated
PR.

The normal dispatch defaults to a read-only preview. For rc1 of a new minor line:

```bash
gh workflow run release-notes-cut.yml \
  --repo valkey-io/valkey-ci-agent \
  --field version=9.1.0 \
  --field stage=rc1 \
  --field urgency=LOW
```

The target branch (`9.1`) and baseline are derived automatically. Other `.0`
stages differ only in `stage`; a patch release leaves it empty:

```bash
# Next RC (the 9.1.0-rc1 tag must exist on the 9.1 branch)
gh workflow run release-notes-cut.yml --repo valkey-io/valkey-ci-agent \
  --field version=9.1.0 --field stage=rc2 --field urgency=LOW

# GA after the final RC (the last rc tag must exist on the 9.1 branch)
gh workflow run release-notes-cut.yml --repo valkey-io/valkey-ci-agent \
  --field version=9.1.0 --field stage=ga --field urgency=LOW

# Patch GA (the 9.1.0 tag must exist on the 9.1 branch)
gh workflow run release-notes-cut.yml --repo valkey-io/valkey-ci-agent \
  --field version=9.1.1 --field urgency=LOW
```

After reviewing the preview, repeat the same dispatch with
`--field dry_run=false` to open or update the release PR. The normal workflow
exposes only `version`, `stage`, `urgency`, and `dry_run`; `stage` is
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
   once (`M.m.p`, no leading zeros / stray whitespace) so `version.h`, the dated
   heading, the commit title, and the branch names all agree. The requested state
   must be newer than both `src/version.h` and every existing tag on that release
   line; an already-released stage or downgrade is rejected before the AI runs.
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
   and bump `src/version.h`. These format primitives live
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
to or older than `src/version.h`, or at or behind an existing tag on that M.m
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
