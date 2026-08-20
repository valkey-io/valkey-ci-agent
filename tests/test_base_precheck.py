"""Tests for scripts/cve_scan/base_precheck.py.

Covers the rebuild-simulation verification model: confirm when the base
already ships the fix (fast path, no simulation) or the simulated Dockerfile
install plan lands the package at or above the fix; downgrade when the plan
falls short, when the package is not in the plan (rebuild leaves the base
version), or on any fetch/parse/simulation/comparison failure (fail-closed).
Also covers install-list parsing, plan parsing, docker command construction,
package-db parsing, and per-(base, platform) / per-plan caching.

The docker-based native comparator is patched with a deterministic local stub
(autouse fixture) so no real Docker runs; the real comparator is tested in
test_version_compare.py.
"""

from __future__ import annotations

import re
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from scripts.cve_scan.base_precheck import (
    BasePrecheckError,
    _dockerfile_target,
    _parse_apk_installed,
    _parse_apk_plan,
    _parse_apt_plan,
    _parse_dpkg_query,
    _parse_install_list,
    get_base_packages,
    simulate_install,
    verify_fixable_in_base,
)
from scripts.cve_scan.models import Classification, Finding, Severity


def _stub_compare(a: str, b: str, flavor: str, base_image: str | None = None) -> int | None:
    """Deterministic test stand-in for the native comparator.

    Parses only the 'X.Y.Z[-rN]' shapes used in these tests as tuples of
    ints; anything else returns None (ambiguous), mirroring the native
    comparator's fail-closed contract.
    """
    def parse(version: str) -> "tuple[int, ...] | None":
        nums, _, rev = version.partition("-r")
        try:
            return tuple(int(p) for p in nums.split(".")) + (int(rev) if rev else 0,)
        except ValueError:
            return None

    pa, pb = parse(a), parse(b)
    if pa is None or pb is None:
        return None
    return (pa > pb) - (pa < pb)


