# Release notes

Cutting a release used to mean going through every merged PR since the last
release, deciding which ones users care about, writing a line for each,
updating the version number and the contributor list, and opening a PR. The
release notes workflow does all of that from one dispatch, for Valkey and for
the `valkey-search`, `valkey-json` and `valkey-bloom` modules.

The result is a normal pull request into the release branch. Nothing changes
on the release branch until a maintainer merges it. Maintainers can leave
inline comments on the notes in that PR, and the agent will make the edits
for them.

For Valkey itself, this is one step of a longer pipeline that goes on to tag
and publish the release. That is covered in
[Release automation](release-automation.md).

## Before you start

Releases follow one branch per minor version, named `M.m` (for example
`9.1`), and the release tags on it decide what each release contains.
Maintainers create the branch and push the tags. The agent never creates,
deletes or force-pushes a release branch. It only writes to its own branch,
`agent/release-cut/<version>-<stage>`, and opens a PR from there.

So before you cut a release, make sure the branch exists and the previous
release's tag has been pushed. Before cutting `9.1.0-rc2`, for instance, the
`9.1.0-rc1` tag needs to be on the `9.1` branch.

## Cutting a release

Run **Cut Release Notes** with four values:

- `repo`: `valkey`, `valkey-search`, `valkey-json` or `valkey-bloom`.
- `version`: the release you are cutting, always written as `M.m.p`. Every
  release candidate of 9.2.0 and its GA are all `9.2.0`; the `-rc2` part comes
  from `stage`.
- `stage`: `rc1`, `rc2` and so on, or `ga`. You only need it for a `.0`
  release, since `9.2.0` could be any of those. For a patch release like
  `9.1.9`, leave it empty and it means `ga`.
- `urgency`: `LOW`, `MODERATE`, `HIGH`, `CRITICAL` or `SECURITY`.

The agent works out the rest. The table shows which range of changes each
kind of release covers:

| You are cutting | `version` | `stage` | The notes cover changes since |
|---|---|---|---|
| The first RC of a new minor | `9.2.0` | `rc1` | the previous release, such as `9.1.0` |
| The next RC | `9.2.0` | `rc2` | `9.2.0-rc1` |
| GA after the last RC | `9.2.0` | `ga` | all RC notes, plus changes since the last RC |
| A patch release | `9.1.9` | | `9.1.8` |
| The first RC of a new major | `10.0.0` | `rc1` | the highest earlier release |

For the final `.0` release, the agent first generates notes for changes since
the last RC. It then uses AI to combine those notes with this version's RC
sections into one dated section, reconciling duplicates and superseded changes.
Every source entry must be accounted for, and code restores its authors and PR
references. Security entries retain their supplied wording. An incomplete or
invalid response is retried once. If it still fails validation, the cut stops
before publication. RC-only changes retain their affected names and qualifier.

The final section uses the version alone, such as `Valkey 9.2.0`, and ends with
a sentence explaining that it covers all release candidates. To compare RCs,
diff the notes files shipped with their tags. Patch releases prepend their own
dated sections above the combined `.0` notes.

By default the workflow does a dry run. It shows you the plan, including the
exact commit range the notes will cover, without pushing anything. Once that
looks right, run it again with `dry_run=false` to open the PR.

```bash
# Preview rc1 of 9.1.0
gh workflow run release-notes-cut.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey --field version=9.1.0 --field stage=rc1 --field urgency=LOW

# The next RC, for real this time
gh workflow run release-notes-cut.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey --field version=9.1.0 --field stage=rc2 --field urgency=LOW \
  --field dry_run=false

# GA after the final RC
gh workflow run release-notes-cut.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey --field version=9.1.0 --field stage=ga --field urgency=LOW

# A patch release
gh workflow run release-notes-cut.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey --field version=9.1.1 --field urgency=LOW

# A patch release of a module
gh workflow run release-notes-cut.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey-search --field version=1.2.2 --field urgency=LOW
```

### When you need more control

**Cut Release Notes (Advanced)** runs exactly the same job, with extra inputs
for the cases that need them: a specific release date, a different starting
point for the range or the contributor list, security fixes to list, reading
security fixes from the repository's published advisories, turning off
wording reuse across release lines, and opening the PR as ready even when
the agent wants to hold it.

You should rarely need to change where the range starts. If the dry run shows
the wrong range, that's the time to use it.

When no date is given, the release is dated today in UTC. If the release date
is a different calendar day where you are, set the date explicitly.

