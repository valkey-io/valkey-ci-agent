"""Tests for scripts/cve_scan/build_conformance.py.

The conformant fixture is a faithful subset of valkey-container's REAL
mainline ci.yml (multiple jobs, QEMU/buildx/login steps, and the actual
docker/build-push-action inputs: file/push/tags/platforms/provenance/load with
a matrix expression in `file:`). It is not an idealized minimal step, so the
five drift cases exercise the check against realistic surrounding structure.
"""

from __future__ import annotations

from io import BytesIO

import pytest

from scripts.cve_scan.build_conformance import (
    ConformanceError,
    check_conformance,
    fetch_ci_yaml,
    main,
    parse_our_verify_contract,
)
from scripts.cve_scan.config import DEFAULT_PLATFORMS

# A faithful subset of valkey-io/valkey-container mainline ci.yml (today's shape).
CONFORMANT_CI = """\
name: GitHub CI
on:
  push:
  schedule:
    - cron: '0 0 * * *'
  workflow_dispatch:
    inputs:
      version:
        description: 'Versions to build and push, space-separated'
        required: true
        type: string
env:
  PUBLISH_GHCR: ${{ github.repository == 'valkey-io/valkey-container' && github.ref == 'refs/heads/mainline' }}
defaults:
  run:
    shell: 'bash -Eeuo pipefail -x {0}'
jobs:
  generate-jobs:
    name: Generate Jobs
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6.0.3
        with:
          fetch-depth: 2
      - uses: docker-library/bashbrew@a519e31d2ed9cb229ff38ab4488e78bfa09d7a2a # v0.1.14
  test:
    needs: generate-jobs
    name: ${{ matrix.name }} - test
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6.0.3
      - name: Build ${{ matrix.name }}
        run: ${{ matrix.runs.build }}
  build_and_push:
    needs:
      - generate-jobs
      - test
    name: ${{ matrix.name }} - Build and push
    runs-on: ${{ matrix.os }}
    permissions:
      id-token: write
      contents: read
      packages: write
    steps:
      - name: Modify Tags
        id: modify_tags
        run: |
          echo "tags=ghcr.io/valkey-io/valkey:8.0" >> $GITHUB_OUTPUT
      - name: Set up QEMU
        uses: docker/setup-qemu-action@06116385d9baf250c9f4dcb4858b16962ea869c3 # v4.1.0
      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@d7f5e7f509e45cec5c76c4d5afdd7de93d0b3df5 # v4.1.0
      - name: Login to GitHub Container Registry
        if: env.PUBLISH_GHCR == 'true'
        uses: docker/login-action@650006c6eb7dba73a995cc03b0b2d7f5ca915bee # v4.2.0
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ github.token }}
      - name: Build and push
        uses: docker/build-push-action@f9f3042f7e2789586610d6e8b85c8f03e5195baf # v7.2.0
        with:
          file: ./${{ matrix.meta.entries[0].directory }}/Dockerfile
          push: ${{ env.PUBLISH_GHCR == 'true' }}
          tags: ${{ steps.modify_tags.outputs.tags }}
          platforms: linux/amd64,linux/arm64,linux/arm/v7,linux/ppc64le
          provenance: false
          load: false
"""

_FILE_LINE = "          file: ./${{ matrix.meta.entries[0].directory }}/Dockerfile\n"


class TestConformant:
    def test_real_shape_passes(self) -> None:
        assert check_conformance(CONFORMANT_CI, DEFAULT_PLATFORMS) == []

    def test_platform_order_does_not_matter(self) -> None:
        reordered = ["linux/ppc64le", "linux/amd64", "linux/arm/v7", "linux/arm64"]
        assert check_conformance(CONFORMANT_CI, reordered) == []


