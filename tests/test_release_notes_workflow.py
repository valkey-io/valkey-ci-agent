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

    assert list(inputs) == ["repo", "version", "stage", "urgency", "dry_run"]
    assert inputs["repo"]["required"] == "true"
    assert inputs["repo"]["default"] == "valkey"
    assert inputs["repo"]["options"] == [
        "valkey", "valkey-search", "valkey-json", "valkey-bloom",
    ]
    assert inputs["version"]["required"] == "true"
    assert inputs["stage"]["required"] == "false"
    assert inputs["urgency"]["required"] == "true"
    assert inputs["dry_run"]["default"] == "true"


def test_shared_workflow_keeps_advanced_inputs_available() -> None:
    inputs = _workflow(_SIMPLE)["on"]["workflow_call"]["inputs"]

    assert set(inputs) == {
        "repo",
        "version",
        "stage",
        "urgency",
        "date",
        "contrib_base_ref",
        "base_ref",
        "security_fixes",
        "security_from_advisories",
        "force_ready",
        "release_owner",
        "dry_run",
    }
    assert inputs["repo"]["default"] == "valkey"
    assert inputs["stage"]["default"] == ""
    assert inputs["dry_run"]["default"] == "true"


def test_advanced_dispatch_delegates_to_the_shared_release_job() -> None:
    workflow = _workflow(_ADVANCED)
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    job = workflow["jobs"]["cut"]

    assert set(inputs) == {
        "repo",
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
    # The called job resolves credentials from its branch-restricted
    # release-control environment; a manually selected ref must not inherit
    # the repository's unrelated secret set.
    assert "secrets" not in job
    assert set(job["with"]) == set(inputs)
    for name in inputs:
        assert job["with"][name] == f"${{{{ inputs.{name} }}}}"


def test_release_concurrency_serializes_inferred_and_explicit_ga() -> None:
    concurrency = _workflow(_SIMPLE)["jobs"]["cut"]["concurrency"]

    assert concurrency["group"] == "release-cut-${{ inputs.repo }}-${{ inputs.version }}"
    assert concurrency["cancel-in-progress"] == "false"


def test_app_credentials_require_both_secrets() -> None:
    job_env = _workflow(_SIMPLE)["jobs"]["cut"]["env"]

    assert job_env["HAS_APP_CREDS"] == (
        "${{ secrets.VALKEY_RELEASE_CONTROL_APP_ID != '' && "
        "secrets.VALKEY_RELEASE_CONTROL_APP_PRIVATE_KEY != '' }}"
    )


def test_app_tokens_are_scoped_to_the_selected_repo() -> None:
    steps = _workflow(_SIMPLE)["jobs"]["cut"]["steps"]
    tokens = [
        step for step in steps if "create-github-app-token" in step.get("uses", "")
    ]

    assert len(tokens) == 2
    for step in tokens:
        assert step["with"]["owner"] == "valkey-io"
        assert step["with"]["repositories"] == "${{ inputs.repo }}"


def test_repo_gate_runs_before_any_token_is_minted() -> None:
    # The allowlist must reject a bad repo name before an App token could be
    # minted for it (defense-in-depth on top of the dispatch choice list).
    steps = _workflow(_SIMPLE)["jobs"]["cut"]["steps"]
    names = [step.get("name", "") for step in steps]
    gate = names.index("Validate target repository")
    tokens = [i for i, step in enumerate(steps)
              if "create-github-app-token" in step.get("uses", "")]
    assert tokens, "expected App token steps in the release job"
    assert gate < min(tokens)


def test_repo_gate_rejects_unsupported_repo(tmp_path) -> None:
    steps = _workflow(_SIMPLE)["jobs"]["cut"]["steps"]
    gate = next(s for s in steps if s.get("name") == "Validate target repository")
    for repo, ok in (("valkey", True), ("valkey-bloom", True),
                     ("valkey-evil", False), ("", False)):
        env = os.environ.copy()
        env["RELEASE_NOTES_TARGET_REPO_NAME"] = repo
        result = subprocess.run(
            ["bash", "-c", gate["run"]], env=env, text=True,
            capture_output=True, check=False,
        )
        assert (result.returncode == 0) == ok, (repo, result.stderr)
        if not ok:
            assert "Invalid repo" in result.stderr


def _run_cut_step(
    tmp_path: Path, *, version: str, stage: str, repo: str = "valkey",
    env_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    capture = tmp_path / "python-invocation"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$RELEASE_NOTES_REPO" "$RELEASE_NOTES_STAGE" "$@" > "$CAPTURE"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "CAPTURE": str(capture),
            "RELEASE_NOTES_TARGET_REPO_NAME": repo,
            "RELEASE_NOTES_GITHUB_TOKEN": "dummy-token",
            "RELEASE_NOTES_DRY_RUN_FALLBACK_TOKEN": "false",
            "RELEASE_NOTES_VERSION": version,
            "RELEASE_NOTES_STAGE": stage,
            "RELEASE_NOTES_BASE_REF": "",
            "RELEASE_NOTES_CONTRIB_BASE": "",
            "RELEASE_NOTES_SECURITY_FIXES": "",
            "RELEASE_NOTES_DRY_RUN": "true",
        }
    )
    env.update(env_overrides or {})
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
            "valkey-io/valkey",
            "ga",
            "-m",
            "scripts.release_notes.main",
            "--dry-run",
        ]