### When more changes land

If more PRs merge into the release branch while the release PR is open, run
the same `version` and `stage` again with `dry_run=false`. The agent
regenerates the whole release from scratch, through the latest commit on the
branch, and updates the open PR in place. It re-reads every PR, not just the
new ones, so changed labels are picked up too. Any edits made by hand directly
on the agent's branch are lost when this happens, so make changes through
review comments instead.

### Cutting several release lines at once

When the same fix ships on several release lines, it should be described the
same way on each. The agent does this by reusing the wording from release
lines whose release PR has already merged. That means the first line to
merge sets the wording for the others. If you cut several lines in parallel,
re-run the ones that are still open after the first one merges, and they will
pick up its wording.

## How the notes are put together

**Finding the changes.** The agent fetches the release branch once, pins its
latest commit, and walks every commit between the previous release and that
commit. Each commit is traced back to the PR it came from. A backport is
credited to the original PR on `unstable`, not to the backport PR, using the
backport's summary table, its cherry-pick or branch information, or a
`backport of <PR link>` line in its description.

**Deciding what goes in.** PRs labelled `release-notes` always go in. PRs
labelled `no-release-notes` never do, and the PR description lists them so a
wrong label can be spotted. If a PR has both labels, `no-release-notes` wins.
Every other PR is a judgement call, and Claude makes it: does this change
affect users, or is it internal? Each decision comes with a short reason, and
all of them are listed in the PR. For patch releases, Claude is told to
include a correctness or safety fix when in doubt.

Claude isn't the only check. Code independently looks for signs that a
change matters for the release, such as crashes, memory safety, data
corruption, access control, protocol or compatibility changes, in each PR's
title and description. If Claude leaves out a PR that shows those signs, or
doesn't give a verdict for it, the PR goes in anyway and is flagged for a
maintainer.

**Writing the notes.** Claude writes one line per PR, grouped into
categories, describing the change from a user's point of view. Code then
cleans up after it: it moves generic logging, metrics and INFO changes into
"Observability and Logging" and flags that it did so, and it adds the PR
number and author itself, so they are always right.

**Matching other release lines.** If another release line has already
published a note for the same PR, the agent uses that wording instead of the
new one, keeping its own category, credit and PR number. It only reuses notes
the agent itself wrote, so older hand-written sections are left alone. If
the two lines disagree about something more than wording, such as who wrote
the change or which platforms it affects, it doesn't reuse the other line's
note and holds the PR for a maintainer to decide. The PR lists everything that
was and wasn't reused.

**Updating the files.** The agent adds a new dated section to the top of
`00-RELEASENOTES`, refreshes the contributor list at the bottom, and bumps
the version file. It then commits to its branch, checks one last time that
the release branch hasn't moved since it started, pushes, and opens or updates
the PR.

## When the PR needs attention

If anything needs a maintainer's attention before release, the top of the PR
description says so, with a section for each item. A PR opened by **Prepare
Release** is always opened ready for review, so a release never waits on a
draft nobody noticed; the banner at the top lists what to check, and a
security item (a PR that names a CVE, or `SECURITY` urgency without Security
Fixes entries) gets a CAUTION banner that must be settled before the merge.
Give Prepare Release the `security_fixes` entries (separated by `;;`) and the
`urgency` to clear it, or edit the notes through review comments.

A PR opened by **Cut Release Notes** or its Advanced version is instead held
as a draft so it can't be merged by accident. You can fix the issue and re-run
the cut, decide it's fine and click **Ready for review**, or use the advanced
workflow's `force_ready` to open the PR as ready anyway.

The PR is held as a draft when:

- release candidates are out of order, either repeated or with a number
  skipped;
- the first RC of a new major version had to guess where its range starts;
- there are no notes at all, either because no PRs were found or because
  none were included;
- a PR is credited twice, an included PR has no note, or Claude was unsure
  about a note;
- Claude decided whether to include a PR that had no label, which a
  maintainer should confirm, or couldn't decide at all;
- a PR shows signs of being important to the release but was going to be left
  out, or the urgency is `LOW` or `MODERATE` despite those signs (the agent
  never sets urgency or severity itself);
- the urgency is `SECURITY` but no security fixes are listed, or the
  repository's advisories couldn't be read;
- a commit couldn't be traced to a PR, a PR couldn't be fetched, or a backport
  couldn't be traced to its original PR;
- two release lines disagree about the same change.