@pytest.fixture(autouse=True)
def _mock_native_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the docker-based native comparator with a deterministic stub (no real Docker)."""
    monkeypatch.setattr(
        "scripts.cve_scan.base_precheck._native_compare",
        _stub_compare,
    )


def _make_finding(
    image: str = "valkey/valkey:9.1-alpine",
    package: str = "openssl",
    installed: str = "3.0.12-r0",
    cve_id: str = "CVE-2024-1234",
    fixed: str | None = "3.0.13-r0",
    platform: str = "",
) -> Finding:
    return Finding(
        image=image,
        package=package,
        installed_version=installed,
        cve_id=cve_id,
        severity=Severity.HIGH,
        fixed_version=fixed,
        platform=platform,
    )


def _make_classification(finding: Finding) -> Classification:
    return Classification(
        finding=finding,
        fixable=True,
        rationale=f"A rebuild would upgrade {finding.package}.",
    )


# ---------------------------------------------------------------------------
# Verification decision: confirm via the simulated rebuild plan
# ---------------------------------------------------------------------------


class TestConfirmViaPlan:
    """Base ships an older version, but the rebuild plan lands the fix -> confirmed.

    These are the false-negatives the old installed-vs-base gate suppressed:
    Debian's libssl-dev drags a newer libssl3t64, Alpine's openssl drags a
    newer libcrypto3, so a rebuild DOES upgrade them.
    """

    def test_libssl3t64_debian_upgraded_by_plan_confirmed(self) -> None:
        finding = _make_finding(
            image="valkey/valkey:9.0",
            package="libssl3t64",
            installed="3.0.11-r0",
            fixed="3.0.13-r0",
        )
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:9.0": "debian:trixie-slim"}

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages",
            return_value={"libssl3t64": "3.0.11-r0"},  # base older than fix
        ), patch(
            "scripts.cve_scan.base_precheck._fetch_dockerfile",
            return_value="RUN apt-get install -y --no-install-recommends libssl-dev\n",
        ), patch(
            "scripts.cve_scan.base_precheck.simulate_install",
            return_value={"libssl3t64": "3.0.13-r0", "libssl-dev": "3.0.13-r0"},
        ):
            confirmed, downgraded = verify_fixable_in_base([classification], base_map)

        assert len(confirmed) == 1
        assert len(downgraded) == 0
        assert confirmed[0].fixable is True
        assert "rebuild installs libssl3t64 3.0.13-r0" in confirmed[0].rationale
        assert ">= fix 3.0.13-r0" in confirmed[0].rationale

    def test_libcrypto3_alpine_upgraded_by_plan_confirmed(self) -> None:
        finding = _make_finding(
            image="valkey/valkey:9.1-alpine",
            package="libcrypto3",
            installed="3.0.11-r0",
            fixed="3.0.13-r0",
        )
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:9.1-alpine": "alpine:3.23"}

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages",
            return_value={"libcrypto3": "3.0.11-r0"},
        ), patch(
            "scripts.cve_scan.base_precheck._fetch_dockerfile",
            return_value="RUN apk add --no-cache openssl\n",
        ), patch(
            "scripts.cve_scan.base_precheck.simulate_install",
            return_value={"libcrypto3": "3.0.13-r0", "libssl3": "3.0.13-r0"},
        ):
            confirmed, downgraded = verify_fixable_in_base([classification], base_map)

        assert len(confirmed) == 1
        assert len(downgraded) == 0
        assert "rebuild installs libcrypto3 3.0.13-r0" in confirmed[0].rationale


# ---------------------------------------------------------------------------
# Verification decision: downgrade via the plan
# ---------------------------------------------------------------------------


class TestDowngradeViaPlan:
    """The plan does not deliver the fix -> downgrade fail-closed."""

    def test_zlib1g_present_stale_not_in_plan_downgraded(self) -> None:
        """zlib1g case: present in base, stale, and NOT installed by any Dockerfile block."""
        finding = _make_finding(
            image="valkey/valkey:9.0",
            package="zlib1g",
            installed="1.2.13",
            fixed="1.2.14",
        )
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:9.0": "debian:trixie-slim"}

        sim = MagicMock(return_value={"libssl3t64": "3.0.14", "libssl-dev": "3.0.14"})
        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages",
            return_value={"zlib1g": "1.2.13"},  # base older than fix
        ), patch(
            "scripts.cve_scan.base_precheck._fetch_dockerfile",
            return_value="RUN apt-get install -y --no-install-recommends libssl-dev\n",
        ), patch(
            "scripts.cve_scan.base_precheck.simulate_install",
            sim,
        ):
            confirmed, downgraded = verify_fixable_in_base([classification], base_map)

        sim.assert_called_once()  # the plan is consulted
        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert downgraded[0].fixable is False
        assert "not in the Dockerfile install plan" in downgraded[0].rationale
        assert "base image update" in downgraded[0].rationale
        assert "zlib1g" in downgraded[0].rationale

    def test_plan_below_fix_downgraded(self) -> None:
        finding = _make_finding(installed="3.0.11-r0", fixed="3.0.13-r0")
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:9.1-alpine": "alpine:3.23"}

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages",
            return_value={"openssl": "3.0.11-r0"},
        ), patch(
            "scripts.cve_scan.base_precheck._fetch_dockerfile",
            return_value="RUN apk add --no-cache openssl\n",
        ), patch(
            "scripts.cve_scan.base_precheck.simulate_install",
            return_value={"openssl": "3.0.12-r0"},  # below fix 3.0.13-r0
        ):
            confirmed, downgraded = verify_fixable_in_base([classification], base_map)

        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert "installs 3.0.12-r0, below fix 3.0.13-r0" in downgraded[0].rationale


# ---------------------------------------------------------------------------
# Verification decision: fail-closed on fetch / simulation failure
# ---------------------------------------------------------------------------


class TestFailClosed:
    """Fetch or simulation failures downgrade conservatively."""

    BASE_MAP = {"valkey/valkey:9.1-alpine": "alpine:3.23"}

    def test_simulation_failure_downgraded(self) -> None:
        finding = _make_finding(installed="3.0.11-r0", fixed="3.0.13-r0")
        classification = _make_classification(finding)

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages",
            return_value={"openssl": "3.0.11-r0"},
        ), patch(
            "scripts.cve_scan.base_precheck._fetch_dockerfile",
            return_value="RUN apk add --no-cache openssl\n",
        ), patch(
            "scripts.cve_scan.base_precheck.simulate_install",
            side_effect=BasePrecheckError("docker not available"),
        ):
            confirmed, downgraded = verify_fixable_in_base(
                [classification], self.BASE_MAP
            )

        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert downgraded[0].fixable is False
        assert "could not simulate the rebuild" in downgraded[0].rationale
        assert "fail-closed" in downgraded[0].rationale

    def test_dockerfile_fetch_failure_downgraded(self) -> None:
        finding = _make_finding(installed="3.0.11-r0", fixed="3.0.13-r0")
        classification = _make_classification(finding)

        sim = MagicMock()
        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages",
            return_value={"openssl": "3.0.11-r0"},
        ), patch(
            "scripts.cve_scan.base_precheck._fetch_dockerfile",
            side_effect=BasePrecheckError("HTTP 404"),
        ), patch(
            "scripts.cve_scan.base_precheck.simulate_install",
            sim,
        ):
            confirmed, downgraded = verify_fixable_in_base(
                [classification], self.BASE_MAP
            )

        sim.assert_not_called()  # no point simulating without an install list
        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert "could not simulate the rebuild" in downgraded[0].rationale


# ---------------------------------------------------------------------------
# Verification decision: base already ships the fix (fast path)
# ---------------------------------------------------------------------------


class TestBaseHasFix:
    """Base already ships the fix -> confirmed WITHOUT any simulation."""

    BASE_MAP = {"valkey/valkey:9.1-alpine": "alpine:3.23"}

    def test_base_ships_fix_confirmed_without_simulation(self) -> None:
        finding = _make_finding(installed="3.0.12-r0", fixed="3.0.13-r0")
        classification = _make_classification(finding)

        sim = MagicMock()
        fetch = MagicMock()
        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages",
            return_value={"openssl": "3.0.14-r0"},  # base >= fix
        ), patch(
            "scripts.cve_scan.base_precheck._fetch_dockerfile", fetch,
        ), patch(
            "scripts.cve_scan.base_precheck.simulate_install", sim,
        ):
            confirmed, downgraded = verify_fixable_in_base(
                [classification], self.BASE_MAP
            )

        sim.assert_not_called()
        fetch.assert_not_called()
        assert len(confirmed) == 1
        assert len(downgraded) == 0
        assert "base alpine:3.23 ships 3.0.14-r0" in confirmed[0].rationale
        assert ">= fix 3.0.13-r0" in confirmed[0].rationale

    def test_base_equals_fix_confirmed(self) -> None:
        finding = _make_finding(installed="3.0.12-r0", fixed="3.0.13-r0")
        classification = _make_classification(finding)

        sim = MagicMock()
        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages",
            return_value={"openssl": "3.0.13-r0"},
        ), patch(
            "scripts.cve_scan.base_precheck.simulate_install", sim,
        ):
            confirmed, downgraded = verify_fixable_in_base(
                [classification], self.BASE_MAP
            )

        sim.assert_not_called()
        assert len(confirmed) == 1
        assert confirmed[0].fixable is True


# ---------------------------------------------------------------------------
# Verification decision: no known fixed version (Item B)
# ---------------------------------------------------------------------------


class TestFixedVersionNone:
    """finding.fixed_version is None -> downgraded fail-closed (never a silent confirm)."""

    def test_fixed_version_none_downgraded(self) -> None:
        finding = _make_finding(fixed=None)
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:9.1-alpine": "alpine:3.23"}

        sim = MagicMock()
        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages",
            return_value={"openssl": "3.0.12-r0"},
        ), patch(
            "scripts.cve_scan.base_precheck.simulate_install", sim,
        ):
            confirmed, downgraded = verify_fixable_in_base([classification], base_map)

        sim.assert_not_called()
        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert downgraded[0].fixable is False
        assert "no known fixed version" in downgraded[0].rationale


# ---------------------------------------------------------------------------
# Verification decision: ambiguous comparison and missing base map
# ---------------------------------------------------------------------------


class TestAmbiguousComparison:
    """Ambiguous version comparison (None) -> downgrade without simulating."""

    def test_ambiguous_base_comparison_downgraded_no_simulation(self) -> None:
        finding = _make_finding(package="weird-pkg", installed="1.0.0", fixed="1.0.0beta1")
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:9.1-alpine": "alpine:3.23"}

        sim = MagicMock()
        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages",
            return_value={"weird-pkg": "1.0.0alpha2"},  # stub compare -> None
        ), patch(
            "scripts.cve_scan.base_precheck.simulate_install", sim,
        ):
            confirmed, downgraded = verify_fixable_in_base([classification], base_map)

        sim.assert_not_called()
        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert "ambiguous" in downgraded[0].rationale

    def test_ambiguous_plan_comparison_downgraded(self) -> None:
        finding = _make_finding(installed="3.0.11-r0", fixed="3.0.13-r0")
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:9.1-alpine": "alpine:3.23"}

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages",
            return_value={"openssl": "3.0.11-r0"},
        ), patch(
            "scripts.cve_scan.base_precheck._fetch_dockerfile",
            return_value="RUN apk add --no-cache openssl\n",
        ), patch(
            "scripts.cve_scan.base_precheck.simulate_install",
            return_value={"openssl": "not-a-version"},  # stub compare -> None
        ):
            confirmed, downgraded = verify_fixable_in_base([classification], base_map)

        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert "ambiguous" in downgraded[0].rationale


class TestMissingBaseMap:
    """Image not in base_map -> downgraded conservatively (fail-closed)."""

    def test_missing_base_map_entry_downgrades(self) -> None:
        finding = _make_finding(image="custom/image:latest")
        classification = _make_classification(finding)
        base_map: dict[str, str] = {}

        with patch("scripts.cve_scan.base_precheck.get_base_packages") as mock_get:
            confirmed, downgraded = verify_fixable_in_base([classification], base_map)

        mock_get.assert_not_called()
        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert "No base image mapping" in downgraded[0].rationale
        assert "fail-closed" in downgraded[0].rationale


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestBaseCaching:
    """Base package db read once per (base_ref, platform); plan simulated once per plan key."""

    def test_shared_base_queried_once(self) -> None:
        finding1 = _make_finding(image="valkey/valkey:9.1-alpine", cve_id="CVE-2024-1111")
        finding2 = _make_finding(image="valkey/valkey:9.0-alpine", cve_id="CVE-2024-2222")
        classifications = [_make_classification(finding1), _make_classification(finding2)]
        base_map = {
            "valkey/valkey:9.1-alpine": "alpine:3.23",
            "valkey/valkey:9.0-alpine": "alpine:3.23",
        }

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get:
            mock_get.return_value = {"openssl": "3.0.13-r0"}  # base has fix
            confirmed, downgraded = verify_fixable_in_base(classifications, base_map)

        assert mock_get.call_count == 1
        mock_get.assert_called_once_with("alpine:3.23", platform="")
        assert len(confirmed) == 2
        assert len(downgraded) == 0

    def test_same_base_different_platforms_queried_separately(self) -> None:
        finding_amd64 = _make_finding(cve_id="CVE-2024-1111", platform="linux/amd64")
        finding_arm64 = _make_finding(cve_id="CVE-2024-1111", platform="linux/arm64")
        classifications = [
            _make_classification(finding_amd64),
            _make_classification(finding_arm64),
        ]
        base_map = {"valkey/valkey:9.1-alpine": "alpine:3.23"}

        call_args: list[tuple[str, str]] = []

        def mock_get(base_ref, platform=""):
            call_args.append((base_ref, platform))
            if platform == "linux/amd64":
                return {"openssl": "3.0.13-r0"}  # amd64 has the fix
            return {"openssl": "3.0.11-r0"}  # arm64 base older than fix

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages", side_effect=mock_get,
        ), patch(
            "scripts.cve_scan.base_precheck._fetch_dockerfile",
            return_value="RUN apk add --no-cache openssl\n",
        ), patch(
            # arm64 base is stale -> simulate; the plan lacks openssl here,
            # so it downgrades (not-in-plan).
            "scripts.cve_scan.base_precheck.simulate_install",
            return_value={"musl": "1.2.5-r0"},
        ):
            confirmed, downgraded = verify_fixable_in_base(classifications, base_map)

        assert len(call_args) == 2
        assert ("alpine:3.23", "linux/amd64") in call_args
        assert ("alpine:3.23", "linux/arm64") in call_args
        assert len(confirmed) == 1
        assert confirmed[0].finding.platform == "linux/amd64"
        assert len(downgraded) == 1
        assert downgraded[0].finding.platform == "linux/arm64"

    def test_plan_simulated_once_per_plan_key(self) -> None:
        """Two findings sharing (base, platform, install-list) -> one fetch + one simulation."""
        finding1 = _make_finding(cve_id="CVE-2024-1111", installed="3.0.11-r0")
        finding2 = _make_finding(cve_id="CVE-2024-2222", installed="3.0.11-r0")
        classifications = [_make_classification(finding1), _make_classification(finding2)]
        base_map = {"valkey/valkey:9.1-alpine": "alpine:3.23"}

        fetch = MagicMock(return_value="RUN apk add --no-cache openssl\n")
        sim = MagicMock(return_value={"openssl": "3.0.13-r0"})
        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages",
            return_value={"openssl": "3.0.11-r0"},  # stale base -> simulate
        ), patch(
            "scripts.cve_scan.base_precheck._fetch_dockerfile", fetch,
        ), patch(
            "scripts.cve_scan.base_precheck.simulate_install", sim,
        ):
            confirmed, downgraded = verify_fixable_in_base(classifications, base_map)

        assert fetch.call_count == 1
        assert sim.call_count == 1
        assert len(confirmed) == 2
        assert len(downgraded) == 0


# ---------------------------------------------------------------------------
# get_base_packages: docker command + failure handling (Items A, C)
# ---------------------------------------------------------------------------


SAMPLE_APK_DB = """\
C:Q1abc123=
P:musl
V:1.2.5-r0
A:x86_64