def test_workflow_shell_builds_module_repo_full_name(tmp_path) -> None:
    result, invocation = _run_cut_step(
        tmp_path, version="1.2.2", stage="", repo="valkey-search"
    )

    assert result.returncode == 0, result.stderr
    assert invocation.splitlines()[0] == "valkey-io/valkey-search"


def test_workflow_shell_fallback_token_refuses_non_dry_run(tmp_path) -> None:
    # The personal-token fallback (fork testing without App creds) is read-only:
    # a real publish must never run on it.
    result, invocation = _run_cut_step(
        tmp_path, version="1.2.2", stage="", repo="valkey-search",
        env_overrides={
            "RELEASE_NOTES_DRY_RUN_FALLBACK_TOKEN": "true",
            "RELEASE_NOTES_DRY_RUN": "false",
        },
    )
    assert result.returncode != 0
    assert "Refusing a non-dry-run cut" in result.stderr
    assert invocation == ""


def test_workflow_shell_fallback_token_allows_dry_run(tmp_path) -> None:
    result, _ = _run_cut_step(
        tmp_path, version="1.2.2", stage="", repo="valkey-search",
        env_overrides={"RELEASE_NOTES_DRY_RUN_FALLBACK_TOKEN": "true"},
    )
    assert result.returncode == 0, result.stderr


def test_workflow_shell_requires_some_token(tmp_path) -> None:
    result, invocation = _run_cut_step(
        tmp_path, version="1.2.2", stage="", repo="valkey-search",
        env_overrides={"RELEASE_NOTES_GITHUB_TOKEN": ""},
    )
    assert result.returncode != 0
    assert "No token available" in result.stderr
    assert invocation == ""


def test_workflow_shell_rejects_path_shaped_repo_name(tmp_path) -> None:
    # The cut step re-checks shape only (the allowlist gate ran earlier): a
    # path-like value must never compose into RELEASE_NOTES_REPO.
    result, invocation = _run_cut_step(
        tmp_path, version="1.0.1", stage="", repo="valkey-io/valkey"
    )

    assert result.returncode != 0
    assert "Invalid repo" in result.stderr
    assert invocation == ""


def test_workflow_shell_rejects_ambiguous_dot_zero_stage(tmp_path) -> None:
    result, invocation = _run_cut_step(tmp_path, version="9.2.0", stage="")

    assert result.returncode != 0
    assert "Stage is required for a .0 release" in result.stderr
    assert invocation == ""
