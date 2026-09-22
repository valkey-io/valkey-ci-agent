from __future__ import annotations

import re
from pathlib import Path

import yaml

_USE_RE = re.compile(r"uses:\s*([^@\s]+)@([^#\s]+)")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_CLAUDE_CODE_INSTALL_RE = re.compile(r"npm install -g @anthropic-ai/claude-code(?:@([^\s]+))?")
_PIP_INSTALL_RE = r"(?:python -m )?pip install"
_OLD_REQUIREMENTS_INSTALL_RE = re.compile(rf"{_PIP_INSTALL_RE} (?:-r requirements\.txt|pyyaml)")
_DIRECT_AGENT_SETUP_RE = re.compile(
    rf"actions/setup-python@|{_PIP_INSTALL_RE} \.|{_PIP_INSTALL_RE} -e|npm install -g @anthropic-ai/claude-code"
)
_EXPECTED_CLAUDE_CODE_VERSION = "2.1.175"
_TRUSTED_REUSABLE_WORKFLOW_REFS = {
    # Qualification is an organization-owned workflow, not a third-party
    # action. It intentionally follows the automation repository's protected
    # main branch so the two release components can evolve together without a
    # tag-management bootstrap cycle. The called workflow pins all of its
    # implementation checkouts to github.workflow_sha, the exact commit this
    # reference resolved to. It receives no secrets, and candidate-code jobs
    # are constrained to contents:read. Pinning this reference to a SHA would
    # add no security boundary: the production artifacts themselves are built
    # by build-release.yml in that same repository at its own protected refs,
    # so an attacker who controls valkey-release-automation main already
    # controls the release outputs regardless of how qualification is called.
    "valkey-io/valkey-release-automation/.github/workflows/qualify-release.yml@main",
}


def _workflow_files():
    return sorted(Path(".github/workflows").glob("*.yml"))


def _action_files():
    action_dir = Path(".github/actions")
    if not action_dir.exists():
        return []
    return sorted([*action_dir.glob("**/*.yml"), *action_dir.glob("**/*.yaml")])


def _automation_yaml_files():
    return [*_workflow_files(), *_action_files()]


def test_automation_yaml_files_parse():
    for path in _automation_yaml_files():
        assert yaml.safe_load(path.read_text(encoding="utf-8")) is not None


def test_external_actions_are_pinned_to_shas():
    offenders = []
    for path in _automation_yaml_files():
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = _USE_RE.search(line)
            if not match:
                continue
            action, ref = match.groups()
            if action.startswith("./"):
                continue
            if f"{action}@{ref}" in _TRUSTED_REUSABLE_WORKFLOW_REFS:
                continue
            if not _SHA_RE.fullmatch(ref):
                offenders.append(f"{path}:{line_no}: {action}@{ref}")

    assert offenders == []


def test_claude_code_install_is_version_pinned_consistently():
    offenders = []
    for path in _automation_yaml_files():
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = _CLAUDE_CODE_INSTALL_RE.search(line)
            if not match:
                continue
            version = match.group(1)
            if version != _EXPECTED_CLAUDE_CODE_VERSION:
                offenders.append(
                    f"{path}:{line_no}: expected claude-code@{_EXPECTED_CLAUDE_CODE_VERSION}, got {version or 'latest'}"
                )

    assert offenders == []


def test_workflows_install_project_metadata_not_duplicate_requirements():
    offenders = []
    for path in _automation_yaml_files():
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _OLD_REQUIREMENTS_INSTALL_RE.search(line):
                offenders.append(f"{path}:{line_no}: {line.strip()}")

    assert offenders == []


def test_workflows_use_shared_agent_setup_action():
    offenders = []
    for path in _workflow_files():
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _DIRECT_AGENT_SETUP_RE.search(line):
                offenders.append(f"{path}:{line_no}: {line.strip()}")

    assert offenders == []


def test_github_app_tokens_request_explicit_permissions():
    offenders = []
    token_action = "uses: actions/create-github-app-token@"
    for path in _workflow_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_no, line in enumerate(lines, start=1):
            if token_action not in line:
                continue
            block = []
            for later in lines[line_no:]:
                if later.startswith("      - name: "):
                    break
                block.append(later)
            if not any("permission-" in later for later in block):
                offenders.append(f"{path}:{line_no}: create-github-app-token has no explicit permissions")

    assert offenders == []


