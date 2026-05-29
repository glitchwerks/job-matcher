#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy-remote-linux.sh — Provision a Linux deploy server over SSH
#
# PURPOSE:
#   Runs from a developer workstation (Git Bash, WSL, or any POSIX shell).
#   Copies the required deployment files (compose files, scripts, config
#   examples) from the local checkout to a remote Linux host, then
#   optionally runs scripts/docker-setup.sh on the remote (interactively via
#   SSH TTY) to complete the first-time Docker stack configuration.
#   No git or GitHub credentials are needed on the remote server.
#
# USAGE:
#   ./scripts/deploy-remote-linux.sh <host> [user] [remote-path]
#
#   host:        hostname or IP of the Linux deploy server
#   user:        SSH user (default: current $USER)
#   remote-path: deployment directory on the remote (default: /opt/job-matcher-pr)
#
# EXAMPLES:
#   ./scripts/deploy-remote-linux.sh 192.168.1.50
#   ./scripts/deploy-remote-linux.sh myserver.example.com deploy /srv/job-matcher
#
# PREREQUISITES (remote):
#   - SSH access with key-based or password auth
#   - docker + docker compose plugin installed (checked in Step 3)
#   - sudo access for docker-setup.sh (requires root for chown operations)
#   - sudo access on the remote for the live .env push step
#     (the script stages to /tmp then sudo-installs to /opt/job-matcher-pr)
#
# RE-RUN SAFETY:
#   All steps are idempotent. Re-running after a partial failure is safe.
# ---------------------------------------------------------------------------
set -euo pipefail

# ---------------------------------------------------------------------------
# ANSI color helpers
# ---------------------------------------------------------------------------
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
BOLD='\033[1m'
RESET='\033[0m'

banner() {
    local border
    border="$(printf '=%.0s' {1..60})"
    echo ""
    echo -e "${CYAN}${border}${RESET}"
    echo -e "${CYAN}  ${1}${RESET}"
    echo -e "${CYAN}${border}${RESET}"
    echo ""
}

step() {
    echo -e "${YELLOW}[DEPLOY] ${1}${RESET}"
}

ok() {
    echo -e "${GREEN}[  OK  ] ${1}${RESET}"
}

warn() {
    echo -e "${YELLOW}[ WARN ] ${1}${RESET}"
}

fail() {
    echo -e "${RED}[ FAIL ] ${1}${RESET}" >&2
}

# ---------------------------------------------------------------------------
# Step 1 — Parse arguments
# ---------------------------------------------------------------------------

