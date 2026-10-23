from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
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
        ".github/workflows/backport-poll.yml": "Generate publication token",
        ".github/workflows/backport-sweep.yml": "Generate publication token",
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


def test_ci_fix_concurrency_groups_never_drop_a_queued_run():
    expected_groups = {
        "ci-fix.yml": "ci-fix-${{ inputs.repo }}-${{ inputs.pr }}",
        "backport-ci-followup.yml": (
            "backport-branch-mutation-${{ matrix.repo }}-${{ matrix.branch }}"
        ),
        # The whole run of one issue is the unit, not a job of it.
        "ci-fix-issues.yml": "ci-fix-issue-${{ inputs.issue }}",
    }
    for filename, job_name in (
        ("ci-fix.yml", "ci-fix"), ("backport-ci-followup.yml", "follow-up"), ("ci-fix-issues.yml", None),
    ):
        workflow = yaml.load(
            (Path(".github/workflows") / filename).read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        concurrency = workflow["jobs"][job_name]["concurrency"] if job_name else workflow["concurrency"]
        assert concurrency == {
            "group": expected_groups[filename],
            "cancel-in-progress": "false",
            "queue": "max",
        }, filename


def test_the_comment_poller_dispatches_with_a_separate_token():
    workflow = yaml.load(Path(".github/workflows/ci-fix-comment-poll.yml").read_text(encoding="utf-8"),
                         Loader=yaml.BaseLoader)
    steps = {step.get("name"): step for step in next(iter(workflow["jobs"].values()))["steps"]}
    target, dispatch = steps["Generate App token"]["with"], steps["Generate dispatch token"]["with"]
    assert target["repositories"] == "valkey" and "permission-actions" not in target
    assert dispatch["repositories"] == "valkey-ci-agent" and dispatch["permission-actions"] == "write"
    assert set(k for k in dispatch if k.startswith("permission-")) == {"permission-actions", "permission-metadata"}


def test_the_fix_runs_start_only_from_their_front_doors():
    # The poller and the backport follow-up keep main's hourly schedules; the
    # runs they start are dispatched, and the issue dispatcher has its switch.
    for filename, triggers in (
        ("ci-fix-comment-poll.yml", {"schedule", "workflow_dispatch"}),
        ("backport-ci-followup.yml", {"schedule", "workflow_dispatch"}),
        ("ci-fix.yml", {"workflow_dispatch"}),
        ("ci-fix-issues.yml", {"workflow_dispatch"}),
    ):
        workflow = yaml.load(
            (Path(".github/workflows") / filename).read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        assert set(workflow["on"]) == triggers, filename


def test_the_issue_dispatcher_schedule_waits_for_its_switch_and_never_writes_valkey():
    workflow = yaml.load(Path(".github/workflows/ci-fix-issue-dispatch.yml").read_text(encoding="utf-8"),
                         Loader=yaml.BaseLoader)
    job = workflow["jobs"]["dispatch"]
    assert "vars.CI_FIX_ISSUES_AUTO_DISPATCH == 'true'" in job["if"]
    assert "github.event_name == 'workflow_dispatch'" in job["if"]
    steps = {step.get("name"): step for step in job["steps"]}
    token = steps["Generate target repository token"]["with"]
    assert token["repositories"] == "valkey"
    assert {k: v for k, v in token.items() if k.startswith("permission-")} == {
        "permission-issues": "read", "permission-pull-requests": "read", "permission-metadata": "read",
    }


def test_ci_fix_issue_runs_are_named_for_the_dispatcher():
    from scripts.ci_fix.issue_dispatch import RUN_NAME

    workflow = yaml.load(Path(".github/workflows/ci-fix-issues.yml").read_text(encoding="utf-8"),
                         Loader=yaml.BaseLoader)
    assert RUN_NAME.match(workflow["run-name"].replace("${{ inputs.issue }}", "123"))


# --- ci-fix-run.yml: every fix is plan -> fix-in-job | fix-in-container | fix-local -> publish

_RUN = Path(".github/workflows/ci-fix-run.yml")


def _run_jobs():
    return yaml.safe_load(_RUN.read_text(encoding="utf-8"))["jobs"]


def test_every_fix_front_door_runs_the_shared_fix():
    from scripts.ci_fix.models import FrontDoor

    doors = set()
    for filename, job_id in (("ci-fix.yml", "ci-fix"), ("backport-ci-followup.yml", "follow-up"),
                             ("ci-fix-issues.yml", "fix")):
        job = yaml.safe_load((Path(".github/workflows") / filename).read_text(encoding="utf-8"))["jobs"][job_id]
        assert job["uses"] == f"./{_RUN.as_posix()}", filename
        assert job["permissions"] == {"contents": "read", "id-token": "write"}, filename
        # Exactly what the run declares, not every secret of the repository.
        assert job["secrets"] == {
            name: f"${{{{ secrets.{name} }}}}"
            for name in ("VALKEYRIE_BOT_APP_ID", "VALKEYRIE_BOT_PRIVATE_KEY", "AWS_ROLE_ARN")
        }, filename
        doors.add(job["with"]["front_door"])
    assert doors == {door.value for door in FrontDoor}


def test_only_the_plan_and_publish_jobs_hold_a_github_token():
    jobs = _run_jobs()
    for job_id, command in (("plan", "python -m scripts.ci_fix.run plan"),
                            ("publish", "python -m scripts.ci_fix.run publish")):
        steps = jobs[job_id]["steps"]
        tokens = [step for step in steps if "actions/create-github-app-token@" in step.get("uses", "")]
        assert len(tokens) == 1 and tokens[0]["id"] == "target-token", job_id
        permissions = {k: v for k, v in tokens[0]["with"].items() if k.startswith("permission-")}
        # The plan only reads and answers (a comment, a reaction); publish pushes and opens PRs.
        plan = job_id == "plan"
        assert permissions == {
            "permission-actions": "read", "permission-issues": "write", "permission-metadata": "read",
            "permission-contents": "read" if plan else "write",
            "permission-pull-requests": "read" if plan else "write",
            # The PR command's gate checks team membership.
            **({"permission-members": "read"} if plan else {}),
        }, job_id
        assert not any("configure-aws-credentials@" in step.get("uses", "") for step in steps), job_id
        assert "\n".join(step.get("run", "") for step in steps).count(command) == 1, job_id
        if job_id == "publish":
            publish_step = next(step for step in steps if command in step.get("run", ""))
            assert publish_step["env"]["CI_FIX_ISSUES_PUBLISH"] == "${{ vars.CI_FIX_ISSUES_PUBLISH }}"
        assert jobs[job_id]["permissions"] == {"contents": "read"}, job_id
        assert jobs[job_id]["runs-on"] == "ubuntu-24.04", job_id
    assert _RUN.read_text(encoding="utf-8").count("actions/create-github-app-token@") == 2


_FIX_JOBS = ("fix-in-job", "fix-in-container", "fix-local")
_CI_FIX_STEPS = "CI fix: check out the agent"


def _ci_fix_steps(job):
    """The CI-fix steps a fix job ends with, and the plan's download."""
    steps = job["steps"]
    first = next(index for index, step in enumerate(steps) if step.get("name") == _CI_FIX_STEPS)
    download = next(step for step in steps if step.get("name") == "CI fix: download the plan")
    return [download, *steps[first:]]


def test_the_fix_runs_in_the_failed_job_or_on_the_bots_runner():
    from scripts.ci_fix import run
    from scripts.ci_fix.engine import AWS_SESSION_S

    jobs = _run_jobs()
    in_job, in_container, local = (jobs[job_id] for job_id in _FIX_JOBS)
    assert "needs.plan.outputs.container == ''" in in_job["if"] and "container" not in in_job
    assert "needs.plan.outputs.container != ''" in in_container["if"]
    assert in_container["container"] == "${{ needs.plan.outputs.container }}"
    # Without git in the container, job.sh makes the checkout an archive of the commit.
    assert in_container["env"]["CI_FIX_REPOSITORY"] == "${{ needs.plan.outputs.use_repo }}"
    assert in_container["env"]["CI_FIX_COMMIT"] == "${{ needs.plan.outputs.use_git_ref }}"
    for job in (in_job, in_container):
        assert job["runs-on"] == "${{ needs.plan.outputs.runs_on }}"
        assert job["timeout-minutes"] == "${{ fromJSON(needs.plan.outputs.timeout_minutes) }}"
        assert job["env"]["CI_FIX_IN_JOB"] == "1"
        main, extra, download, setup = job["steps"][:4]
        assert main["with"]["repository"] == "${{ needs.plan.outputs.use_repo }}"
        assert main["with"]["ref"] == "${{ needs.plan.outputs.use_git_ref }}"
        assert extra["if"] == "needs.plan.outputs.extra_repository != ''"
        assert extra["with"]["path"] == "${{ needs.plan.outputs.extra_path }}"
        assert download["name"] == "CI fix: download the plan"
        assert setup["run"] == f'sh "${{RUNNER_TEMP}}/ci-fix-plan/{run.SETUP_FILE}"' and setup["shell"] == "sh"
    assert "CI_FIX_IN_JOB" not in str(local)
    assert local["runs-on"] == "ubuntu-24.04"
    assert local["timeout-minutes"] * 60 == AWS_SESSION_S
    for job_id in _FIX_JOBS:
        assert jobs[job_id]["permissions"] == {"contents": "read", "id-token": "write"}, job_id
    assert jobs["publish"]["needs"] == ["plan", *_FIX_JOBS]


def test_every_fix_job_ends_with_the_same_ci_fix_steps():
    from scripts.ci_fix.engine import AWS_SESSION_S

    jobs = _run_jobs()
    shared = {job_id: _ci_fix_steps(jobs[job_id]) for job_id in _FIX_JOBS}
    # In a container, the agent's tools come from the distribution's packages.
    setup = next(step for step in shared["fix-in-container"] if step["name"] == "CI fix: set up the agent")
    assert setup["with"].pop("system-packages") == "true"
    assert shared["fix-in-job"] == shared["fix-in-container"] == shared["fix-local"]
    aws = next(step for step in shared["fix-local"] if "configure-aws-credentials@" in step.get("uses", ""))
    assert aws["with"]["role-duration-seconds"] == AWS_SESSION_S


def test_the_job_setup_and_dependencies_run_before_any_credential_and_the_agent_apart():
    for job_id in _FIX_JOBS:
        names = [step.get("name") for step in _run_jobs()[job_id]["steps"]]
        before = "Install Valkey build and test dependencies" if job_id == "fix-local" else "Run the job's own setup and build"
        assert names.index(before) < names.index("CI fix: configure AWS credentials"), job_id
        # The job's setup runs before the agent's code is there to change.
        assert names.index(before) < names.index(_CI_FIX_STEPS), job_id
    script = Path("scripts/ci_fix/job.sh").read_text(encoding="utf-8")
    assert script.index("CI_FIX_AGENT_USER=") < script.index("python -m scripts.ci_fix.run fix")
    assert "TOKEN" not in script


def test_the_jobs_cleanup_runs_git_without_fsmonitor_or_hooks():
    """The post steps run git in a workspace the agent's account wrote to, as the workflow user."""
    script = Path("scripts/ci_fix/job.sh").read_text(encoding="utf-8")
    block = script[script.index("GIT_CONFIG_COUNT=2"):script.index('>> "${GITHUB_ENV}"')]
    assert "GIT_CONFIG_KEY_0=core.fsmonitor" in block and "GIT_CONFIG_VALUE_0=false" in block
    assert "GIT_CONFIG_KEY_1=core.hooksPath" in block and "GIT_CONFIG_VALUE_1=/dev/null" in block


def _restore_agent(workspace: Path, temp: Path) -> subprocess.CompletedProcess[str]:
    """Run job.sh's own ``restore_agent`` (and ``as_root``) against ``workspace``."""
    script = Path("scripts/ci_fix/job.sh").read_text(encoding="utf-8")
    functions = "".join(
        re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", script, re.MULTILINE | re.DOTALL).group(0)
        for name in ("as_root", "restore_agent")
    )
    program = (
        'set -euo pipefail\nsudo() { "$@"; }\n' + functions
        + f'agent={temp / "ci-fix-agent"}\nhome={workspace / ".ci-fix-agent"}\nrestore_agent\n'
    )
    return subprocess.run(["bash", "-c", program], capture_output=True, text=True, check=False)


def _agent_code(root: Path) -> Path:
    action = root / ".github" / "actions" / "setup-agent" / "action.yml"
    action.parent.mkdir(parents=True)
    action.write_text("name: the real agent setup\n")
    return action


def test_the_agents_code_is_put_back_for_the_jobs_own_cleanup():
    """Its setup is a composite action of this checkout: the post phase reads it from there."""
    script = Path("scripts/ci_fix/job.sh").read_text(encoding="utf-8")

    moved = script.index('mv -- "${home}" "${agent}"')
    assert moved < script.index("trap 'restore_agent") < script.index("python -m scripts.ci_fix.run fix")


@pytest.mark.parametrize("planted", ["directory", "link"])
def test_what_the_agent_left_at_the_agents_path_is_never_what_the_cleanup_reads(tmp_path, planted):
    workspace, temp, outside = tmp_path / "workspace", tmp_path / "temp", tmp_path / "outside"
    workspace.mkdir()
    _agent_code(temp / "ci-fix-agent")
    home = workspace / ".ci-fix-agent"
    if planted == "directory":
        # The account owns the workspace: it can make the path a directory of
        # its own, which a plain move would move the real code into.
        (_agent_code(home)).write_text("name: the agent's own action\n")
    else:
        _agent_code(outside).write_text("name: the agent's own action\n")
        home.symlink_to(outside)

    result = _restore_agent(workspace, temp)

    assert result.returncode == 0, result.stderr
    assert not home.is_symlink()
    assert (home / ".github/actions/setup-agent/action.yml").read_text() == "name: the real agent setup\n"
    assert not (home / "ci-fix-agent").exists()
    if planted == "link":
        # The link went; what it pointed at was not followed.
        assert (outside / ".github/actions/setup-agent/action.yml").exists()


def test_the_agents_code_is_put_back_where_nothing_was_left(tmp_path):
    workspace, temp = tmp_path / "workspace", tmp_path / "temp"
    workspace.mkdir()
    _agent_code(temp / "ci-fix-agent")

    assert _restore_agent(workspace, temp).returncode == 0
    assert (workspace / ".ci-fix-agent/.github/actions/setup-agent/action.yml").is_file()
    assert not (temp / "ci-fix-agent").exists()


def test_a_container_fix_mode_installs_the_agents_tools_from_the_distribution():
    """setup-python's builds do not run on every distribution, and a container has no sudo."""
    steps = yaml.safe_load(Path(".github/actions/setup-agent/action.yml").read_text(encoding="utf-8"))["runs"]["steps"]
    # The first step: the others need bash, which the container may lack.
    assert steps[0]["name"] == "Install Python and Node from the system's packages"
    assert steps[0]["shell"] == "sh" and steps[0]["if"] == "${{ inputs['system-packages'] == 'true' }}"
    assert steps[1]["uses"].startswith("actions/setup-python@")
    assert steps[1]["if"] == "${{ inputs['system-packages'] != 'true' }}"
    for job_id, job in _run_jobs().items():
        for step in job.get("steps") or []:
            if step.get("name") == "CI fix: set up the agent":
                assert (step["with"].get("system-packages") == "true") == bool(job.get("container")), job_id


def test_the_isolation_tests_also_run_as_root_in_a_container():
    job = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]["isolation-as-root"]
    setup = next(step for step in job["steps"] if step.get("uses") == "./.github/actions/setup-agent")

    assert job["container"] and setup["with"]["system-packages"] == "true"
    assert job["container"] != "alpine:latest"  # pinned, so the test does not drift with Alpine
    assert any("pytest" in step.get("run", "") and "tests/test_isolation.py" in step["run"] for step in job["steps"])


def test_a_rerun_of_a_fix_replaces_its_artifacts():
    """An artifact name is unique per run: re-running a failed fix run would fail on its upload."""
    uploads = [
        step for job in _run_jobs().values() for step in job.get("steps") or []
        if "actions/upload-artifact@" in step.get("uses", "")
    ]
    assert len(uploads) == 4
    assert all(step["with"].get("overwrite") is True for step in uploads)
