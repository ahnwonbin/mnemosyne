"""Isolated supplemental delivery-feature audit; no live services."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

from bridge.outbox import Outbox, get_default_device_id
from bridge.sanitizer import sanitize_payload
from bridge.cache import LocalCache
from bridge.cli import build_parser, cmd_status, cmd_config

def test_parallel_capture_allocates_unique_contiguous_sequences(tmp_path):
    path = tmp_path / "concurrent.db"
    Outbox(path)
    gate = threading.Barrier(8)
    def capture(worker):
        box = Outbox(path)
        gate.wait(timeout=10)
        return [box.enqueue("d", "p", "codex", "s", "prompt_submit",
                {"prompt": f"{worker}:{i}"}, f"source:{worker}:{i}")
                for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        events = [event for batch in pool.map(capture, range(8)) for event in batch]
    assert sorted(e.session_sequence for e in events) == list(range(1, 65))
    assert len({e.event_id for e in events}) == 64
    assert Outbox(path).get_stats()["pending"] == 64

def test_parallel_first_device_identity_is_single_and_durable(tmp_path):
    directory = tmp_path / "identity"
    gate = threading.Barrier(8)
    def identify(_):
        gate.wait(timeout=10)
        return get_default_device_id(directory)
    with ThreadPoolExecutor(max_workers=8) as pool:
        identities = list(pool.map(identify, range(8)))
    assert len(set(identities)) == 1
    assert get_default_device_id(directory) == identities[0]

def test_quarantine_manual_retry_restores_deliverable_event(tmp_path):
    box = Outbox(tmp_path / "queue.db")
    e = box.enqueue("d", "p", "codex", "s", "prompt_submit", {"prompt": "recover"}, "test")
    box.quarantine_event(e.event_id, "synthetic rejection")
    assert box.get_stats()["quarantined"] == 1
    assert box.get_pending_batch() == []
    box.retry(e.event_id)
    batch = box.get_pending_batch()
    assert [event.event_id for event in batch] == [e.event_id]
    assert batch[0].last_error is None
    assert box.get_stats()["in_flight"] == 1

def test_deep_nesting_and_total_payload_are_bounded():
    deep = "leaf"
    for _ in range(30):
        deep = {"child": deep}
    clean, notes = sanitize_payload({"output": deep})
    assert "Nesting limit" in notes
    assert "[OMITTED: nesting limit]" in json.dumps(clean)
    clean, notes = sanitize_payload({"output": ["x" * 4000] * 200}, max_payload_bytes=4096)
    assert len(json.dumps(clean).encode()) <= 4096
    assert "Payload total byte limit exceeded" in notes

def test_status_and_config_do_not_expose_tokens(tmp_path, monkeypatch, capsys):
    box = Outbox(tmp_path / "queue.db")
    box.enqueue("d", "p", "codex", "s", "prompt_submit", {"prompt": "x"}, "test")
    cache = LocalCache(tmp_path / "cache")
    cache.save_config({"token": "synthetic-secret", "read_token": "synthetic-read"})
    monkeypatch.setattr("bridge.cli.get_default_outbox", lambda: box)
    monkeypatch.setattr("bridge.cli.LocalCache", lambda: cache)
    monkeypatch.setattr("bridge.cli.get_default_device_id", lambda: "audit-device")
    parser = build_parser()
    assert cmd_status(parser.parse_args(["status"])) == 0
    assert cmd_config(parser.parse_args(["config"])) == 0
    output = capsys.readouterr().out
    assert "audit-device" in output
    assert "Pending:       1" in output
    assert "synthetic-secret" not in output and "synthetic-read" not in output

def test_process_exit_releases_sender_lock(tmp_path):
    import subprocess
    import sys
    from bridge.worker import file_lock
    lock = tmp_path / "sender.lock"
    code = ("import os,sys\nfrom bridge.worker import file_lock\n"
            "with file_lock(sys.argv[1]) as acquired:\n"
            " assert acquired\n os._exit(0)\n")
    child = subprocess.run([sys.executable, "-B", "-c", code, str(lock)],
                           capture_output=True, text=True, timeout=15)
    assert child.returncode == 0, child.stderr
    with file_lock(lock) as acquired:
        assert acquired

def test_discovery_depth_excludes_nested_projects(tmp_path):
    from bridge.discovery import DiscoveryEngine
    shallow = tmp_path / "one"
    shallow.mkdir()
    (shallow / "pyproject.toml").write_text("[project]")
    deep = tmp_path / "container" / "two" / "three"
    deep.mkdir(parents=True)
    (deep / "pyproject.toml").write_text("[project]")
    engine = DiscoveryEngine([tmp_path])
    names = {candidate["name"] for candidate in engine.scan(max_depth=1)["candidates"]}
    assert names == {"one"}
    assert "three" in {candidate["name"] for candidate in engine.scan(max_depth=4)["candidates"]}
