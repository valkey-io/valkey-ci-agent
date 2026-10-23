# Fixing a failing Valkey test

Valkey documents its own tests. Read, in your checkout, whichever of these
exist before you change a test; they are the authority on helpers, tags and
conventions, and they match the branch you are on:

- `tests/README.md`
- `.github/instructions/integration-tests.instructions.md`
- `AGENTS.md`, `DEVELOPMENT_GUIDE.md`, `CONTRIBUTING.md`
- `tests/support/util.tcl`, `tests/support/test.tcl` and
  `tests/test_helper.tcl` for what a `wait_for_*` or `verify_*` helper
  actually does, and whether this branch has it: helpers and runtest options
  differ between release lines, so use only what this branch's `tests/`
  defines.

What follows is the bot's own policy, not a summary of those files.

## First decide what failed

- Product bug: the server misbehaved. Fix `src/`, not the test. Never make a
  test pass by weakening what it checks.
- Test bug: the test assumes a timing, an ordering, or a state the server does
  not guarantee. Fix the test, with the polling helpers the repository's docs
  describe rather than a fixed `after N`.
- Harness or environment: a port clash, an out-of-disk runner, a missing tool.
  Refuse; that is not a code fix.

A test that fails once in many runs is still a bug. Find the race in the log
(`--dump-logs` prints each server's log after a failure) before editing.
Valgrind and sanitizer builds are many times slower; scale a wait with
`$::valgrind` rather than hard-coding a longer one everywhere.

## Scope

- Keep the change minimal and easy to backport: release branches take the
  same change.
- Do not reorder or rename tests; other tests in a file may depend on state
  that earlier ones leave behind.
- A tag that skips a test is not a fix for a race.
- Do not change `.github/`.

## Running a test

The reproduction section of your briefing names the exact command the bot
used, with the job's own build; prefer it. In general:

```sh
make -j"$(nproc)"
./runtest --single unit/type/incr --only 'exact test name' --loops 20 --fastfail --dump-logs
```

`--only` is an exact name match (a leading `/` makes it a regexp). With
several clients (the default) loop copies run in parallel, and a `run_solo`
block runs inline, so loop a file with `run_solo` with `--clients 1`. Add
`--accurate` for what scheduled CI runs, `--valgrind --clients 1` after
`make valgrind`, `--tls` after `make BUILD_TLS=yes && ./utils/gen-test-certs.sh`.

A C unit test is a Google Test in `src/unit/test_*.cpp`, not a Tcl test:

```sh
make -j"$(nproc)" all-with-unit-tests
./src/unit/valkey-unit-gtests --gtest_filter='Suite.Test' --gtest_repeat=20
```

Release branches before the Google Test harness run their unit tests through
`./src/valkey-unit-tests` (its `--help` lists how to select one); older ones
through `./src/valkey-server test <name>`.
