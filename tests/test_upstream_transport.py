"""Unit and fault tests for UpstreamTransportClient (Claude-Mem bridge).
Authored by: Agy (Antigravity) on 2026-09-29.
"""
from __future__ import annotations
import json
from pathlib import Path
import pytest
import httpx

from bridge.outbox import Outbox, OutboxEvent, current_iso_timestamp
from bridge.upstream_transport import (
    UpstreamTransportClient,
    validate_upstream_endpoint,
)


def make_test_event(
    event_id: str,
    event_type: str,
    session_id: str = "sess-001",
    project_id: str = "proj-test",
    payload: dict = None,
    platform: str = "agy",
) -> OutboxEvent:
    return OutboxEvent(
        event_id=event_id,
        schema_version=1,
        device_id="dev-001",
        project_id=project_id,
        platform=platform,
        native_session_id=session_id,
        session_sequence=1,
        event_type=event_type,
        occurred_at=current_iso_timestamp(),
        received_at=current_iso_timestamp(),
        source_identity="hook:test",
        payload_hash="abc",
        sanitized_payload=payload or {},
        capture_adapter_version="0.2.0",
        status="pending",
        retry_count=0,
        next_retry_at=None,
        last_error=None,
        created_at=current_iso_timestamp(),
        sent_at=None,
    )


class TestEndpointValidation:
    def test_valid_loopback(self):
        assert validate_upstream_endpoint("http://127.0.0.1:37777") == "http://127.0.0.1:37777"
        assert validate_upstream_endpoint("http://localhost:37777/") == "http://localhost:37777"
        assert validate_upstream_endpoint("") == "http://127.0.0.1:37777"

    def test_valid_https(self):
        assert validate_upstream_endpoint("https://secure.example.com") == "https://secure.example.com"

    def test_reject_insecure_remote_http(self):
        with pytest.raises(ValueError, match="use HTTPS or loopback HTTP"):
            validate_upstream_endpoint("http://memory.example:37777")

    def test_reject_credentials_or_query(self):
        with pytest.raises(ValueError, match="invalid endpoint URL"):
            validate_upstream_endpoint("http://user:pass@127.0.0.1:37777")
        with pytest.raises(ValueError, match="invalid endpoint URL"):
            validate_upstream_endpoint("http://127.0.0.1:37777?foo=bar")


class TestPayloadMapping:
    def test_map_prompt_submit(self):
        client = UpstreamTransportClient()
        event = make_test_event("e-1", "prompt_submit", payload={"prompt": "Fix bug in parser"})
        mapping = client.map_event_to_upstream(event)
        assert mapping["path"] == "/api/sessions/init"
        assert mapping["body"]["contentSessionId"] == "sess-001"
        assert mapping["body"]["project"] == "proj-test"
        assert mapping["body"]["prompt"] == "Fix bug in parser"
        assert mapping["body"]["platformSource"] == "agy"

    def test_map_tool_result(self):
        client = UpstreamTransportClient()
        event = make_test_event(
            "e-2",
            "tool_result",
            payload={
                "tool": "bash",
                "input": {"command": "git status"},
                "output": "clean",
                "exit_code": 0,
                "success": True,
            },
        )
        mapping = client.map_event_to_upstream(event)
        assert mapping["path"] == "/api/sessions/observations"
        assert mapping["body"]["tool_name"] == "bash"
        assert mapping["body"]["tool_use_id"] == "e-2"
        assert mapping["body"]["tool_response"]["output"] == "clean"
        assert mapping["body"]["tool_response"]["success"] is True

    def test_map_assistant_response(self):
        client = UpstreamTransportClient()
        event = make_test_event("e-3", "assistant_response", payload={"response": "Task complete"})
        mapping = client.map_event_to_upstream(event)
        assert mapping["path"] == "/api/sessions/summarize"
        assert mapping["body"]["last_assistant_message"] == "Task complete"

    def test_map_session_end(self):
        client = UpstreamTransportClient()
        event = make_test_event("e-4", "session_end")
        mapping = client.map_event_to_upstream(event)
        assert mapping["path"] == "/api/sessions/session-end"
        assert mapping["body"]["contentSessionId"] == "sess-001"

    def test_map_metadata_event(self):
        client = UpstreamTransportClient()
        event = make_test_event("e-5", "tool_call", payload={"tool": "bash"})
        mapping = client.map_event_to_upstream(event)
        assert mapping["path"] is None


