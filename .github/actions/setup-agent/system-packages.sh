#!/bin/sh
# Install the agent's tools in a job's container, as root, with the
# distribution's package manager: setup-python's builds do not run on every
# distribution Valkey's CI uses (Alpine's musl, AlmaLinux 8's older glibc),
# and a container has no sudo.
#
# - bash, which the steps after this one run in;
# - git: without it in the container, the job checked Valkey out as an
#   archive (scripts/ci_fix/job.sh makes that a repository);
# - Python 3.11 or later, in a virtual environment put first on PATH;
# - Node, for Claude Code;
# - what the agent's own account needs (scripts/common/isolation.py):
#   useradd, runuser, setpriv, pkill and ps, and find.
set -eu

retry() {
  attempt=1
  while ! "$@"; do
    if [ "${attempt}" -ge 3 ]; then
      return 1
    fi
    sleep "${attempt}"
    attempt=$((attempt + 1))
  done
}

if command -v apk >/dev/null 2>&1; then
  # Claude Code on musl uses the system's ripgrep, and libstdc++.
  retry apk add --no-cache bash git python3 nodejs npm shadow runuser util-linux procps findutils \
    libgcc libstdc++ ripgrep
  python=python3
elif command -v dnf >/dev/null 2>&1; then
  # Enterprise Linux's default Node is older than Claude Code supports;
  # Fedora has no module streams, and a current Node.
  retry dnf -y -q module enable nodejs:20 >/dev/null 2>&1 || true
  retry dnf -y -q install bash git python3 nodejs npm shadow-utils util-linux procps-ng findutils
  python=python3
  if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    # AlmaLinux 8 has Python 3.6 and CentOS Stream 9 has Python 3.9.
    retry dnf -y -q install python3.11
    python=python3.11
  fi
elif command -v apt-get >/dev/null 2>&1; then
  retry apt-get update -q
  retry env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
    bash git ca-certificates python3 python3-venv nodejs npm passwd util-linux procps findutils
  python=python3
else
  echo "::error::No apk, dnf or apt-get to install the agent's tools with"
  exit 1
fi

"${python}" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || {
  echo "::error::The container does not provide Python 3.11 or later"
  exit 1
}
venv="${RUNNER_TEMP}/ci-fix-python"
"${python}" -m venv "${venv}"
echo "${venv}/bin" >> "${GITHUB_PATH}"
