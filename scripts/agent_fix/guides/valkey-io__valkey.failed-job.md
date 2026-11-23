# Fixing a failed CI job

A failed job is not always a product bug. Before the conventions below, which
hold for every change, decide what this failure is.

## First decide what failed

- Test bug: the test assumes a timing, an ordering, or a state the server does
  not guarantee. Fix the test, with the polling helpers the repository's docs
  describe rather than a fixed `after N`.
- Harness bug: the test framework itself races or mishandles a server
  (`tests/support/`, `tests/helpers/`, `tests/test_helper.tcl`). Fix it there.
- Environment: a port clash, an out-of-disk runner, a missing tool. Refuse;
  that is not a code fix.
- Product bug: the server broke something it promises. Fix `src/`. Never make
  a test pass by weakening what it checks.

Look for the test or harness fix first, and change a C file only when the fix
has to be there. Of eleven fixes people merged for failures like these, eight
changed only tests or the harness and two changed `src/`.

A C change is necessary when the log shows the server breaking a promise a
user could see (a crash, a wrong or lost reply, lost data, a state it never
leaves) and no correct test could pass without it. A server that is briefly
in a state the test did not expect, and gets out of it on its own, is not
that: fix the test to wait for the state it depends on, and say in
`validation` what the server did, so a maintainer can decide whether it needs
a change of its own.

When a C change is necessary, make the smallest one, the way earlier fixes to
that code were made: the test's history and the existing fixes you are given
show them. Say why no test change would be right.

A test that fails once in many runs is still a bug. Find the race in the log
(`--dump-logs` prints each server's log after a failure) before editing.
Valgrind and sanitizer builds are many times slower; scale a wait with
`$::valgrind` rather than hard-coding a longer one everywhere.