class TestSyncExecution:
    def test_sync_batch_successful(self, tmp_path: Path):
        db_path = tmp_path / "outbox.db"
        outbox = Outbox(db_path)

        # Enqueue events using outbox.enqueue
        outbox.enqueue("dev-1", "proj-test", "agy", "sess-1", "prompt_submit", {"prompt": "Start working"}, "hook:test:1", event_id="evt-1")
        outbox.enqueue("dev-1", "proj-test", "agy", "sess-1", "tool_result", {"tool": "bash", "output": "ok"}, "hook:test:2", event_id="evt-2")
        outbox.enqueue("dev-1", "proj-test", "agy", "sess-1", "assistant_response", {"response": "Done"}, "hook:test:3", event_id="evt-3")

        requests_received = []

        def handler(request: httpx.Request):
            requests_received.append(request.url.path)
            if request.url.path == "/api/tool-uses/batch":
                return httpx.Response(200, json=[{"tool_use_id": "evt-2"}])
            if request.url.path == "/api/sessions/init":
                return httpx.Response(200, json={"sessionDbId": 1, "status": "initialized"})
            elif request.url.path in {"/api/sessions/observations", "/api/sessions/summarize"}:
                return httpx.Response(200, json={"status": "queued"})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        client = UpstreamTransportClient(outbox=outbox, transport=transport)

        res = client.sync_batch(batch_size=10)
        assert res.success is True
        assert res.batch_size == 3
        assert res.accepted == 3

        # Verify requests hit upstream routes (including Stage 2 durability check)
        assert requests_received == [
            "/api/sessions/init",
            "/api/sessions/observations",
            "/api/tool-uses/batch",
            "/api/sessions/summarize",
        ]

        # Verify outbox has zero pending remaining
        stats = outbox.get_stats()
        assert stats["pending"] == 0

    def test_sync_batch_error_and_release(self, tmp_path: Path):
        db_path = tmp_path / "outbox.db"
        outbox = Outbox(db_path)

        outbox.enqueue("dev-1", "proj-test", "agy", "sess-1", "prompt_submit", {"prompt": "Test error"}, "hook:test:err", event_id="evt-err")

        def handler(request: httpx.Request):
            return httpx.Response(500, text="Internal Server Error")

        transport = httpx.MockTransport(handler)
        client = UpstreamTransportClient(outbox=outbox, transport=transport)

        res = client.sync_batch(batch_size=10)
        assert res.success is False
        assert "HTTP 500" in (res.error or "")

        # Verify event was released back to pending (with retry_count incremented)
        stats = outbox.get_stats()
        assert stats["in_flight"] == 0
        assert stats["pending"] == 1

    def test_sync_batch_worker_initializing_rejection(self, tmp_path: Path):
        db_path = tmp_path / "outbox.db"
        outbox = Outbox(db_path)

        outbox.enqueue("dev-1", "proj-test", "agy", "sess-1", "prompt_submit", {"prompt": "Test init"}, "hook:test:init", event_id="evt-init")

        def handler(request: httpx.Request):
            return httpx.Response(503, json={"error": "Service initializing", "message": "Database is still initializing, please retry"})

        transport = httpx.MockTransport(handler)
        client = UpstreamTransportClient(outbox=outbox, transport=transport)

        res = client.sync_batch(batch_size=10)
        assert res.success is False
        assert "initializing" in (res.error or "").lower()

    def test_sync_batch_duplicate_skip(self, tmp_path: Path):
        db_path = tmp_path / "outbox.db"
        outbox = Outbox(db_path)

        outbox.enqueue("dev-1", "proj-test", "agy", "sess-1", "tool_result", {"tool": "bash", "output": "repeated"}, "hook:test:dup", event_id="evt-dup")

        def handler(request: httpx.Request):
            if request.url.path == "/api/tool-uses/batch":
                return httpx.Response(200, json=[{"tool_use_id": "evt-dup"}])
            return httpx.Response(200, json={"status": "skipped", "reason": "duplicate observation"})

        transport = httpx.MockTransport(handler)
        client = UpstreamTransportClient(outbox=outbox, transport=transport)

        res = client.sync_batch(batch_size=10)
        assert res.success is True
        assert res.duplicate == 1
        assert res.accepted == 0
        stats = outbox.get_stats()
        assert stats["pending"] == 0

    def test_sync_two_simultaneous_sessions(self, tmp_path: Path):
        db_path = tmp_path / "outbox.db"
        outbox = Outbox(db_path)

        # Enqueue interleaved events from two distinct sessions
        outbox.enqueue("dev-1", "proj-alpha", "agy", "sess-alpha", "prompt_submit", {"prompt": "Alpha start"}, "hook:alpha:1", event_id="evt-a1")
        outbox.enqueue("dev-1", "proj-beta", "codex", "sess-beta", "prompt_submit", {"prompt": "Beta start"}, "hook:beta:1", event_id="evt-b1")
        outbox.enqueue("dev-1", "proj-alpha", "agy", "sess-alpha", "tool_result", {"tool": "cat", "output": "data"}, "hook:alpha:2", event_id="evt-a2")
        outbox.enqueue("dev-1", "proj-beta", "codex", "sess-beta", "tool_result", {"tool": "grep", "output": "match"}, "hook:beta:2", event_id="evt-b2")

        session_map = {}

        def handler(request: httpx.Request):
            if request.url.path == "/api/tool-uses/batch":
                body = json.loads(request.content) if request.content else {}
                return httpx.Response(200, json=[{"tool_use_id": tid} for tid in body.get("ids", [])])
            body = json.loads(request.content)
            sess = body.get("contentSessionId")
            session_map.setdefault(sess, []).append(request.url.path)
            if request.url.path == "/api/sessions/init":
                return httpx.Response(200, json={"status": "initialized", "sessionDbId": 10})
            return httpx.Response(200, json={"status": "queued"})

        transport = httpx.MockTransport(handler)
        client = UpstreamTransportClient(outbox=outbox, transport=transport)

        res = client.sync_batch(batch_size=10)
        assert res.success is True
        assert res.batch_size == 4
        assert res.accepted == 4
        assert "sess-alpha" in session_map and len(session_map["sess-alpha"]) == 2
        assert "sess-beta" in session_map and len(session_map["sess-beta"]) == 2
        assert outbox.get_stats()["pending"] == 0


