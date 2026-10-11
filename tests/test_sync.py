"""Unit tests for cloud sync client with mock HTTP transport."""

from pathlib import Path
import httpx
from bridge.outbox import Outbox
from bridge.sync import SyncClient


def test_sync_batch_success(tmp_path: Path):
    db_file = tmp_path / "outbox.db"
    outbox = Outbox(db_file)
    evt = outbox.enqueue(
        device_id="dev-1",
        project_id="proj-1",
        platform="claude_code",
        native_session_id="s1",
        event_type="prompt_submit",
        raw_payload={"prompt": "hello"},
        source_identity="hook:test",
    )

    def mock_handler(request: httpx.Request) -> httpx.Response:
        data = request.read().decode("utf-8")
        assert evt.event_id in data
        assert request.headers["Authorization"] == "Bearer test-secret-token"
        return httpx.Response(
            200,
            json={
                "success": True,
                "processed": 1,
                "acknowledgments": [{"event_id": evt.event_id, "status": "accepted"}],
            },
        )

    client = SyncClient(
        endpoint_url="https://testserver",
        auth_token="test-secret-token",
        outbox=outbox,
        device_id="dev-1",
    )

    # Monkeypatch transport in httpx.Client
    real_client_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(mock_handler)
        real_client_init(self, *args, **kwargs)

    try:
        httpx.Client.__init__ = patched_init
        res = client.sync_batch(batch_size=10)
        assert res.success is True
        assert res.accepted == 1
        assert outbox.get_stats()["sent"] == 1
    finally:
        httpx.Client.__init__ = real_client_init


def test_sync_batch_network_failure(tmp_path: Path):
    db_file = tmp_path / "outbox.db"
    outbox = Outbox(db_file)
    outbox.enqueue(
        device_id="dev-1",
        project_id="proj-1",
        platform="codex",
        native_session_id="s1",
        event_type="prompt_submit",
        raw_payload={"prompt": "hello"},
        source_identity="hook:test",
    )

    def failing_handler(request: httpx.Request):
        raise httpx.ConnectError("Connection refused")

    client = SyncClient(
        endpoint_url="https://offline-server",
        auth_token="token",
        outbox=outbox,
        device_id="dev-1",
    )

    real_client_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(failing_handler)
        real_client_init(self, *args, **kwargs)

    try:
        httpx.Client.__init__ = patched_init
        res = client.sync_batch(batch_size=10)
        assert res.success is False
        assert "ConnectError" in res.error
        # Event must be released back to pending
        stats = outbox.get_stats()
        assert stats["pending"] == 1
        assert stats["in_flight"] == 0
    finally:
        httpx.Client.__init__ = real_client_init
