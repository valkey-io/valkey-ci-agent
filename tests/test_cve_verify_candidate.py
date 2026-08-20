"""Tests for scripts/cve_scan/verify_candidate.py.

Uses realistic Trivy JSON fixtures (full result objects, mixed
fixable/not-fixable vulnerabilities, real key set), not idealized stubs: an
audit caught a parser that passed only on unrealistically clean fixtures.
Trivy itself is patched at the subprocess boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.cve_scan import verify_candidate
from scripts.cve_scan.targets import Target, encode_targets

# Contract: one targeted pair on 8.0/alpine/linux/amd64.
_TARGETS_B64 = encode_targets(
    [
        Target(
            image="valkey/valkey:8.0-alpine",
            line="8.0",
            variant="alpine",
            platform="linux/amd64",
            cve="CVE-2024-1234",
            package="openssl",
            fixed_version="3.0.13-r0",
        )
    ]
)


def _trivy_doc(vulnerabilities: list[dict]) -> str:
    """Build a realistic Trivy JSON document for an alpine image scan."""
    doc = {
        "SchemaVersion": 2,
        "CreatedAt": "2026-08-20T00:00:00Z",
        "ArtifactName": "valkey/valkey:8.0-alpine",
        "ArtifactType": "container_image",
        "Metadata": {
            "OS": {"Family": "alpine", "Name": "3.23.0"},
            "ImageID": "sha256:deadbeef",
        },
        "Results": [
            {
                "Target": "valkey/valkey:8.0-alpine (alpine 3.23.0)",
                "Class": "os-pkgs",
                "Type": "alpine",
                "Vulnerabilities": vulnerabilities,
            }
        ],
    }
    return json.dumps(doc)


# openssl fix landed (targeted pair absent), but an unrelated not-fixable
# busybox CVE remains: a realistic post-rebuild scan is not pristine.
_VULN_BUSYBOX_NOFIX = {
    "VulnerabilityID": "CVE-2024-9999",
    "PkgID": "busybox@1.36.1-r0",
    "PkgName": "busybox",
    "InstalledVersion": "1.36.1-r0",
    "Severity": "HIGH",
    "Title": "busybox: some unrelated issue",
    "PrimaryURL": "https://avd.aquasec.com/nvd/cve-2024-9999",
}
# The targeted openssl CVE still present at the vulnerable version.
_VULN_OPENSSL_PRESENT = {
    "VulnerabilityID": "CVE-2024-1234",
    "PkgID": "openssl@3.0.12-r0",
    "PkgName": "openssl",
    "InstalledVersion": "3.0.12-r0",
    "FixedVersion": "3.0.13-r0",
    "Severity": "HIGH",
    "Title": "openssl: vulnerable to something",
    "PrimaryURL": "https://avd.aquasec.com/nvd/cve-2024-1234",
}


class _FakeCompleted:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_trivy(monkeypatch: pytest.MonkeyPatch, completed: _FakeCompleted) -> None:
    monkeypatch.setattr(
        verify_candidate.subprocess,
        "run",
        lambda *a, **k: completed,
    )


def _args(image_ref: str = "localbuild:8.0-alpine") -> list[str]:
    return [
        "--image-ref", image_ref,
        "--targets", _TARGETS_B64,
        "--line", "8.0",
        "--variant", "alpine",
        "--platform", "linux/amd64",
    ]


class TestPass:
    def test_exit_zero_when_targeted_pair_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The targeted openssl CVE is gone; an unrelated CVE remaining is fine."""
        _patch_trivy(monkeypatch, _FakeCompleted(0, _trivy_doc([_VULN_BUSYBOX_NOFIX])))
        assert verify_candidate.main(_args()) == 0

    def test_verify_returns_no_survivors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_trivy(monkeypatch, _FakeCompleted(0, _trivy_doc([_VULN_BUSYBOX_NOFIX])))
        passed, survivors = verify_candidate.verify(
            image_ref="localbuild:8.0-alpine",
            targets_b64=_TARGETS_B64,
            line="8.0",
            variant="alpine",
            platform="linux/amd64",
        )
        assert passed is True
        assert survivors == []

    def test_step_summary_records_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        summary = tmp_path / "step_summary"
        summary.write_text("")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        _patch_trivy(monkeypatch, _FakeCompleted(0, _trivy_doc([_VULN_BUSYBOX_NOFIX])))
        verify_candidate.main(_args())
        text = summary.read_text()
        assert "CVE Candidate Verification" in text
        assert "PASS" in text
        assert "linux/amd64" in text


