import json
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock

import pytest

from bridge.cache import LocalCache
from bridge.cli import _trigger_background_sync, build_parser, cmd_config, cmd_sync
from bridge.migration import HistoricalMigrator
from bridge.outbox import Outbox
from server.db import Store, Conflict
from server.models import Event, MilestonePlan
from server.observer import Observer
from server.retrieval import (estimate_tokens, get_project_context, get_memories,
                              get_memory_timeline, search_memories)


@pytest.fixture
def pipeline(tmp_path):
    store = Store(tmp_path / "cloud.db")
    box = Outbox(tmp_path / "outbox.db")

    def ingest(payload=None, kind="prompt_submit", project="p"):
        payload = {**(payload or {"prompt": "Use SQLite"}), "_context": {"project_name": project}}
        e = box.enqueue("dev", project, "codex", "session", kind,
                        payload, "source")
        store.ingest(Event.model_validate(e.to_transmission_dict()))
        return e.event_id

    return store, box, ingest


def test_all_retrieval_envelopes_obey_budget(pipeline):
    store, _, ingest = pipeline
    ingest({"prompt": "SQLite " + "\u0e01" * 20000})
    assert Observer(store).tick(force=True)["status"] == "success"
    with store.connect(write=True) as c:
        oid = c.execute("SELECT id FROM observations").fetchone()[0]
        c.execute("UPDATE summary_versions SET body=?", ("summary " * 20000,))
        c.execute("UPDATE observations SET title=?", ("title " * 20000,))
    responses = [get_project_context(store, "p", 150),
                 get_memories(store, "p", [oid], 150),
                 get_memory_timeline(store, "p", max_tokens=150),
                 search_memories(store, "p", "SQLite", max_tokens=150)]
    for response in responses:
        assert estimate_tokens(response) <= 150
        assert response["estimated_tokens"] == estimate_tokens(response)
        assert response["truncated"] is True


@pytest.mark.parametrize("output", [None, [], {}, {"summary": "bad", "observations": [{}]},
    {"summary": "bad", "observations": [{"type": "change", "title": "x", "narrative": "x",
                                          "source_event_ids": ["foreign-event"]}]}])
def test_invalid_inference_is_durable_failure(pipeline, output):
    store, _, ingest = pipeline
    ingest()
    result = Observer(store, Mock(model="test", generate=Mock(return_value=output))).tick(force=True)
    assert result["status"] == "failed"
    assert store.health()["unprocessed_events"] == 1
    with store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        assert c.execute("SELECT status FROM processing_attempts").fetchone()[0] == "failed"
        assert c.execute("SELECT state FROM processing_jobs").fetchone()[0] == "pending"


def test_inferred_verified_milestone_requires_acceptance(pipeline):
    store, _, ingest = pipeline
    eid = ingest()
    store.set_plan("p", MilestonePlan(milestones=[{"milestone_id": "m", "title": "Ship", "weight": 1}]))
    output = {"summary": "done", "observations": [], "candidate_milestones": [
        {"milestone_id": "m", "status": "done", "evidence_level": "verified", "event_ids": [eid]}]}
    assert Observer(store, Mock(model="test", generate=Mock(return_value=output))).tick(force=True)["status"] == "failed"
    with store.connect() as c:
        assert c.execute("SELECT status FROM milestones").fetchone()[0] == "todo"


