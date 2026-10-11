"""Tests for cross-CLI handoff, discovery, and upstream transport guarantees (made by agy).

Verifies:
1. Automatic discovery of new projects in outbox without pre-registered aliases.
2. Context preservation: _context (cwd, branch, worktree_root) mapped to upstream body.
3. Durable acceptance: stored=false and skipped non-duplicate rejection.
4. Compaction budget enforcement and stale offline labeling.
"""

import json
import tempfile
from pathlib import Path
import pytest
import httpx

from bridge.outbox import Outbox, OutboxEvent, current_iso_timestamp
from bridge.cache import LocalCache, format_context_prompt
from bridge.upstream_transport import UpstreamTransportClient


def test_automatic_discovery_without_manual_alias():
    """Verify that unaliased projects in outbox are automatically discovered for context refresh."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "outbox.db")
        outbox = Outbox(db_path=db_path)

        # Enqueue event for an unaliased project
        outbox.enqueue(
            device_id="dev-test",
            project_id="auto-discovered-proj",
            platform="agy",
            native_session_id="sess-auto-1",
            event_type="session_start",
            raw_payload={"prompt": "Work on auto discovered project"},
            source_identity="hook:test"
        )

        # Test outbox._get_connection query as in bridge/cli.py
        cache = LocalCache(base_dir=Path(tmp_dir) / "cache")
        known_projects = [
            info["project_id"] for info in cache.get_known_aliases().values() if "project_id" in info
        ]
        assert "auto-discovered-proj" not in known_projects

        conn = outbox._get_connection()
        try:
            rows = conn.execute("SELECT DISTINCT project_id FROM outbox_events WHERE project_id IS NOT NULL").fetchall()
            for r in rows:
                if r[0] and r[0] not in known_projects:
                    known_projects.append(r[0])
        finally:
            conn.close()

        assert "auto-discovered-proj" in known_projects


def test_context_and_provenance_preservation():
    """Verify that _context metadata (branch, worktree_root, cwd) is preserved in upstream payload."""
    client = UpstreamTransportClient(endpoint_url="http://127.0.0.1:37777")

    event = OutboxEvent(
        event_id="ev-meta-001",
        schema_version=1,
        device_id="dev-test",
        project_id="test-proj",
        platform="agy",
        native_session_id="sess-meta-1",
        session_sequence=1,
        event_type="tool_result",
        occurred_at=current_iso_timestamp(),
        received_at=current_iso_timestamp(),
        source_identity="hook:agy:PostToolUse:1",
        payload_hash="hash-meta",
        sanitized_payload={
            "tool": "git",
            "input": {"command": "checkout feat/login"},
            "output": "Switched to branch 'feat/login'",
            "_context": {
                "cwd": "/home/user/repo-worktree",
                "branch": "feat/login",
                "worktree_root": "/home/user/repo-worktree"
            }
        },
        capture_adapter_version="0.2.0",
        status="pending",
        retry_count=0,
        next_retry_at=None,
        last_error=None,
        created_at=current_iso_timestamp(),
        sent_at=None
    )

    mapped = client.map_event_to_upstream(event)
    body = mapped["body"]

    assert body["cwd"] == "/home/user/repo-worktree", "Must use cwd from _context instead of falling back to source_identity"
    assert body["tool_response"]["branch"] == "feat/login", "Must preserve branch provenance"
    assert body["tool_response"]["worktree_root"] == "/home/user/repo-worktree", "Must preserve worktree provenance"


def test_durable_acceptance_stored_false_rejected():
    """Verify that stored=false causes retry rather than marking event as sent."""
    client = UpstreamTransportClient(endpoint_url="http://127.0.0.1:37777")
    event = OutboxEvent(
        event_id="ev-store-fail",
        schema_version=1,
        device_id="dev-test",
        project_id="test-proj",
        platform="agy",
        native_session_id="sess-1",
        session_sequence=1,
        event_type="session_start",
        occurred_at=current_iso_timestamp(),
        received_at=current_iso_timestamp(),
        source_identity="hook:test",
        payload_hash="hash-1",
        sanitized_payload={"prompt": "test"},
        capture_adapter_version="0.2.0",
        status="pending",
        retry_count=0,
        next_retry_at=None,
        last_error=None,
        created_at=current_iso_timestamp(),
        sent_at=None
    )

    # Mock transport returning HTTP 200 with stored=false
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"status": "queued", "stored": False}))
    with httpx.Client(transport=transport) as http_client:
        with pytest.raises(RuntimeError, match="not durably stored"):
            client.sync_event(http_client, event)


def test_skipped_reasons_distinguish_duplicate_from_rejection():
    """Verify that skipped with reason=duplicate is duplicate, but other reasons are rejected."""
    client = UpstreamTransportClient(endpoint_url="http://127.0.0.1:37777")
    event = OutboxEvent(
        event_id="ev-skip-1",
        schema_version=1,
        device_id="dev-test",
        project_id="test-proj",
        platform="agy",
        native_session_id="sess-1",
        session_sequence=1,
        event_type="session_start",
        occurred_at=current_iso_timestamp(),
        received_at=current_iso_timestamp(),
        source_identity="hook:test",
        payload_hash="hash-1",
        sanitized_payload={"prompt": "test"},
        capture_adapter_version="0.2.0",
        status="pending",
        retry_count=0,
        next_retry_at=None,
        last_error=None,
        created_at=current_iso_timestamp(),
        sent_at=None
    )

    # Duplicate case
    t_dup = httpx.MockTransport(lambda req: httpx.Response(200, json={"status": "skipped", "reason": "already_exists", "sessionDbId": 1}))
    with httpx.Client(transport=t_dup) as http:
        res = client.sync_event(http, event)
        assert res["status"] == "duplicate"

    # Rejection case (e.g. private or empty)
    t_priv = httpx.MockTransport(lambda req: httpx.Response(200, json={"status": "skipped", "reason": "private"}))
    with httpx.Client(transport=t_priv) as http:
        res = client.sync_event(http, event)
        assert res["status"] == "rejected"


def test_compaction_budget_and_stale_label():
    """Verify format_context_prompt truncates oversized context and adds stale banner."""
    oversized_text = "A" * 5000
    ctx = {
        "project": "proj-big",
        "stale": True,
        "content": [{"type": "text", "text": oversized_text}]
    }

    formatted = format_context_prompt(ctx, max_chars=1000)
    assert "[NOTE: Cached offline context" in formatted
    assert "[... truncated remaining context to fit budget]" in formatted
    assert len(formatted) <= 1200


def test_native_subprocess_dispatch_handoff():
    """Verify end-to-end native subprocess dispatch (codex -> outbox -> cache -> agy)."""
    import os
    import sys
    import subprocess
    from bridge.cli import refresh_recent_context_cache
    from bridge.identity import resolve_project

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        proj_dir = tmp_path / "fresh_unaliased_project"
        proj_dir.mkdir(parents=True, exist_ok=True)
        bridge_home = tmp_path / ".hermes"
        outbox_db = bridge_home / "bridge" / "outbox.db"

        env = os.environ.copy()
        env["HERMES_BRIDGE_DIR"] = str(bridge_home)
        env["HERMES_OUTBOX"] = str(outbox_db)

        runner = str(Path(__file__).resolve().parent.parent / "bridge" / "hook_entry.py")

        # 1. Native host invocation: Codex SessionStart
        p_start = subprocess.run(
            [sys.executable, runner, "hook", "codex", "SessionStart"],
            input=json.dumps({"session_id": "sess-codex-native-1", "source": "startup"}),
            cwd=str(proj_dir),
            capture_output=True,
            text=True,
            env=env
        )
        assert p_start.returncode == 0, f"SessionStart failed: {p_start.stderr}"

        # 2. Verify outbox event persistence
        assert outbox_db.exists(), "outbox.db must be created"
        outbox = Outbox(db_path=str(outbox_db))
        stats = outbox.get_stats()
        assert stats["pending"] == 1, f"Expected 1 pending event, got {stats}"

        # 3. Native host invocation: UserPromptSubmit & PostToolUse & Stop
        p_prompt = subprocess.run(
            [sys.executable, runner, "hook", "codex", "UserPromptSubmit"],
            input=json.dumps({"session_id": "sess-codex-native-1", "prompt": "Implement secure authentication"}),
            cwd=str(proj_dir),
            capture_output=True,
            text=True,
            env=env
        )
        assert p_prompt.returncode == 0

        p_tool = subprocess.run(
            [sys.executable, runner, "hook", "codex", "PostToolUse"],
            input=json.dumps({
                "session_id": "sess-codex-native-1",
                "tool_name": "bash",
                "tool_result": "pytest passed 5/5 tests"
            }),
            cwd=str(proj_dir),
            capture_output=True,
            text=True,
            env=env
        )
        assert p_tool.returncode == 0

        p_stop = subprocess.run(
            [sys.executable, runner, "hook", "codex", "Stop"],
            input=json.dumps({"session_id": "sess-codex-native-1"}),
            cwd=str(proj_dir),
            capture_output=True,
            text=True,
            env=env
        )
        assert p_stop.returncode == 0

        stats = outbox.get_stats()
        assert stats["pending"] == 4, f"Expected 4 pending events from native codex dispatch, got {stats}"

        # 4. Sync events to mocked upstream worker
        resolved_proj = resolve_project(str(proj_dir))
        mock_summary = (
            f"# Recent Session Context\n\nShowing last 1 session(s) for **{resolved_proj.project_id}**:\n\n---\n\n"
            "**Summary (Prompt #1)**\n\n**Request:** Implement secure authentication\n"
            "**Completed:** Initialized auth module with 5 passing tests.\n"
            "**Learned:** Security tokens must include explicit expiration timestamps.\n"
            "**Next Steps:** Connect authentication routes to the API gateway."
        )

        def mock_handler(req: httpx.Request):
            if "/api/context/recent" in str(req.url):
                return httpx.Response(200, json={"content": [{"type": "text", "text": mock_summary}]})
            if "/api/tool-uses/batch" in str(req.url):
                body = json.loads(req.content.decode()) if req.content else {}
                return httpx.Response(200, json=[{"tool_use_id": tid} for tid in body.get("ids", [])])
            if "/api/sessions/init" in str(req.url):
                return httpx.Response(200, json={"status": "ok", "stored": True, "sessionDbId": 42})
            return httpx.Response(200, json={"status": "queued", "stored": True})

        transport = httpx.MockTransport(mock_handler)
        client = UpstreamTransportClient(endpoint_url="http://127.0.0.1:37777", outbox=outbox, transport=transport)
        # Drain outbox
        sync_res = client.sync_all()
        assert sync_res["total_accepted"] == 4
        assert sync_res["last_error"] is None

        # Refresh cache using automatic discovery (no aliases registered)
        cache = LocalCache(base_dir=bridge_home)
        known_projects = [
            info["project_id"] for info in cache.get_known_aliases().values() if "project_id" in info
        ]
        conn = outbox._get_connection()
        try:
            rows = conn.execute("SELECT DISTINCT project_id FROM outbox_events WHERE project_id IS NOT NULL").fetchall()
            for r in rows:
                if r[0] and r[0] not in known_projects:
                    known_projects.append(r[0])
        finally:
            conn.close()

        cache_res = refresh_recent_context_cache(client, cache, known_projects)
        assert cache_res["refreshed"] == 1
        assert resolved_proj.project_id in known_projects

        # 5. Native host invocation: Agy PreInvocation in same unaliased directory
        p_agy = subprocess.run(
            [sys.executable, runner, "hook", "agy", "PreInvocation"],
            input=json.dumps({"session_id": "sess-agy-native-1"}),
            cwd=str(proj_dir),
            capture_output=True,
            text=True,
            env=env
        )
        assert p_agy.returncode == 0
        agy_out = json.loads(p_agy.stdout)
        assert "injectSteps" in agy_out, "Agy must receive injectSteps"
        injected_msg = agy_out["injectSteps"][0]["ephemeralMessage"]
        assert f"[Hermes Memory: {resolved_proj.project_id}]" in injected_msg
        assert "Security tokens must include explicit expiration timestamps" in injected_msg


def test_offline_warning_stale_flag():
    """Verify that failed context refresh marks cache as stale and includes offline warning."""
    from bridge.cli import refresh_recent_context_cache
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        cache_dir = tmp_path / "cache"
        cache = LocalCache(base_dir=cache_dir)

        # Seed existing cache entry
        cache.save_cached_context("proj-offline", {
            "project": "proj-offline",
            "content": [{"type": "text", "text": "Cached previous context"}]
        })

        # Mock transport that fails (offline)
        transport = httpx.MockTransport(lambda req: httpx.Response(503, text="Service Unavailable"))
        client = UpstreamTransportClient(endpoint_url="http://127.0.0.1:37777", transport=transport)
        res = refresh_recent_context_cache(client, cache, ["proj-offline"])
        assert len(res["errors"]) == 1

        cached = cache.get_cached_context("proj-offline")
        assert cached is not None
        assert cached.get("stale") is True, "Failed refresh must mark cached context as stale"

        formatted = format_context_prompt(cached)
        assert "[NOTE: Cached offline context — upstream refresh pending]" in formatted