class TestFailSurvivor:
    def test_exit_nonzero_when_targeted_pair_survives(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_trivy(
            monkeypatch,
            _FakeCompleted(0, _trivy_doc([_VULN_OPENSSL_PRESENT, _VULN_BUSYBOX_NOFIX])),
        )
        assert verify_candidate.main(_args()) == 1

    def test_verify_reports_surviving_pair_and_installed_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_trivy(
            monkeypatch, _FakeCompleted(0, _trivy_doc([_VULN_OPENSSL_PRESENT]))
        )
        passed, survivors = verify_candidate.verify(
            image_ref="localbuild:8.0-alpine",
            targets_b64=_TARGETS_B64,
            line="8.0",
            variant="alpine",
            platform="linux/amd64",
        )
        assert passed is False
        assert survivors == [("CVE-2024-1234", "openssl", "3.0.12-r0")]

    def test_step_summary_lists_survivor(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        summary = tmp_path / "step_summary"
        summary.write_text("")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        _patch_trivy(
            monkeypatch, _FakeCompleted(0, _trivy_doc([_VULN_OPENSSL_PRESENT]))
        )
        verify_candidate.main(_args())
        text = summary.read_text()
        assert "FAIL" in text
        assert "CVE-2024-1234" in text
        assert "openssl" in text
        assert "3.0.12-r0" in text


class TestFailClosed:
    def test_trivy_nonzero_exit_is_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_trivy(
            monkeypatch,
            _FakeCompleted(1, "", "trivy: failed to pull/scan local image"),
        )
        assert verify_candidate.main(_args()) == 2

    def test_trivy_empty_output_is_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_trivy(monkeypatch, _FakeCompleted(0, "   "))
        assert verify_candidate.main(_args()) == 2

    def test_parse_failure_is_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valid JSON but invalid Trivy schema (no SchemaVersion) fails closed."""
        bad = json.dumps({"Results": [{"Target": "x", "Vulnerabilities": []}]})
        _patch_trivy(monkeypatch, _FakeCompleted(0, bad))
        assert verify_candidate.main(_args()) == 2

    def test_invalid_json_is_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_trivy(monkeypatch, _FakeCompleted(0, "not json {{{"))
        assert verify_candidate.main(_args()) == 2

    def test_malformed_targets_contract_is_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken --targets blob fails closed before Trivy even runs."""
        _patch_trivy(monkeypatch, _FakeCompleted(0, _trivy_doc([])))
        argv = [
            "--image-ref", "localbuild:8.0-alpine",
            "--targets", "@@@ not base64 @@@",
            "--line", "8.0",
            "--variant", "alpine",
            "--platform", "linux/amd64",
        ]
        assert verify_candidate.main(argv) == 2

    def test_verify_raises_verify_error_on_trivy_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_trivy(monkeypatch, _FakeCompleted(1, "", "boom"))
        with pytest.raises(verify_candidate.VerifyError, match="exited with code 1"):
            verify_candidate.verify(
                image_ref="localbuild:8.0-alpine",
                targets_b64=_TARGETS_B64,
                line="8.0",
                variant="alpine",
                platform="linux/amd64",
            )


class TestPlatformScoping:
    def test_pair_targeted_on_other_platform_is_not_checked_here(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pair targeted only on arm64 is not asserted on the amd64 build."""
        targets_b64 = encode_targets(
            [
                Target(
                    image="valkey/valkey:8.0-alpine",
                    line="8.0",
                    variant="alpine",
                    platform="linux/arm64",
                    cve="CVE-2024-1234",
                    package="openssl",
                    fixed_version="3.0.13-r0",
                )
            ]
        )
        # openssl still present, but the contract targets it on arm64, not amd64.
        _patch_trivy(
            monkeypatch, _FakeCompleted(0, _trivy_doc([_VULN_OPENSSL_PRESENT]))
        )
        passed, survivors = verify_candidate.verify(
            image_ref="localbuild:8.0-alpine",
            targets_b64=targets_b64,
            line="8.0",
            variant="alpine",
            platform="linux/amd64",
        )
        assert passed is True
        assert survivors == []
