from __future__ import annotations

from scripts.common.incidents import compute_fingerprint


def test_same_inputs_same_fingerprint():
    kwargs = dict(
        namespace=("valkey-io/valkey-fuzzer", "fuzzer-run.yml", "split-brain"),
        shapes=["Split-brain:detected"],
    )
    assert compute_fingerprint(**kwargs) == compute_fingerprint(**kwargs)


def test_different_namespaces_differ():
    fp1 = compute_fingerprint(namespace=("r", "w", "split-brain"), shapes=["a:x"])
    fp2 = compute_fingerprint(namespace=("r", "w", "crash"), shapes=["b:y"])
    assert fp1 != fp2


def test_volatile_substrings_normalized_before_slice():
    """Normalization happens before sort/slice so volatile addresses, node IDs,
    and run-specific numbers don't change which shapes survive the cap."""
    fp1 = compute_fingerprint(
        namespace=("r", "w", "crash"),
        shapes=[
            "crash:node-1 at 0xaaa",
            "crash:node-2 at 0xbbb",
            "timeout:after 120 seconds",
        ],
    )
    fp2 = compute_fingerprint(
        namespace=("r", "w", "crash"),
        shapes=[
            "crash:node-9 at 0xdeadbeef",
            "crash:node-10 at 0xfeedface",
            "timeout:after 999 seconds",
        ],
    )
    assert fp1 == fp2


def test_max_shapes_caps_input():
    """Only the first `max_shapes` distinct normalized shapes affect the
    fingerprint. Later additions that produce a new shape *do* change it
    (this test pins the cap-after-sort behavior)."""
    # 9 distinct shapes after normalization. With max_shapes=8, the 9th
    # (sorted last) is dropped — so adding a 10th that sorts after must
    # not change the fingerprint.
    shapes_a = [f"alpha{c}:x" for c in "abcdefghi"]   # 9 shapes
    shapes_b = shapes_a + ["zzz_extra:x"]             # adds a shape that sorts last
    fp_a = compute_fingerprint(namespace=("r",), shapes=shapes_a, max_shapes=8)
    fp_b = compute_fingerprint(namespace=("r",), shapes=shapes_b, max_shapes=8)
    assert fp_a == fp_b