C:Q1def456=
P:openssl
V:3.0.13-r0
A:x86_64

C:Q1ghi789=
P:zlib
V:1.3.1-r0
A:x86_64

"""

SAMPLE_DPKG_OUTPUT = """\
adduser 3.134
apt 2.7.14
bash 5.2.21-2+deb12u1
libssl3 3.0.13-1~deb12u1
openssl 3.0.13-1~deb12u1
zlib1g 1:1.2.13.dfsg-1
"""


class TestUnknownBaseFlavor:
    """Unknown base image prefix -> raises BasePrecheckError."""

    def test_unknown_base_raises(self) -> None:
        with pytest.raises(BasePrecheckError, match="Unknown base image flavor"):
            get_base_packages("ubuntu:22.04")

    def test_unknown_base_in_verify_raises(self) -> None:
        finding = _make_finding()
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:9.1-alpine": "ubuntu:22.04"}

        with pytest.raises(BasePrecheckError, match="Unknown base image flavor"):
            verify_fixable_in_base([classification], base_map)


class TestSubprocessFailure:
    """Docker failure, timeout, OSError, empty, or unparseable output -> BasePrecheckError."""

    def test_nonzero_exit_raises(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["docker", "run"], returncode=1, stdout="", stderr="image not found",
            )
            with pytest.raises(BasePrecheckError, match="docker run failed"):
                get_base_packages("alpine:3.23")

    def test_timeout_raises(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["docker", "run"], timeout=300)
            with pytest.raises(BasePrecheckError, match="Timed out"):
                get_base_packages("alpine:3.23")

    def test_oserror_raises(self) -> None:
        """Item C: docker binary missing (OSError) wraps into BasePrecheckError."""
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("docker not found")
            with pytest.raises(BasePrecheckError, match="Failed to run docker"):
                get_base_packages("alpine:3.23")

    def test_empty_output_raises(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["docker", "run"], returncode=0, stdout="", stderr="",
            )
            with pytest.raises(BasePrecheckError, match="Empty package database"):
                get_base_packages("alpine:3.23")

    def test_nonempty_unparseable_raises(self) -> None:
        """Item A: nonempty but unparseable output -> raise (not silently {})."""
        garbage = "unrecognizable\nformat\nnopackageshere\n"
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["docker", "run"], returncode=0, stdout=garbage, stderr="",
            )
            with pytest.raises(BasePrecheckError, match="parsed zero packages"):
                get_base_packages("debian:trixie-slim")

    def test_nonempty_unparseable_names_byte_count(self) -> None:
        garbage = "x" * 42 + "\n"
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["docker", "run"], returncode=0, stdout=garbage, stderr="",
            )
            with pytest.raises(BasePrecheckError, match="Read 43 bytes"):
                get_base_packages("debian:trixie-slim")


class TestApkDbParsing:
    """Parse real-format /lib/apk/db/installed content."""

    def test_parse_real_apk_db(self) -> None:
        packages = _parse_apk_installed(SAMPLE_APK_DB)
        assert packages["musl"] == "1.2.5-r0"
        assert packages["openssl"] == "3.0.13-r0"
        assert packages["zlib"] == "1.3.1-r0"
        assert len(packages) == 3

    def test_get_base_packages_alpine_with_real_format(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["docker", "run"], returncode=0, stdout=SAMPLE_APK_DB, stderr="",
            )
            result = get_base_packages("alpine:3.23")
        assert result["openssl"] == "3.0.13-r0"
        assert len(result) == 3

    def test_parse_apk_db_no_trailing_blank_line(self) -> None:
        packages = _parse_apk_installed("P:busybox\nV:1.36.1-r0\n")
        assert packages["busybox"] == "1.36.1-r0"

    def test_parse_apk_db_empty(self) -> None:
        assert _parse_apk_installed("") == {}


class TestDpkgQueryParsing:
    """Parse real-format dpkg-query output."""

    def test_parse_real_dpkg_output(self) -> None:
        packages = _parse_dpkg_query(SAMPLE_DPKG_OUTPUT)
        assert packages["bash"] == "5.2.21-2+deb12u1"
        assert packages["openssl"] == "3.0.13-1~deb12u1"
        assert packages["zlib1g"] == "1:1.2.13.dfsg-1"
        assert len(packages) == 6

    def test_get_base_packages_debian_with_real_format(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["docker", "run"], returncode=0, stdout=SAMPLE_DPKG_OUTPUT, stderr="",
            )
            result = get_base_packages("debian:trixie-slim")
        assert result["openssl"] == "3.0.13-1~deb12u1"
        assert len(result) == 6

    def test_parse_dpkg_empty(self) -> None:
        assert _parse_dpkg_query("") == {}


# ---------------------------------------------------------------------------
# Dockerfile target derivation and install-list parsing
# ---------------------------------------------------------------------------


class TestDockerfileTarget:
    """Derive (line, variant) from the image ref; strip the -alpine suffix only."""

    def test_debian_tag(self) -> None:
        assert _dockerfile_target("valkey/valkey:9.0") == ("9.0", "debian")

    def test_alpine_tag(self) -> None:
        assert _dockerfile_target("valkey/valkey:9.0-alpine") == ("9.0", "alpine")

    def test_alpine_suffix_stripped_only_once(self) -> None:
        # 'alpine' embedded in the line name must survive; only the suffix goes.
        assert _dockerfile_target("valkey/valkey:alpine-test-alpine") == (
            "alpine-test",
            "alpine",
        )


# Realistic Dockerfile snippets (backslash continuations, flags, virtuals).
DOCKERFILE_APT = """\
FROM debian:trixie-slim
RUN set -eux; \\
\tapt-get update; \\
\tapt-get install -y --no-install-recommends \\
\t\tlibssl-dev \\
\t\tca-certificates \\
\t; \\
\trm -rf /var/lib/apt/lists/*
"""

DOCKERFILE_APK = """\
FROM alpine:3.23
RUN set -eux; \\
\tapk add --no-cache \\
\t\topenssl \\
\t\ttzdata \\
\t; \\
\tapk add --no-cache --virtual .build-deps \\
\t\tgcc \\
\t\tmake \\
\t; \\
\tapk del .build-deps
"""


class TestParseInstallList:
    """Union package tokens from apk/apt install blocks; skip flags, virtuals, vars."""

    def test_apt_backslash_continuations_and_no_install_recommends(self) -> None:
        pkgs = _parse_install_list(DOCKERFILE_APT)
        assert pkgs == ["libssl-dev", "ca-certificates"]
        assert "-y" not in pkgs
        assert "--no-install-recommends" not in pkgs

    def test_apk_virtual_build_deps_name_skipped(self) -> None:
        pkgs = _parse_install_list(DOCKERFILE_APK)
        assert pkgs == ["openssl", "tzdata", "gcc", "make"]
        assert ".build-deps" not in pkgs

    def test_apk_dash_t_virtual_argument_skipped(self) -> None:
        df = "RUN apk add --no-cache -t .build-deps gcc make\n"
        assert _parse_install_list(df) == ["gcc", "make"]

    def test_shell_variables_skipped(self) -> None:
        df = "RUN apt-get install -y $EXTRA_PKGS libssl-dev ${MORE}\n"
        assert _parse_install_list(df) == ["libssl-dev"]

    def test_multiple_blocks_unioned_and_deduped(self) -> None:
        df = (
            "RUN apk add --no-cache openssl\n"
            "RUN apk add --no-cache openssl tzdata\n"
        )
        assert _parse_install_list(df) == ["openssl", "tzdata"]

    def test_no_install_returns_empty(self) -> None:
        df = "FROM alpine:3.23\nRUN echo hello && apk del foo\n"
        assert _parse_install_list(df) == []

    def test_apt_install_split_on_double_ampersand(self) -> None:
        df = "RUN apt-get update && apt-get install -y libssl-dev\n"
        assert _parse_install_list(df) == ["libssl-dev"]


# ---------------------------------------------------------------------------
# Install-plan parsing
# ---------------------------------------------------------------------------


SAMPLE_APT_PLAN = """\
NOTE: This is only a simulation!
Reading package lists...
Building dependency tree...
Inst libssl3t64 [3.0.11-1] (3.0.13-1 Debian:trixie [amd64])
Inst ca-certificates (20240203 Debian:trixie [all])
Conf libssl3t64 (3.0.13-1 Debian:trixie [amd64])
Conf ca-certificates (20240203 Debian:trixie [all])
"""

SAMPLE_APK_PLAN = """\
(1/3) Upgrading libcrypto3 (3.0.11-r0 -> 3.0.13-r0)
(2/3) Upgrading libssl3 (3.0.11-r0 -> 3.0.13-r0)
(3/3) Installing openssl (3.0.13-r0)
OK: 12 MiB in 20 packages
"""


class TestParseAptPlan:
    """Parse apt-get install --dry-run output."""

    def test_inst_and_conf(self) -> None:
        plan = _parse_apt_plan(SAMPLE_APT_PLAN)
        assert plan["libssl3t64"] == "3.0.13-1"  # Inst version, not the [old]
        assert plan["ca-certificates"] == "20240203"

    def test_conf_only_used_when_no_inst(self) -> None:
        plan = _parse_apt_plan("Conf foo (1.2.3 Debian:trixie [amd64])\n")
        assert plan == {"foo": "1.2.3"}

    def test_empty(self) -> None:
        assert _parse_apt_plan("Reading package lists...\n") == {}


class TestParseApkPlan:
    """Parse apk add --simulate output."""

    def test_installing_and_upgrading(self) -> None:
        plan = _parse_apk_plan(SAMPLE_APK_PLAN)
        assert plan["libcrypto3"] == "3.0.13-r0"  # post-arrow version
        assert plan["libssl3"] == "3.0.13-r0"
        assert plan["openssl"] == "3.0.13-r0"

    def test_empty(self) -> None:
        assert _parse_apk_plan("OK: 12 MiB in 20 packages\n") == {}


# ---------------------------------------------------------------------------
# simulate_install: docker command construction and failure handling
# ---------------------------------------------------------------------------


class TestSimulateInstall:
    """Argv-safe command construction and fail-closed error handling."""

    def test_packages_passed_as_positional_args_not_interpolated(self) -> None:
        hostile = 'openssl"; rm -rf /'
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="(1/1) Installing openssl (3.0.13-r0)\n", stderr="",
            )
            simulate_install("alpine:3.23", [hostile, "tzdata"], platform="linux/arm64")

        cmd = mock_run.call_args[0][0]
        assert cmd[:2] == ["docker", "run"]
        assert "--platform" in cmd and "linux/arm64" in cmd
        script = cmd[cmd.index("-c") + 1]
        assert '"$@"' in script
        assert hostile not in script
        # Positional args: sh -c <script> _ <pkg...>
        assert cmd[cmd.index("-c") + 2] == "_"
        assert cmd[-2:] == [hostile, "tzdata"]

    def test_alpine_uses_apk_simulate(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=SAMPLE_APK_PLAN, stderr="",
            )
            plan = simulate_install("alpine:3.23", ["openssl"])
        script = mock_run.call_args[0][0][mock_run.call_args[0][0].index("-c") + 1]
        assert "apk update" in script
        assert "apk add --simulate" in script
        assert plan["openssl"] == "3.0.13-r0"

    def test_debian_uses_apt_dry_run(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=SAMPLE_APT_PLAN, stderr="",
            )
            plan = simulate_install("debian:trixie-slim", ["libssl-dev"])
        script = mock_run.call_args[0][0][mock_run.call_args[0][0].index("-c") + 1]
        assert "apt-get update" in script
        assert "apt-get install --dry-run --no-install-recommends" in script
        assert plan["libssl3t64"] == "3.0.13-1"

    def test_empty_packages_raises(self) -> None:
        with pytest.raises(BasePrecheckError, match="No install packages"):
            simulate_install("alpine:3.23", [])

    def test_unknown_flavor_raises(self) -> None:
        with pytest.raises(BasePrecheckError, match="Unknown base image flavor"):
            simulate_install("ubuntu:22.04", ["openssl"])

    def test_nonzero_exit_raises(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="network unreachable",
            )
            with pytest.raises(BasePrecheckError, match="Install simulation failed"):
                simulate_install("alpine:3.23", ["openssl"])

    def test_timeout_raises(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["docker"], timeout=180)
            with pytest.raises(BasePrecheckError, match="Timed out simulating"):
                simulate_install("alpine:3.23", ["openssl"])

    def test_oserror_raises(self) -> None:
        """Item C: docker OSError wraps into BasePrecheckError."""
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("docker not found")
            with pytest.raises(BasePrecheckError, match="Failed to run docker"):
                simulate_install("alpine:3.23", ["openssl"])

    def test_empty_plan_raises(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="nothing to install here\n", stderr="",
            )
            with pytest.raises(BasePrecheckError, match="empty install plan"):
                simulate_install("alpine:3.23", ["openssl"])


# ---------------------------------------------------------------------------
# Regression: interior '#' comment lines in real valkey-container install blocks
# ---------------------------------------------------------------------------
#
# WHY THESE FIXTURES ARE DELIBERATELY MESSY (do not "simplify" them):
# an audit found _parse_install_list was broken on EVERY real
# valkey-container Dockerfile, yet the old tests passed. The old fixtures put
# every package on its own clean continuation line with no interior comments.
# The real Dockerfiles put a '#' comment line INSIDE the install block with no
# trailing backslash. Before the fix, merging continuations first absorbed the
# comment prose as "packages" (tokens like '#', a URL, '(see', 'related)') AND
# orphaned the real names (tzdata, libssl3t64) onto a segment with no install
# verb, so they were dropped. apt/apk then reject the junk names with a nonzero
# exit, every simulation fails, and every stale-base finding silently
# downgrades: the feature goes inert. The fix strips comment lines BEFORE
# merging continuations. These fixtures reproduce the real structure so the
# realistic path stays covered.

# A conservative Debian/Alpine package-name shape: lowercase/digit start, then
# lowercase alnum plus . + _ -. Prose, URLs, and '#' fail this, so a future
# comment leak trips the assertion even when we forget to blocklist the word.
_PLAUSIBLE_PKG_RE = re.compile(r"^[a-z0-9][a-z0-9.+_-]*$")

# Prose words the broken parser leaked from the interior comment. Kept explicit
# because some (add, for, and) still look like plausible package names.
_COMMENT_PROSE = {"add", "explicitly", "for", "also", "and", "(see", "related)"}


def _assert_no_junk_tokens(pkgs: list[str]) -> None:
    """Every token must look like a package name, never comment prose or a URL."""
    for tok in pkgs:
        assert not tok.startswith("#"), f"comment leak: {tok!r}"
        assert "://" not in tok, f"URL leak: {tok!r}"
        assert tok not in _COMMENT_PROSE, f"prose leak: {tok!r}"
        assert _PLAUSIBLE_PKG_RE.match(tok), f"implausible package name: {tok!r}"


# Debian block: interior '#' comment carrying a URL and parentheses, sitting
# between the install verb and the real package names (tab-indented, as real).
DOCKERFILE_APT_INTERIOR_COMMENT = """\
FROM debian:trixie-slim
RUN set -eux; \\
\tapt-get update; \\
\tapt-get install -y --no-install-recommends \\
# add tzdata explicitly for https://github.com/docker-library/redis/issues/138 (see also https://bugs.debian.org/837060 and related)
\t\ttzdata \\
\t\tlibssl3t64 \\
\t; \\
\trm -rf /var/lib/apt/lists/*
"""

# Alpine block: a --virtual .build-deps build block, then a runtime block whose
# interior '#' comment also carries a URL and parentheses.
DOCKERFILE_APK_INTERIOR_COMMENT = """\
FROM alpine:3.23
RUN set -eux; \\
\tapk add --no-cache --virtual .build-deps \\
\t\tgcc \\
\t\tmake \\
\t; \\
\tapk add --no-cache \\
# pull tzdata in explicitly, see https://github.com/docker-library/redis/issues/138 (and related)
\t\topenssl \\
\t\ttzdata \\
\t; \\
\tapk del --no-network .build-deps
"""

# Boundary: comment sits immediately BEFORE the install verb line.
DOCKERFILE_APK_COMMENT_BEFORE_VERB = """\
FROM alpine:3.23
RUN set -eux; \\
# comment sits immediately before the install verb line
\tapk add --no-cache \\
\t\topenssl \\
\t\ttzdata \\
\t;
"""

# Boundary: comment is the LAST line inside the block (just before its ';').
DOCKERFILE_APK_COMMENT_LAST_LINE = """\
FROM alpine:3.23
RUN set -eux; \\
\tapk add --no-cache \\
\t\topenssl \\
\t\ttzdata \\
# trailing comment is the last line inside the install block
\t;
"""


class TestParseInstallListInteriorComments:
    """Regression cover for interior '#' comment lines in real Dockerfiles."""

    def test_debian_interior_comment_keeps_real_names_drops_prose(self) -> None:
        pkgs = _parse_install_list(DOCKERFILE_APT_INTERIOR_COMMENT)
        # Real names survive despite the comment splitting the block.
        assert "tzdata" in pkgs
        assert "libssl3t64" in pkgs
        # No comment prose, URL, or '#' leaked in as a "package".
        _assert_no_junk_tokens(pkgs)

    def test_alpine_interior_comment_keeps_runtime_names_drops_virtual(self) -> None:
        pkgs = _parse_install_list(DOCKERFILE_APK_INTERIOR_COMMENT)
        assert "openssl" in pkgs
        # Other real names from both blocks are present.
        for name in ("gcc", "make", "tzdata"):
            assert name in pkgs
        # The apk virtual label is not a package, and no flag leaks through.
        assert ".build-deps" not in pkgs
        assert not any(p.startswith(("-", ".")) for p in pkgs)
        _assert_no_junk_tokens(pkgs)

    def test_comment_immediately_before_install_verb(self) -> None:
        pkgs = _parse_install_list(DOCKERFILE_APK_COMMENT_BEFORE_VERB)
        assert pkgs == ["openssl", "tzdata"]
        _assert_no_junk_tokens(pkgs)

    def test_comment_as_last_line_of_block(self) -> None:
        pkgs = _parse_install_list(DOCKERFILE_APK_COMMENT_LAST_LINE)
        assert pkgs == ["openssl", "tzdata"]
        _assert_no_junk_tokens(pkgs)

    def test_multiple_blocks_union_dedup_order_preserved(self) -> None:
        # ca-certificates appears in both blocks; kept once, first-seen order.
        df = (
            "RUN apt-get install -y --no-install-recommends libssl-dev ca-certificates\n"
            "RUN apt-get install -y --no-install-recommends ca-certificates tzdata\n"
        )
        assert _parse_install_list(df) == ["libssl-dev", "ca-certificates", "tzdata"]

    def test_flags_and_shell_variables_never_returned(self) -> None:
        # -t consumes its arg (an apt release name), flags and $vars are dropped.
        df = (
            "RUN apt-get install -y --no-install-recommends -t bookworm-backports \\\n"
            "\t\t$EXTRA ${MORE} libssl-dev\n"
        )
        pkgs = _parse_install_list(df)
        assert pkgs == ["libssl-dev"]
        for junk in (
            "-y", "--no-install-recommends", "-t", "bookworm-backports",
            "$EXTRA", "${MORE}",
        ):
            assert junk not in pkgs
        assert not any(p.startswith(("-", "$")) for p in pkgs)


class TestRealisticDockerfileEndToEnd:
    """End-to-end: a realistic interior-comment Dockerfile feeds clean names
    into the simulation, so a stale-base finding is confirmed rather than
    silently downgraded by a failed simulation on junk package names."""

    def test_interior_comment_dockerfile_feeds_clean_install_list(self) -> None:
        finding = _make_finding(
            image="valkey/valkey:9.0",
            package="libssl3t64",
            installed="3.0.11-r0",
            fixed="3.0.13-r0",
        )
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:9.0": "debian:trixie-slim"}

        sim = MagicMock(return_value={"libssl3t64": "3.0.13-r0"})
        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages",
            return_value={"libssl3t64": "3.0.11-r0"},  # base older than fix
        ), patch(
            "scripts.cve_scan.base_precheck._fetch_dockerfile",
            return_value=DOCKERFILE_APT_INTERIOR_COMMENT,
        ), patch(
            "scripts.cve_scan.base_precheck.simulate_install", sim,
        ):
            confirmed, downgraded = verify_fixable_in_base([classification], base_map)

        # The parser fed real package names (not comment prose) to the simulation.
        called_pkgs = sim.call_args.args[1]
        assert called_pkgs == ["tzdata", "libssl3t64"]
        _assert_no_junk_tokens(called_pkgs)
        # And the finding is confirmed, not downgraded on a junk-name failure.
        assert len(confirmed) == 1
        assert len(downgraded) == 0
