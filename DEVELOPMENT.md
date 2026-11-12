# Working on valkey-ci-agent

This guide is for changing the agent itself: setting up a local environment,
running the tests, and adding a new workflow. To learn what each workflow
does and how to run it, start from the [README](README.md).

## What you need

- Python 3.11 or later, which CI tests, lints and type checks on.
- The GitHub CLI, `gh`, to start workflows.
- A GitHub token, for anything that talks to the GitHub API.
- AWS credentials with access to Bedrock, for anything that calls Claude.
- Claude Code CLI 2.1.286 or later, for anything that calls Claude: earlier
  versions refuse Fable 5.2. The workflows use 2.1.300:

```bash
npm install -g @anthropic-ai/claude-code@2.1.300
```

## Getting set up

Install the agent and its development tools:

```bash
python -m pip install -e ".[dev]"
```

If you want to run anything that talks to GitHub or AWS, copy the
environment template, fill in what you need, and load it into your shell:

```bash
cp .env.example .env.local
```

```bash
set -a; source .env.local; set +a
```

Locally, it's easiest to use an `AWS_PROFILE` and `AWS_REGION`. In GitHub
Actions the workflows get AWS credentials through OIDC instead; see
[docs/setup.md](docs/setup.md).

## Running the tests

```bash
python -m pytest
```

Before you push, run the same checks CI does:

```bash
ruff check scripts/ tests/
pytest --cov=scripts --cov-report=term-missing --cov-fail-under=60
mypy scripts/
```

Some tests only run when there is a real `ci-fix-agent` account and Claude
Code is installed, and skip otherwise. CI sets both up on its Python 3.11
job, and runs the isolation tests a second time as root inside an Alpine
container, so those tests always run there.

Tests live in `tests/`, with one file per module. The tests for
`scripts/ci_fix/engine.py`, for example, are in `tests/test_ci_fix_engine.py`.
Shared git helpers are in `tests/git_helpers.py` and test data is under
`tests/fixtures/`.

## Commits

Every commit needs a DCO sign-off:

```bash
git commit -s
```

## Trying a workflow

Each workflow's page ends with the commands to run it, both as a GitHub
workflow and, where it's useful, on your own machine:
[Backports](docs/backport.md#running-things-by-hand),
[Fixing CI failures](docs/ci-fix.md#running-it-by-hand),
[Test Failure Detector](docs/test-failure-detector.md#running-it-by-hand),
[Fuzzer monitor](docs/fuzzer-monitor.md#running-it-by-hand),
[Release notes](docs/release-notes.md#cutting-a-release) and
[CVE scan](docs/cve-scan.md#running-it-by-hand).

Be careful with anything that can push or open a PR. Most commands have a
`--dry-run` or `--discover-only` flag, and most workflows default to a dry run
when you start them by hand. When you do run something for real, make sure
the repository and token are the ones you mean.

## Changing repos.yml

[The registry page](docs/registry.md) explains every setting. After a change,
check that the file still loads:

```bash
python -m scripts.backport.matrix --registry repos.yml
```

## Adding a workflow

New workflows follow the same pattern as the existing ones, described in
[Architecture](docs/architecture.md#the-shape-of-a-workflow).

1. Add a package under `scripts/` with a `main.py`. Keep the part that
   receives and checks a request, the part that does the work, and the part
   that publishes the result separate.
2. Use what's already in `scripts/common/` for git credentials, cloning,
   GitHub API retries, artifacts, issues and polling.
3. If the workflow calls Claude, add a profile in `scripts/ai/runtime.py` with
   as few tools as the task allows, and keep the prompt in your package.
   Never give Claude a GitHub token.
4. Add the workflow file under `.github/workflows/`. Check out this
   repository and use `.github/actions/setup-agent`. Pin every external action
   to a commit and ask only for the permissions the job needs; the tests will
   tell you if you don't.
5. Write only to the bot's own branches, check the destination again right
   before pushing, and leave merging to people.
6. Add tests, a page under `docs/` written like the others, and a paragraph in
   the README.
