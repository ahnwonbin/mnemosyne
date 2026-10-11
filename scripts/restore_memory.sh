#!/usr/bin/env bash
# Hermes Memory Database Restore Script (made by agy)
set -euo pipefail

DRY_RUN=0
LIVE_TARGET=0
BACKUP_FILE=""
TARGET_PATH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --live-target)
            LIVE_TARGET=1
            if [[ $# -ge 2 && "$2" != --* ]]; then
                TARGET_PATH="$2"
                shift 2
            else
                shift
            fi
            ;;
        -*)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
        *)
            if [[ -z "$BACKUP_FILE" ]]; then
                BACKUP_FILE="$1"
            elif [[ -z "$TARGET_PATH" ]]; then
                TARGET_PATH="$1"
            else
                echo "Unexpected extra argument: $1" >&2
                exit 1
            fi
            shift
            ;;
    esac
done

if [[ -z "$BACKUP_FILE" ]]; then
    echo "Usage: $0 <path-to-backup.db> [--dry-run] [--live-target [target-path]]"
    exit 1
fi

if [[ ! -f "$BACKUP_FILE" ]]; then
    echo "ERROR: Backup file does not exist: $BACKUP_FILE" >&2
    exit 1
fi

if [[ -z "$TARGET_PATH" ]]; then
    TARGET_PATH="$HOME/.claude-mem/claude-mem.db"
fi

# If TARGET_PATH is a directory, append claude-mem.db
if [[ -d "$TARGET_PATH" ]] || [[ "$TARGET_PATH" == */ ]]; then
    TARGET_DB="${TARGET_PATH%/}/claude-mem.db"
    TARGET_DIR="${TARGET_PATH%/}"
else
    TARGET_DB="$TARGET_PATH"
    TARGET_DIR="$(dirname "$TARGET_PATH")"
fi

echo "=== Hermes Memory Database Restore (made by agy) ==="
echo "Source backup: $BACKUP_FILE"
echo "Target DB:     $TARGET_DB"
echo "Dry run:       $DRY_RUN"

# Verify backup integrity first
echo "[1/4] Verifying backup database integrity..."
python3 -c "
import sqlite3, sys
conn = sqlite3.connect('$BACKUP_FILE')
cur = conn.cursor()
cur.execute('PRAGMA integrity_check')
rows = cur.fetchall()
if len(rows) != 1 or rows[0][0] != 'ok':
    print('Integrity check failed:', rows, file=sys.stderr)
    sys.exit(1)
print('Backup integrity: OK')
"

if [[ $DRY_RUN -eq 1 ]]; then
    echo "[DRY-RUN] Integrity verified. No services stopped, no files modified."
    exit 0
fi

# Detect if target is active live worker data
REAL_TARGET_DIR="$(realpath "$TARGET_DIR" 2>/dev/null || echo "$TARGET_DIR")"
REAL_ACTIVE_1="$(realpath "$HOME/.claude-mem" 2>/dev/null || echo "$HOME/.claude-mem")"
REAL_ACTIVE_2="$(realpath "$HOME/.claude-mem-trial/data" 2>/dev/null || echo "$HOME/.claude-mem-trial/data")"

IS_LIVE=0
if [[ $LIVE_TARGET -eq 1 ]] || [[ "$REAL_TARGET_DIR" == "$REAL_ACTIVE_1"* ]] || [[ "$REAL_TARGET_DIR" == "$REAL_ACTIVE_2"* ]]; then
    IS_LIVE=1
fi

if [[ $IS_LIVE -eq 1 ]]; then
    echo "[2/4] Stopping claude-mem-worker service..."
    if systemctl --user is-active --quiet claude-mem-worker.service; then
        systemctl --user stop claude-mem-worker.service
        if systemctl --user is-active --quiet claude-mem-worker.service; then
            echo "ERROR: Failed to stop claude-mem-worker service! Aborting restore." >&2
            exit 1
        fi
    fi
else
    echo "[2/4] Target is isolated test path; service stop not required."
fi

# Archive existing target if present
if [[ -f "$TARGET_DB" ]]; then
    PRE_RESTORE_BAK="${TARGET_DB}.pre_restore_archive_$(date +%Y%m%d_%H%M%S).db"
    echo "[3/4] Archiving existing database to $PRE_RESTORE_BAK..."
    cp "$TARGET_DB" "$PRE_RESTORE_BAK"
    rm -f "${TARGET_DB}-shm" "${TARGET_DB}-wal"
else
    echo "[3/4] No existing database at target; skipping archive."
fi

# Perform restore
echo "[4/4] Restoring database file..."
mkdir -p "$TARGET_DIR"
cp "$BACKUP_FILE" "$TARGET_DB"

if [[ $IS_LIVE -eq 1 ]]; then
    echo "Restarting claude-mem-worker service..."
    systemctl --user start claude-mem-worker.service
    sleep 2
    if systemctl --user is-active --quiet claude-mem-worker.service; then
        echo "Worker active: OK"
    else
        echo "ERROR: Worker failed to restart!" >&2
        exit 1
    fi
fi

echo "=== Restore completed successfully! ==="
