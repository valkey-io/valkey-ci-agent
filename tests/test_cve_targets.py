"""Tests for scripts/cve_scan/targets.py.

Covers the PINNED contract: encode/decode round-trip, strict rejection of
malformed input (never a silent empty list), line/variant derivation, and the
verify_matrix dedupe policy (one entry per distinct (line, variant, platform),
so every affected architecture is verified).
"""

from __future__ import annotations

import base64
import json

import pytest

from scripts.cve_scan.targets import (
    Target,
    TargetDecodeError,
    decode_targets,
    encode_targets,
    line_variant_from_image,
    verify_matrix,
)


def _target(
    *,
    image: str = "valkey/valkey:8.0-alpine",
    line: str = "8.0",
    variant: str = "alpine",
    platform: str = "linux/amd64",
    cve: str = "CVE-2024-1234",
    package: str = "openssl",
    fixed_version: str = "3.0.13-r0",
) -> Target:
    return Target(
        image=image,
        line=line,
        variant=variant,
        platform=platform,
        cve=cve,
        package=package,
        fixed_version=fixed_version,
    )


def _b64(obj: object) -> str:
    """Base64 of an arbitrary JSON object (for crafting malformed contracts)."""
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


class TestRoundTrip:
    def test_single_target_round_trip(self) -> None:
        targets = [_target()]
        assert decode_targets(encode_targets(targets)) == targets

    def test_multiple_targets_round_trip_preserves_order(self) -> None:
        targets = [
            _target(cve="CVE-2024-0001", package="openssl"),
            _target(
                image="valkey/valkey:9.1",
                line="9.1",
                variant="debian",
                platform="linux/arm64",
                cve="CVE-2024-0002",
                package="zlib1g",
                fixed_version="1:1.3.dfsg-3",
            ),
        ]
        decoded = decode_targets(encode_targets(targets))
        assert decoded == targets

    def test_empty_list_round_trips_to_empty(self) -> None:
        assert decode_targets(encode_targets([])) == []

    def test_encoded_is_base64_of_exact_contract_keys(self) -> None:
        raw = base64.b64decode(encode_targets([_target()]))
        payload = json.loads(raw)
        assert isinstance(payload, list)
        assert set(payload[0]) == {
            "image", "line", "variant", "platform", "cve", "package", "fixed_version",
        }


class TestDecodeRejectsMalformed:
    def test_bad_base64_raises(self) -> None:
        with pytest.raises(TargetDecodeError, match="not valid base64"):
            decode_targets("this is !!! not base64 @@@")

    def test_valid_base64_but_not_json_raises(self) -> None:
        blob = base64.b64encode(b"not json at all {{{").decode("ascii")
        with pytest.raises(TargetDecodeError, match="not valid JSON"):
            decode_targets(blob)

    def test_non_list_payload_raises(self) -> None:
        with pytest.raises(TargetDecodeError, match="must be a JSON list"):
            decode_targets(_b64({"image": "x"}))

    def test_entry_not_object_raises(self) -> None:
        with pytest.raises(TargetDecodeError, match=r"targets\[0\] must be an object"):
            decode_targets(_b64(["just a string"]))

    def test_missing_key_raises(self) -> None:
        entry = {
            "image": "valkey/valkey:8.0-alpine",
            "line": "8.0",
            "variant": "alpine",
            "platform": "linux/amd64",
            "cve": "CVE-2024-1234",
            # 'package' missing
            "fixed_version": "3.0.13-r0",
        }
        with pytest.raises(TargetDecodeError, match="missing=\\['package'\\]"):
            decode_targets(_b64([entry]))

    def test_extra_key_raises(self) -> None:
        entry = {
            "image": "valkey/valkey:8.0-alpine",
            "line": "8.0",
            "variant": "alpine",
            "platform": "linux/amd64",
            "cve": "CVE-2024-1234",
            "package": "openssl",
            "fixed_version": "3.0.13-r0",
            "severity": "HIGH",  # extra
        }
        with pytest.raises(TargetDecodeError, match="extra=\\['severity'\\]"):
            decode_targets(_b64([entry]))

    def test_non_string_value_raises(self) -> None:
        entry = {
            "image": "valkey/valkey:8.0-alpine",
            "line": "8.0",
            "variant": "alpine",
            "platform": "linux/amd64",
            "cve": "CVE-2024-1234",
            "package": "openssl",
            "fixed_version": 3013,  # not a string
        }
        with pytest.raises(TargetDecodeError, match=r"targets\[0\].fixed_version must be a string"):
            decode_targets(_b64([entry]))

    def test_non_string_input_raises(self) -> None:
        with pytest.raises(TargetDecodeError, match="must be a base64 string"):
            decode_targets(None)  # type: ignore[arg-type]

    def test_malformed_never_returns_empty_list(self) -> None:
        """A broken contract must raise, never silently decode to []."""
        for bad in ("@@@bad", _b64({"not": "a list"}), _b64([{"image": "x"}])):
            with pytest.raises(TargetDecodeError):
                decode_targets(bad)


