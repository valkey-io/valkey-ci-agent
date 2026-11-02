#!/usr/bin/env bash
# Run one CI fix in a disposable checkout below the system temp directory.
#
# The workflow checkout remains runner-owned for Actions cleanup. Target setup,
# reproduction, tests, and the edit agent use the copied checkout, whose parent
# is naturally traversable; no ACL is granted on the runner's home or on /.
set -euo pipefail

unset BASH_ENV ENV PYTHONPATH PYTHONHOME NODE_OPTIONS
unset LD_PRELOAD LD_LIBRARY_PATH DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH

state="${RUNNER_TEMP}/ci-fix-sandbox-state"
home="${GITHUB_WORKSPACE}/.ci-fix-agent"
agent="${RUNNER_TEMP}/ci-fix-agent"
out="${RUNNER_TEMP}/ci-fix-out"

as_root() {
  if (( EUID == 0 )); then "$@"; else sudo -n "$@"; fi
}

fail() {
  echo "::error::$1"
  exit 1
}

restore_agent() {
  [[ -d "${agent}" ]] || return 0
  as_root rm -rf -- "${home}" || return 1
  mv -- "${agent}" "${home}"
}

safe_sandbox() {
  local physical parent
  [[ -n "${sandbox:-}" && -d "${sandbox}" && ! -L "${sandbox}" ]] || return 1
  physical="$(cd -- "${sandbox}" && pwd -P)"
  parent="${physical%/*}"
  [[ "${physical}" == "${sandbox}" \
    && ( "${parent}" == /tmp || "${parent}" == /private/tmp ) \
    && "${physical##*/}" == valkey-ci-fix.* ]]
}

create_account() {
  local id
  user="cifix$(python -c 'import secrets; print(secrets.token_hex(5))')"
  account_home="${sandbox}/home"
  if [[ "$(uname -s)" == Darwin ]]; then
    id=$(( $( (dscl . -list /Users UniqueID; dscl . -list /Groups PrimaryGroupID) \
      | awk '{print $2}' | sort -n | tail -1) + 1 ))
    as_root dscl . -create "/Groups/${user}" PrimaryGroupID "${id}"
    as_root dscl . -create "/Users/${user}"
    as_root dscl . -create "/Users/${user}" UniqueID "${id}"
    as_root dscl . -create "/Users/${user}" PrimaryGroupID "${id}"
    as_root dscl . -create "/Users/${user}" UserShell /bin/bash
    as_root dscl . -create "/Users/${user}" NFSHomeDirectory "${account_home}"
    as_root mkdir -m 700 "${account_home}"
    as_root chown "${id}:${id}" "${account_home}"
  else
    as_root useradd --user-group --create-home --home-dir "${account_home}" \
      --shell /bin/bash "${user}"
  fi
  uid="$(id -u "${user}")"
  gid="$(id -g "${user}")"
}

stop_account() {
  [[ -n "${user:-}" && -n "${uid:-}" && -d "${agent}" ]] || return 0
  (
    cd "${agent}"
    python - "${user}" <<'PY'
import sys
from scripts.common.isolation import kill_all

kill_all(sys.argv[1])
PY
  )
}

group_gid() {
  if [[ "$(uname -s)" == Darwin ]]; then
    dscl . -read "/Groups/$1" PrimaryGroupID 2>/dev/null | awk 'NR == 1 { print $2 }' || true
  else
    getent group "$1" 2>/dev/null | awk -F: 'NR == 1 { print $3 }' || true
  fi
}

delete_account() {
  local live_uid live_gid live_group_gid
  [[ -n "${user:-}" && -n "${uid:-}" && -n "${gid:-}" ]] || return 0
  live_uid="$(id -u "${user}" 2>/dev/null || true)"
  live_gid="$(id -g "${user}" 2>/dev/null || true)"
  live_group_gid="$(group_gid "${user}")"
  if [[ -z "${live_uid}" && -z "${live_group_gid}" ]]; then return 0; fi
  if [[ -n "${live_uid}" && ( "${live_uid}" != "${uid}" || "${live_gid}" != "${gid}" ) ]] \
    || [[ -n "${live_group_gid}" && "${live_group_gid}" != "${gid}" ]]; then
    echo "::error::Refusing to delete changed isolation account ${user}"
    return 1
  fi
  if [[ -n "${live_uid}" ]]; then
    stop_account || return 1
    if [[ "$(uname -s)" == Darwin ]]; then
      as_root dscl . -delete "/Users/${user}" || return 1
    else
      as_root userdel "${user}" || return 1
    fi
  fi
  live_group_gid="$(group_gid "${user}")"
  if [[ -n "${live_group_gid}" && "${live_group_gid}" != "${gid}" ]]; then
    echo "::error::Refusing to delete changed isolation group ${user}"
    return 1
  fi
  if [[ -n "${live_group_gid}" ]]; then
    if [[ "$(uname -s)" == Darwin ]]; then
      as_root dscl . -delete "/Groups/${user}" || return 1
    else
      as_root groupdel "${user}" || return 1
    fi
  fi
}

write_state() {
  {
    printf '%s\n' ci-fix-sandbox-v1
    printf '%s\n' "${mode}"
    printf '%s\n' "${sandbox}"
    printf '%s\n' "${repo}"
    printf '%s\n' "${user}"
    printf '%s\n' "${uid}"
    printf '%s\n' "${gid}"
  } > "${state}"
  chmod 600 "${state}"
}

