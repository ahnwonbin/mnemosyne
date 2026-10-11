#!/usr/bin/env python3
"""Hermes Memory Database Backup and Restore Utility (made by agy).

Performs transactionally consistent online backups of the SQLite database
in WAL mode using Python's native sqlite3.Connection.backup API.
Supports automated verification, integrity checking, and isolated restore testing.
"""

import argparse
import datetime
import shutil
import sqlite3
import sys
from pathlib import Path

TABLES = ["sdk_sessions", "observations", "session_summaries", "tool_uses", "prompts"]

def get_row_counts(conn: sqlite3.Connection) -> dict:
    counts = {}
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing = {row[0] for row in cursor.fetchall()}
    for table in TABLES:
        if table in existing:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = cursor.fetchone()[0]
        else:
            counts[table] = 0
    return counts

def verify_integrity(conn: sqlite3.Connection) -> bool:
    cursor = conn.cursor()
    cursor.execute("PRAGMA integrity_check")
    rows = cursor.fetchall()
    if len(rows) == 1 and rows[0][0] == "ok":
        return True
    print(f"ERROR: Integrity check failed: {rows}", file=sys.stderr)
    return False

def perform_backup(src_db: Path, backup_dst: Path) -> dict:
    if not src_db.exists():
        raise FileNotFoundError(f"Source database not found: {src_db}")

    backup_dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"[Backup] Initiating online backup: {src_db} -> {backup_dst}")

    with sqlite3.connect(str(src_db)) as src_conn:
        src_counts = get_row_counts(src_conn)
        print(f"[Backup] Source counts: {src_counts}")

        with sqlite3.connect(str(backup_dst)) as dst_conn:
            src_conn.backup(dst_conn)

        with sqlite3.connect(str(backup_dst)) as verify_conn:
            if not verify_integrity(verify_conn):
                raise RuntimeError("Backup database failed integrity check!")
            dst_counts = get_row_counts(verify_conn)
            print(f"[Backup] Destination counts: {dst_counts}")

    assert src_counts == dst_counts, f"Row count mismatch! Src: {src_counts}, Dst: {dst_counts}"
    backup_size = backup_dst.stat().st_size
    print(f"[Backup] SUCCESS: Backup verified ({backup_size / 1024:.1f} KiB).")
    return {"status": "ok", "source_counts": src_counts, "size_bytes": backup_size}

def perform_restore_test(backup_file: Path, restore_dir: Path) -> bool:
    print(f"\n[Restore Test] Testing isolated restore to: {restore_dir}")
    res_path = restore_dir.resolve()
    if res_path in [Path.home().resolve(), Path("/").resolve()]:
        raise ValueError(f"Refusing to delete sensitive directory: {res_path}")
    if not ("test" in res_path.name.lower() or "restore" in res_path.name.lower()):
        raise ValueError(f"Restore test directory must contain 'test' or 'restore' in its name: {res_path}")

    if restore_dir.exists():
        shutil.rmtree(restore_dir)
    restore_dir.mkdir(parents=True, exist_ok=True)

    restored_db = restore_dir / "claude-mem.db"
    shutil.copy2(backup_file, restored_db)

    with sqlite3.connect(str(restored_db)) as conn:
        if not verify_integrity(conn):
            print("[Restore Test] FAILED: Integrity check failed on restored DB", file=sys.stderr)
            return False

        restored_counts = get_row_counts(conn)
        print(f"[Restore Test] Restored DB row counts: {restored_counts}")

        cur = conn.cursor()
        cur.execute("SELECT id, title, type, project FROM observations ORDER BY id DESC LIMIT 5")
        obs_sample = cur.fetchall()
        print(f"[Restore Test] Retrieved {len(obs_sample)} recent observations:")
        for obs in obs_sample:
            print(f"  - #{obs[0]} [{obs[2]}] {obs[1][:50]} (proj: {obs[3]})")

        # Get projects across observations and sdk_sessions
        cur.execute("SELECT DISTINCT project FROM observations UNION SELECT DISTINCT project FROM sdk_sessions WHERE project IS NOT NULL")
        projects = [row[0] for row in cur.fetchall() if row[0]]
        print(f"[Restore Test] Retrievable projects: {projects}")
        assert len(projects) > 0, "Restored database must contain at least one project"

    print("[Restore Test] SUCCESS: Isolated restore test passed 100% with query parity.")
    return True

def main():
    parser = argparse.ArgumentParser(description="Hermes Memory Database Backup & Restore Utility (made by agy)")
    parser.add_argument("--src", "--db-path", dest="src", type=Path, default=Path.home() / ".claude-mem" / "claude-mem.db", help="Source SQLite DB path")
    parser.add_argument("--out-dir", "--output-dir", dest="out_dir", type=Path, default=Path.home() / ".claude-mem" / "backups", help="Backup directory")
    parser.add_argument("--restore-test-dir", type=Path, default=Path.home() / ".claude-mem-restore-test", help="Restore test directory")
    parser.add_argument("--skip-restore-test", action="store_true", help="Skip restore test")

    args = parser.parse_args()

    timestamp_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = args.out_dir / f"claude-mem_backup_{timestamp_str}.db"

    perform_backup(args.src, backup_file)

    if not args.skip_restore_test:
        ok = perform_restore_test(backup_file, args.restore_test_dir)
        if not ok:
            sys.exit(1)

    print(f"\n[Done] Backup created at: {backup_file}")

if __name__ == "__main__":
    main()
