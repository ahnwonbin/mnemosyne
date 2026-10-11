"""Maintenance operations for Hermes Memory Service:
prunes expired snapshots, executes WAL checkpoints, verifies integrity, and backs up database.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from server.db import Store


def run_maintenance(db_path: Path, backup_dest: Optional[Path] = None, prune_days: int = 1) -> dict:
    store = Store(db_path)
    report = {"database": str(db_path), "timestamp": datetime.now(timezone.utc).isoformat()}

    # 1. Prune expired snapshots
    with store.connect(write=True) as c:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=prune_days)).isoformat()
        deleted = c.execute("DELETE FROM registry_snapshots WHERE created_at < ?", (cutoff,)).rowcount
        report["snapshots_pruned"] = deleted

    # 2. Checkpoint WAL and check integrity outside active write transaction
    with store.connect(write=False) as c:
        c.execute("PRAGMA wal_checkpoint(PASSIVE)")
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        report["integrity_check"] = integrity
        if integrity != "ok":
            raise RuntimeError(f"Database integrity check failed: {integrity}")

    # 3. Optional backup
    if backup_dest:
        dest_path = Path(backup_dest).resolve()
        store.backup(dest_path)
        report["backup_created"] = str(dest_path)

    # 4. Fetch health stats
    report["health"] = store.health()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Memory Database Maintenance")
    parser.add_argument("--db", required=True, help="Path to SQLite memory database")
    parser.add_argument("--backup-to", help="Destination path for consistent backup")
    parser.add_argument("--prune-days", type=int, default=1, help="Age in days of snapshots to prune")
    args = parser.parse_args()

    res = run_maintenance(
        db_path=Path(args.db),
        backup_dest=Path(args.backup_to) if args.backup_to else None,
        prune_days=args.prune_days,
    )
    import json
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
