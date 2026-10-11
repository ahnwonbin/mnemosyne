"""Durable local delivery queue. No model calls; SQLite commits precede return."""
from __future__ import annotations
from dataclasses import dataclass, fields
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import uuid

from bridge.sanitizer import sanitize_payload

SCHEMA_VERSION = 1
DEFAULT_ADAPTER_VERSION = "0.2.0"


def current_iso_timestamp():
    return datetime.now(timezone.utc).isoformat()


def get_default_device_id(config_dir=None):
    directory = Path(config_dir or Path.home() / ".hermes")
    directory.mkdir(parents=True, exist_ok=True)
    # SQLite makes simultaneous first hooks choose a single identity.
    with sqlite3.connect(directory / "device.sqlite3", timeout=10) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        old_file = directory / "device_id"
        value = old_file.read_text(encoding="utf-8").strip() if old_file.is_file() else str(uuid.uuid4())
        conn.execute("INSERT OR IGNORE INTO identity VALUES (1, ?)", (value,))
        return conn.execute("SELECT value FROM identity WHERE id=1").fetchone()[0]


@dataclass
class OutboxEvent:
    event_id: str
    schema_version: int
    device_id: str
    project_id: str
    platform: str
    native_session_id: str
    session_sequence: int
    event_type: str
    occurred_at: str
    received_at: str
    source_identity: str
    payload_hash: str
    sanitized_payload: dict
    capture_adapter_version: str
    status: str
    retry_count: int
    next_retry_at: str | None
    last_error: str | None
    created_at: str
    sent_at: str | None

    def to_transmission_dict(self):
        data = {f.name: getattr(self, f.name) for f in fields(self)[:14]}
        data["payload"] = data.pop("sanitized_payload")
        return data


