#!/usr/bin/env bash
# Run using an explicitly configured interpreter or the system Python.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${PYTHON_BIN:-python3}" "$SCRIPT_DIR/backup_memory.py" "$@"
