# Release automation

Release automation takes a Valkey release from "let's prepare 9.1.1" to a
tagged, published GitHub release. Maintainers make four decisions along the
way, and the automation handles everything between them. It keeps no state
of its own: at every step it looks at what is actually true on GitHub, such
as which PR merged, which commit is at the head of the branch, and which runs
passed, and acts on that.

## What a maintainer does

1. Run **Prepare Release** from this repository's Actions tab. Give it the
   release line (such as `9.1`), the kind of release (`rc`, `ga` or `patch`),
   and the urgency. When a fix in the release names a CVE, also give it the
   Security Fixes entries (separated by `;;`); the release date is today in
   UTC unless you set one.
2. Review and merge the release notes PR it opens. It opens ready for review,
   with everything that needs a decision listed at the top. Leave inline
   comments on the notes to have the agent edit them.
3. Approve the `release` environment when publication asks for it.
4. Approve the `release-publish` environment in `valkey-release-automation`.

The agent opens a tracking issue called `Release <tag>` as soon as you start.
That issue always shows where the release is, links the current run, and says
what to do next if something fails.

## What happens in between

**Preparing.** Prepare Release first checks that you are a member of
`valkey-io/valkey-committers` or `valkey-io/valkey-release`. It works out the
next version from the release branch and its tags, refuses a patch or a
further release candidate when the branch has no commits since the previous
one (merge the pending backports first; promoting the last candidate to GA
unchanged is fine), opens the tracking issue, and runs the
[release notes cut](release-notes.md), which opens the notes PR. If you only
want to see which version it would prepare, run it with `dry_run`.

**Waiting for the merge.** A watcher runs every hour and checks every five
minutes for most of that hour. It updates the tracking issue from what it
sees. Once the notes PR has merged, and the merge commit is still the latest
commit on the release branch, it starts **Publish Release** for that exact
commit, once.

**Publishing.** Publish Release checks the commit again: it has to be the
latest commit on the branch and the merge commit of the notes PR. It checks
that `version.h` and the release notes agree with the release, that the tag
doesn't already exist, whether the release should be marked as the latest one,
and what the branch's CI says about that commit. It then
asks `valkey-release-automation` to qualify the release, and waits for that to
pass. Only then does it ask for approval in the protected `release`
environment.

After approval it repeats every check, makes sure the person who approved is
still on one of the two teams, and confirms that nothing about the planned
release has changed since it was approved. Then it creates the tag on that
exact commit and publishes the GitHub release.

**Production.** Publishing the release starts the production workflow in
`valkey-release-automation`, which asks for its own `release-publish`
approval before it uploads anything public. It builds and publishes the
archives, RPM and DEB packages, hashes, container images, documentation,
website, Try Valkey and the Bundle. For a GA release it also opens a Helm
chart update as a draft, which a maintainer marks ready once the matching
container image is public. The tracking issue links to each of these.

For a new minor or major release (`X.Y.0`), the agent also opens an issue and
a PR to add the new release line to the backport registry.

## When something goes wrong

The watcher never tries to fix anything, and it never decides something
succeeded because of what the issue says. If the release branch moves after
the notes PR merged, automatic publication stops and the issue asks for a new
notes PR: revert the release-notes commit with a PR, then rerun Prepare
Release. If a build or the qualification fails, the issue links the failed
run and says what to do next. You fix the problem in the repository and run
the right workflow again.

The tracking issue is for people to read. Its status comment, which only the
bot writes, is what the automation relies on. Editing the issue body or
ticking its checkboxes can't approve a release, choose a commit, or skip a
check.

### Abandoning a release

Close the tracking issue. The watcher only looks at open issues, so closing it
stops everything for that release, including any new publication.

If you need to stop all release activity while you look into a problem,
disable the **Refresh Release Progress** workflow in this repository. Close
every abandoned tracking issue before you enable it again.

## Release day

