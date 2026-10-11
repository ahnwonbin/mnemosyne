#!/usr/bin/env bash
# ==============================================================================
# Deploy Claude-Mem Persistent Worker Service on VM
# Authored by: Agy (Antigravity) on 2026-09-29
# Target: memory-worker (developer@memory.example)
# Purpose: Configure and start persistent user systemd service for upstream Claude-Mem
# ==============================================================================
set -euo pipefail
umask 077
: "${HERMES_PROXY_KEY:?Set HERMES_PROXY_KEY to your own local proxy credential}"

echo "===================================================================="
echo " Deploying Claude-Mem Worker Service (Authored by Agy)"
echo " Timestamp: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo " Host: $(hostname) | User: $(id -un)"
echo "===================================================================="

CMEM_DIR="$HOME/.claude-mem"
CMEM_SCRIPTS="$CMEM_DIR/scripts"
CMEM_MODES="$CMEM_DIR/modes"
CMEM_UI="$CMEM_DIR/ui"
mkdir -p "$CMEM_SCRIPTS" "$CMEM_MODES" "$CMEM_UI"

# 1. Verify Bun runtime
export PATH="$HOME/.bun/bin:$PATH"
if ! command -v bun >/dev/null 2>&1; then
    echo "ERROR: Bun runtime not found in ~/.bun/bin. Please run Phase 1 setup first." >&2
    exit 1
fi
BUN_BIN=$(command -v bun)
echo "  - Bun runtime verified: $($BUN_BIN --version) at $BUN_BIN"

# 2. Stage pinned artifacts (scripts, modes, ui)
echo "  - Staging pinned claude-mem@13.28.0 artifacts..."
if [ -d "$HOME/.claude-mem-trial/scripts" ] && [ -f "$HOME/.claude-mem-trial/modes/code.json" ] && [ -f "$HOME/.claude-mem/scripts/mcp-server.cjs" ]; then
    echo "  - Copying staged assets from trial directory..."
    cp -r "$HOME/.claude-mem-trial/scripts/"* "$CMEM_SCRIPTS/"
    cp -r "$HOME/.claude-mem-trial/modes/"* "$CMEM_MODES/"
    cp -r "$HOME/.claude-mem-trial/ui/"* "$CMEM_UI/"
