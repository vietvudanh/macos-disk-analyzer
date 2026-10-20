#!/usr/bin/env bash
# ==============================================================================
# Daily snapshot runner for macos-disk-analyzer
# Executed automatically by launchd daemon or manually via `make schedule-run`
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Ensure Homebrew and standard system binaries are in PATH
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH}"

LOG_DIR="${REPO_DIR}/data"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/snapshot.log"
LOCK_FILE="/tmp/macos-disk-analyzer-snapshot.lock"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# Simple PID-based lock to prevent overlapping runs
if [ -f "${LOCK_FILE}" ]; then
    OLD_PID="$(cat "${LOCK_FILE}" 2>/dev/null || true)"
    if [ -n "${OLD_PID}" ] && kill -0 "${OLD_PID}" 2>/dev/null; then
        log "Another snapshot process (PID: ${OLD_PID}) is currently running. Skipping."
        exit 0
    fi
fi
echo "$$" > "${LOCK_FILE}"
trap 'rm -f "${LOCK_FILE}"' EXIT INT TERM

log "================================================================================"
log "Starting scheduled daily snapshot..."
log "Working directory: ${REPO_DIR}"

cd "${REPO_DIR}"

# Run make snapshot
if make snapshot; then
    log "Snapshot successfully generated."
else
    STATUS=$?
    log "ERROR: Snapshot failed with exit code ${STATUS}."
    exit "${STATUS}"
fi

# If running as root, restore repo owner permissions on data directory
REPO_OWNER="$(stat -f '%Su:%Sg' "${REPO_DIR}" 2>/dev/null || true)"
if [ -n "${REPO_OWNER}" ] && [ "$(id -u)" -eq 0 ]; then
    chown -R "${REPO_OWNER}" "${LOG_DIR}" 2>/dev/null || true
fi

log "Scheduled daily snapshot task completed."
log "================================================================================"
