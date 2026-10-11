#!/usr/bin/env bash
# deploy_memory_stack.sh - Install and start the Hermes Project Memory service on GCP VM
set -euo pipefail
umask 077

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${HERMES_PYTHON:-$REPO_DIR/.venv/bin/python}"
: "${HERMES_DEVICE_ID:?Set HERMES_DEVICE_ID to the ID printed by hermes-bridge status on the capture device}"
if [[ ! "$HERMES_DEVICE_ID" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "Device ID must use letters, digits, underscores or hyphens." >&2
    exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Install the project dependencies into $REPO_DIR/.venv before deployment." >&2
    exit 1
fi
cd "$REPO_DIR"
"$PYTHON_BIN" -c 'import server.api, server.observer_daemon'

BASE_DIR="$HOME/.hermes/server"
DB_PATH="$BASE_DIR/memory.db"
CRED_PATH="$BASE_DIR/credentials.json"
SYSTEMD_DIR="$HOME/.config/systemd/user"

echo "=========================================================="
echo " 1. Setup Hermes Memory Directory and Credentials        "
echo "=========================================================="
mkdir -p "$BASE_DIR"
mkdir -p "$SYSTEMD_DIR"

if [ ! -f "$CRED_PATH" ]; then
    # Generate random credentials if not already present
    ADMIN_TOKEN=$(openssl rand -hex 24)
    DEVICE_TOKEN=$(openssl rand -hex 24)
    READ_TOKEN=$(openssl rand -hex 24)

    cat << EOF > "$CRED_PATH"
[
  {"token_hash": "$(echo -n "$ADMIN_TOKEN" | sha256sum | awk '{print $1}')", "role": "admin"},
  {"token_hash": "$(echo -n "$DEVICE_TOKEN" | sha256sum | awk '{print $1}')", "role": "ingest", "device_id": "$HERMES_DEVICE_ID"},
  {"token_hash": "$(echo -n "$READ_TOKEN" | sha256sum | awk '{print $1}')", "role": "read"}
]
EOF
    chmod 600 "$CRED_PATH"

    cat << EOF > "$BASE_DIR/tokens.env"
# KEEP THESE TOKENS SECURE - DO NOT COMMIT TO VERSION CONTROL
HERMES_ADMIN_TOKEN="$ADMIN_TOKEN"
HERMES_DEVICE_TOKEN="$DEVICE_TOKEN"
HERMES_READ_TOKEN="$READ_TOKEN"
EOF
    chmod 600 "$BASE_DIR/tokens.env"
    echo "Generated new credentials in $CRED_PATH and $BASE_DIR/tokens.env"
fi

echo "=========================================================="
echo " 2. Configure Systemd Services for Memory API & Observer   "
echo "=========================================================="

# 1. API Service Unit
cat << EOF > "$SYSTEMD_DIR/hermes-memory-api.service"
[Unit]
Description=Hermes Project Memory Cloud API
After=network.target

[Service]
Type=simple
WorkingDirectory="$REPO_DIR"
Environment="HERMES_MEMORY_DB=$DB_PATH"
Environment="HERMES_MEMORY_CREDENTIALS=$CRED_PATH"
Environment="HERMES_MEMORY_PORT=8787"
ExecStart="$PYTHON_BIN" -m server.api
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

# 2. Observer Daemon Service Unit
cat << EOF > "$SYSTEMD_DIR/hermes-memory-observer.service"
[Unit]
Description=Hermes Project Memory Observer Daemon
After=hermes-memory-api.service

[Service]
Type=simple
WorkingDirectory="$REPO_DIR"
Environment="HERMES_MEMORY_DB=$DB_PATH"
EnvironmentFile=-$BASE_DIR/observer.env
ExecStart="$PYTHON_BIN" -m server.observer_daemon
TimeoutStopSec=45
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable hermes-memory-api.service hermes-memory-observer.service
systemctl --user restart hermes-memory-api.service hermes-memory-observer.service

echo "=========================================================="
echo " 3. Register Memory MCP Tool in Hermes Agent config      "
echo "=========================================================="
export REPO_DIR PYTHON_BIN DB_PATH BASE_DIR
"$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path
fragment = {"mcp_servers": {"project_memory": {
    "command": os.environ["PYTHON_BIN"],
    "args": ["-m", "server.mcp_server", "--db", os.environ["DB_PATH"]],
    "env": {"PYTHONPATH": os.environ["REPO_DIR"]},
}}}
(Path(os.environ["BASE_DIR"]) / "project-memory-mcp.json").write_text(
    json.dumps(fragment, indent=2), encoding="utf-8")
PY
echo "Review and merge $BASE_DIR/project-memory-mcp.json into Hermes config."
echo "For proxy inference, configure HERMES_PROXY_URL, HERMES_PROXY_KEY and HERMES_MODEL in $BASE_DIR/observer.env."

echo "=========================================================="
echo " Services started; run validate_cloud_target.py on VM.   "
echo " Memory API running on http://127.0.0.1:8787              "
echo " Private credentials saved at $BASE_DIR/tokens.env       "
echo "=========================================================="
