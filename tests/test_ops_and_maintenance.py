"""Acceptance tests for database maintenance and cloud target validation."""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient

from server.api import create_app, Principal, token_hash
from server.db import Store
from server.maintenance import run_maintenance
from scripts.validate_cloud_target import validate_cloud, check_system_resources


@pytest.fixture
def test_cloud(tmp_path):
    db_path = tmp_path / "maint_test.db"
    principals = [
        Principal(token_hash("admin-secret"), "admin"),
        Principal(token_hash("read-secret"), "read"),
    ]
    app = create_app(db_path, principals)
    with TestClient(app) as client:
        yield client, db_path


def test_maintenance_wal_checkpoint_and_backup(test_cloud, tmp_path):
    client, db_path = test_cloud
    backup_dest = tmp_path / "backups" / "maint_backup.db"

    # Run maintenance
    report = run_maintenance(db_path=db_path, backup_dest=backup_dest, prune_days=1)
    assert report["integrity_check"] == "ok"
    assert report["snapshots_pruned"] == 0
    assert backup_dest.is_file()

    # Restored backup check
    restored = Store(backup_dest)
    assert restored.health()["projects"] == 0


def test_system_resource_checks():
    res = check_system_resources()
    assert "disk" in res
    assert res["disk"]["total_gb"] > 0
    assert res["disk"]["free_gb"] > 0


def test_cloud_target_validator(test_cloud, monkeypatch):
    client, db_path = test_cloud

    # Point validate_cloud at TestClient using custom transport
    import httpx
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)

    res = validate_cloud(
        api_url="http://testserver",
        token="read-secret",
    )
    assert res["all_passed"] is True
    checks = {c["name"]: c["status"] for c in res["checks"]}
    assert checks["healthz"] == "pass"
    assert checks["authenticated_health"] == "pass"