if [[ $# -lt 1 ]]; then
    echo ""
    echo -e "${BOLD}Usage:${RESET} ./scripts/deploy-remote-linux.sh <host> [user] [remote-path]"
    echo ""
    echo "  host:        hostname or IP of the Linux deploy server"
    echo "  user:        SSH user (default: ${USER})"
    echo "  remote-path: deployment directory (default: /opt/job-matcher-pr)"
    echo ""
    echo "Examples:"
    echo "  ./scripts/deploy-remote-linux.sh 192.168.1.50"
    echo "  ./scripts/deploy-remote-linux.sh myserver.example.com deploy /srv/job-matcher"
    echo ""
    exit 1
fi

REMOTE_HOST="${1}"
REMOTE_USER="${2:-${USER}}"
REMOTE_PATH="${3:-/opt/job-matcher-pr}"

SSH_TARGET="${REMOTE_USER}@${REMOTE_HOST}"

# Resolve the local project root (parent of the scripts/ directory).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

banner "Job Matcher -- Linux Remote Deployment"
echo -e "  ${CYAN}Local source  :${RESET} ${LOCAL_PROJECT_ROOT}"
echo -e "  ${CYAN}Remote target :${RESET} ${REMOTE_PATH} on ${REMOTE_HOST}"
echo -e "  ${CYAN}SSH user      :${RESET} ${REMOTE_USER}"
echo ""

# ---------------------------------------------------------------------------
# Step 2 — Test SSH connectivity
# ---------------------------------------------------------------------------

step "Testing SSH connectivity to ${SSH_TARGET}..."

if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "${SSH_TARGET}" echo ok &>/dev/null; then
    fail "Cannot reach ${SSH_TARGET} via SSH."
    echo ""
    echo "  Ensure the server is reachable and your SSH key is authorised:"
    echo ""
    echo "    ssh-copy-id ${SSH_TARGET}"
    echo ""
    echo "  If using password auth, remove -o BatchMode=yes above or add your key first."
    exit 1
fi

ok "SSH connection to ${SSH_TARGET} succeeded."

# ---------------------------------------------------------------------------
# Step 3 — Check prerequisites on remote (docker + docker compose)
# ---------------------------------------------------------------------------

step "Checking prerequisites on ${REMOTE_HOST}..."

REMOTE_CHECKS=$(ssh -o ConnectTimeout=10 "${SSH_TARGET}" bash <<'REMOTE'
DOCKER_OK=0
COMPOSE_OK=0
DOCKER_VER=""
COMPOSE_VER=""

if command -v docker &>/dev/null; then
    DOCKER_OK=1
    DOCKER_VER="$(docker --version 2>/dev/null || echo 'unknown')"
fi

if docker compose version &>/dev/null 2>&1; then
    COMPOSE_OK=1
    COMPOSE_VER="$(docker compose version --short 2>/dev/null || echo 'unknown')"
fi

echo "DOCKER_OK=\"${DOCKER_OK}\""
echo "COMPOSE_OK=\"${COMPOSE_OK}\""
echo "DOCKER_VER=\"${DOCKER_VER}\""
echo "COMPOSE_VER=\"${COMPOSE_VER}\""
REMOTE
)

# Safely parse the key=value pairs emitted by the heredoc above.
# Uses a case statement instead of eval to prevent command injection
# from a compromised remote host.
while IFS='=' read -r key value; do
    # Strip surrounding quotes from value
    value="${value#\"}"
    value="${value%\"}"
    case "$key" in
        DOCKER_OK)    DOCKER_OK="$value" ;;
        COMPOSE_OK)   COMPOSE_OK="$value" ;;
        DOCKER_VER)   DOCKER_VER="$value" ;;
        COMPOSE_VER)  COMPOSE_VER="$value" ;;
        *)            ;;  # Ignore unexpected keys
    esac
done <<< "$REMOTE_CHECKS"

PREREQ_FAILED=0

if [[ "${DOCKER_OK}" == "1" ]]; then
    ok "docker found: ${DOCKER_VER}"
else
    fail "docker is not installed on ${REMOTE_HOST}."
    echo "  Install Docker Engine: https://docs.docker.com/engine/install/"
    PREREQ_FAILED=1
fi

if [[ "${COMPOSE_OK}" == "1" ]]; then
    ok "docker compose found: ${COMPOSE_VER}"
else
    fail "docker compose plugin is not installed on ${REMOTE_HOST}."
    echo "  Install the Compose plugin: https://docs.docker.com/compose/install/linux/"
    PREREQ_FAILED=1
fi

if [[ "${PREREQ_FAILED}" == "1" ]]; then
    echo ""
    fail "Prerequisites missing on remote. Install them and re-run this script."
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 4 — Create remote directory and copy deployment files via scp
# ---------------------------------------------------------------------------

step "Preparing ${REMOTE_PATH} on ${REMOTE_HOST}..."

# Ensure the remote directory exists (needs sudo + TTY for password prompt).
# shellcheck disable=SC2029  # ${REMOTE_PATH} and ${SSH_TARGET} intentionally expand client-side
DIR_EXISTS=$(ssh "${SSH_TARGET}" "test -e '${REMOTE_PATH}' && echo yes || echo no")

if [[ "${DIR_EXISTS}" == "no" ]]; then
    echo "  Creating ${REMOTE_PATH} (requires sudo)..."
    ssh -tt "${SSH_TARGET}" "sudo mkdir -p '${REMOTE_PATH}' && sudo chown \$(id -un):\$(id -gn) '${REMOTE_PATH}'"
fi

# Create required sub-directories on the remote.
# shellcheck disable=SC2029  # ${REMOTE_PATH} and ${SSH_TARGET} intentionally expand client-side
ssh "${SSH_TARGET}" "mkdir -p '${REMOTE_PATH}/scripts' '${REMOTE_PATH}/config'"

step "Copying deployment files to ${REMOTE_HOST}:${REMOTE_PATH}..."

# --- Docker Compose files ---
for COMPOSE_FILE in docker-compose.prod.yml docker-compose.dev.yml; do
    LOCAL_FILE="${LOCAL_PROJECT_ROOT}/${COMPOSE_FILE}"
    if [[ -f "${LOCAL_FILE}" ]]; then
        scp -q "${LOCAL_FILE}" "${SSH_TARGET}:${REMOTE_PATH}/${COMPOSE_FILE}"
        ok "Copied ${COMPOSE_FILE}"
    else
        warn "Local file not found, skipping: ${COMPOSE_FILE}"
    fi