def test_push_capable_app_tokens_can_update_workflows():
    """Push tokens need workflows:write for commits touching .github/workflows."""
    required_steps = {
        ".github/workflows/backport.yml": "Generate GitHub App token",
        ".github/workflows/backport-ci-followup.yml": "Generate target repository token",
        ".github/workflows/backport-poll.yml": "Generate publication token",
        ".github/workflows/backport-sweep.yml": "Generate publication token",
        ".github/workflows/ci-fix.yml": "Generate GitHub App token",
        ".github/workflows/manual-revert-commit.yml": "Generate GitHub App token",
    }
    offenders = []
    for workflow, step_name in required_steps.items():
        path = Path(workflow)
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_no, line in enumerate(lines, start=1):
            if line.strip() != f"- name: {step_name}":
                continue
            block = []
            for later in lines[line_no:]:
                if later.startswith("      - name: "):
                    break
                block.append(later)
            if not any("permission-workflows: write" in later for later in block):
                offenders.append(f"{path}:{line_no}: {step_name} lacks permission-workflows: write")
            break
        else:
            offenders.append(f"{path}: missing token step {step_name!r}")

    assert offenders == []


def test_backport_workflows_refresh_credentials_after_validation():
    for filename, job_name in (
        ("backport-sweep.yml", "sweep"),
        ("backport-poll.yml", "poll"),
    ):
        text = (Path(".github/workflows") / filename).read_text(encoding="utf-8")
        assert (
            text.index("- name: Generate preparation token")
            < text.index("- name: Prepare backport sweep")
            < text.index("- name: Generate publication token")
            < text.index("- name: Publish backport sweep")
            < text.index("- name: Clean up prepared sweep")
        )
        assert "TARGET_TOKEN: ${{ steps.prepare-token.outputs.token }}" in text
        assert "TARGET_TOKEN: ${{ steps.publish-token.outputs.token }}" in text
        assert "--target-token" not in text

        workflow = yaml.load(text, Loader=yaml.BaseLoader)
        job = workflow["jobs"][job_name]
        aws_step = next(
            step
            for step in job["steps"]
            if step.get("name") == "Configure AWS credentials"
        )
        assert int(aws_step["with"]["role-duration-seconds"]) >= (
            int(job["timeout-minutes"]) * 60
        )

    poll = (Path(".github/workflows") / "backport-poll.yml").read_text()
    assert 'cron: "0 * * * *"' in poll
    assert "'1800'" in poll and "'3300'" in poll
    assert "poll_started=${SECONDS}" in poll
    assert 'sleep "${wait}"' in poll
    assert "if action=$(poll_once); then" in poll
    assert 'echo "had_error=${poll_had_error}"' in poll
    assert "steps.poll.outputs.had_error == 'true'" in poll


def test_backport_branch_mutations_share_one_concurrency_group():
    groups = []
    for filename, job_name in (
        ("backport-poll.yml", "poll"),
        ("backport-sweep.yml", "sweep"),
        ("backport-ci-followup.yml", "follow-up"),
    ):
        workflow = yaml.load(
            (Path(".github/workflows") / filename).read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        concurrency = workflow["jobs"][job_name]["concurrency"]
        groups.append(concurrency["group"])
        assert concurrency["cancel-in-progress"] == "false"
        assert concurrency["queue"] == "max"

    assert groups == [
        "backport-branch-mutation-${{ matrix.repo }}-${{ matrix.branch }}",
        "backport-branch-mutation-${{ matrix.repo }}-${{ matrix.branch }}",
        "backport-branch-mutation-${{ matrix.repo }}-${{ matrix.branch }}",
    ]


def test_backport_ci_followup_passes_matrix_values_through_step_env():
    text = Path(
        ".github/workflows/backport-ci-followup.yml"
    ).read_text(encoding="utf-8")

    assert "TARGET_REPO: ${{ matrix.repo }}" in text
    assert "TARGET_BRANCH: ${{ matrix.branch }}" in text
    assert '--repo "${TARGET_REPO}"' in text
    assert '--branch "${TARGET_BRANCH}"' in text
    assert '--repo "${{ matrix.repo }}"' not in text
    assert '--branch "${{ matrix.branch }}"' not in text
