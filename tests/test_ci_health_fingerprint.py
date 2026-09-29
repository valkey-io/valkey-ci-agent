"""The ci_health fingerprint must be byte-identical to the detector's."""

from __future__ import annotations

from scripts.ci_health.fingerprint import (
    UNKNOWN_FILE,
    fingerprint_for_name,
    fingerprint_for_pair,
    split_display_name,
)
from scripts.test_failure_detector.issue_renderer import fingerprint_for
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
        test, file = split_display_name(name)
        assert file == "tests/unit/cluster/slot-migration.tcl"
        assert test.endswith("in source shard")

    def test_spawn_timeout_is_attributed_to_its_file(self) -> None:
        assert split_display_name("spawn timeout in tests/integration/repl-compression.tcl") == (
            "spawn timeout",
            "tests/integration/repl-compression.tcl",
        )

    def test_job_level_failure_gets_unknown_file(self) -> None:
        name = "test-valgrind-test (unit): unattributed failure"
        assert split_display_name(name) == (name, UNKNOWN_FILE)

    def test_exception_line_gets_unknown_file(self) -> None:
        name = "Exception: Executing test client: I/O error reading reply."
        assert split_display_name(name) == (name, UNKNOWN_FILE)


class TestDetectorParity:
    """A scorecard row and a [TEST-FAILURE] issue must agree on the key."""

    def test_pair_matches_detector_fingerprint(self) -> None:
        failure = UniqueFailure(
            test_name="INCREX BYFLOAT arithmetic overflow does not propagate",
            test_file="tests/integration/replication.tcl",
        )
        assert fingerprint_for_pair(failure.test_name, failure.test_file) == fingerprint_for(failure)

    def test_display_name_matches_detector_fingerprint(self) -> None:
        failure = UniqueFailure(
            test_name="modify new keys during forkless bgsave",
            test_file="tests/integration/rdb.tcl",
        )
        display = f"{failure.test_name} in {failure.test_file}"
        assert fingerprint_for_name(display) == fingerprint_for(failure)

    def test_unknown_file_matches_runner_fallback(self) -> None:
        # tests/test_helper.tcl writes test_file="unknown" for an unparsable
        # line; a detector fed the same record must land on this key.
        failure = UniqueFailure(test_name="test-valgrind-misc: unattributed failure", test_file="unknown")
        assert fingerprint_for_name(failure.test_name) == fingerprint_for(failure)

    def test_name_and_file_swap_do_not_collide(self) -> None:
        a = fingerprint_for_pair("alpha", "tests/unit/b.tcl")
        b = fingerprint_for_pair("tests/unit/b.tcl", "alpha")
        assert a != b

    def test_digits_are_significant(self) -> None:
        # PSYNC2 vs PSYNC3 must stay distinct: the pair is namespace, never
        # volatile-normalized.
        assert fingerprint_for_pair("PSYNC2 #3899 regression", "tests/integration/psync2.tcl") != fingerprint_for_pair(
            "PSYNC3 #3899 regression", "tests/integration/psync2.tcl"
        )

    def test_shape_is_20_hex_chars(self) -> None:
        fp = fingerprint_for_name("spawn timeout in tests/integration/aof-multi-part.tcl")
        assert len(fp) == 20 and int(fp, 16) >= 0