class TestDriftCases:
    def test_platforms_drift(self) -> None:
        drifted = CONFORMANT_CI.replace(
            "platforms: linux/amd64,linux/arm64,linux/arm/v7,linux/ppc64le",
            "platforms: linux/amd64,linux/arm64",
        )
        drift = check_conformance(drifted, DEFAULT_PLATFORMS)
        assert len(drift) == 1
        assert "platforms drift" in drift[0]

    def test_context_drift(self) -> None:
        drifted = CONFORMANT_CI.replace(_FILE_LINE, "          context: .\n" + _FILE_LINE)
        drift = check_conformance(drifted, DEFAULT_PLATFORMS)
        assert len(drift) == 1
        assert "context drift" in drift[0]

    def test_build_args_drift(self) -> None:
        drifted = CONFORMANT_CI.replace(
            _FILE_LINE,
            "          build-args: |\n            FOO=bar\n" + _FILE_LINE,
        )
        drift = check_conformance(drifted, DEFAULT_PLATFORMS)
        assert len(drift) == 1
        assert "build-args drift" in drift[0]

    def test_target_drift(self) -> None:
        drifted = CONFORMANT_CI.replace(
            _FILE_LINE, "          target: builder\n" + _FILE_LINE
        )
        drift = check_conformance(drifted, DEFAULT_PLATFORMS)
        assert len(drift) == 1
        assert "target drift" in drift[0]

    def test_file_drift(self) -> None:
        drifted = CONFORMANT_CI.replace(
            "/Dockerfile", "/Containerfile"
        )
        drift = check_conformance(drifted, DEFAULT_PLATFORMS)
        assert len(drift) == 1
        assert "file drift" in drift[0]

    def test_drift_messages_are_specific_per_case(self) -> None:
        """Each mutation names exactly its own drift, not the others."""
        cases = {
            "platforms drift": CONFORMANT_CI.replace(
                "platforms: linux/amd64,linux/arm64,linux/arm/v7,linux/ppc64le",
                "platforms: linux/amd64",
            ),
            "context drift": CONFORMANT_CI.replace(
                _FILE_LINE, "          context: .\n" + _FILE_LINE
            ),
            "build-args drift": CONFORMANT_CI.replace(
                _FILE_LINE, "          build-args: |\n            X=1\n" + _FILE_LINE
            ),
            "target drift": CONFORMANT_CI.replace(
                _FILE_LINE, "          target: build\n" + _FILE_LINE
            ),
            "file drift": CONFORMANT_CI.replace("/Dockerfile", "/Containerfile"),
        }
        for expected, text in cases.items():
            drift = check_conformance(text, DEFAULT_PLATFORMS)
            assert drift == [d for d in drift if expected in d]
            assert any(expected in d for d in drift)


class TestFailClosedParsing:
    def test_missing_build_push_step_raises(self) -> None:
        no_step = CONFORMANT_CI.replace(
            "docker/build-push-action@f9f3042f7e2789586610d6e8b85c8f03e5195baf",
            "docker/setup-buildx-action@f9f3042f7e2789586610d6e8b85c8f03e5195baf",
        )
        with pytest.raises(ConformanceError, match="No docker/build-push-action step"):
            check_conformance(no_step, DEFAULT_PLATFORMS)

    def test_invalid_yaml_raises(self) -> None:
        with pytest.raises(ConformanceError, match="parse ci.yml"):
            check_conformance("jobs: [this: is: bad", DEFAULT_PLATFORMS)

    def test_non_mapping_yaml_raises(self) -> None:
        with pytest.raises(ConformanceError, match="did not parse to a mapping"):
            check_conformance("- just\n- a\n- list\n", DEFAULT_PLATFORMS)

    def test_step_without_with_block_raises(self) -> None:
        no_with = CONFORMANT_CI.replace(
            "        with:\n"
            "          file: ./${{ matrix.meta.entries[0].directory }}/Dockerfile\n"
            "          push: ${{ env.PUBLISH_GHCR == 'true' }}\n"
            "          tags: ${{ steps.modify_tags.outputs.tags }}\n"
            "          platforms: linux/amd64,linux/arm64,linux/arm/v7,linux/ppc64le\n"
            "          provenance: false\n"
            "          load: false\n",
            "        env:\n          FOO: bar\n",
        )
        with pytest.raises(ConformanceError, match="no 'with:' block"):
            check_conformance(no_with, DEFAULT_PLATFORMS)