The automation handles each release on its own, but a few things are worth
settling before you start, especially when several release lines go out at
once:

- **Merge the backport sweeps first.** The notes PR has to be the last merge
  on the release branch: anything merged after it invalidates the candidate,
  and the tracker will ask for a new Prepare Release. Merging into the branch
  while the cut is running fails the cut.
- **Freeze `main` in this repository and in `valkey-release-automation`**
  until the last release is published. Publication refuses to run if this
  repository's `main` moved after the Publish run started, and the watcher
  then cancels and re-qualifies; production refuses in the same way when the
  automation repository moved.
- **Approve production runs one at a time.** Wait for the previous run's
  "Publish to S3" job to finish before approving the next `release-publish`
  request. The package publish and the hashes update are serialized through
  shared resources that are not safe under several concurrent runs.
- **Rehearse what has not run before.** Qualification can be run by hand in
  `valkey-release-automation` against the previous tag of each line; it
  publishes nothing. **Cut Release Notes** defaults to a dry run and shows
  what each line's notes would contain.
- **Expect hours, not minutes.** Qualification and production together run
  hundreds of jobs per release; the production run also waits for Docker Hub
  before it triggers the Bundle.
- **Release PRs that were already open** when a new version of this
  automation landed have no review watcher yet. Start one by hand for each
  (see [Release notes](release-notes.md#applying-review-comments)), or wait
  for the six-hourly restart to do it.

## Setting it up securely

Only release maintainers should be able to run the release workflows. Set the
repository's GitHub Actions policy so that only they can, and the team check
inside the workflow will act as a second layer.

Three environments guard the credentials:

- `release-control` holds the token the agent uses to prepare and track
  releases. It must only allow the default branch.
- `release` holds the token used to create the tag and publish. It must only
  allow the default branch, require a reviewer, stop people approving their
  own runs, and not let admins bypass it.
- `release-publish`, in `valkey-release-automation`, guards the production
  uploads.

A GitHub environment only needs one of its listed reviewers to approve, so
the two approvals come from two separate environments, not from two names on
one.

Release tags are protected by a tag ruleset. Before publishing, the workflow
checks that the ruleset is active and stops anyone from creating, changing or
deleting release tags. The publication App must be the only actor allowed to
bypass it. The workflow can't check that list itself, because GitHub only
shows it to tokens that could also edit the ruleset, and an unattended
workflow shouldn't hold that power. An admin needs to keep that list correct.

Today one GitHub App, the Valkeyrie Bot, does both preparation and
publication, using the `VALKEYRIE_BOT_APP_ID` and `VALKEYRIE_BOT_PRIVATE_KEY`
secrets. To give publication its own App, create it, store its ID and key as
secrets with the same names inside the `release` environment (environment
secrets take precedence over repository secrets), and make it the ruleset's
bypass actor instead. No code needs to change.

The permissions each repository needs are listed in
[setup.md](setup.md#github-app-permissions). On `valkey`, the publication App
needs to read administration, actions, checks and pull requests, write
contents, and read organization members.

Qualification is a reusable workflow in `valkey-release-automation` that the
publish job calls directly and waits for. It needs no secrets and no
permission to start workflows in another repository. For that to work,
`valkey-release-automation` has to be deployed first, with its
`qualify-release.yml` callable as a reusable workflow, and this repository's
Actions policy has to allow reusable workflows from `valkey-io`.

## Where the code lives

`scripts/release/main.py` is the command-line entry point used by the
workflows. `authorize.py` checks team membership, `versioning.py` works out
the next version, `tracker.py` keeps the tracking issue up to date,
`checks.py` validates the release, and `publish.py` builds the release plan
and publishes it. `policy.py` reads `release_policy.yml`, which lists the
teams allowed to release, the release branches, and the CI checks the
tracking issue reports on. Adding a new release line to the backport registry
is handled by `scripts/backport/onboarding.py`.