read_state() {
  [[ -f "${state}" && ! -L "${state}" ]] || {
    echo "::error::The CI-fix sandbox state is missing"
    return 1
  }
  exec 3< "${state}"
  if ! {
    IFS= read -r header <&3 \
      && IFS= read -r mode <&3 \
      && IFS= read -r sandbox <&3 \
      && IFS= read -r repo <&3 \
      && IFS= read -r user <&3 \
      && IFS= read -r uid <&3 \
      && IFS= read -r gid <&3
  }; then
    exec 3<&-
    echo "::error::The CI-fix sandbox state is incomplete"
    return 1
  fi
  if IFS= read -r _unexpected <&3; then
    exec 3<&-
    echo "::error::The CI-fix sandbox state has unexpected data"
    return 1
  fi
  exec 3<&-
  [[ "${header}" == ci-fix-sandbox-v1 \
    && ( "${mode}" == job || "${mode}" == local ) \
    && "${user}" =~ ^cifix[0-9a-f]{10}$ \
    && "${uid}" =~ ^[1-9][0-9]*$ \
    && "${gid}" =~ ^[1-9][0-9]*$ ]] || {
    echo "::error::The CI-fix sandbox state is invalid"
    return 1
  }
  safe_sandbox || {
    echo "::error::The CI-fix sandbox path is invalid"
    return 1
  }
  if [[ "${mode}" == job ]]; then
    [[ "${repo}" == "${sandbox}/repo" && -d "${repo}" && ! -L "${repo}" ]] \
      || {
        echo "::error::The CI-fix checkout is invalid"
        return 1
      }
  elif [[ -n "${repo}" ]]; then
    echo "::error::A local fix must create its own checkout"
    return 1
  fi
}

stage() {
  local temp_root setup
  [[ -d "${home}" && ! -L "${home}" ]] || fail "The trusted agent checkout is missing"
  [[ ! -e "${state}" && ! -L "${state}" ]] || fail "A CI-fix sandbox already exists"
  [[ ! -e "${agent}" && ! -L "${agent}" ]] || fail "The trusted agent temp path is occupied"

  mv -- "${home}" "${agent}"
  mode=local
  repo=""
  sandbox=""
  user=""
  uid=""
  gid=""

  rollback() {
    local status=$?
    delete_account || status=1
    if safe_sandbox; then as_root rm -rf -- "${sandbox}" || status=1; fi
    as_root rm -f -- "${state}" || status=1
    restore_agent || status=1
    exit "${status}"
  }
  trap rollback EXIT

  temp_root=/tmp
  if [[ "$(uname -s)" == Darwin ]]; then temp_root=/private/tmp; fi
  sandbox="$(mktemp -d "${temp_root}/valkey-ci-fix.XXXXXX")"
  chmod 711 "${sandbox}"

  if [[ -n "${CI_FIX_IN_JOB:-}" ]]; then
    mode=job
    repo="${sandbox}/repo"
    mkdir -m 700 "${repo}"
    cp -R "${GITHUB_WORKSPACE}/." "${repo}/"
    as_root rm -rf -- "${repo}/.ci-fix-agent"

    if [[ ! -d "${repo}/.git" ]]; then
      git init -q "${repo}"
      git -C "${repo}" remote add origin "https://github.com/${CI_FIX_REPOSITORY}"
      git -C "${repo}" fetch -q --depth=1 origin "${CI_FIX_COMMIT}"
      git -C "${repo}" reset -q FETCH_HEAD
    fi
    git -C "${repo}" config --local --unset-all http.https://github.com/.extraheader || true
    while IFS= read -r key; do
      [[ -n "${key}" ]] && git -C "${repo}" config --local --unset-all "${key}"
    done < <(git -C "${repo}" config --local --name-only --get-regexp '^includeif\.' || true)
    if [[ -f "${repo}/.git/shallow" ]]; then
      git -C "${repo}" fetch -q --unshallow --filter=blob:none origin
    fi
    git -C "${repo}" remote set-head origin --auto >/dev/null

    setup="${sandbox}/setup.sh"
    install -m 555 "${RUNNER_TEMP}/ci-fix-plan/setup.sh" "${setup}"
  fi

  create_account
  write_state
  trap - EXIT
}

setup_job() {
  read_state || fail "Cannot use an invalid CI-fix sandbox"
  [[ "${mode}" == job ]] || fail "Only a hosted job has setup to replay"
  [[ "$(id -u "${user}" 2>/dev/null || true)" == "${uid}" ]] \
    || fail "The CI-fix isolation account changed"
  (
    cd "${agent}"
    python -m scripts.ci_fix.sandbox \
      --repo "${repo}" --setup "${sandbox}/setup.sh" --user "${user}"
  )
}

run_fix() {
  local args=()
  read_state || fail "Cannot use an invalid CI-fix sandbox"
  [[ "$(id -u "${user}" 2>/dev/null || true)" == "${uid}" ]] \
    || fail "The CI-fix isolation account changed"
  export CI_FIX_AGENT_USER="${user}"
  export CI_AGENT_EVIDENCE_DIR="${out}/agent-evidence"
  export TMPDIR="${sandbox}"
  if [[ "${mode}" == job ]]; then args+=(--repo-dir "${repo}"); fi
  cd "${agent}"
  python -m scripts.ci_fix.run fix \
    --plan "${RUNNER_TEMP}/ci-fix-plan" --out "${out}" "${args[@]}"
}

cleanup() {
  local status=0
  if [[ -e "${state}" || -L "${state}" ]]; then
    if read_state; then
      delete_account || status=1
      if safe_sandbox; then as_root rm -rf -- "${sandbox}" || status=1; else status=1; fi
      as_root rm -f -- "${state}" || status=1
    else
      status=1
    fi
  fi
  restore_agent || status=1
  return "${status}"
}

case "${1:-}" in
  stage) stage ;;
  setup) setup_job ;;
  run) run_fix ;;
  cleanup) cleanup ;;
  *)
    echo "usage: $0 stage|setup|run|cleanup" >&2
    exit 2
    ;;
esac