def test_expired_lease_cannot_commit(pipeline):
    store, _, ingest = pipeline
    ingest()
    observer = Observer(store)
    pid, lid = observer.lease_job(force=True)
    with store.connect(write=True) as c:
        c.execute("UPDATE processing_jobs SET lease_until=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
    with pytest.raises(Conflict):
        observer.process_project(pid, lid)
    assert store.health()["unprocessed_events"] == 1


@pytest.mark.parametrize("kind,payload", [("tool_call", {"tool": "write", "success": True}),
                                         ("tool_result", {"tool": "write", "success": None})])
def test_unconfirmed_tools_are_not_verified(pipeline, kind, payload):
    store, _, ingest = pipeline
    ingest(payload, kind)
    Observer(store).tick(force=True)
    with store.connect() as c:
        row = c.execute("SELECT evidence_level,narrative FROM observations").fetchone()
        assert row["evidence_level"] == "reported"
        assert "successfully" not in row["narrative"]


def test_failed_job_is_retryable_after_new_event(pipeline):
    store, _, ingest = pipeline
    ingest()
    with store.connect(write=True) as c:
        c.execute("UPDATE processing_jobs SET state='failed',attempts=5")
    ingest({"prompt": "Retry"})
    assert Observer(store).tick(force=True)["status"] == "success"


def test_migration_retry_is_idempotent(pipeline):
    _, box, _ = pipeline
    migrator = HistoricalMigrator(box, "dev")
    bundle = {"project": "legacy", "platform": "codex", "prompt": "Hello",
              "observations": [{"title": "Saved"}], "summary": {"completed": "Saved"}}
    first = migrator.migrate_turn_bundle(bundle)
    second = migrator.migrate_turn_bundle(bundle)
    assert first["event_ids"] == second["event_ids"]
    assert box.get_stats()["total"] == 3


def test_backup_same_named_sources_do_not_overwrite(tmp_path):
    sources = []
    for name in ("a", "b"):
        path = tmp_path / name / "config.json"
        path.parent.mkdir()
        path.write_text(name)
        sources.append(path)
    result = HistoricalMigrator.backup_legacy_stores(sources, tmp_path / "backup")
    from pathlib import Path
    assert [Path(item["backup"]).read_text() for item in result["backups"]] == ["a", "b"]


def test_background_sync_survives_hook_exit_without_token_in_argv(tmp_path, monkeypatch):
    cache = LocalCache(tmp_path / "cache")
    cache.save_config({"token": "secret"})
    monkeypatch.delenv("HERMES_DEVICE_TOKEN", raising=False)
    box = Outbox(tmp_path / "outbox.db")
    popen = Mock()
    monkeypatch.setattr("bridge.cli.subprocess.Popen", popen)
    _trigger_background_sync(cache, box)
    args, kwargs = popen.call_args
    assert "secret" not in args[0]
    assert kwargs["env"]["HERMES_DEVICE_TOKEN"] == "secret"
    assert kwargs["env"]["HERMES_OUTBOX"] == str(box.db_path)
    assert kwargs["close_fds"] is True


def test_config_redacts_credentials(tmp_path, monkeypatch, capsys):
    cache = LocalCache(tmp_path)
    cache.save_config({"token": "secret-device", "read_token": "secret-read"})
    monkeypatch.setattr("bridge.cli.LocalCache", lambda: cache)
    cmd_config(build_parser().parse_args(["config"]))
    assert "secret" not in capsys.readouterr().out


def test_sync_failure_has_nonzero_exit(pipeline, tmp_path, monkeypatch):
    _, box, _ = pipeline
    cache = LocalCache(tmp_path / "cache")
    monkeypatch.setattr("bridge.cli.LocalCache", lambda: cache)
    monkeypatch.setattr("bridge.cli.get_default_outbox", lambda: box)
    client = Mock()
    client.sync_all.return_value = dict(total_batches=1, total_accepted=0, total_duplicate=0,
                                       total_rejected=0, last_error="HTTP 401", outbox_stats={})
    monkeypatch.setattr("bridge.cli.SyncClient", Mock(return_value=client))
    assert cmd_sync(build_parser().parse_args(["sync", "--token", "test"])) == 1


def test_cache_refresh_uses_read_scope_and_preserves_offline_data(pipeline, tmp_path, monkeypatch):
    import httpx
    from bridge.sync import refresh_context_cache
    _, box, ingest = pipeline
    ingest()
    cache = LocalCache(tmp_path / "cache")
    cache.save_config({"token": "ingest-secret", "read_token": "read-secret"})
    monkeypatch.delenv("HERMES_READ_TOKEN", raising=False)

    def respond(request):
        assert request.headers["authorization"] == "Bearer read-secret"
        return httpx.Response(200, json={"project_id": "p", "summary": {"version": 2, "body": "Saved"}})

    result = refresh_context_cache(cache, box, transport=httpx.MockTransport(respond))
    assert result["refreshed"] == 1
    original = cache.get_cached_context("p")
    failed = refresh_context_cache(cache, box, transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    assert len(failed["errors"]) == 1
    assert cache.get_cached_context("p") == original


def test_uninstall_preserves_unowned_config(tmp_path, monkeypatch):
    from bridge.installer import uninstall_hooks
    config = tmp_path / "hooks.json"
    config.write_text('{"other": true}')
    monkeypatch.setattr("bridge.installer.get_hook_config_paths", lambda: {"codex": config})
    assert uninstall_hooks("codex")["status"] == "not_installed"
    assert json.loads(config.read_text()) == {"other": True}


def test_reinstall_keeps_original_restore_point(tmp_path, monkeypatch):
    from bridge.installer import install_hooks, uninstall_hooks
    config = tmp_path / "hooks.json"
    original = '{"other": true}'
    config.write_text(original)
    monkeypatch.setattr("bridge.installer.get_hook_config_paths", lambda: {"codex": config})
    first = install_hooks("codex")
    second = install_hooks("codex")
    assert first["backup_path"] == second["backup_path"]
    assert uninstall_hooks("codex")["status"] == "reverted_from_backup"
    assert config.read_text() == original


def test_plan_change_during_inference_retries_without_commit(pipeline):
    store, _, ingest = pipeline
    ingest()
    def generate(**kwargs):
        store.set_plan("p", MilestonePlan(milestones=[{"milestone_id": "m", "title": "New", "weight": 1}]))
        return {"summary": "stale", "observations": []}
    assert Observer(store, Mock(model="test", generate=generate)).tick(force=True)["status"] == "failed"
    with store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM summary_versions").fetchone()[0] == 0
    assert store.health()["unprocessed_events"] == 1


def test_explicit_acceptance_can_complete_milestone(pipeline):
    store, _, ingest = pipeline
    ingest()
    store.set_plan("p", MilestonePlan(milestones=[{"milestone_id": "m", "title": "Ship", "weight": 1}]))
    ingest({"milestone_id": "m", "plan_version": 1}, "milestone_acceptance")
    assert Observer(store).tick(force=True)["status"] == "success"
    with store.connect() as c:
        row = c.execute("SELECT status,evidence_level FROM milestones").fetchone()
        assert tuple(row) == ("done", "verified")


def test_invalid_batch_size_does_not_claim_job(pipeline):
    store, _, ingest = pipeline
    ingest()
    with pytest.raises(ValueError):
        Observer(store).tick(batch_size=0, force=True)
    assert store.health()["jobs"][0]["attempts"] == 0


def test_daemon_cooperative_shutdown(tmp_path, monkeypatch):
    import threading
    from server.observer_daemon import run_daemon
    stop = threading.Event()
    tick = Mock(side_effect=lambda **_: stop.set())
    monkeypatch.setattr("server.observer_daemon.Observer", lambda **_: Mock(tick=tick))
    run_daemon(tmp_path / "daemon.db", stop_event=stop, poll_interval=60)
    assert tick.call_count == 1
