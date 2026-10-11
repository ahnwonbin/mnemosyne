"""Isolated feature-audit acceptance checks for retained backend gaps."""
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from fastapi.testclient import TestClient
from server.api import create_app, Principal, token_hash
from bridge.outbox import Outbox
from server.db import Store
from server.maintenance import run_maintenance
from server.mcp_server import MemoryMCPServer
from server.models import Event
from server.observer import Observer


@pytest.fixture
def cloud(tmp_path):
    app = create_app(tmp_path / "remote.db", [
        Principal(token_hash("read-fixture"), "read")])
    with TestClient(app) as client:
        yield client, app.state.store


def seed(store, tmp_path, prompt="Architecture: use SQLite"):
    record = Outbox(tmp_path / "client.db").enqueue(
        "device-1", "proj-1", "codex", "session", "prompt_submit",
        {"prompt": prompt, "_context": {"project_name": "AuditProject"}}, "audit")
    store.ingest(Event.model_validate(record.to_transmission_dict()))
    return record.event_id


@pytest.mark.parametrize("name,args,key", [
    ("list_projects", {"limit": 1}, "projects"),
    ("project_context", {"project_id": "proj-1", "max_tokens": 800}, "summary"),
    ("search_memory", {"project_id": "proj-1", "query": "SQLite", "type": "decision"}, "results"),
    ("memory_timeline", {"project_id": "proj-1", "limit": 1}, "timeline"),
    ("get_memories", {"project_id": "proj-1"}, "memories"),
    ("memory_health", {}, "events"),
])
def test_remote_mcp_all_tools_use_authenticated_api(cloud, tmp_path, monkeypatch, name, args, key):
    client, store = cloud
    seed(store, tmp_path)
    assert Observer(store).tick(force=True)["status"] == "success"
    args = dict(args)
    if name == "get_memories":
        with store.connect() as connection:
            args["memory_ids"] = [connection.execute("SELECT id FROM observations").fetchone()[0]]
    expected = MemoryMCPServer(store=store).execute_tool(name, args)
    monkeypatch.setattr("server.mcp_server.httpx.Client", lambda **_: client)
    remote = MemoryMCPServer(endpoint_url="http://testserver/", auth_token="read-fixture")
    actual = remote.execute_tool(name, args)
    assert actual[key] == expected[key]
    if name == "get_memories":
        assert actual[key][0]["source_event_ids"]


def test_remote_mcp_authentication_failure_is_tool_error(cloud, monkeypatch):
    client, _ = cloud
    monkeypatch.setattr("server.mcp_server.httpx.Client", lambda **_: client)
    result = MemoryMCPServer(endpoint_url="http://testserver", auth_token="invalid").handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "memory_health", "arguments": {}}})
    assert result["result"]["isError"] is True
    assert "HTTP 401" in result["result"]["content"][0]["text"]


def test_stdio_subprocess_executes_all_six_tools(tmp_path):
    db_path = tmp_path / "stdio.db"
    store = Store(db_path)
    seed(store, tmp_path)
    assert Observer(store).tick(force=True)["status"] == "success"
    with store.connect() as connection:
        observation_id = connection.execute("SELECT id FROM observations").fetchone()[0]
    tools = [
        ("list_projects", {}),
        ("project_context", {"project_id": "proj-1"}),
        ("search_memory", {"project_id": "proj-1", "query": "SQLite"}),
        ("memory_timeline", {"project_id": "proj-1"}),
        ("get_memories", {"project_id": "proj-1", "memory_ids": [observation_id]}),
        ("memory_health", {}),
    ]
    requests = [{"jsonrpc": "2.0", "id": i, "method": "tools/call",
                 "params": {"name": name, "arguments": args}}
                for i, (name, args) in enumerate(tools)]
    messages = [json.dumps(request) for request in requests]
    messages.insert(0, json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    environment = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-B", "-m", "server.mcp_server", "--db", str(db_path)],
                            input="\n".join(messages) + "\n", text=True, encoding="utf-8",
                            capture_output=True, env=environment, timeout=15)
    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert [response["id"] for response in responses] == list(range(6))
    assert all(response["result"]["isError"] is False for response in responses)
    assert json.loads(responses[2]["result"]["content"][0]["text"])["total_matches"] >= 1
    assert json.loads(responses[4]["result"]["content"][0]["text"])["memories"][0]["source_event_ids"]


def test_simultaneous_observers_claim_only_one_lease(tmp_path):
    store = Store(tmp_path / "lease.db")
    seed(store, tmp_path)
    barrier = threading.Barrier(8)
    def claim(_):
        barrier.wait(timeout=5)
        return Observer(store).lease_job(force=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        leases = list(pool.map(claim, range(8)))
    assert sum(lease is not None for lease in leases) == 1
    with store.connect() as connection:
        job = connection.execute("SELECT attempts,state FROM processing_jobs").fetchone()
        assert (job["attempts"], job["state"]) == (1, "in_flight")
    pid, lid = next(lease for lease in leases if lease)
    assert Observer(store).process_project(pid, lid)["status"] == "success"
    assert store.health()["unprocessed_events"] == 0


def test_summary_versions_retain_prior_sources(tmp_path):
    store = Store(tmp_path / "versions.db")
    first = seed(store, tmp_path)
    assert Observer(store).tick(force=True)["summary_version"] == 1
    second = seed(store, tmp_path, prompt="Implement SQLite schema")
    assert Observer(store).tick(force=True)["summary_version"] == 2
    with store.connect() as connection:
        rows = connection.execute("SELECT version,covered_events FROM summary_versions ORDER BY version").fetchall()
    assert [row["version"] for row in rows] == [1, 2]
    assert first in json.loads(rows[0]["covered_events"])
    assert second in json.loads(rows[1]["covered_events"])


def test_maintenance_prunes_only_expired_snapshots(tmp_path):
    path = tmp_path / "prune.db"
    store = Store(path)
    old = store.list_projects()["snapshot"]
    fresh = store.list_projects()["snapshot"]
    with store.connect(write=True) as connection:
        connection.execute("UPDATE registry_snapshots SET created_at=? WHERE id=?",
                           ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(), old))
    result = run_maintenance(path, backup_dest=tmp_path / "backup.db", prune_days=1)
    assert result["snapshots_pruned"] == 1
    assert result["integrity_check"] == "ok"
    restored = Store(tmp_path / "backup.db")
    with restored.connect() as connection:
        snapshots = [row[0] for row in connection.execute("SELECT id FROM registry_snapshots")]
    assert snapshots == [fresh]
