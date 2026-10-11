"""Unit tests for durable SQLite outbox."""

from pathlib import Path
from bridge.outbox import Outbox, get_default_device_id


def test_device_id_stable(tmp_path: Path):
    dev1 = get_default_device_id(tmp_path)
    dev2 = get_default_device_id(tmp_path)
    assert dev1 == dev2
    assert len(dev1) > 10


def test_outbox_enqueue_and_monotonic_sequence(tmp_path: Path):
    db_file = tmp_path / "outbox.db"
    outbox = Outbox(db_file)

    evt1 = outbox.enqueue(
        device_id="dev-1",
        project_id="proj-1",
        platform="claude_code",
        native_session_id="sess-A",
        event_type="session_start",
        raw_payload={"initial": True},
        source_identity="hook:session_start",
    )
    assert evt1.session_sequence == 1
    assert evt1.status == "pending"

    evt2 = outbox.enqueue(
        device_id="dev-1",
        project_id="proj-1",
        platform="claude_code",
        native_session_id="sess-A",
        event_type="prompt_submit",
        raw_payload={"prompt": "hello"},
        source_identity="hook:prompt",
    )
    assert evt2.session_sequence == 2

    # Different session should start at sequence 1
    evt3 = outbox.enqueue(
        device_id="dev-1",
        project_id="proj-1",
        platform="claude_code",
        native_session_id="sess-B",
        event_type="session_start",
        raw_payload={},
        source_identity="hook:session_start",
    )
    assert evt3.session_sequence == 1


def test_outbox_batch_and_acknowledgments(tmp_path: Path):
    db_file = tmp_path / "outbox.db"
    outbox = Outbox(db_file)

    evt1 = outbox.enqueue(
        device_id="dev-1",
        project_id="proj-1",
        platform="codex",
        native_session_id="sess-1",
        event_type="prompt_submit",
        raw_payload={"msg": "test 1"},
        source_identity="hook:prompt",
    )
    evt2 = outbox.enqueue(
        device_id="dev-1",
        project_id="proj-1",
        platform="codex",
        native_session_id="sess-1",
        event_type="tool_call",
        raw_payload={"tool": "read"},
        source_identity="hook:tool",
    )
    evt3 = outbox.enqueue(
        device_id="dev-1",
        project_id="proj-1",
        platform="codex",
        native_session_id="sess-1",
        event_type="tool_result",
        raw_payload={"tool": "read", "bad": "poison"},
        source_identity="hook:tool_res",
    )

    batch = outbox.get_pending_batch(limit=10)
    assert len(batch) == 3
    assert all(e.status == "in_flight" for e in batch)

    # Empty on immediate second fetch because all are in_flight
    assert len(outbox.get_pending_batch(limit=10)) == 0

    # Acknowledge: 1 accepted, 1 duplicate, 1 rejected
    outbox.acknowledge_results([
        {"event_id": evt1.event_id, "status": "accepted"},
        {"event_id": evt2.event_id, "status": "duplicate"},
        {"event_id": evt3.event_id, "status": "rejected", "reason": "malformed schema"},
    ])

    stats = outbox.get_stats()
    assert stats["sent"] == 2
    assert stats["quarantined"] == 1
    assert stats["pending"] == 0
    assert stats["in_flight"] == 0


def test_outbox_release_in_flight_backoff(tmp_path: Path):
    db_file = tmp_path / "outbox.db"
    outbox = Outbox(db_file)

    evt = outbox.enqueue(
        device_id="dev-1",
        project_id="proj-1",
        platform="agy",
        native_session_id="sess-1",
        event_type="session_start",
        raw_payload={},
        source_identity="hook:start",
    )

    batch = outbox.get_pending_batch(limit=10)
    assert len(batch) == 1

    outbox.release_in_flight([evt.event_id], error_message="Network unreachable")

    stats = outbox.get_stats()
    assert stats["pending"] == 1
    assert stats["in_flight"] == 0

    # Events with future retry timestamp won't immediately appear in pending batch
    # unless next_retry_at <= now
    immediate_batch = outbox.get_pending_batch(limit=10)
    assert len(immediate_batch) == 0  # scheduled in the future due to backoff