class TestLineVariantFromImage:
    def test_debian_bare_tag(self) -> None:
        assert line_variant_from_image("valkey/valkey:8.0") == ("8.0", "debian")

    def test_alpine_suffix_stripped(self) -> None:
        assert line_variant_from_image("valkey/valkey:8.0-alpine") == ("8.0", "alpine")

    def test_only_trailing_alpine_suffix_stripped(self) -> None:
        """A line that contains 'alpine' mid-string keeps it; only the suffix goes."""
        assert line_variant_from_image("valkey/valkey:alpine-test") == (
            "alpine-test",
            "debian",
        )

    def test_no_colon_uses_whole_ref_as_tag(self) -> None:
        assert line_variant_from_image("localbuild-alpine") == ("localbuild", "alpine")


class TestVerifyMatrix:
    def test_single_line_variant_platform_yields_one_entry(self) -> None:
        """Findings on one (line, variant, platform) collapse to a single entry."""
        targets = [
            _target(line="8.0", variant="alpine", cve="CVE-1", package="openssl"),
            _target(line="8.0", variant="alpine", cve="CVE-2", package="zlib"),
        ]
        matrix = verify_matrix(targets)
        assert matrix == [
            {"line": "8.0", "variant": "alpine", "platform": "linux/amd64",
             "image": "valkey/valkey:8.0-alpine"},
        ]

    def test_four_platforms_one_line_variant_yield_four_entries(self) -> None:
        """A target set spanning 4 platforms for one (line, variant) -> 4 entries."""
        platforms = ["linux/amd64", "linux/arm64", "linux/arm/v7", "linux/ppc64le"]
        targets = [_target(platform=p) for p in platforms]
        matrix = verify_matrix(targets)
        assert matrix == [
            {"line": "8.0", "variant": "alpine", "platform": p,
             "image": "valkey/valkey:8.0-alpine"}
            for p in platforms
        ]

    def test_two_variants_across_four_platforms_yield_eight_entries(self) -> None:
        """Two variants each across 4 platforms -> 8 distinct entries."""
        platforms = ["linux/amd64", "linux/arm64", "linux/arm/v7", "linux/ppc64le"]
        targets = []
        for variant, image in (
            ("alpine", "valkey/valkey:8.0-alpine"),
            ("debian", "valkey/valkey:8.0"),
        ):
            for p in platforms:
                targets.append(
                    _target(line="8.0", variant=variant, image=image, platform=p)
                )
        matrix = verify_matrix(targets)
        assert len(matrix) == 8
        # Every (variant, platform) combination is present exactly once.
        combos = {(e["variant"], e["platform"]) for e in matrix}
        assert combos == {(v, p) for v in ("alpine", "debian") for p in platforms}

    def test_duplicate_platform_entries_collapse(self) -> None:
        """Multiple findings on the same (line, variant, platform) yield one entry."""
        targets = [
            _target(platform="linux/arm64", cve="CVE-A", package="libfoo"),
            _target(platform="linux/arm64", cve="CVE-B", package="libbar"),
            _target(platform="linux/arm64", cve="CVE-C", package="libbaz"),
        ]
        matrix = verify_matrix(targets)
        assert matrix == [
            {"line": "8.0", "variant": "alpine", "platform": "linux/arm64",
             "image": "valkey/valkey:8.0-alpine"},
        ]

    def test_ordering_is_stable_first_seen(self) -> None:
        """Entries appear in first-seen (line, variant, platform) order."""
        targets = [
            _target(line="8.0", variant="alpine", platform="linux/arm64", cve="CVE-1"),
            _target(line="8.0", variant="alpine", platform="linux/amd64", cve="CVE-2"),
            # Duplicate of the first key; must not reorder or re-append.
            _target(line="8.0", variant="alpine", platform="linux/arm64", cve="CVE-3"),
            _target(
                image="valkey/valkey:9.1",
                line="9.1",
                variant="debian",
                platform="linux/amd64",
                cve="CVE-4",
                package="curl",
            ),
        ]
        matrix = verify_matrix(targets)
        assert [(e["line"], e["variant"], e["platform"]) for e in matrix] == [
            ("8.0", "alpine", "linux/arm64"),
            ("8.0", "alpine", "linux/amd64"),
            ("9.1", "debian", "linux/amd64"),
        ]

    def test_image_is_first_seen_per_line_variant(self) -> None:
        """All entries for a (line, variant) carry the first image seen for it."""
        targets = [
            _target(image="valkey/valkey:8.0-alpine", platform="linux/amd64"),
            _target(image="valkey/valkey:8.0-alpine-alt", platform="linux/arm64"),
        ]
        matrix = verify_matrix(targets)
        assert {e["image"] for e in matrix} == {"valkey/valkey:8.0-alpine"}

    def test_distinct_platforms_each_get_an_entry(self) -> None:
        """Findings on arm64 and ppc64le each get their own entry (no amd64 baseline)."""
        targets = [
            _target(platform="linux/arm64", cve="CVE-ARM", package="libfoo"),
            _target(platform="linux/ppc64le", cve="CVE-PPC", package="libbar"),
        ]
        matrix = verify_matrix(targets)
        platforms = sorted(e["platform"] for e in matrix)
        assert platforms == ["linux/arm64", "linux/ppc64le"]

    def test_empty_targets_yield_empty_matrix(self) -> None:
        assert verify_matrix([]) == []


