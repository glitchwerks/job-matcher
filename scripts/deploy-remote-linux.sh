#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy-remote-linux.sh — Provision a Linux deploy server over SSH
#
# PURPOSE:
#   Runs from a developer workstation (Git Bash, WSL, or any POSIX shell).
#   Clones or updates the job-matcher-pr repo on a remote Linux host, copies
#   local .env example files if the live files don't yet exist, then
#   optionally runs scripts/docker-setup.sh on the remote (interactively via
#   SSH TTY) to complete the first-time Docker stack configuration.
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
#   - git installed
#   - docker + docker compose plugin installed (checked in Step 3)
#   - sudo access for docker-setup.sh (requires root for chown operations)
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

REPO_URL="https://github.com/cbeaulieu-gt/job-matcher-pr.git"

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

# Parse the key=value pairs emitted by the heredoc above.
eval "${REMOTE_CHECKS}"

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
# Step 4 — Clone or update the repo
# ---------------------------------------------------------------------------

step "Cloning / updating repository at ${REMOTE_PATH}..."

ssh "${SSH_TARGET}" bash <<REMOTE
set -euo pipefail

if [[ ! -e "${REMOTE_PATH}" ]]; then
    echo "  Directory not found -- cloning from ${REPO_URL}..."
    git clone "${REPO_URL}" "${REMOTE_PATH}"
    echo "  Clone complete."

elif [[ -d "${REMOTE_PATH}/.git" ]]; then
    echo "  Git repo found -- pulling latest from origin/main..."
    cd "${REMOTE_PATH}"
    git pull origin main
    echo "  Pull complete."

else
    echo "  WARN: ${REMOTE_PATH} exists but is NOT a git repository."
    echo "        Skipping git operations. Files already present will be used as-is."
fi
REMOTE

ok "Repository is up to date at ${REMOTE_PATH}."

# ---------------------------------------------------------------------------
# Step 5 — Copy local .env example files (don't overwrite existing live files)
# ---------------------------------------------------------------------------

step "Copying .env example files to remote (skipping if live files already exist)..."

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
    if ssh "${SSH_TARGET}" "[[ -f '${REMOTE_LIVE}' ]]" 2>/dev/null; then
        ok "${LIVE_NAME} already exists on remote -- skipping example copy."
    else
        scp -q "${LOCAL_EXAMPLE}" "${SSH_TARGET}:${REMOTE_EXAMPLE}"
        ok "Copied ${ENV_EXAMPLE} to ${REMOTE_PATH}/"
    fi
done

# ---------------------------------------------------------------------------
# Step 6 — Run docker-setup.sh (interactive, requires sudo)
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
# Step 7 — Summary
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