done

# --- Scripts ---
SCRIPTS=(
    docker-setup.sh
    docker-status.sh
    docker-teardown.sh
    entrypoint.sh
    ingest-cron.sh
    backup.sh
)
for SCRIPT in "${SCRIPTS[@]}"; do
    LOCAL_FILE="${LOCAL_PROJECT_ROOT}/scripts/${SCRIPT}"
    if [[ -f "${LOCAL_FILE}" ]]; then
        scp -q "${LOCAL_FILE}" "${SSH_TARGET}:${REMOTE_PATH}/scripts/${SCRIPT}"
        ok "Copied scripts/${SCRIPT}"
    else
        warn "Local file not found, skipping: scripts/${SCRIPT}"
    fi
done

# --- Config example files ---
shopt -s nullglob
for EXAMPLE in "${LOCAL_PROJECT_ROOT}"/config/*.example.json; do
    BASENAME="$(basename "${EXAMPLE}")"
    scp -q "${EXAMPLE}" "${SSH_TARGET}:${REMOTE_PATH}/config/${BASENAME}"
    ok "Copied config/${BASENAME}"
done
shopt -u nullglob

# --- .env example files (don't overwrite existing live files) ---
for ENV_EXAMPLE in .env.prod.example .env.dev.example; do
    LOCAL_EXAMPLE="${LOCAL_PROJECT_ROOT}/${ENV_EXAMPLE}"
    REMOTE_EXAMPLE="${REMOTE_PATH}/${ENV_EXAMPLE}"

    # Derive the live filename: strip the .example suffix.
    LIVE_NAME="${ENV_EXAMPLE%.example}"   # e.g. .env.prod
    REMOTE_LIVE="${REMOTE_PATH}/${LIVE_NAME}"

    if [[ ! -f "${LOCAL_EXAMPLE}" ]]; then
        warn "Local file not found, skipping: ${LOCAL_EXAMPLE}"
        continue
    fi

    # Check whether the live file already exists on the remote.
    # shellcheck disable=SC2029  # ${REMOTE_LIVE} and ${SSH_TARGET} intentionally expand client-side
    if ssh "${SSH_TARGET}" "[[ -f '${REMOTE_LIVE}' ]]" 2>/dev/null; then
        ok "${LIVE_NAME} already exists on remote -- skipping example copy."
    else
        scp -q "${LOCAL_EXAMPLE}" "${SSH_TARGET}:${REMOTE_EXAMPLE}"
        ok "Copied ${ENV_EXAMPLE}"
    fi
done

# --- Live .env files (opt-in, with overwrite confirmation + chmod 600) ---
#
# The remote only runs `docker compose pull && up -d` during deploys; it never
# invents its own env values. When `.env.*.example` gains new required fields
# (e.g. the 2026-04-06 dev/prod split added SECRET_KEY and renamed POSTGRES_DB),
# the server silently drifts unless someone copies the updated live file up.
# This block fixes that: if a live `.env.prod` / `.env.dev` exists locally, we
# scp it to the remote -- with an interactive confirmation if the remote already
# has a live file so we never silently clobber a password that's in use.
#
# Files are chmod 600 on the remote so the secrets aren't world-readable.
for LIVE_ENV in .env.prod .env.dev; do
    LOCAL_LIVE="${LOCAL_PROJECT_ROOT}/${LIVE_ENV}"
    REMOTE_LIVE="${REMOTE_PATH}/${LIVE_ENV}"

    if [[ ! -f "${LOCAL_LIVE}" ]]; then
        # No local live file -> nothing to push. The .example already landed above.
        continue
    fi

    # Pre-push sanity check: catch unedited local copies before they hit the wire.
    # The GHA preflight will reject this server-side, but failing fast here saves
    # the round trip and is a clearer error for the operator at their workstation.
    if grep -qE '^(POSTGRES_PASSWORD|SECRET_KEY)=changeme' "${LOCAL_LIVE}"; then
        echo ""
        warn "Local ${LIVE_ENV} still contains 'changeme_*' placeholder values:"
        grep -nE '^(POSTGRES_PASSWORD|SECRET_KEY)=changeme' "${LOCAL_LIVE}" || true
        if [[ "${LIVE_ENV}" == ".env.prod" ]]; then
            read -r -p "Push anyway? The prod GHA deploy will reject this file. [y/N] " PUSH_ANYWAY
        else
            read -r -p "Push anyway? Not recommended for deployed environments. [y/N] " PUSH_ANYWAY
        fi
        echo ""
        if [[ ! "${PUSH_ANYWAY}" =~ ^[Yy]$ ]]; then
            warn "Skipped ${LIVE_ENV} -- remote unchanged."
            continue
        fi
    fi

    # Check whether the remote already has a live file and confirm overwrite.
    # shellcheck disable=SC2029  # ${REMOTE_LIVE} and ${SSH_TARGET} intentionally expand client-side
    if ssh "${SSH_TARGET}" "[[ -f '${REMOTE_LIVE}' ]]" 2>/dev/null; then
        echo ""
        warn "Remote already has a live ${LIVE_ENV} at ${REMOTE_PATH}."
        warn "Overwriting will replace the credentials the running stack is using."
        read -r -p "Overwrite remote ${LIVE_ENV} with the local copy? [y/N] " CONFIRM_OVERWRITE
        echo ""
        if [[ ! "${CONFIRM_OVERWRITE}" =~ ^[Yy]$ ]]; then
            warn "Skipped live ${LIVE_ENV} -- remote unchanged."
            continue
        fi
    fi

    # Stage-then-install pattern: scp to a tmp path the SSH user owns, then
    # use `sudo install` to atomically place the file at the final destination
    # with correct mode and ownership. This avoids "Permission denied" when the
    # target file is owned by root (e.g. from initial provisioning or GHA), and
    # is atomic so the running stack never sees a half-written secrets file.
    # shellcheck disable=SC2029  # ${LIVE_ENV} and ${SSH_TARGET} intentionally expand client-side
    REMOTE_TMP=$(ssh "${SSH_TARGET}" "mktemp /tmp/.${LIVE_ENV}.XXXXXX")
    if [[ -z "${REMOTE_TMP}" ]]; then
        warn "Could not create temp file on remote -- skipped ${LIVE_ENV}."
        continue
    fi
    scp -q "${LOCAL_LIVE}" "${SSH_TARGET}:${REMOTE_TMP}"
    # Defense-in-depth: enforce 600 on the staged file in case the remote umask is loose.
    # shellcheck disable=SC2029  # ${REMOTE_TMP} and ${SSH_TARGET} intentionally expand client-side
    ssh "${SSH_TARGET}" "chmod 600 '${REMOTE_TMP}'"
    # Atomically place the file at the final destination with correct mode
    # and ownership. One `ssh -tt` session handles sudo prompt + install +
    # temp-file cleanup together so sudo's `tty_tickets` credential cache
    # applies consistently. (A previous two-pass attempt -- `sudo -v` then
    # `sudo -n install` in separate ssh sessions -- failed with "a password
    # is required" because tty_tickets scopes cached credentials per-TTY,
    # and the two ssh calls landed on different TTYs.) Output streams to
    # the user's terminal so they see the sudo prompt and any install
    # errors directly; we rely on the ssh exit code for success/failure
    # rather than capturing stderr into a variable.
    # shellcheck disable=SC2029  # ${REMOTE_TMP}, ${REMOTE_LIVE}, ${SSH_TARGET} intentionally expand client-side
    if ssh -tt "${SSH_TARGET}" "sudo install -m 600 -o \"\$(id -un)\" -g \"\$(id -gn)\" '${REMOTE_TMP}' '${REMOTE_LIVE}'"; then
        ssh "${SSH_TARGET}" "rm -f '${REMOTE_TMP}'" 2>/dev/null || true
        ok "Copied live ${LIVE_ENV} (chmod 600)"
    else
        warn "sudo install failed for ${LIVE_ENV} (see output above for details)."
        ssh "${SSH_TARGET}" "rm -f '${REMOTE_TMP}'" 2>/dev/null || true
        continue
    fi
done

# --- Live secrets/db_password.* files (opt-in, with overwrite confirmation + chmod 600) ---
#
# The DB password now lives in secrets/db_password.{dev,prod} rather than in
# DATABASE_URL inside the .env file. entrypoint.sh reads /run/secrets/db_password
# and constructs DATABASE_URL at container start. These files must be present on
# the remote before `docker compose up` or the web container falls back to the
# (absent) DATABASE_URL env var and will fail to connect.
#
# Mirrors the live .env push pattern above: stage to /tmp, sudo-install to the
# final path with 600 permissions, then clean up the temp file.
# shellcheck disable=SC2029  # ${REMOTE_PATH} and ${SSH_TARGET} intentionally expand client-side
ssh "${SSH_TARGET}" "mkdir -p '${REMOTE_PATH}/secrets'"
ok "Remote secrets/ directory ensured."

for SECRET_FILE in secrets/db_password.prod secrets/db_password.dev; do
    LOCAL_SECRET="${LOCAL_PROJECT_ROOT}/${SECRET_FILE}"
    REMOTE_SECRET="${REMOTE_PATH}/${SECRET_FILE}"
    SECRET_BASENAME="$(basename "${SECRET_FILE}")"

    if [[ ! -f "${LOCAL_SECRET}" ]]; then
        # No local secret file -> nothing to push. Operator must create it manually.
        warn "Local ${SECRET_FILE} not found -- skipping. Create it from secrets/db_password.example before deploying."
        continue
    fi

    # Pre-push sanity check: catch placeholder values before they go to the wire.
    if grep -qE '^changeme' "${LOCAL_SECRET}"; then
        echo ""
        warn "Local ${SECRET_FILE} still contains a 'changeme' placeholder password."
        if [[ "${SECRET_FILE}" == *prod* ]]; then
            read -r -p "Push anyway? The prod stack will use this weak password. [y/N] " PUSH_ANYWAY
        else
            read -r -p "Push anyway? Not recommended for deployed environments. [y/N] " PUSH_ANYWAY
        fi
        echo ""
        if [[ ! "${PUSH_ANYWAY}" =~ ^[Yy]$ ]]; then
            warn "Skipped ${SECRET_FILE} -- remote unchanged."
            continue
        fi
    fi

    # Check whether the remote already has the secret and confirm overwrite.
    # shellcheck disable=SC2029  # ${REMOTE_SECRET} and ${SSH_TARGET} intentionally expand client-side
    if ssh "${SSH_TARGET}" "[[ -f '${REMOTE_SECRET}' ]]" 2>/dev/null; then
        echo ""
        warn "Remote already has a live ${SECRET_FILE} at ${REMOTE_PATH}."
        warn "Overwriting will replace the DB password the running stack is using."
        read -r -p "Overwrite remote ${SECRET_FILE} with the local copy? [y/N] " CONFIRM_OVERWRITE
        echo ""
        if [[ ! "${CONFIRM_OVERWRITE}" =~ ^[Yy]$ ]]; then
            warn "Skipped live ${SECRET_FILE} -- remote unchanged."
            continue
        fi
    fi

    # Stage-then-install: same atomic pattern as the .env push above.
    # shellcheck disable=SC2029  # ${SECRET_BASENAME} and ${SSH_TARGET} intentionally expand client-side
    REMOTE_TMP=$(ssh "${SSH_TARGET}" "mktemp /tmp/.${SECRET_BASENAME}.XXXXXX")
    if [[ -z "${REMOTE_TMP}" ]]; then
        warn "Could not create temp file on remote -- skipped ${SECRET_FILE}."
        continue
    fi
    scp -q "${LOCAL_SECRET}" "${SSH_TARGET}:${REMOTE_TMP}"
    # shellcheck disable=SC2029  # ${REMOTE_TMP} and ${SSH_TARGET} intentionally expand client-side
    ssh "${SSH_TARGET}" "chmod 600 '${REMOTE_TMP}'"
    # shellcheck disable=SC2029  # ${REMOTE_TMP}, ${REMOTE_SECRET}, ${SSH_TARGET} intentionally expand client-side
    if ssh -tt "${SSH_TARGET}" "sudo install -m 600 -o \"\$(id -un)\" -g \"\$(id -gn)\" '${REMOTE_TMP}' '${REMOTE_SECRET}'"; then
        ssh "${SSH_TARGET}" "rm -f '${REMOTE_TMP}'" 2>/dev/null || true
        ok "Copied live ${SECRET_FILE} (chmod 600)"
    else
        warn "sudo install failed for ${SECRET_FILE} (see output above for details)."
        ssh "${SSH_TARGET}" "rm -f '${REMOTE_TMP}'" 2>/dev/null || true
        continue
    fi
done

# --- Fix line endings (Windows → Unix) ---
# scp from a Windows workstation copies files with CRLF line endings.
# Bash on the remote will choke on \r in shell scripts (e.g. "set -euo pipefail\r"
# becomes ": invalid option name"). Convert all text files to LF.
step "Converting line endings to LF on remote..."
# shellcheck disable=SC2029  # ${REMOTE_PATH} and ${SSH_TARGET} intentionally expand client-side
ssh "${SSH_TARGET}" "cd '${REMOTE_PATH}' && sed -i 's/\r\$//' docker-compose.*.yml scripts/*.sh config/*.json .env*.example .env.prod .env.dev 2>/dev/null || true"
ok "Line endings converted."

ok "All deployment files copied to ${REMOTE_PATH}."

# ---------------------------------------------------------------------------
# Step 5 — Run docker-setup.sh (interactive, requires sudo)
# ---------------------------------------------------------------------------

echo ""
warn "docker-setup.sh requires sudo on the remote and has interactive prompts"
warn "(password entry for .env files and GitHub Container Registry login)."
echo ""
read -r -p "Run docker-setup.sh now interactively via SSH? [y/N] " RUN_SETUP
echo ""

if [[ "${RUN_SETUP}" =~ ^[Yy]$ ]]; then
    step "Launching docker-setup.sh on ${REMOTE_HOST} via interactive SSH..."
    echo ""

    # Sanity check: verify the file copy succeeded before attempting to run the script.
    # shellcheck disable=SC2029  # ${REMOTE_PATH} and ${SSH_TARGET} intentionally expand client-side
    if ! ssh "${SSH_TARGET}" "[[ -f '${REMOTE_PATH}/scripts/docker-setup.sh' ]]" 2>/dev/null; then
        fail "scripts/docker-setup.sh not found at ${REMOTE_PATH}/scripts/docker-setup.sh on the remote."
        fail "The file copy in Step 4 may have failed. Re-run this script."
        exit 1
    fi

    # -t allocates a pseudo-TTY so that interactive prompts (read -p) work.
    ssh -t "${SSH_TARGET}" "cd '${REMOTE_PATH}' && sudo bash scripts/docker-setup.sh"
    ok "docker-setup.sh completed."
else
    echo -e "${CYAN}Skipped. To run docker-setup.sh manually, SSH into the server and run:${RESET}"
    echo ""
    echo "    ssh ${SSH_TARGET}"
    echo "    cd ${REMOTE_PATH}"
    echo "    sudo bash scripts/docker-setup.sh"
    echo ""
    echo "  docker-setup.sh will:"
    echo "    - Create .env.prod and .env.dev from the example files (with prompts)"
    echo "    - Copy example config JSON files into config/ and config-dev/"
    echo "    - Log in to GitHub Container Registry (ghcr.io)"
    echo "    - Pull and start both dev and prod Docker stacks"
    echo "    - Make ingest-cron.sh and backup.sh executable"
    echo "    - Print instructions for scheduling host-level cron jobs"
fi

# ---------------------------------------------------------------------------
# Step 6 — Summary
# ---------------------------------------------------------------------------

banner "Deployment Complete"

echo -e "${CYAN}Summary:${RESET}"
echo "  Server         : ${REMOTE_HOST}"
echo "  SSH user       : ${REMOTE_USER}"
echo "  Remote path    : ${REMOTE_PATH}"
echo ""
echo -e "${CYAN}Check stack status:${RESET}"
echo "  ssh ${SSH_TARGET} '${REMOTE_PATH}/scripts/docker-status.sh'"
echo ""

if [[ ! "${RUN_SETUP}" =~ ^[Yy]$ ]]; then
    echo -e "${CYAN}Complete setup manually:${RESET}"
    echo "  ssh ${SSH_TARGET}"
    echo "  cd ${REMOTE_PATH}"
    echo "  sudo bash scripts/docker-setup.sh"
    echo ""
fi

echo -e "${CYAN}URLs (once stacks are running):${RESET}"
echo "  Dev  (port 5000): http://${REMOTE_HOST}:5000"
echo "  Prod (port 5001): http://${REMOTE_HOST}:5001"
echo ""
echo -e "${CYAN}First-run config:${RESET}"
echo "  Dev  settings:    http://${REMOTE_HOST}:5000/settings"
echo "  Prod settings:    http://${REMOTE_HOST}:5001/settings"
echo ""