class TestVerifyMatrixCli:
    """The --verify-matrix CLI the workflow feeds into strategy.matrix.include."""

    def test_cli_prints_json_matrix(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from scripts.cve_scan.targets import main

        blob = encode_targets(
            [
                _target(line="8.0", variant="alpine", cve="CVE-1", package="openssl"),
                _target(
                    image="valkey/valkey:9.1",
                    line="9.1",
                    variant="debian",
                    platform="linux/amd64",
                    cve="CVE-2",
                    package="curl",
                ),
            ]
        )
        rc = main(["--verify-matrix", blob])
        assert rc == 0
        printed = json.loads(capsys.readouterr().out)
        assert printed == [
            {"line": "8.0", "variant": "alpine", "platform": "linux/amd64",
             "image": "valkey/valkey:8.0-alpine"},
            {"line": "9.1", "variant": "debian", "platform": "linux/amd64",
             "image": "valkey/valkey:9.1"},
        ]

    def test_cli_output_is_single_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """GITHUB_OUTPUT assignment needs single-line JSON."""
        from scripts.cve_scan.targets import main

        main(["--verify-matrix", encode_targets([_target()])])
        out = capsys.readouterr().out.strip()
        assert "\n" not in out

    def test_cli_fails_closed_on_malformed_contract(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from scripts.cve_scan.targets import main

        assert main(["--verify-matrix", "@@@ not base64 @@@"]) == 2
        assert capsys.readouterr().out.strip() == ""
