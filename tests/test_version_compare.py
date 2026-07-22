"""Tests for scripts/cve_scan/version_compare.py -- native version comparison.

Covers:
  - Debian comparison via mocked docker output (dpkg --compare-versions).
  - Alpine comparison via mocked docker output (apk version -t).
  - Error handling: docker failure -> None (fail-closed).
  - Error handling: timeout -> None (fail-closed).
  - Error handling: unexpected apk output -> None (fail-closed).
  - Unknown flavor -> None (fail-closed).
  - B3 regression: python comparator on 1.0-1 vs 1.0+deb12u1 -> -1.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scripts.cve_scan.version_compare import compare_versions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _docker_result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Build a mock subprocess.CompletedProcess."""
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# ---------------------------------------------------------------------------
# Debian: mocked dpkg --compare-versions
# ---------------------------------------------------------------------------


class TestCompareVersionsDebian:
    """compare_versions with flavor='debian' uses dpkg semantics."""

    def test_a_less_than_b(self) -> None:
        """a < b: dpkg lt exits 0."""
        def fake_run(cmd, **kwargs):
            # First call: dpkg a lt b -> exit 0 (a < b)
            return _docker_result(returncode=0)

        with patch("scripts.cve_scan.version_compare.subprocess.run", side_effect=[
            _docker_result(returncode=0),   # lt check: True
        ]):
            result = compare_versions("1.0", "1.1", "debian")
        assert result == -1

    def test_a_equals_b(self) -> None:
        """a == b: dpkg lt exits 1, dpkg eq exits 0."""
        with patch("scripts.cve_scan.version_compare.subprocess.run", side_effect=[
            _docker_result(returncode=1),   # lt check: False
            _docker_result(returncode=0),   # eq check: True
        ]):
            result = compare_versions("1.0", "1.0", "debian")
        assert result == 0

    def test_a_greater_than_b(self) -> None:
        """a > b: dpkg lt exits 1, dpkg eq exits 1."""
        with patch("scripts.cve_scan.version_compare.subprocess.run", side_effect=[
            _docker_result(returncode=1),   # lt check: False
            _docker_result(returncode=1),   # eq check: False -> a > b
        ]):
            result = compare_versions("1.1", "1.0", "debian")
        assert result == 1

    def test_docker_failure_returns_none(self) -> None:
        """Docker failure (unexpected exit code 125) -> None (fail-closed)."""
        with patch("scripts.cve_scan.version_compare.subprocess.run", return_value=
            _docker_result(returncode=125, stderr="docker: Error response from daemon"),
        ):
            result = compare_versions("1.0", "1.1", "debian")
        assert result is None

    def test_unexpected_exit_code_127_returns_none(self) -> None:
        """Unexpected exit code 127 (command not found) -> None (fail-closed)."""
        with patch("scripts.cve_scan.version_compare.subprocess.run", return_value=
            _docker_result(returncode=127, stderr="docker: command not found"),
        ):
            result = compare_versions("1.0", "1.1", "debian")
        assert result is None

    def test_unexpected_exit_code_126_on_eq_returns_none(self) -> None:
        """Unexpected exit code on the eq check -> None (fail-closed)."""
        with patch("scripts.cve_scan.version_compare.subprocess.run", side_effect=[
            _docker_result(returncode=1),    # lt check: False (legitimate)
            _docker_result(returncode=126),  # eq check: unexpected
        ]):
            result = compare_versions("1.0", "1.1", "debian")
        assert result is None

    def test_timeout_returns_none(self) -> None:
        """TimeoutExpired -> None (fail-closed)."""
        import subprocess
        with patch("scripts.cve_scan.version_compare.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("docker", 60)):
            result = compare_versions("1.0", "1.1", "debian")
        assert result is None

    def test_oserror_returns_none(self) -> None:
        """OSError (docker not found) -> None (fail-closed)."""
        with patch("scripts.cve_scan.version_compare.subprocess.run",
                   side_effect=OSError("No such file")):
            result = compare_versions("1.0", "1.1", "debian")
        assert result is None

    def test_plus_suffix_ordering(self) -> None:
        """1.0-1 < 1.0+deb12u1 per Debian rules: dpkg lt exits 0."""
        with patch("scripts.cve_scan.version_compare.subprocess.run", side_effect=[
            _docker_result(returncode=0),   # lt: True -> -1
        ]):
            result = compare_versions("1.0-1", "1.0+deb12u1", "debian")
        assert result == -1

    def test_uses_provided_base_image(self) -> None:
        """base_image parameter is passed to docker run."""
        calls = []

        def capture_run(cmd, **kwargs):
            calls.append(cmd)
            return _docker_result(returncode=0)

        with patch("scripts.cve_scan.version_compare.subprocess.run", side_effect=capture_run):
            compare_versions("1.0", "1.1", "debian", base_image="debian:bookworm-slim")

        assert any("debian:bookworm-slim" in " ".join(c) for c in calls)


# ---------------------------------------------------------------------------
# Alpine: mocked apk version -t
# ---------------------------------------------------------------------------


class TestCompareVersionsAlpine:
    """compare_versions with flavor='alpine' uses apk semantics."""

    def test_a_less_than_b(self) -> None:
        """apk version -t prints '<' -> -1."""
        with patch("scripts.cve_scan.version_compare._run_docker",
                   return_value=(0, "<", "")):
            result = compare_versions("3.0.12-r0", "3.0.13-r0", "alpine")
        assert result == -1

    def test_a_equals_b(self) -> None:
        """apk version -t prints '=' -> 0."""
        with patch("scripts.cve_scan.version_compare._run_docker",
                   return_value=(0, "=", "")):
            result = compare_versions("3.0.13-r0", "3.0.13-r0", "alpine")
        assert result == 0

    def test_a_greater_than_b(self) -> None:
        """apk version -t prints '>' -> 1."""
        with patch("scripts.cve_scan.version_compare._run_docker",
                   return_value=(0, ">", "")):
            result = compare_versions("3.0.14-r0", "3.0.13-r0", "alpine")
        assert result == 1

    def test_unexpected_output_returns_none(self) -> None:
        """Unexpected apk output -> None (fail-closed)."""
        with patch("scripts.cve_scan.version_compare._run_docker",
                   return_value=(0, "UNKNOWN", "")):
            result = compare_versions("3.0.12-r0", "3.0.13-r0", "alpine")
        assert result is None

    def test_docker_failure_returns_none(self) -> None:
        """Non-zero exit -> None."""
        with patch("scripts.cve_scan.version_compare._run_docker",
                   return_value=(1, "", "error")):
            result = compare_versions("3.0.12-r0", "3.0.13-r0", "alpine")
        assert result is None

    def test_timeout_returns_none(self) -> None:
        """rc == -1 (timeout/OSError from _run_docker) -> None."""
        with patch("scripts.cve_scan.version_compare._run_docker",
                   return_value=(-1, "", "timeout")):
            result = compare_versions("3.0.12-r0", "3.0.13-r0", "alpine")
        assert result is None

    def test_argv_no_shell(self) -> None:
        """Alpine path invokes argv directly: no 'sh' or '-c' in command."""
        calls: list[list[str]] = []

        def capture(cmd: list[str]) -> "tuple[int, str, str]":
            calls.append(cmd)
            return (0, "<", "")

        with patch("scripts.cve_scan.version_compare._run_docker", side_effect=capture):
            compare_versions("3.0.12-r0", "3.0.13-r0", "alpine")

        assert len(calls) == 1
        cmd = calls[0]
        assert "sh" not in cmd, f"Shell invocation found in command: {cmd}"
        assert "-c" not in cmd, f"Shell flag found in command: {cmd}"
        # Verify argv structure
        assert cmd == [
            "docker", "run", "--rm",
            "public.ecr.aws/docker/library/alpine:latest",
            "apk", "version", "-t", "3.0.12-r0", "3.0.13-r0",
        ]

    def test_stdout_whitespace_stripped(self) -> None:
        """Stdout with trailing newline/space is stripped before comparison."""
        with patch("scripts.cve_scan.version_compare._run_docker",
                   return_value=(0, " > \n", "")):
            result = compare_versions("3.0.14-r0", "3.0.13-r0", "alpine")
        assert result == 1

    def test_empty_stdout_returns_none(self) -> None:
        """Empty stdout (rc=0 but no output) -> None (fail-closed)."""
        with patch("scripts.cve_scan.version_compare._run_docker",
                   return_value=(0, "", "")):
            result = compare_versions("3.0.12-r0", "3.0.13-r0", "alpine")
        assert result is None


# ---------------------------------------------------------------------------
# Unknown flavor
# ---------------------------------------------------------------------------


class TestCompareVersionsUnknownFlavor:
    """Unknown flavor returns None (fail-closed)."""

    def test_unknown_flavor_returns_none(self) -> None:
        result = compare_versions("1.0", "1.1", "rpm")
        assert result is None


# ---------------------------------------------------------------------------
# B3 regression: Python comparator + suffix ordering
# ---------------------------------------------------------------------------


class TestPythonComparatorPlusSuffix:
    """Regression tests for the fixed pure-Python comparator.

    These tests verify that the comparator now correctly handles the Debian
    case where a + suffix indicates a later version (debian patch on top of
    the upstream release): 1.0 < 1.0+deb12u1.
    """

    def test_bare_less_than_plus_suffix(self) -> None:
        """1.0-1 < 1.0+deb12u1: Debian patch is newer than upstream."""
        from scripts.cve_scan.rebuild_decider import _compare_versions

        result = _compare_versions("1.0-1", "1.0+deb12u1")
        assert result == -1, (
            f"Expected 1.0-1 < 1.0+deb12u1 (Debian rules: + suffix is newer), got {result}"
        )

    def test_plus_suffix_greater_than_bare(self) -> None:
        """1.0+deb12u1 > 1.0: confirmed reverse direction."""
        from scripts.cve_scan.rebuild_decider import _compare_versions

        result = _compare_versions("1.0+deb12u1", "1.0")
        assert result == 1

    def test_bare_less_than_bare_plus(self) -> None:
        """3.0.13 < 3.0.13+deb12u1."""
        from scripts.cve_scan.rebuild_decider import _compare_versions

        result = _compare_versions("3.0.13", "3.0.13+deb12u1")
        assert result == -1

    def test_tilde_still_less_than_bare(self) -> None:
        """1.0~rc1 < 1.0 (tilde ordering unchanged)."""
        from scripts.cve_scan.rebuild_decider import _compare_versions

        result = _compare_versions("1.0~rc1", "1.0")
        assert result == -1

    def test_tilde_less_than_plus(self) -> None:
        """1.0~beta < 1.0+patch (tilde < anything including plus)."""
        from scripts.cve_scan.rebuild_decider import _compare_versions

        result = _compare_versions("1.0~beta", "1.0+patch")
        assert result == -1

    def test_alpine_revision_ordering(self) -> None:
        """3.0.12-r0 < 3.0.12-r1 (Alpine revision unchanged)."""
        from scripts.cve_scan.rebuild_decider import _compare_versions

        result = _compare_versions("3.0.12-r0", "3.0.12-r1")
        assert result == -1

    def test_epoch_comparison(self) -> None:
        """1:1.0 > 0:2.0 (epoch takes precedence)."""
        from scripts.cve_scan.rebuild_decider import _compare_versions

        result = _compare_versions("1:1.0", "0:2.0")
        assert result == 1

    def test_debian_real_case_libssl(self) -> None:
        """3.0.13-1~deb12u1 is a valid Debian version: installed == fixed -> not fixable."""
        from scripts.cve_scan.rebuild_decider import _compare_versions

        # installed and fixed are the same Debian version
        result = _compare_versions("3.0.13-1~deb12u1", "3.0.13-1~deb12u1")
        assert result == 0
