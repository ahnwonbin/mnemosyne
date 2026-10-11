"""Acceptance tests for discovery reconciliation, historical migration, and cost benchmark."""
from __future__ import annotations
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from benchmarks.cost_benchmark import BenchmarkSimulator
from bridge.discovery import DiscoveryEngine
from bridge.migration import HistoricalMigrator
from bridge.outbox import Outbox
from server.api import create_app, Principal, token_hash


@pytest.fixture
def cloud_app(tmp_path):
    db_path = tmp_path / "cloud_disc.db"
    principals = [
        Principal(token_hash("admin-disc"), "admin"),
        Principal(token_hash("device-disc"), "ingest", "device-d1"),
        Principal(token_hash("read-disc"), "read"),
    ]
    app = create_app(db_path, principals)
    with TestClient(app) as client:
        yield client, app.state.store


def test_discovery_coverage_failures_and_vault(tmp_path):
    root1 = tmp_path / "workspaces"
    root1.mkdir()
    proj1 = root1 / "code_proj"
    proj1.mkdir()
    (proj1 / "pyproject.toml").write_text("[project]\nname='CodeProj'", encoding="utf-8")

    # Vault directory with project charters
    vault_root = tmp_path / "Obsidian_Vault"
    vault_root.mkdir()
    vault_proj = vault_root / "DesignProject"
    vault_proj.mkdir()
    (vault_proj / "charter.md").write_text("# Project Charter for Design", encoding="utf-8")

    # Non-existent root to test failure recording
    bad_root = tmp_path / "does_not_exist"

    engine = DiscoveryEngine(workspace_roots=[root1, bad_root], vault_roots=[vault_root])
    result = engine.scan(max_depth=2)

    assert result["total_found"] >= 2
    assert len(result["failures"]) >= 1
    assert any("does_not_exist" in f["path"] for f in result["failures"])

    names = [c["name"] for c in result["candidates"]]
    assert "code_proj" in names
    assert "DesignProject" in names

    vault_candidates = [c for c in result["candidates"] if c.get("is_vault")]
    assert len(vault_candidates) >= 1
    assert vault_candidates[0]["name"] == "DesignProject"


def test_discovery_submission_and_candidate_reconciliation(cloud_app, tmp_path):
    client, store = cloud_app
    auth_ingest = {"Authorization": "Bearer device-disc"}
    auth_admin = {"Authorization": "Bearer admin-disc"}
    auth_read = {"Authorization": "Bearer read-disc"}

    scan_data = {
        "run_id": "run-disc-1",
        "device_id": "device-d1",
        "roots": ["c:/repo"],
        "failures": [],
        "candidates": [
            {
                "candidate_id": "cand-1",
                "name": "DiscoveredAlpha",
                "path": "c:/repo/alpha",
                "is_git": True,
                "disposition": "unresolved",
            },
            {
                "candidate_id": "cand-2",
                "name": "DiscoveredBeta",
                "path": "c:/repo/beta",
                "is_git": False,
                "disposition": "unresolved",
            },
        ],
    }

    # Submit discovery run
    post_res = client.post("/api/v1/discovery/runs", headers=auth_ingest, json=scan_data)
    assert post_res.status_code == 200
    assert post_res.json()["candidates_recorded"] == 2

    # Query candidates
    list_res = client.get("/api/v1/discovery/candidates?disposition=unresolved", headers=auth_read)
    assert list_res.status_code == 200
    candidates = list_res.json()
    assert len(candidates) == 2

    # Reconcile cand-1 as registered
    rec_res = client.put(
        "/api/v1/discovery/candidates/cand-1/reconcile",
        headers=auth_admin,
        json={"run_id": "run-disc-1", "disposition": "registered"},
    )
    assert rec_res.status_code == 200
    assert rec_res.json()["disposition"] == "registered"

    # Verify project was added to projects table
    projects = client.get("/api/v1/projects", headers=auth_read).json()["projects"]
    assert any(p["name"] == "DiscoveredAlpha" for p in projects)

    # Reconcile cand-2 as excluded
    rec_res2 = client.put(
        "/api/v1/discovery/candidates/cand-2/reconcile",
        headers=auth_admin,
        json={"run_id": "run-disc-1", "disposition": "excluded"},
    )
    assert rec_res2.status_code == 200
    assert rec_res2.json()["disposition"] == "excluded"


def test_historical_migration_and_provenance(tmp_path):
    outbox = Outbox(tmp_path / "mig_outbox.db")
    migrator = HistoricalMigrator(outbox=outbox, device_id="dev-mig")

    turn_data = {
        "turn_id": "turn-historic-1",
        "platform": "codex",
        "project": "legacy_hermes",
        "timestamp": "2026-09-01T12:00:00+00:00",
        "prompt": "Initial setup of SQLite schema",
        "observations": [
            {
                "type": "change",
                "title": "Created tables",
                "narrative": "Created initial SQLite tables",
            }
        ],
        "summary": {
            "completed": "Setup initial database tables",
            "next_steps": "Add endpoints",
        },
    }

    res = migrator.migrate_turn_bundle(turn_data)
    assert res["events_enqueued"] == 3
    stats = outbox.get_stats()
    assert stats["pending"] == 3

    events = outbox.get_pending_batch()
    types = {e.event_type for e in events}
    assert "prompt_submit" in types
    assert "tool_result" in types
    assert "assistant_response" in types
    for e in events:
        assert e.platform == "migration"
        assert "legacy_turn_id" in e.sanitized_payload["_context"]


def test_backup_legacy_stores(tmp_path):
    legacy_file = tmp_path / "legacy_notes.json"
    legacy_file.write_text('{"data": "test"}', encoding="utf-8")

    backup_dir = tmp_path / "backups"
    res = HistoricalMigrator.backup_legacy_stores([legacy_file], backup_dir)
    assert res["backup_count"] == 1
    assert Path(res["backups"][0]["backup"]).is_file()


def test_cost_benchmark_simulator():
    sim = BenchmarkSimulator()
    res = sim.run_full_benchmark()
    comp = res["summary_comparison"]
    assert "local_agent_tokens" in comp
    assert comp["local_agent_tokens"]["config_c"] < comp["local_agent_tokens"]["config_a"]
    assert comp["local_memory_overhead_on_agent_output"]["config_c"] == "0%"
