import json
import httpx
import pytest

from bridge.outbox import Outbox
from bridge.upstream_transport import UpstreamTransportClient


def enqueue(box, eid, session="s", kind="tool_result"):
    return box.enqueue("device", "project", "codex", session, kind,
                       {"tool": "test", "output": "ok"}, "fixture", event_id=eid)


@pytest.mark.parametrize("code,body", [(404, {}), (405, {}), (500, {}),
                                      (200, {}), (200, []), (200, [{"tool_use_id": "unrelated"}])])
@pytest.mark.parametrize("duplicate", [False, True])
def test_unverified_observation_stays_pending(tmp_path, code, body, duplicate):
    box = Outbox(tmp_path / "queue.db")
    enqueue(box, "event")
    def handler(req):
        if req.url.path == "/api/tool-uses/batch":
            return httpx.Response(code, json=body)
        return httpx.Response(200, json={"status": "skipped", "reason": "duplicate"}
                              if duplicate else {"status": "queued"})
    result = UpstreamTransportClient(outbox=box, transport=httpx.MockTransport(handler)).sync_batch()
    assert not result.success
    assert box.get_stats()["pending"] == 1
    assert box.get_stats()["sent"] == 0


def test_lost_response_retry_reads_receipt_without_reposting(tmp_path):
    box = Outbox(tmp_path / "queue.db")
    enqueue(box, "event")
    stored = set()
    uploads = []
    def handler(req):
        if req.url.path == "/api/tool-uses/batch":
            return httpx.Response(200, json=[{"tool_use_id": eid} for eid in stored])
        eid = json.loads(req.content)["tool_use_id"]
        stored.add(eid)
        uploads.append(eid)
        raise httpx.ReadError("response lost", request=req)
    client = UpstreamTransportClient(outbox=box, transport=httpx.MockTransport(handler))
    assert not client.sync_batch().success
    box.retry("event")
    result = client.sync_batch()
    assert result.success and result.duplicate == 1
    assert uploads == ["event"]
    assert box.get_stats()["sent"] == 1


@pytest.mark.parametrize("session_id", [None, True, 0, -1, "1"])
def test_init_needs_positive_integer_database_id(tmp_path, session_id):
    box = Outbox(tmp_path / "queue.db")
    enqueue(box, "event", kind="prompt_submit")
    client = UpstreamTransportClient(outbox=box, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"status": "initialized", "sessionDbId": session_id})))
    assert not client.sync_batch().success
    assert box.get_stats()["pending"] == 1


def test_failed_session_waits_while_other_session_progresses(tmp_path):
    box = Outbox(tmp_path / "queue.db")
    enqueue(box, "first", kind="prompt_submit")
    enqueue(box, "later")
    enqueue(box, "other", session="other")
    uploaded = []
    def handler(req):
        if req.url.path == "/api/sessions/init":
            return httpx.Response(503)
        if req.url.path == "/api/tool-uses/batch":
            return httpx.Response(200, json=[{"tool_use_id": "other"}])
        eid = json.loads(req.content)["tool_use_id"]
        uploaded.append(eid)
        return httpx.Response(200, json={"status": "queued"})
    result = UpstreamTransportClient(outbox=box, transport=httpx.MockTransport(handler)).sync_batch()
    assert not result.success
    assert result.accepted == 1
    assert uploaded == ["other"]
    # The later event cannot jump past the first event's backoff on the next run.
    box.retry("later")
    assert box.get_pending_batch() == []


def test_acknowledgment_saved_before_next_network_request(tmp_path):
    box = Outbox(tmp_path / "queue.db")
    enqueue(box, "one")
    enqueue(box, "two")
    def handler(req):
        payload = json.loads(req.content)
        if req.url.path == "/api/tool-uses/batch":
            return httpx.Response(200, json=[{"tool_use_id": eid} for eid in payload["ids"]])
        if payload["tool_use_id"] == "two":
            assert box.get_stats()["sent"] == 1
            raise httpx.ConnectError("offline", request=req)
        return httpx.Response(200, json={"status": "queued"})
    result = UpstreamTransportClient(outbox=box, transport=httpx.MockTransport(handler)).sync_batch()
    assert not result.success and result.accepted == 1
    assert box.get_stats()["sent"] == 1
    assert box.get_stats()["pending"] == 1
