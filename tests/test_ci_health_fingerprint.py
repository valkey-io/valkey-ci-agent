"""The ci_health fingerprint must be byte-identical to the detector's.

The detector has two keys: ``[TEST-FAILURE]`` over ``(test_name, test_file)``
and ``[JOB-FAILURE]`` over the runtest cause line. ci_health must land on
exactly those for the failures the detector files, and on something that is
provably neither for the failures it does not.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from scripts.ci_health.fingerprint import (
    LOCAL_KIND,
    fingerprint_for_cause,
    fingerprint_for_name,
    fingerprint_for_test,
    split_display_name,
)
from scripts.common.incidents import compute_fingerprint
from scripts.test_failure_detector.issue_renderer import MARKER_NAMESPACE, fingerprint_for, job_fingerprint_for
from scripts.test_failure_detector.job_failures import find_unrecorded_failures
from scripts.test_failure_detector.parse_failures import UniqueFailure


class TestSplitDisplayName:
    def test_attributed_failure_splits_on_trailing_in_file(self) -> None:
        name = "WATCHed key in another slot that expired aborts EXEC in tests/unit/cluster/misc.tcl"
        assert split_display_name(name) == (
            "WATCHed key in another slot that expired aborts EXEC",
            "tests/unit/cluster/misc.tcl",
        )

    def test_inner_in_does_not_split_early(self) -> None:
        # The test name itself contains " in "; only the trailing file wins.
        name = "Migration source is auto-updated after failover in source shard in tests/unit/cluster/slot-migration.tcl"
        pair = split_display_name(name)
        assert pair is not None
        assert pair[1] == "tests/unit/cluster/slot-migration.tcl"
        assert pair[0].endswith("in source shard")

    def test_spawn_timeout_is_attributed_to_its_file(self) -> None:
        assert split_display_name("spawn timeout in tests/integration/repl-compression.tcl") == (
            "spawn timeout",
            "tests/integration/repl-compression.tcl",
        )

    def test_client_state_folds_like_the_detector(self) -> None:
        # A test client that times out outside a test is named by its state,
        # which changes every run; the detector folds it to one stable name.
        a = split_display_name("pid:74389 in tests/unit/type/list.tcl")
        b = split_display_name("port 21581 in tests/unit/type/list.tcl")
        assert a == b
        assert a is not None and a[1] == "tests/unit/type/list.tcl"

    def test_job_level_bucket_is_not_attributed(self) -> None:
        assert split_display_name("test-valgrind-test (unit): unattributed failure") is None

    def test_exception_line_is_not_attributed(self) -> None:
        assert split_display_name("Exception: Executing test client: I/O error reading reply.") is None


class TestDetectorParity:
    """A scorecard row and a [TEST-FAILURE] issue must agree on the key."""

    def test_pair_matches_detector_fingerprint(self) -> None:
        failure = UniqueFailure(
            test_name="INCREX BYFLOAT arithmetic overflow does not propagate",
            test_file="tests/integration/replication.tcl",
        )
        assert fingerprint_for_test(failure.test_name, failure.test_file) == fingerprint_for(failure)

    def test_display_name_matches_detector_fingerprint(self) -> None:
        failure = UniqueFailure(
            test_name="modify new keys during forkless bgsave",
            test_file="tests/integration/rdb.tcl",
        )
        assert fingerprint_for_name(failure.display_name) == fingerprint_for(failure)

    def test_name_and_file_swap_do_not_collide(self) -> None:
        a = fingerprint_for_test("alpha", "tests/unit/b.tcl")
        b = fingerprint_for_test("tests/unit/b.tcl", "alpha")
        assert a != b

    def test_digits_are_significant(self) -> None:
        # PSYNC2 vs PSYNC3 must stay distinct: the pair is namespace, never
        # volatile-normalized.
        assert fingerprint_for_test("PSYNC2 #3899 regression", "tests/integration/psync2.tcl") != fingerprint_for_test(
            "PSYNC3 #3899 regression", "tests/integration/psync2.tcl"
        )

    def test_shape_is_20_hex_chars(self) -> None:
        fp = fingerprint_for_name("spawn timeout in tests/integration/aof-multi-part.tcl")
        assert len(fp) == 20 and int(fp, 16) >= 0


_VALGRIND_LOG = (
    "2026-10-01T04:10:00.0000000Z !!! WARNING The following tests failed:\n"
    "2026-10-01T04:10:00.0000000Z *** [err]: Valgrind error: ==4321== Memcheck, a memory error detector\n"
    "2026-10-01T04:10:00.0000000Z ==4321== Invalid read of size 8\n"
    "2026-10-01T04:10:00.0000000Z ==4321==    at 0x4A1B2C: hashtableFind (hashtable.c:1201)\n"
    "2026-10-01T04:10:00.0000000Z ==4321==    by 0x4B2C3D: dbFind (db.c:88)\n"
    "2026-10-01T04:10:00.0000000Z ==4321==    by 0x4C3D4E: lookupKey (db.c:140)\n"
)


def _detector_job_failures(log: str, *jobs: str):
    client = MagicMock()
    client.download_job_log.side_effect = lambda _repo, _job_id: log
    found = [
        SimpleNamespace(id=i, name=name, conclusion="failure", html_url=f"https://job/{i}",
                        steps=[SimpleNamespace(name="test", conclusion="failure")])
        for i, name in enumerate(jobs, start=1)
    ]
    _tests, job_failures = find_unrecorded_failures(client, "valkey-io/valkey", {}, jobs=found)
    return job_failures


class TestJobFailureParity:
    """A cause line and a [JOB-FAILURE] issue must agree on the key."""

    def test_cause_matches_detector_fingerprint(self) -> None:
        # Run the detector's own reader so the cause line is the one it
        # builds, not one written here by hand.
        (failure,) = _detector_job_failures(_VALGRIND_LOG, "test-valgrind-test (unit)")
        assert failure.summary[0].startswith("[err]: Valgrind error: Invalid read of size")
        assert fingerprint_for_cause(failure.summary[0]) == job_fingerprint_for(failure)

    def test_cause_key_ignores_job_and_step(self) -> None:
        # One cause in two jobs is one detector issue; the cause alone must
        # reach it.
        (failure,) = _detector_job_failures(
            _VALGRIND_LOG, "test-valgrind-test (unit)", "test-valgrind-no-malloc-usable-size-test (unit)"
        )
        assert len(failure.jobs) == 2
        assert fingerprint_for_cause(failure.summary[0]) == job_fingerprint_for(failure)

    def test_volatile_parts_of_a_cause_are_normalized(self) -> None:
        # The cause is a shape: pids and addresses change every run.
        a = fingerprint_for_cause("[exception]: I/O error reading reply (pid 4321) at tests/unit/x.tcl")
        b = fingerprint_for_cause("[exception]: I/O error reading reply (pid 9876) at tests/unit/x.tcl")
        assert a == b


class TestLocalIds:
    """Names the detector files nothing for get an id that is provably not a detector id."""

    def test_job_level_bucket_gets_local_id(self) -> None:
        name = "test-valgrind-test (unit): unattributed failure"
        assert fingerprint_for_name(name) == compute_fingerprint(
            namespace=(MARKER_NAMESPACE, LOCAL_KIND, name), shapes=()
        )

    def test_local_id_is_not_a_test_fingerprint(self) -> None:
        name = "test-valgrind-test (unit): unattributed failure"
        # The old scheme hashed this as a test with test_file="unknown"; that
        # key no longer exists on either side.
        assert fingerprint_for_name(name) != fingerprint_for(UniqueFailure(test_name=name, test_file="unknown"))

    def test_local_id_is_not_a_job_fingerprint(self) -> None:
        name = "Exception: Executing test client: I/O error reading reply."
        assert fingerprint_for_name(name) != fingerprint_for_cause(name)

    def test_local_id_is_stable_and_distinct_per_name(self) -> None:
        a = fingerprint_for_name("test-ubuntu-tls: server crash")
        assert a == fingerprint_for_name("test-ubuntu-tls: server crash")
        assert a != fingerprint_for_name("test-ubuntu-io-threads: server crash")
