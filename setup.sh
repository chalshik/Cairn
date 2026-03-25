#!/usr/bin/env bash
# setup.sh — build, start, and register Cairn in one command.
#
# Usage:
#   ./setup.sh          # start on default port 8000
#   ./setup.sh 9000     # start on a custom port
#   ./setup.sh stop     # stop the container
#   ./setup.sh restart  # restart the container
#   ./setup.sh logs     # follow container logs

set -euo pipefail

PORT="${1:-8000}"
MCP_NAME="cairn"
SSE_URL="http://localhost:${PORT}/sse"

# ── Helpers ────────────────────────────────────────────────────────────────

info()    { printf '\033[1;34m[cairn]\033[0m %s\n' "$*"; }
success() { printf '\033[1;32m[cairn]\033[0m %s\n' "$*"; }
warn()    { printf '\033[1;33m[cairn]\033[0m %s\n' "$*"; }
error()   { printf '\033[1;31m[cairn]\033[0m %s\n' "$*" >&2; exit 1; }

require() {
    command -v "$1" &>/dev/null || error "'$1' is required but not found."
}

wait_healthy() {
    local max=90 interval=3 elapsed=0
    info "Waiting for container to become healthy (up to ${max}s)…"
    while (( elapsed < max )); do
        local status
        status=$(docker inspect --format='{{.State.Health.Status}}' cairn 2>/dev/null || echo "missing")
        case "$status" in
            healthy) return 0 ;;
            unhealthy) error "Container is unhealthy. Run: docker logs cairn" ;;
        esac
        sleep "$interval"
        (( elapsed += interval ))
    done
    error "Timed out waiting for healthy status. Run: docker logs cairn"
}

# ── Sub-commands ────────────────────────────────────────────────────────────

cmd_stop() {
    info "Stopping cairn…"
    docker compose down --remove-orphans
    success "Stopped."
}

cmd_restart() {
    info "Restarting cairn…"
    docker compose restart
    wait_healthy
    success "Restarted and healthy."
}

cmd_logs() {
    docker logs -f cairn
}

# ── Main start flow ─────────────────────────────────────────────────────────

require docker
require claude

case "${PORT}" in
    stop)    cmd_stop;    exit 0 ;;
    restart) cmd_restart; exit 0 ;;
    logs)    cmd_logs;    exit 0 ;;
esac

# Override port if non-default
export CAIRN_PORT="${PORT}"

info "Building image…"
CAIRN_PORT="${PORT}" docker compose build --pull

info "Starting container on port ${PORT}…"
CAIRN_PORT="${PORT}" docker compose up -d --remove-orphans

wait_healthy
success "Container is healthy at ${SSE_URL}"

# Register (or re-register) with Claude Code
info "Registering MCP server with Claude Code…"
if claude mcp list 2>/dev/null | grep -q "^${MCP_NAME}"; then
    warn "MCP server '${MCP_NAME}' already registered — skipping."
else
    claude mcp add --transport sse "${MCP_NAME}" "${SSE_URL}"
    success "Registered: ${MCP_NAME} → ${SSE_URL}"
fi

echo ""
success "Done! Cairn is running. Quick test:"
echo "  claude -p 'Use the cairn MCP to scrape https://boards.greenhouse.io/stripe and count engineering jobs'"
echo ""
echo "  Manage:"
echo "    ./setup.sh stop      # stop"
echo "    ./setup.sh restart   # restart"
echo "    ./setup.sh logs      # follow logs"
echo "    docker stats cairn   # resource usage"
