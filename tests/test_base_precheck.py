"""Tests for scripts/cve_scan/base_precheck.py.

Covers confirm/downgrade paths, fail-closed behavior, per-(base, platform)
caching, docker failure handling, real-format apk/dpkg parsing, and the
repo-candidate check for packages absent from the base db. The docker-based
native comparator is patched with a deterministic local stub (autouse
fixture) to avoid real Docker calls; the real comparator is tested in
test_version_compare.py.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from scripts.cve_scan.base_precheck import (
    BasePrecheckError,
    _parse_apk_installed,
    _parse_apk_policy,
    _parse_apt_cache_policy,
    _parse_dpkg_query,
    get_base_packages,
    get_repo_candidates,
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
) -> Finding:
    return Finding(
        image=image,
        package=package,
        installed_version=installed,
        cve_id=cve_id,
        severity=Severity.HIGH,
        fixed_version=fixed,
    )


def _make_classification(finding: Finding) -> Classification:
    return Classification(
        finding=finding,
        fixable=True,
        rationale=f"A rebuild would upgrade {finding.package}.",
    )


# Canned Alpine apk db content (real format from /lib/apk/db/installed)
SAMPLE_APK_DB = """\
C:Q1abc123=
P:musl
V:1.2.5-r0
A:x86_64
S:383152
I:622592
T:the musl c library (libc) implementation
U:https://musl.libc.org/
L:MIT
o:musl
m:Maintainer <m@example.com>
t:1700000000
c:abc123
D:
p:so:libc.musl-x86_64.so.1=1

C:Q1def456=
P:openssl
V:3.0.13-r0
A:x86_64
S:2800000
I:7200000
T:toolkit for TLS
U:https://www.openssl.org/
L:Apache-2.0
o:openssl
m:Maintainer <m@example.com>
t:1700000001
c:def456
D:so:libc.musl-x86_64.so.1
p:so:libcrypto.so.3=3

C:Q1ghi789=
P:zlib
V:1.3.1-r0
A:x86_64
S:53248
I:114688
T:zlib compression library
U:https://zlib.net/
L:Zlib
o:zlib
m:Maintainer <m@example.com>
t:1700000002
c:ghi789
D:so:libc.musl-x86_64.so.1
p:so:libz.so.1=1

"""

# Canned Debian dpkg-query output
SAMPLE_DPKG_OUTPUT = """\
adduser 3.134
apt 2.7.14
base-files 13.5
bash 5.2.21-2+deb12u1
coreutils 9.1-1
dpkg 1.22.6
libc6 2.36-9+deb12u9
libssl3 3.0.13-1~deb12u1
openssl 3.0.13-1~deb12u1
zlib1g 1:1.2.13.dfsg-1
"""


class TestBaseOlderThanFix:
    """Base has an older version than the fix -> downgrade."""

    def test_downgraded_when_base_has_old_version(self) -> None:
        finding = _make_finding()
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:9.1-alpine": "alpine:3.23"}

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get:
            # Base has openssl 3.0.12-r0 (older than fix 3.0.13-r0)
            mock_get.return_value = {"openssl": "3.0.12-r0", "musl": "1.2.5-r0"}
            confirmed, downgraded = verify_fixable_in_base(
                [classification], base_map
            )

        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert downgraded[0].fixable is False
        assert "still ships" in downgraded[0].rationale
        assert "3.0.12-r0" in downgraded[0].rationale
        assert "alpine:3.23" in downgraded[0].rationale
        assert "CVE-2024-1234" in downgraded[0].rationale

    def test_downgraded_rationale_mentions_package(self) -> None:
        finding = _make_finding(package="zlib")
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:9.1-alpine": "alpine:3.23"}

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get:
            mock_get.return_value = {"zlib": "1.2.13-r0"}
            _, downgraded = verify_fixable_in_base(
                [classification], base_map
            )

        assert "zlib" in downgraded[0].rationale


class TestBaseHasFix:
    """Base has the fix (installed >= fixed) -> confirmed."""

    def test_confirmed_when_base_has_newer_version(self) -> None:
        finding = _make_finding(installed="3.0.12-r0", fixed="3.0.13-r0")
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:9.1-alpine": "alpine:3.23"}

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get:
            mock_get.return_value = {"openssl": "3.0.14-r0"}
            confirmed, downgraded = verify_fixable_in_base(
                [classification], base_map
            )

        assert len(confirmed) == 1
        assert len(downgraded) == 0
        assert "Verified: base alpine:3.23 ships 3.0.14-r0" in confirmed[0].rationale

    def test_confirmed_when_base_equals_fixed(self) -> None:
        finding = _make_finding(installed="3.0.12-r0", fixed="3.0.13-r0")
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:9.1-alpine": "alpine:3.23"}

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get:
            mock_get.return_value = {"openssl": "3.0.13-r0"}
            confirmed, downgraded = verify_fixable_in_base(
                [classification], base_map
            )

        assert len(confirmed) == 1
        assert confirmed[0].fixable is True
        assert "Verified: base alpine:3.23 ships 3.0.13-r0" in confirmed[0].rationale


# Canned `apk policy openssl` output (candidate at the fix version)
SAMPLE_APK_POLICY = """\
openssl policy:
  3.0.13-r0:
    https://dl-cdn.alpinelinux.org/alpine/v3.23/main