class Outbox:
    def __init__(self, db_path, max_pending_bytes=256 * 1024 * 1024):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_pending_bytes = max_pending_bytes
        with self._get_connection() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS outbox_events (
                    event_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
                    device_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    platform TEXT NOT NULL, native_session_id TEXT NOT NULL,
                    session_sequence INTEGER NOT NULL, event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL, received_at TEXT NOT NULL,
                    source_identity TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    sanitized_payload TEXT NOT NULL, capture_adapter_version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', retry_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT, last_error TEXT, created_at TEXT NOT NULL, sent_at TEXT,
                    lease_until TEXT);
                CREATE INDEX IF NOT EXISTS idx_outbox_delivery ON outbox_events(status,next_retry_at);
                CREATE INDEX IF NOT EXISTS idx_outbox_session ON outbox_events(device_id,platform,native_session_id,session_sequence);
                CREATE INDEX IF NOT EXISTS idx_outbox_created ON outbox_events(created_at,project_id);
            """)
            if "lease_until" not in {r["name"] for r in c.execute("PRAGMA table_info(outbox_events)")}:
                c.execute("ALTER TABLE outbox_events ADD COLUMN lease_until TEXT")
            c.execute("PRAGMA user_version=2")
        c.close()

    def _get_connection(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA busy_timeout=10000")
        return c

    @staticmethod
    def _event(row):
        data = {f.name: row[f.name] for f in fields(OutboxEvent)}
        data["sanitized_payload"] = json.loads(data["sanitized_payload"])
        return OutboxEvent(**data)

    def enqueue(self, device_id, project_id, platform, native_session_id, event_type,
                raw_payload, source_identity, occurred_at=None, event_id=None,
                adapter_version=DEFAULT_ADAPTER_VERSION):
        now = current_iso_timestamp()
        eid = event_id or str(uuid.uuid4())
        sanitized, _ = sanitize_payload(raw_payload)
        payload = json.dumps(sanitized, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        c = self._get_connection()
        try:
            with c:
                c.execute("BEGIN IMMEDIATE")
                old = c.execute("SELECT * FROM outbox_events WHERE event_id=?", (eid,)).fetchone()
                if old:
                    if (old["payload_hash"], old["device_id"], old["project_id"], old["platform"],
                        old["native_session_id"], old["event_type"], old["source_identity"]) != (
                        digest, device_id, project_id, platform, native_session_id, event_type, source_identity):
                        raise ValueError("event ID reused with different content")
                    return self._event(old)
                used = c.execute("SELECT COALESCE(SUM(length(CAST(sanitized_payload AS BLOB))),0) FROM outbox_events WHERE status!='sent'").fetchone()[0]
                if used + len(payload.encode()) > self.max_pending_bytes:
                    raise OSError("Outbox capacity reached; unsent events preserved. Capture needs attention.")
                seq = c.execute("SELECT COALESCE(MAX(session_sequence),0)+1 FROM outbox_events WHERE device_id=? AND platform=? AND native_session_id=?",
                                (device_id, platform, native_session_id)).fetchone()[0]
                c.execute("""INSERT INTO outbox_events
                    (event_id,schema_version,device_id,project_id,platform,native_session_id,
                     session_sequence,event_type,occurred_at,received_at,source_identity,payload_hash,
                     sanitized_payload,capture_adapter_version,status,retry_count,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',0,?)""",
                    (eid, SCHEMA_VERSION, device_id, project_id, platform, native_session_id, seq,
                     event_type, occurred_at or now, now, source_identity, digest, payload, adapter_version, now))
                return self._event(c.execute("SELECT * FROM outbox_events WHERE event_id=?", (eid,)).fetchone())
        finally:
            c.close()

    def get_pending_batch(self, limit=50, lease_seconds=120):
        if not 1 <= limit <= 100:
            raise ValueError("batch limit must be between 1 and 100")
        now = current_iso_timestamp()
        until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        c = self._get_connection()
        try:
            with c:
                c.execute("BEGIN IMMEDIATE")
                c.execute("""UPDATE outbox_events SET status='pending',lease_until=NULL
                    WHERE status='in_flight' AND (lease_until IS NULL OR lease_until<=?)""", (now,))
                rows = c.execute("""SELECT e.event_id FROM outbox_events e WHERE e.status='pending'
                    AND (e.next_retry_at IS NULL OR e.next_retry_at<=?)
                    AND NOT EXISTS (
                        SELECT 1 FROM outbox_events earlier
                        WHERE earlier.device_id=e.device_id AND earlier.platform=e.platform
                        AND earlier.native_session_id=e.native_session_id
                        AND earlier.session_sequence<e.session_sequence
                        AND (earlier.status='in_flight' OR
                             (earlier.status='pending' AND earlier.next_retry_at>?))
                    )
                    ORDER BY e.created_at,e.session_sequence LIMIT ?""", (now, now, limit)).fetchall()
                result = []
                for row in rows:
                    c.execute("UPDATE outbox_events SET status='in_flight',lease_until=? WHERE event_id=?", (until, row[0]))
                    result.append(self._event(c.execute("SELECT * FROM outbox_events WHERE event_id=?", (row[0],)).fetchone()))
                return result
        finally:
            c.close()

    def acknowledge_results(self, acknowledgments):
        c = self._get_connection()
        try:
            with c:
                for ack in acknowledgments:
                    status = ack["status"]
                    if status not in {"accepted", "duplicate", "rejected"}:
                        raise ValueError("invalid acknowledgment status")
                    c.execute("""UPDATE outbox_events SET status=?,sent_at=?,last_error=?,lease_until=NULL
                        WHERE event_id=? AND status='in_flight'""",
                        ("quarantined" if status == "rejected" else "sent",
                         None if status == "rejected" else current_iso_timestamp(),
                         ("Server rejected: " + str(ack.get("reason", "invalid event"))[:200]) if status == "rejected" else None,
                         ack["event_id"]))
        finally:
            c.close()

    def release_in_flight(self, event_ids, error_message=""):
        c = self._get_connection()
        try:
            with c:
                for eid in event_ids:
                    row = c.execute("SELECT retry_count FROM outbox_events WHERE event_id=? AND status='in_flight'", (eid,)).fetchone()
                    if not row:
                        continue
                    retries = row[0] + 1
                    delay = min(300, 2 ** min(retries, 8)) + random.uniform(.5, 3)
                    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                    # Network outages do not permanently quarantine valid events.
                    c.execute("""UPDATE outbox_events SET status='pending',retry_count=?,next_retry_at=?,
                        last_error=?,lease_until=NULL WHERE event_id=?""",
                        (retries, retry_at, error_message[:200], eid))
        finally:
            c.close()

    def quarantine_event(self, event_id, reason):
        c = self._get_connection()
        try:
            with c:
                c.execute("UPDATE outbox_events SET status='quarantined',last_error=?,lease_until=NULL WHERE event_id=?", (reason, event_id))
        finally:
            c.close()

    def retry(self, event_id):
        c = self._get_connection()
        try:
            with c:
                c.execute("UPDATE outbox_events SET status='pending',next_retry_at=NULL,last_error=NULL WHERE event_id=? AND status IN ('pending','quarantined')", (event_id,))
        finally:
            c.close()

    def get_stats(self):
        c = self._get_connection()
        try:
            stats = dict(pending=0, in_flight=0, sent=0, quarantined=0, total=0)
            for row in c.execute("SELECT status,COUNT(*) FROM outbox_events GROUP BY status"):
                stats[row[0]] = row[1]
                stats["total"] += row[1]
            return stats
        finally:
            c.close()

    def active_project_ids(self, hours=24):
        """Refresh recently captured projects and unfinished deliveries, not all history."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        c = self._get_connection()
        try:
            return [r[0] for r in c.execute("""
                SELECT project_id FROM outbox_events WHERE created_at>=?
                UNION SELECT project_id FROM outbox_events WHERE status IN ('pending','in_flight')
                ORDER BY project_id
            """, (cutoff,))]
        finally:
            c.close()