class TestContextAndSearch:
    def test_fetch_recent_context(self):
        def handler(request: httpx.Request):
            assert request.url.path == "/api/context/recent"
            assert request.url.params["project"] == "hermes"
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Recent goal: implement bridge"}]})

        transport = httpx.MockTransport(handler)
        client = UpstreamTransportClient(transport=transport)
        ctx = client.fetch_recent_context("hermes")
        assert ctx["content"][0]["text"] == "Recent goal: implement bridge"

    def test_search_memories(self):
        def handler(request: httpx.Request):
            assert request.url.path == "/api/search"
            assert request.url.params["query"] == "parser"
            assert request.url.params["project"] == "hermes"
            return httpx.Response(200, json={"results": [{"id": 1, "title": "Fixed parser bug"}]})

        transport = httpx.MockTransport(handler)
        client = UpstreamTransportClient(transport=transport)
        results = client.search_memories("parser", "hermes")
        assert results["results"][0]["title"] == "Fixed parser bug"

    def test_refresh_recent_context_cache(self, tmp_path: Path):
        from bridge.cache import LocalCache
        from bridge.upstream_transport import refresh_recent_context_cache

        cache = LocalCache(base_dir=tmp_path)

        def handler(request: httpx.Request):
            pid = request.url.params["project"]
            return httpx.Response(200, json={"project": pid, "content": [{"type": "text", "text": f"Context for {pid}"}]})

        transport = httpx.MockTransport(handler)
        client = UpstreamTransportClient(transport=transport)

        res = refresh_recent_context_cache(client, cache, ["proj-1", "proj-2"])
        assert res["refreshed"] == 2
        assert len(res["errors"]) == 0

        # Verify cached files on disk
        c1 = cache.get_cached_context("proj-1")
        assert c1["project"] == "proj-1"
        assert c1["content"][0]["text"] == "Context for proj-1"