"""

# Canned `apk policy openssl` output (candidate older than the fix)
SAMPLE_APK_POLICY_OLD = """\
openssl policy:
  3.0.12-r0:
    https://dl-cdn.alpinelinux.org/alpine/v3.23/main
"""

# Canned `apk policy` output listing multiple versions
SAMPLE_APK_POLICY_MULTI = """\
openssl policy:
  3.0.12-r0:
    https://dl-cdn.alpinelinux.org/alpine/v3.22/main
  3.0.14-r0:
    https://dl-cdn.alpinelinux.org/alpine/v3.23/main
"""

# Canned `apt-cache policy` output with a real candidate
SAMPLE_APT_POLICY = """\
openssl:
  Installed: (none)
  Candidate: 3.0.13
  Version table:
     3.0.13 500
        500 http://deb.debian.org/debian trixie/main amd64 Packages
"""

# Canned `apt-cache policy` output with no candidate
SAMPLE_APT_POLICY_NONE = """\
openssl:
  Installed: (none)
  Candidate: (none)
  Version table:
"""


class TestPackageAbsentFromBase:
    """Package not in base db -> verified against the distro repo candidate (fail-closed)."""

    BASE_MAP = {"valkey/valkey:9.1-alpine": "alpine:3.23"}
    # openssl deliberately absent from the base package db
    BASE_PACKAGES = {"musl": "1.2.5-r0"}

    def test_confirmed_when_repo_candidate_at_fix(self) -> None:
        """Absent package + repo candidate >= fixed -> confirmed, rationale names the candidate."""
        finding = _make_finding()  # openssl, fixed 3.0.13-r0
        classification = _make_classification(finding)

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get, patch(
            "scripts.cve_scan.base_precheck.subprocess.run"
        ) as mock_run:
            mock_get.return_value = self.BASE_PACKAGES
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=SAMPLE_APK_POLICY, stderr="",
            )
            confirmed, downgraded = verify_fixable_in_base(
                [classification], self.BASE_MAP
            )

        assert len(confirmed) == 1
        assert len(downgraded) == 0
        assert confirmed[0].fixable is True
        assert "not in base image" in confirmed[0].rationale
        assert "repo candidate 3.0.13-r0" in confirmed[0].rationale

    def test_confirmed_when_any_listed_candidate_at_fix(self) -> None:
        """Multiple listed versions: confirmed when any candidate >= fixed."""
        finding = _make_finding()  # fixed 3.0.13-r0; policy lists 3.0.12 and 3.0.14
        classification = _make_classification(finding)

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get, patch(
            "scripts.cve_scan.base_precheck.subprocess.run"
        ) as mock_run:
            mock_get.return_value = self.BASE_PACKAGES
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=SAMPLE_APK_POLICY_MULTI, stderr="",
            )
            confirmed, downgraded = verify_fixable_in_base(
                [classification], self.BASE_MAP
            )

        assert len(confirmed) == 1
        assert len(downgraded) == 0
        assert "repo candidate 3.0.14-r0" in confirmed[0].rationale

    def test_downgraded_when_repo_candidate_older(self) -> None:
        """Absent package + repo candidate older than fix -> downgraded fail-closed."""
        finding = _make_finding()  # fixed 3.0.13-r0; policy has 3.0.12-r0
        classification = _make_classification(finding)

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get, patch(
            "scripts.cve_scan.base_precheck.subprocess.run"
        ) as mock_run:
            mock_get.return_value = self.BASE_PACKAGES
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=SAMPLE_APK_POLICY_OLD, stderr="",
            )
            confirmed, downgraded = verify_fixable_in_base(
                [classification], self.BASE_MAP
            )

        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert downgraded[0].fixable is False
        assert "repo candidate unverified/older" in downgraded[0].rationale
        assert "CVE-2024-1234" in downgraded[0].rationale

    def test_downgraded_when_apt_candidate_none(self) -> None:
        """Debian base: 'Candidate: (none)' -> downgraded fail-closed."""
        finding = _make_finding(image="valkey/valkey:8.0", fixed="3.0.13")
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:8.0": "debian:trixie-slim"}

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get, patch(
            "scripts.cve_scan.base_precheck.subprocess.run"
        ) as mock_run:
            mock_get.return_value = {"bash": "5.2.21-2"}
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=SAMPLE_APT_POLICY_NONE, stderr="",
            )
            confirmed, downgraded = verify_fixable_in_base(
                [classification], base_map
            )

        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert "repo candidate unverified/older" in downgraded[0].rationale

    def test_downgraded_on_network_failure(self) -> None:
        """Nonzero docker exit (e.g. no network for index update) -> downgraded fail-closed."""
        finding = _make_finding()
        classification = _make_classification(finding)

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get, patch(
            "scripts.cve_scan.base_precheck.subprocess.run"
        ) as mock_run:
            mock_get.return_value = self.BASE_PACKAGES
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="network unreachable",
            )
            confirmed, downgraded = verify_fixable_in_base(
                [classification], self.BASE_MAP
            )

        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert "repo candidate unverified/older" in downgraded[0].rationale

    def test_downgraded_on_timeout(self) -> None:
        """Repo query timeout -> downgraded fail-closed."""
        finding = _make_finding()
        classification = _make_classification(finding)

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get, patch(
            "scripts.cve_scan.base_precheck.subprocess.run"
        ) as mock_run:
            mock_get.return_value = self.BASE_PACKAGES
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["docker", "run"], timeout=180
            )
            confirmed, downgraded = verify_fixable_in_base(
                [classification], self.BASE_MAP
            )

        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert "repo candidate unverified/older" in downgraded[0].rationale

    def test_repo_candidate_cached_per_base_platform_package(self) -> None:
        """Two findings for the same (base, platform, package) -> one repo query."""
        finding1 = _make_finding(cve_id="CVE-2024-1111")
        finding2 = _make_finding(cve_id="CVE-2024-2222")
        classifications = [
            _make_classification(finding1),
            _make_classification(finding2),
        ]

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get, patch(
            "scripts.cve_scan.base_precheck.subprocess.run"
        ) as mock_run:
            mock_get.return_value = self.BASE_PACKAGES
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=SAMPLE_APK_POLICY, stderr="",
            )
            confirmed, downgraded = verify_fixable_in_base(
                classifications, self.BASE_MAP
            )

        assert mock_run.call_count == 1
        assert len(confirmed) == 2
        assert len(downgraded) == 0


class TestGetRepoCandidates:
    """Command construction (argv-safe) and failure handling for the repo query."""

    def test_package_passed_as_positional_arg_not_interpolated(self) -> None:
        """Package name goes through "$1", never into the sh -c script."""
        hostile = 'openssl"; rm -rf /'

        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr="",
            )
            get_repo_candidates("alpine:3.23", hostile, platform="linux/arm64")

        cmd = mock_run.call_args[0][0]
        assert cmd[:2] == ["docker", "run"]
        assert "--platform" in cmd and "linux/arm64" in cmd
        script = cmd[cmd.index("-c") + 1]
        assert '"$1"' in script
        assert hostile not in script
        # Positional args: sh -c <script> _ <pkg>
        assert cmd[-2] == "_"
        assert cmd[-1] == hostile

    def test_debian_uses_apt_cache_policy(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=SAMPLE_APT_POLICY, stderr="",
            )
            candidates = get_repo_candidates("debian:trixie-slim", "openssl")

        script = mock_run.call_args[0][0][mock_run.call_args[0][0].index("-c") + 1]
        assert "apt-get update" in script
        assert "apt-cache policy" in script
        assert candidates == ["3.0.13"]

    def test_unknown_flavor_returns_empty(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            assert get_repo_candidates("ubuntu:22.04", "openssl") == []
        mock_run.assert_not_called()

    def test_oserror_returns_empty(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("docker not found")
            assert get_repo_candidates("alpine:3.23", "openssl") == []


class TestApkPolicyParsing:
    """Parse real-format `apk policy` output."""

    def test_single_version(self) -> None:
        assert _parse_apk_policy(SAMPLE_APK_POLICY) == ["3.0.13-r0"]

    def test_multiple_versions(self) -> None:
        assert _parse_apk_policy(SAMPLE_APK_POLICY_MULTI) == ["3.0.12-r0", "3.0.14-r0"]

    def test_garbage_returns_empty(self) -> None:
        assert _parse_apk_policy("no such package\n") == []

    def test_empty_returns_empty(self) -> None:
        assert _parse_apk_policy("") == []


class TestAptCachePolicyParsing:
    """Parse real-format `apt-cache policy` output."""

    def test_candidate_extracted(self) -> None:
        assert _parse_apt_cache_policy(SAMPLE_APT_POLICY) == ["3.0.13"]

    def test_none_candidate_returns_empty(self) -> None:
        assert _parse_apt_cache_policy(SAMPLE_APT_POLICY_NONE) == []

    def test_missing_candidate_line_returns_empty(self) -> None:
        assert _parse_apt_cache_policy("openssl:\n  Installed: (none)\n") == []


class TestAmbiguousComparison:
    """Ambiguous version comparison (None) -> downgrade conservatively."""

    def test_downgraded_on_ambiguous_comparison(self) -> None:
        # Use versions that produce ambiguous comparison (mixed int/alpha at same position)
        finding = _make_finding(
            package="weird-pkg",
            installed="1.0.0",
            fixed="1.0.0beta1",
        )
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:9.1-alpine": "alpine:3.23"}

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get:
            # Non-numeric version: the stub comparator returns None (ambiguous)
            mock_get.return_value = {"weird-pkg": "1.0.0alpha2"}
            confirmed, downgraded = verify_fixable_in_base(
                [classification], base_map
            )

        # Ambiguous comparison downgrades (fail-closed)
        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert downgraded[0].fixable is False

    def test_downgraded_with_mocked_ambiguous_comparison(self) -> None:
        """Direct test with mocked compare_versions returning None (native comparator)."""
        finding = _make_finding(package="libcurl")
        classification = _make_classification(finding)
        base_map = {"valkey/valkey:9.1-alpine": "alpine:3.23"}

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get, patch(
            "scripts.cve_scan.base_precheck._native_compare"
        ) as mock_cmp:
            mock_get.return_value = {"libcurl": "7.88.0"}
            mock_cmp.return_value = None  # Ambiguous / error -> fail-closed
            confirmed, downgraded = verify_fixable_in_base(
                [classification], base_map
            )

        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert downgraded[0].fixable is False
        assert "ambiguous" in downgraded[0].rationale


class TestBaseCaching:
    """Multiple images sharing a base image -> single get_base_packages call."""

    def test_shared_base_queried_once(self) -> None:
        finding1 = _make_finding(
            image="valkey/valkey:9.1-alpine",
            cve_id="CVE-2024-1111",
        )
        finding2 = _make_finding(
            image="valkey/valkey:9.0-alpine",
            cve_id="CVE-2024-2222",
        )
        classifications = [
            _make_classification(finding1),
            _make_classification(finding2),
        ]
        base_map = {
            "valkey/valkey:9.1-alpine": "alpine:3.23",
            "valkey/valkey:9.0-alpine": "alpine:3.23",
        }

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get:
            mock_get.return_value = {"openssl": "3.0.13-r0"}
            confirmed, downgraded = verify_fixable_in_base(
                classifications, base_map
            )

        # Both findings have platform="" so cache key is ("alpine:3.23", "")
        assert mock_get.call_count == 1
        mock_get.assert_called_once_with("alpine:3.23", platform="")
        assert len(confirmed) == 2
        assert len(downgraded) == 0

    def test_same_base_different_platforms_queried_separately(self) -> None:
        """Same base ref on different platforms -> separate get_base_packages calls."""
        finding_amd64 = Finding(
            image="valkey/valkey:9.1-alpine",
            package="openssl",
            installed_version="3.0.12-r0",
            cve_id="CVE-2024-1111",
            severity=Severity.HIGH,
            fixed_version="3.0.13-r0",
            platform="linux/amd64",
        )
        finding_arm64 = Finding(
            image="valkey/valkey:9.1-alpine",
            package="openssl",
            installed_version="3.0.12-r0",
            cve_id="CVE-2024-1111",
            severity=Severity.HIGH,
            fixed_version="3.0.13-r0",
            platform="linux/arm64",
        )
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
            else:
                return {"openssl": "3.0.12-r0"}  # arm64 base is stale

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages",
            side_effect=mock_get,
        ):
            confirmed, downgraded = verify_fixable_in_base(
                classifications, base_map
            )

        # Two calls: one per (base_ref, platform) pair
        assert len(call_args) == 2
        assert ("alpine:3.23", "linux/amd64") in call_args
        assert ("alpine:3.23", "linux/arm64") in call_args
        # amd64 confirmed, arm64 downgraded
        assert len(confirmed) == 1
        assert confirmed[0].finding.platform == "linux/amd64"
        assert len(downgraded) == 1
        assert downgraded[0].finding.platform == "linux/arm64"
        assert "still ships" in downgraded[0].rationale


class TestMissingBaseMap:
    """Image not in base_map -> downgraded conservatively (fail-closed)."""

    def test_missing_base_map_entry_downgrades(self) -> None:
        finding = _make_finding(image="custom/image:latest")
        classification = _make_classification(finding)
        base_map: dict[str, str] = {}

        with patch(
            "scripts.cve_scan.base_precheck.get_base_packages"
        ) as mock_get:
            confirmed, downgraded = verify_fixable_in_base(
                [classification], base_map
            )

        mock_get.assert_not_called()
        assert len(confirmed) == 0
        assert len(downgraded) == 1
        assert downgraded[0].fixable is False
        assert "No base image mapping" in downgraded[0].rationale
        assert "fail-closed" in downgraded[0].rationale


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
    """Docker failure or timeout -> raises BasePrecheckError."""

    def test_nonzero_exit_raises(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["docker", "run", "--rm", "alpine:3.23", "cat", "/lib/apk/db/installed"],
                returncode=1,
                stdout="",
                stderr="Error: image not found",
            )
            with pytest.raises(BasePrecheckError, match="docker run failed"):
                get_base_packages("alpine:3.23")

    def test_timeout_raises(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["docker", "run"], timeout=300
            )
            with pytest.raises(BasePrecheckError, match="Timed out"):
                get_base_packages("alpine:3.23")

    def test_empty_output_raises(self) -> None:
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["docker", "run", "--rm", "alpine:3.23", "cat", "/lib/apk/db/installed"],
                returncode=0,
                stdout="",
                stderr="",
            )
            with pytest.raises(BasePrecheckError, match="Empty package database"):
                get_base_packages("alpine:3.23")


class TestApkDbParsing:
    """Parse real-format /lib/apk/db/installed content."""

    def test_parse_real_apk_db(self) -> None:
        packages = _parse_apk_installed(SAMPLE_APK_DB)
        assert packages["musl"] == "1.2.5-r0"
        assert packages["openssl"] == "3.0.13-r0"
        assert packages["zlib"] == "1.3.1-r0"
        assert len(packages) == 3

    def test_get_base_packages_alpine_with_real_format(self) -> None:
        """get_base_packages parses real apk db from docker output."""
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["docker", "run", "--rm", "alpine:3.23", "cat", "/lib/apk/db/installed"],
                returncode=0,
                stdout=SAMPLE_APK_DB,
                stderr="",
            )
            result = get_base_packages("alpine:3.23")

        assert result["musl"] == "1.2.5-r0"
        assert result["openssl"] == "3.0.13-r0"
        assert result["zlib"] == "1.3.1-r0"
        assert len(result) == 3

    def test_parse_apk_db_no_trailing_blank_line(self) -> None:
        """Last stanza without trailing blank line is still captured."""
        raw = "P:busybox\nV:1.36.1-r0\n"
        packages = _parse_apk_installed(raw)
        assert packages["busybox"] == "1.36.1-r0"

    def test_parse_apk_db_empty(self) -> None:
        assert _parse_apk_installed("") == {}

    def test_parse_apk_db_only_blanks(self) -> None:
        assert _parse_apk_installed("\n\n\n") == {}


class TestDpkgQueryParsing:
    """Parse real-format dpkg-query output."""

    def test_parse_real_dpkg_output(self) -> None:
        packages = _parse_dpkg_query(SAMPLE_DPKG_OUTPUT)
        assert packages["bash"] == "5.2.21-2+deb12u1"
        assert packages["openssl"] == "3.0.13-1~deb12u1"
        assert packages["zlib1g"] == "1:1.2.13.dfsg-1"
        assert packages["dpkg"] == "1.22.6"
        assert len(packages) == 10

    def test_get_base_packages_debian_with_real_format(self) -> None:
        """get_base_packages parses real dpkg-query output from docker."""
        with patch("scripts.cve_scan.base_precheck.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[
                    "docker", "run", "--rm", "debian:trixie-slim",
                    "dpkg-query", "-W", "-f", "${Package} ${Version}\n",
                ],
                returncode=0,
                stdout=SAMPLE_DPKG_OUTPUT,
                stderr="",
            )
            result = get_base_packages("debian:trixie-slim")

        assert result["openssl"] == "3.0.13-1~deb12u1"
        assert result["bash"] == "5.2.21-2+deb12u1"
        assert len(result) == 10

    def test_parse_dpkg_empty(self) -> None:
        assert _parse_dpkg_query("") == {}

    def test_parse_dpkg_trailing_whitespace(self) -> None:
        raw = "  bash 5.2.21  \n  dpkg 1.22.6  \n"
        packages = _parse_dpkg_query(raw)
        assert packages["bash"] == "5.2.21"
        assert packages["dpkg"] == "1.22.6"
