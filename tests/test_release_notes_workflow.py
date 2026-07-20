"""Contract tests for the simple and advanced release-notes dispatches."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

_SIMPLE = Path(".github/workflows/release-notes-cut.yml")
_ADVANCED = Path(".github/workflows/release-notes-cut-advanced.yml")


def _workflow(path: Path) -> dict:
    # BaseLoader preserves the literal `on` key instead of treating it as YAML 1.1
    # boolean True, matching GitHub's YAML interpretation closely enough for these
    # structural assertions.
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_simple_dispatch_exposes_only_normal_release_decisions() -> None:
    inputs = _workflow(_SIMPLE)["on"]["workflow_dispatch"]["inputs"]

    assert list(inputs) == ["version", "stage", "urgency", "dry_run"]
    assert inputs["version"]["required"] == "true"
    assert inputs["stage"]["required"] == "false"
    assert inputs["urgency"]["required"] == "true"
    assert inputs["dry_run"]["default"] == "true"


def test_shared_workflow_keeps_advanced_inputs_available() -> None:
    inputs = _workflow(_SIMPLE)["on"]["workflow_call"]["inputs"]

    assert set(inputs) == {
        "version",
        "stage",
        "urgency",
        "date",
        "contrib_base_ref",
        "base_ref",
        "security_fixes",
        "security_from_advisories",
        "force_ready",
        "dry_run",
    }
    assert inputs["stage"]["default"] == ""
    assert inputs["dry_run"]["default"] == "true"


def test_advanced_dispatch_delegates_to_the_shared_release_job() -> None:
    workflow = _workflow(_ADVANCED)
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    job = workflow["jobs"]["cut"]

    assert set(inputs) == {
        "version",
        "stage",
        "urgency",
        "date",
        "contrib_base_ref",
        "base_ref",
        "security_fixes",
        "security_from_advisories",
        "force_ready",
        "dry_run",
    }
    assert inputs["dry_run"]["default"] == "true"
    assert job["uses"] == "./.github/workflows/release-notes-cut.yml"
    assert job["secrets"] == "inherit"
    assert set(job["with"]) == set(inputs)
    for name in inputs:
        assert job["with"][name] == f"${{{{ inputs.{name} }}}}"


def test_release_concurrency_serializes_inferred_and_explicit_ga() -> None:
    concurrency = _workflow(_SIMPLE)["jobs"]["cut"]["concurrency"]

    assert concurrency["group"] == "release-cut-${{ inputs.version }}"
    assert concurrency["cancel-in-progress"] == "false"


def test_release_tokens_can_authorize_feedback_commenters() -> None:
    steps = _workflow(_SIMPLE)["jobs"]["cut"]["steps"]
    token_steps = [
        step for step in steps
        if step.get("id") in {"generate-token", "generate-token-advisories"}
    ]

    assert len(token_steps) == 2
    assert all(step["with"]["permission-members"] == "read" for step in token_steps)
    assert all(step["with"]["permission-pull-requests"] == "write" for step in token_steps)


def _run_cut_step(
    tmp_path: Path, *, version: str, stage: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    capture = tmp_path / "python-invocation"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$RELEASE_NOTES_STAGE" "$@" > "$CAPTURE"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "CAPTURE": str(capture),
            "RELEASE_NOTES_VERSION": version,
            "RELEASE_NOTES_STAGE": stage,
            "RELEASE_NOTES_BASE_REF": "",
            "RELEASE_NOTES_CONTRIB_BASE": "",
            "RELEASE_NOTES_SECURITY_FIXES": "",
            "RELEASE_NOTES_DRY_RUN": "true",
        }
    )
    run_script = _workflow(_SIMPLE)["jobs"]["cut"]["steps"][-1]["run"]
    result = subprocess.run(
        ["bash", "-c", run_script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    invocation = capture.read_text(encoding="utf-8") if capture.exists() else ""
    return result, invocation


def test_workflow_shell_infers_patch_ga_and_preserves_dry_run(tmp_path) -> None:
    for version in ("7.2.14", "8.0.10", "8.1.9", "9.0.5", "9.1.1"):
        result, invocation = _run_cut_step(
            tmp_path / version, version=version, stage=""
        )

        assert result.returncode == 0, result.stderr
        assert invocation.splitlines() == [
            "ga",
            "-m",
            "scripts.release_notes.main",
            "--dry-run",
        ]


def test_workflow_shell_rejects_ambiguous_dot_zero_stage(tmp_path) -> None:
    result, invocation = _run_cut_step(tmp_path, version="9.2.0", stage="")

    assert result.returncode != 0
    assert "Stage is required for a .0 release" in result.stderr
    assert invocation == ""