class TestUpstreamSyncAllAndCli:
    def test_sync_all_multiple_batches(self, tmp_path: Path):
        outbox = Outbox(tmp_path / "outbox.db")
        for i in range(5):
            outbox.enqueue("dev-1", "proj-test", "agy", "sess-1", "prompt_submit", {"prompt": f"Test {i}"}, f"hook:{i}", event_id=f"e-{i}")

        calls = []

        def handler(request: httpx.Request):
            calls.append(request.url.path)
            return httpx.Response(200, json={"status": "ok", "sessionDbId": 1})

        transport = httpx.MockTransport(handler)
        client = UpstreamTransportClient(outbox=outbox, transport=transport)

        res = client.sync_all(batch_size=2, max_batches=10)
        assert res["total_batches"] == 3
        assert res["total_accepted"] == 5
        assert res["last_error"] is None
        assert outbox.get_stats()["pending"] == 0

    def test_format_context_prompt_upstream(self):
        from bridge.cache import format_context_prompt

        ctx = {
            "project": "secret-project",
            "content": [
                {"type": "text", "text": "Recent goal: launch rocket\nBlocker: weather"},
            ],
        }
        formatted = format_context_prompt(ctx)
        assert "[Hermes Memory: secret-project]" in formatted
        assert "Recent goal: launch rocket" in formatted
        assert "Blocker: weather" in formatted

    def test_cmd_sync_upstream_mode_no_token_required(self, tmp_path: Path, monkeypatch):
        from bridge.cache import LocalCache
        from bridge.cli import cmd_sync, build_parser

        base_dir = tmp_path / "bridge"
        base_dir.mkdir(parents=True, exist_ok=True)
        cache = LocalCache(base_dir=base_dir)
        outbox = Outbox(base_dir / "outbox.db")
        outbox.enqueue("dev-1", "proj-test", "agy", "sess-1", "prompt_submit", {"prompt": "Hello"}, "hook:1", event_id="e-sync-1")

        monkeypatch.setattr("bridge.cli.get_default_outbox", lambda: outbox)
        monkeypatch.setattr("bridge.cli.LocalCache", lambda: cache)
        monkeypatch.delenv("HERMES_DEVICE_TOKEN", raising=False)

        def handler(request: httpx.Request):
            if request.url.path == "/api/context/recent":
                return httpx.Response(200, json={"content": [{"type": "text", "text": "ctx"}]})
            return httpx.Response(200, json={"status": "ok", "sessionDbId": 1})

        # Mock UpstreamTransportClient transport
        orig_init = UpstreamTransportClient.__init__

        def mock_init(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            orig_init(self, *args, **kwargs)

        monkeypatch.setattr(UpstreamTransportClient, "__init__", mock_init)

        parser = build_parser()
        args = parser.parse_args(["sync", "--endpoint", "http://127.0.0.1:37778"])
        exit_code = cmd_sync(args)
        assert exit_code == 0
        assert outbox.get_stats()["pending"] == 0