class TestFetch:
    def test_fetch_returns_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = BytesIO(CONFORMANT_CI.encode("utf-8"))
        resp.status = 200  # type: ignore[attr-defined]
        monkeypatch.setattr(
            "scripts.cve_scan.build_conformance.urllib.request.urlopen",
            lambda *a, **k: resp,
        )
        assert "build-push-action" in fetch_ci_yaml("https://example.com/ci.yml")

    def test_fetch_network_error_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from urllib.error import URLError

        def _boom(*a, **k):
            raise URLError("connection refused")

        monkeypatch.setattr(
            "scripts.cve_scan.build_conformance.urllib.request.urlopen", _boom
        )
        with pytest.raises(ConformanceError, match="Failed to fetch ci.yml"):
            fetch_ci_yaml("https://example.com/ci.yml")


class TestMain:
    def test_main_returns_zero_when_conformant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "scripts.cve_scan.build_conformance.fetch_ci_yaml",
            lambda url=None: CONFORMANT_CI,
        )
        assert main(["--platforms", ",".join(DEFAULT_PLATFORMS)]) == 0

    def test_main_returns_one_on_drift(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        drifted = CONFORMANT_CI.replace("/Dockerfile", "/Containerfile")
        monkeypatch.setattr(
            "scripts.cve_scan.build_conformance.fetch_ci_yaml",
            lambda url=None: drifted,
        )
        assert main(["--platforms", ",".join(DEFAULT_PLATFORMS)]) == 1

    def test_main_returns_two_on_fetch_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(url=None):
            raise ConformanceError("fetch blew up")

        monkeypatch.setattr(
            "scripts.cve_scan.build_conformance.fetch_ci_yaml", _boom
        )
        assert main(["--platforms", ",".join(DEFAULT_PLATFORMS)]) == 2

    def test_main_reads_platforms_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CVE_SCAN_PLATFORMS", "linux/amd64,linux/arm64")
        # Fixture ships four platforms, so a two-platform expectation drifts.
        monkeypatch.setattr(
            "scripts.cve_scan.build_conformance.fetch_ci_yaml",
            lambda url=None: CONFORMANT_CI,
        )
        assert main([]) == 1


# A faithful subset of OUR cve-scan.yml verify job, pinned to valkey-container's
# SHAs (matching CONFORMANT_CI). The scan job carries a DIFFERENT setup-qemu pin
# to prove the cross-check is scoped to the verify job and never compares it.
OUR_VERIFY_WORKFLOW = """\
name: CVE Scan
on:
  workflow_dispatch:
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - name: Set up QEMU
        uses: docker/setup-qemu-action@96fe6ef7f33517b61c61be40b68a1882f3264fb8 # v4.2.0
  verify:
    runs-on: ubuntu-latest
    steps:
      - name: Set up QEMU
        uses: docker/setup-qemu-action@06116385d9baf250c9f4dcb4858b16962ea869c3 # v4.1.0
      - name: Set up Buildx
        uses: docker/setup-buildx-action@d7f5e7f509e45cec5c76c4d5afdd7de93d0b3df5 # v4.1.0
      - name: Build candidate image
        id: build
        uses: docker/build-push-action@f9f3042f7e2789586610d6e8b85c8f03e5195baf # v7.2.0
        with:
          file: ./${{ matrix.line }}/${{ matrix.variant }}/Dockerfile
          platforms: ${{ matrix.platform }}
          provenance: false
          push: false
          load: true
          tags: cve-candidate:x
"""

# Their build-push SHA (v7.2.0) and our verify pins, referenced only via these
# fixtures; the check reads both from YAML so no SHA is restated in production.
_THEIR_BUILD_PUSH = "docker/build-push-action@f9f3042f7e2789586610d6e8b85c8f03e5195baf"
_THEIR_QEMU = "docker/setup-qemu-action@06116385d9baf250c9f4dcb4858b16962ea869c3"
_THEIR_BUILDX = "docker/setup-buildx-action@d7f5e7f509e45cec5c76c4d5afdd7de93d0b3df5"


class TestActionRefCrossCheck:
    def test_matching_refs_pass(self) -> None:
        assert check_conformance(CONFORMANT_CI, DEFAULT_PLATFORMS, OUR_VERIFY_WORKFLOW) == []

    def test_build_push_ref_drift_on_our_side(self) -> None:
        drifted = OUR_VERIFY_WORKFLOW.replace(
            _THEIR_BUILD_PUSH,
            "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a",
        )
        drift = check_conformance(CONFORMANT_CI, DEFAULT_PLATFORMS, drifted)
        assert len(drift) == 1
        assert "action ref drift" in drift[0]
        assert "docker/build-push-action" in drift[0]

    def test_qemu_ref_drift_on_our_side(self) -> None:
        drifted = OUR_VERIFY_WORKFLOW.replace(
            _THEIR_QEMU,
            "docker/setup-qemu-action@37fe631027851001ddb9b187196cc803df7f5f0e",
        )
        drift = check_conformance(CONFORMANT_CI, DEFAULT_PLATFORMS, drifted)
        assert len(drift) == 1
        assert "action ref drift" in drift[0]
        assert "docker/setup-qemu-action" in drift[0]

    def test_buildx_ref_drift_on_their_side(self) -> None:
        drifted = CONFORMANT_CI.replace(
            _THEIR_BUILDX,
            "docker/setup-buildx-action@37fe631027851001ddb9b187196cc803df7f5f0e",
        )
        drift = check_conformance(drifted, DEFAULT_PLATFORMS, OUR_VERIFY_WORKFLOW)
        assert len(drift) == 1
        assert "action ref drift" in drift[0]
        assert "docker/setup-buildx-action" in drift[0]

    def test_drift_message_names_both_refs(self) -> None:
        """A moved pin reports the action, their new ref, and ours."""
        their_new = "docker/build-push-action@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        drifted = CONFORMANT_CI.replace(_THEIR_BUILD_PUSH, their_new)
        drift = check_conformance(drifted, DEFAULT_PLATFORMS, OUR_VERIFY_WORKFLOW)
        assert len(drift) == 1
        assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in drift[0]
        assert "f9f3042f7e2789586610d6e8b85c8f03e5195baf" in drift[0]
        assert "Re-sync" in drift[0]

    def test_scan_job_qemu_pin_is_not_compared(self) -> None:
        """The scan job's separate QEMU pin (a different purpose) is excluded."""
        refs, _ = parse_our_verify_contract(OUR_VERIFY_WORKFLOW)
        # Only the verify job's QEMU pin is picked up, never the scan job's.
        assert refs["docker/setup-qemu-action"] == "06116385d9baf250c9f4dcb4858b16962ea869c3"
        assert check_conformance(CONFORMANT_CI, DEFAULT_PLATFORMS, OUR_VERIFY_WORKFLOW) == []

    def test_action_and_platform_drift_reported_together(self) -> None:
        """Their-side platform drift and our-side pin drift both surface."""
        their = CONFORMANT_CI.replace(
            "platforms: linux/amd64,linux/arm64,linux/arm/v7,linux/ppc64le",
            "platforms: linux/amd64",
        )
        ours = OUR_VERIFY_WORKFLOW.replace(
            _THEIR_QEMU,
            "docker/setup-qemu-action@37fe631027851001ddb9b187196cc803df7f5f0e",
        )
        drift = check_conformance(their, DEFAULT_PLATFORMS, ours)
        assert any("platforms drift" in d for d in drift)
        assert any("action ref drift" in d and "setup-qemu" in d for d in drift)


class TestProvenance:
    def test_their_provenance_true_fails(self) -> None:
        drifted = CONFORMANT_CI.replace("provenance: false", "provenance: true")
        drift = check_conformance(drifted, DEFAULT_PLATFORMS)
        assert len(drift) == 1
        assert "provenance drift" in drift[0]

    def test_their_provenance_missing_fails(self) -> None:
        drifted = CONFORMANT_CI.replace("          provenance: false\n", "")
        drift = check_conformance(drifted, DEFAULT_PLATFORMS)
        assert len(drift) == 1
        assert "provenance drift" in drift[0]

    def test_our_provenance_true_fails(self) -> None:
        # Perturb only our side's provenance; theirs stays false.
        ours = OUR_VERIFY_WORKFLOW.replace(
            "          provenance: false\n", "          provenance: true\n"
        )
        drift = check_conformance(CONFORMANT_CI, DEFAULT_PLATFORMS, ours)
        assert len(drift) == 1
        assert "our-side provenance drift" in drift[0]


class TestPushLoad:
    def test_our_push_true_fails(self) -> None:
        ours = OUR_VERIFY_WORKFLOW.replace(
            "          push: false\n", "          push: true\n"
        )
        drift = check_conformance(CONFORMANT_CI, DEFAULT_PLATFORMS, ours)
        assert len(drift) == 1
        assert "our-side push drift" in drift[0]

    def test_our_load_false_fails(self) -> None:
        ours = OUR_VERIFY_WORKFLOW.replace(
            "          load: true\n", "          load: false\n"
        )
        drift = check_conformance(CONFORMANT_CI, DEFAULT_PLATFORMS, ours)
        assert len(drift) == 1
        assert "our-side load drift" in drift[0]

    def test_our_side_defaults_pass(self) -> None:
        assert check_conformance(CONFORMANT_CI, DEFAULT_PLATFORMS, OUR_VERIFY_WORKFLOW) == []


class TestOurContractParsing:
    def test_missing_verify_job_raises(self) -> None:
        no_verify = OUR_VERIFY_WORKFLOW.replace("  verify:", "  build:")
        with pytest.raises(ConformanceError, match="no 'verify' job"):
            parse_our_verify_contract(no_verify)

    def test_verify_without_build_step_raises(self) -> None:
        no_build = OUR_VERIFY_WORKFLOW.replace(_THEIR_BUILD_PUSH, "actions/checkout@" + "0" * 40)
        with pytest.raises(ConformanceError, match="no build-push-action step"):
            parse_our_verify_contract(no_build)


class TestPlatformDefaultFromConfig:
    """The expected platform list is single-sourced from config, not a literal."""

    def test_main_default_platforms_come_from_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CVE_SCAN_PLATFORMS", raising=False)
        monkeypatch.setattr(
            "scripts.cve_scan.build_conformance.fetch_ci_yaml",
            lambda url=None: CONFORMANT_CI,
        )
        monkeypatch.setattr(
            "scripts.cve_scan.build_conformance.read_our_workflow",
            lambda path=None: OUR_VERIFY_WORKFLOW,
        )
        # No --platforms and no env: the default is config.DEFAULT_PLATFORMS,
        # which matches the fixture's four platforms, so the run is conformant.
        assert main([]) == 0

    def test_main_default_platform_mismatch_still_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CVE_SCAN_PLATFORMS", raising=False)
        # Their ci.yml drops a platform: the config default no longer matches.
        drifted = CONFORMANT_CI.replace(
            "platforms: linux/amd64,linux/arm64,linux/arm/v7,linux/ppc64le",
            "platforms: linux/amd64,linux/arm64",
        )
        monkeypatch.setattr(
            "scripts.cve_scan.build_conformance.fetch_ci_yaml",
            lambda url=None: drifted,
        )
        monkeypatch.setattr(
            "scripts.cve_scan.build_conformance.read_our_workflow",
            lambda path=None: OUR_VERIFY_WORKFLOW,
        )
        assert main([]) == 1
