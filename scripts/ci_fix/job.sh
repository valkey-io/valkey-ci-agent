#!/usr/bin/env bash
# Prepare one planned fix inside the job that hosts it.
#
# Run by ci-fix-run.yml from the workspace the agent repository was checked
# out into (.ci-fix-agent):
#
# - in the failed CI job (fix-in-job, fix-in-container: CI_FIX_IN_JOB=1),
#   the workspace is that job's checkout, already set up and built by the
#   job's own steps (scripts/ci_fix/ci_job.py). The fix is prepared there,
#   rebuilt and tested with the job's own commands, from the plan. The job
#   runs on an Ubuntu or macOS runner, or as root in a container, where a
#   checkout without git in it is an archive of CI_FIX_COMMIT;
# - in fix-local, for a failure its job cannot host, the workspace holds
#   nothing else, and the fix is prepared on a fresh clone with the bot's own
#   approximation of the job.
#
# The steps before this one installed the agent and its dependencies
# (.github/actions/setup-agent) and the model's AWS credentials.
#
# The agent's code moves out of the workspace first: the agent's account owns
# the checkout while it works, and must not be able to change the code that
# judges and exports its work.
set -euo pipefail

# A container job runs as root, and has no sudo.
as_root() {
  if (( EUID == 0 )); then "$@"; else sudo "$@"; fi
}

agent="${RUNNER_TEMP}/ci-fix-agent"
home="${GITHUB_WORKSPACE}/.ci-fix-agent"

# The job's own cleanup runs the post phase of every action it used, and the
# agent's setup is one of this checkout's own composite actions: the runner
# reads it from ${home} again then. In the failed job the agent's account owns
# the workspace while it works, so whatever is at ${home} by now may be the
# account's: it goes first (a link is removed, never followed), or a move
# would land the real code inside it and the runner would read the account's
# action instead.
restore_agent() {
  [[ -d "${agent}" ]] || return 0
  as_root rm -rf -- "${home}"
  mv -- "${agent}" "${home}"
}

mv -- "${home}" "${agent}"
trap 'restore_agent || echo "::warning::could not put the CI-fix agent back for the job cleanup"' EXIT

# The job's own cleanup (each action's post step) runs git in the workspace
# as the workflow user, with its tokens. Whatever the agent's account could
# write into a repository's config there must not run then: no fsmonitor,
# no hooks. Written to GITHUB_ENV, so those later steps inherit it.
{
  echo "GIT_CONFIG_COUNT=2"
  echo "GIT_CONFIG_KEY_0=core.fsmonitor"
  echo "GIT_CONFIG_VALUE_0=false"
  echo "GIT_CONFIG_KEY_1=core.hooksPath"
  echo "GIT_CONFIG_VALUE_1=/dev/null"
} >> "${GITHUB_ENV}"

# The agent and its commands run as this account, which cannot read the
# job's credentials (scripts/common/isolation.py).
if [[ "$(uname -s)" == Darwin ]]; then
  # macOS has no useradd: the account gets a group of its own, not staff,
  # which can read the runner's files.
  id=$(( $( (dscl . -list /Users UniqueID; dscl . -list /Groups PrimaryGroupID) \
    | awk '{print $2}' | sort -n | tail -1) + 1 ))
  sudo dscl . -create /Groups/ci-fix-agent PrimaryGroupID "${id}"
  sudo dscl . -create /Users/ci-fix-agent
  sudo dscl . -create /Users/ci-fix-agent UniqueID "${id}"
  sudo dscl . -create /Users/ci-fix-agent PrimaryGroupID "${id}"
  sudo dscl . -create /Users/ci-fix-agent UserShell /bin/bash
  sudo dscl . -create /Users/ci-fix-agent NFSHomeDirectory /Users/ci-fix-agent
  sudo mkdir -p /Users/ci-fix-agent
  sudo chown "${id}:${id}" /Users/ci-fix-agent
else
  # Above the runner's own account, which in a container is just a number
  # that owns the workspace and the runner's files there.
  above=$(( $(stat -c %u "${RUNNER_TEMP}") + 1 ))
  as_root useradd --create-home --shell /bin/bash -K UID_MIN="${above}" -K GID_MIN="${above}" ci-fix-agent
fi
export CI_FIX_AGENT_USER=ci-fix-agent
export CI_AGENT_EVIDENCE_DIR="${RUNNER_TEMP}/ci-fix-out/agent-evidence"

args=()
repo="${GITHUB_WORKSPACE}"
if [[ -n "${CI_FIX_IN_JOB:-}" ]] && (( EUID == 0 )); then
  # The runner made the workspace as its own account; git trusts only a
  # checkout the account running it owns.
  chown 0:0 "${repo}"
fi
if [[ -n "${CI_FIX_IN_JOB:-}" && ! -d "${repo}/.git" ]]; then
  # Without git in its container, the job checked its commit out as an
  # archive: make it the shallow clone a checkout with git would have made,
  # keeping the files the job built.
  git init -q "${repo}"
  git -C "${repo}" remote add origin "https://github.com/${CI_FIX_REPOSITORY}"
  git -C "${repo}" fetch -q --depth=1 origin "${CI_FIX_COMMIT}"
  git -C "${repo}" reset -q FETCH_HEAD
fi
if [[ -d "${repo}/.git" ]]; then
  # The account must reach the checkout, git must not send the job's token,
  # and the search for an existing fix needs the history. The account alone
  # gets to pass through the checkout's parents: ``o+x`` on the runner's home
  # would open every world-readable file under it, the runner's own included.
  if [[ "$(uname -s)" != Darwin ]] && ! command -v setfacl >/dev/null; then
    if command -v apt-get >/dev/null; then
      as_root apt-get install -y -q --no-install-recommends acl >/dev/null
    elif command -v dnf >/dev/null; then
      as_root dnf -y -q install acl
    elif command -v apk >/dev/null; then
      as_root apk add --no-cache acl
    fi
  fi
  dir="${repo}"
  while [[ "${dir}" != / ]]; do
    dir="$(dirname "${dir}")"
    if [[ "$(uname -s)" == Darwin ]]; then
      as_root chmod +a "ci-fix-agent allow search" "${dir}"
    else
      as_root setfacl -m "u:ci-fix-agent:--x" "${dir}"
    fi
  done
  git -C "${repo}" config --local --unset-all http.https://github.com/.extraheader || true
  # actions/checkout v6 keeps the token in a file it includes instead.
  for key in $(git -C "${repo}" config --local --name-only --get-regexp '^includeif\.' || true); do
    git -C "${repo}" config --local --unset-all "${key}"
  done
  if [[ -f "${repo}/.git/shallow" ]]; then
    git -C "${repo}" fetch -q --unshallow --filter=blob:none origin
  fi
  git -C "${repo}" remote set-head origin --auto >/dev/null
  args+=(--repo-dir "${repo}")
fi

cd "${agent}"
python -m scripts.ci_fix.run fix \
  --plan "${RUNNER_TEMP}/ci-fix-plan" --out "${RUNNER_TEMP}/ci-fix-out" "${args[@]}"