else
    echo "  - Downloading official claude-mem@13.28.0 tarball..."
    TMP_TGZ="/tmp/claude-mem-13.28.0-$$.tgz"
    curl -fsSL "https://registry.npmjs.org/claude-mem/-/claude-mem-13.28.0.tgz" -o "$TMP_TGZ"
    tar -xzf "$TMP_TGZ" -C "/tmp" package/plugin/scripts package/plugin/modes package/plugin/ui
    cp -r /tmp/package/plugin/scripts/* "$CMEM_SCRIPTS/"
    cp -r /tmp/package/plugin/modes/* "$CMEM_MODES/"
    cp -r /tmp/package/plugin/ui/* "$CMEM_UI/"
    rm -rf "/tmp/package" "$TMP_TGZ"
fi
echo "  - Worker script staged: $(stat -c%s "$CMEM_SCRIPTS/worker-service.cjs") bytes"
echo "  - MCP server script staged: $(stat -c%s "$CMEM_SCRIPTS/mcp-server.cjs") bytes"
echo "  - Code mode staged: $(stat -c%s "$CMEM_MODES/code.json") bytes"

# 3. Write production configuration
echo "  - Writing production configuration..."
cat << 'EOF' > "$CMEM_DIR/settings.json"
{
  "CLAUDE_MEM_PROVIDER": "claude",
  "CLAUDE_MEM_MODEL": "gemini-3.8-flash-high",
  "CLAUDE_MEM_WORKER_PORT": "37777",
  "CLAUDE_MEM_WORKER_HOST": "127.0.0.1",
  "CLAUDE_MEM_CHROMA_ENABLED": "false",
  "CLAUDE_MEM_MAX_CONCURRENT_AGENTS": "1",
  "CLAUDE_MEM_TIER_ROUTING_ENABLED": "false",
  "CLAUDE_MEM_TELEMETRY": "false"
}
EOF

cat << EOF > "$CMEM_DIR/.env"
ANTHROPIC_BASE_URL=http://127.0.0.1:8317/v1
ANTHROPIC_API_KEY=${HERMES_PROXY_KEY}
CLAUDE_MEM_PROVIDER=claude
CLAUDE_MEM_MODEL=gemini-3.8-flash-high
EOF
chmod 600 "$CMEM_DIR/settings.json" "$CMEM_DIR/.env"

# 4. Install systemd user service
echo "  - Installing systemd user service..."
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"

SERVICE_FILE="$SYSTEMD_USER_DIR/claude-mem-worker.service"
cat << EOF > "$SERVICE_FILE"
[Unit]
Description=Claude-Mem Worker Service (Upstream Single-User Memory Backend, Authored by Agy)
After=network.target
Wants=network.target

[Service]
Type=simple
WorkingDirectory=%h/.claude-mem
Environment="PATH=%h/.bun/bin:/usr/local/bin:/usr/bin:/bin"
Environment="CLAUDE_MEM_DATA_DIR=%h/.claude-mem"
Environment="CLAUDE_MEM_ENV_FILE=%h/.claude-mem/.env"
Environment="CLAUDE_MEM_WORKER_PORT=37777"
Environment="CLAUDE_MEM_WORKER_HOST=127.0.0.1"
Environment="CLAUDE_MEM_CHROMA_ENABLED=false"
Environment="CLAUDE_MEM_MAX_CONCURRENT_AGENTS=1"
Environment="CLAUDE_MEM_TELEMETRY=false"
ExecStart=%h/.bun/bin/bun %h/.claude-mem/scripts/worker-service.cjs
Restart=always
RestartSec=5
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

# 5. Enable and start service
echo "  - Reloading systemd user daemon and starting service..."
systemctl --user daemon-reload
systemctl --user enable claude-mem-worker.service
systemctl --user restart claude-mem-worker.service

# 6. Verify service readiness
echo "  - Waiting for claude-mem-worker.service to become ready..."
READY=0
for i in {1..30}; do
    if curl -s -f "http://127.0.0.1:37777/api/readiness" >/dev/null 2>&1; then
        READY=1
        echo "  - Service is ACTIVE and READY on 127.0.0.1:37777 (after ${i}s)!"
        break
    fi
    sleep 1
done

if [ "$READY" -ne 1 ]; then
    echo "ERROR: Service failed to reach readiness within 30 seconds." >&2
    systemctl --user status claude-mem-worker.service --no-pager >&2 || true
    journalctl --user -u claude-mem-worker.service -n 30 --no-pager >&2 || true
    exit 1
fi

# 7. Measure service footprint
WORKER_PID=$(systemctl --user show claude-mem-worker.service --property=MainPID --value)
WORKER_RSS_KB=$(ps -p "$WORKER_PID" -o rss= 2>/dev/null || echo "0")
WORKER_RSS_MB=$((WORKER_RSS_KB / 1024))
HERMES_MEM=$(systemctl --user show hermes-gateway.service --property=MemoryCurrent --value 2>/dev/null || echo "0")
HERMES_RSS_MB=$((HERMES_MEM / 1024 / 1024))
FREE_RAM=$(free -m | awk '/^Mem:/{print $7}')

echo "===================================================================="
echo " Deployment Complete & Verified (Authored by Agy)"
echo "===================================================================="
echo "| Service | Status | Main PID | RSS Footprint |"
echo "|---|---|---|---|"
echo "| claude-mem-worker.service | $(systemctl --user is-active claude-mem-worker.service) | $WORKER_PID | ${WORKER_RSS_MB} MiB |"
echo "| hermes-gateway.service | $(systemctl --user is-active hermes-gateway.service) | - | ${HERMES_RSS_MB} MiB |"
echo "| Available Host RAM | - | - | ${FREE_RAM} MiB |"
echo "===================================================================="