Some mistakes are caught before any work starts. A malformed version, a
missing stage for a `.0` release, an unknown stage or urgency, a bad date, a
release branch that doesn't exist, or a version that has already been
released all stop the run immediately with an error.

## Applying review comments

To change the notes, leave an inline comment on the `00-RELEASENOTES` change
in the release PR, the same way you would review any PR. You don't need a
command. Every release PR has its own watcher, **Watch Release PR Reviews**,
which the cut starts the moment it opens the PR. Each run holds one runner
for 55 minutes and checks that one PR every five minutes from inside it, so
GitHub's scheduler is never on the critical path. As soon as a run starts it
queues the next hour's run, which takes over the moment the current one ends,
so a failure in one run cannot break the chain. The watch ends on its own
once the PR is merged or closed. The Actions tab shows one run per PR, named
after it.

Two things bound this. A chain stops queueing itself once its PR is three
days old, so a forgotten PR does not hold a runner around the clock; start
the watcher by hand to keep going. And **Restart Release PR Watchers** runs
every six hours and starts a watcher for any open release PR younger than
that which has none, so a watcher whose run was lost comes back on its own.

It collects every unresolved comment on the current notes from members of
`valkey-io/contributors`, posts "Addressing N release-note review comments."
on the PR, and makes all the edits in one pass. Claude is only allowed to
change the current release's section of `00-RELEASENOTES`. The agent pushes
the edit as a normal commit, replies to each comment with the commit, and
resolves the threads that weren't changed in the meantime.

If something fails, the PR says so and the next check tries again. Editing a
comment counts as a new request. If a job dies halfway through, it will be
retried after 90 minutes at most. If the push went through but the replies
didn't, the next check finishes the replies without editing again.

To stop watching a PR, or to start watching one again (the watcher was
stopped, or its run was lost), run the workflow by hand:

```bash
# Stop applying review comments on valkey PR 4717
gh workflow run release-notes-review-watch.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey --field pr=4717 --field stop=true
```

```bash
# Start (or resume) watching valkey PR 4717
gh workflow run release-notes-review-watch.yml --repo valkey-io/valkey-ci-agent \
  --field repo=valkey --field pr=4717
```

## Per-repository details

Each repository keeps its version in a different place, and the agent knows
where to look:

| Repository | Version file | Records the stage |
|---|---|---|
| valkey | `src/version.h` (`VALKEY_VERSION` macros) | yes |
| valkey-search | `src/version.h` (`kModuleVersion` and `MODULE_RELEASE_STAGE`) | yes |
| valkey-json | `CMakeLists.txt` (`project(... VERSION M.m.p)`) | no |
| valkey-bloom | `Cargo.toml` (`[package] version`) | no |

When a repository doesn't record the stage, the tags are what stop a stage
from being released twice. Between releases, each repository's version file
holds a placeholder (`255.255.255-dev` for Valkey, `99.99.99` for
valkey-json, `99.99.99-dev` for valkey-bloom), which the agent replaces with
the real version when a new release line is cut. valkey-search's 1.0 line
keeps its version somewhere else and isn't supported.

The three modules need a one-time cleanup before their first automated
release. The contributor list at the end of `00-RELEASENOTES` has to become a
`### Contributors` heading followed by one `* Display Name @handle` per line.
Older dated sections can stay as they are.

## Configuration

The workflow uses the shared secrets described in [setup.md](setup.md). Each
run gets a token for just the repository being released, allowed to push a
branch and open a PR. A run that reads security advisories also asks for
permission to read them, so ordinary releases still work if the App doesn't
have that permission. The App has to be installed on all four repositories.

## Where the code lives

`scripts/release_notes/main.py` checks the inputs and clones the repository,
and `release_cut.py` runs the cut and decides whether to hold the PR.
`pipeline.py` runs the steps in order. `discover.py` and `backport_refs.py`
find the PRs, and `classify.py`, `triage.py` and `generate.py` decide what
goes in and write the notes. `prior_notes.py` reuses wording from other
release lines, reading only branches already in the clone. `render.py`,
`release_format.py`, `version_bump.py` and `contributors.py` update the files,
and `publish.py` opens or updates the PR. `projects.py` describes each
repository, and `security.py` reads published advisories, which Claude never
writes. The review comment flow is in `review_poll.py` (the per-PR watcher),
`review.py` and `review_handler.py`.

When Claude decides what to include and writes the notes, it has no tools at
all. Everything it needs about each PR is put directly into the prompt.
