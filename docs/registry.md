# The registry: repos.yml

`repos.yml`, at the root of this repository, lists the repositories the
backport workflows look after: their release branches, the project board for
each branch, and how a backport is built and tested. The CI fix bot and the
Test Failure Detector also read it to learn which CI jobs to ignore.

To add a repository or a release branch, edit `repos.yml` and open a PR.
[`examples/repos.yml`](../examples/repos.yml) shows a setup with several
modules.

## An example

```yaml
repos:
  - repo: valkey-io/valkey
    project_owner: valkey-io
    project_owner_type: organization
    language: c
    validation_setup_commands:
      - "./ci/setup-backport-validation.sh"
    build_commands:
      - "make -j$(nproc)"
    validation_profile: valkey-core
    validation_rules:
      - paths: ["src/rdb.c"]
        commands: ["./runtest --single unit/dump --clients 1"]
    generated_file_rules:
      - paths: ["src/unit/*.c"]
        command: "python3 utils/generate-unit-test-header.py"
        outputs: ["src/unit/test_files.h"]
    repair_validation_failures: true
    automatic_ci_followup: true
    ci_followup_ignored_jobs: ["*dco*", "code-coverage*"]
    ci_followup_release_workflow: weekly.yml
    branches:
      - branch: "8.1"
        project_number: 14
      - branch: "9.0"
        project_number: 18
```

## The basics

Every entry needs a `repo` (such as `valkey-io/valkey`), a `project_owner`
for its project boards, a `language`, and a list of `branches`. Each branch
names a release branch and the number of the GitHub Project board where
maintainers queue backports for it. `project_owner_type` is `organization`
unless the boards belong to a user. The `language` is mentioned to Claude
when it resolves conflicts.

The agent pushes its branches to the repository itself and opens pull
requests there. If you really need it to push to a fork owned by someone else,
set `push_repo` to that fork; the agent will keep the fork's release branch in
step with upstream before it cherry-picks. A fork with the same owner isn't
allowed, to stop staging copies from becoming the normal way of working.

## How backports are checked

These settings decide what happens after each cherry-pick. They run in the
order they're listed here.

`validation_setup_commands` run once, after the repository is cloned. Use
them to install tools or generate test certificates.

`build_commands` run before anything is pushed. If one fails, the push is
blocked. A repository with no build commands relies entirely on its own CI.

`validation_rules` run extra commands when a backport touches certain paths.
Each rule has a list of `paths` and the `commands` to run.

`validation_profile` names a built-in profile that chooses checks from
exactly what a backport changed. Today there is one, `valkey-core`, described
in [Backports](backport.md#what-gets-checked-before-a-backport-is-kept).

`generated_file_rules` keep generated files up to date. When a backport
changes one of the `paths`, the agent runs the `command` twice, checks the
result is the same both times, and adds the changes to the backport commit.
The command may only change the files listed in `outputs`. If it changes
anything else, or gives a different result the second time, the backport is
stopped.

`repair_validation_failures`, off by default, gives Claude one chance to
repair a backport that failed its checks, and lets it port a test that the
older branch can't run. `test_path_patterns` tells it which files are tests,
so it knows not to delete them. The default covers Valkey's layout:
`tests/*.tcl` and `src/unit/test_*.c`, `.cc` and `.cpp`.

## Following up on CI

`automatic_ci_followup`, off by default, turns on the hourly
[CI follow-up](backport.md#when-a-backport-prs-own-ci-fails), which sends
failed jobs on the bot's own PRs to the CI fix bot.

`ci_followup_ignored_jobs` is a list of job name patterns that the CI
follow-up, the CI fix bot and the Test Failure Detector all skip. List the
jobs that only report on other jobs, which would otherwise look like failures
the bot should fix. For Valkey these are Daily CI's two result aggregators and
the coverage upload. The patterns only match GitHub Actions jobs. Checks from
other GitHub Apps, such as DCO and Codecov, are never considered in the first
place, so patterns for them are only a precaution.

`ci_followup_release_workflow` names a scheduled workflow that runs the tests
of every release branch, as Valkey's `weekly.yml` does by calling `daily.yml`
once a branch. When it is set, a release branch's failures in the newest such
run are [followed up on that branch's sweep
PR](backport.md#when-a-backport-prs-own-ci-fails). Leave it out to follow up
only the PRs' own CI.

## Smaller settings

| Setting | Default | What it does |
|---|---|---|
| `backport_label` | `backport` | The label put on backport PRs. |
| `llm_conflict_label` | `ai-resolved-conflicts` | The label added when Claude resolved a conflict. |
| `max_conflicting_files` | `100` | A backport that conflicts in more files than this is skipped. |

## Adding a release branch

Add the branch and its project board number to `branches`. For Valkey,
[release automation](release-automation.md) opens an issue and a PR to do
this whenever a new minor or major version is released.

After editing `repos.yml`, you can check that it loads and see which
branches the sweep would cover:

```bash
python -m scripts.backport.matrix --registry repos.yml
```
