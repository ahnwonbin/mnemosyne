import json
import pytest
from fastapi.testclient import TestClient

from bridge.outbox import Outbox
from server.api import create_app, Principal, token_hash
from server.observer import Observer, InferenceClient
from server.retrieval import (
    get_project_context,
    search_memories,
    get_memory_timeline,
    get_memories,
    export_all_projects,
)
from server.mcp_server import MemoryMCPServer


@pytest.fixture
def app_and_store(tmp_path):
    db_path = tmp_path / "cloud_test.db"
    principals = [
        Principal(token_hash("admin-key"), "admin"),
        Principal(token_hash("read-key"), "read"),
        Principal(token_hash("device-key"), "ingest", "device-1"),
    ]
    app = create_app(db_path, principals)
    with TestClient(app) as client:
        yield client, app.state.store, app.state.observer


def enqueue_event(tmp_path, store, session="sess-1", kind="prompt_submit", payload=None, project_id="proj-1"):
    box = Outbox(tmp_path / "client.db")
    record = box.enqueue(
        "device-1",
        project_id,
        "codex",
        session,
        kind,
        payload or {"prompt": "Implement database", "_context": {"project_name": "HermesTest"}},
        "src:1",
    )
    from server.models import Event
    event_model = Event.model_validate(record.to_transmission_dict())
    store.ingest(event_model)
    return event_model


def test_observer_job_lifecycle_and_extraction(tmp_path, app_and_store):
    client, store, observer = app_and_store

    # Ingest prompt and tool use events
    enqueue_event(tmp_path, store, kind="prompt_submit", payload={"prompt": "Decide on SQLite WAL mode", "_context": {"project_name": "TestProj"}})
    enqueue_event(tmp_path, store, kind="tool_result", payload={"tool": "write_file", "success": True, "output": "file created"})
    enqueue_event(tmp_path, store, kind="tool_result", payload={"tool": "run_test", "success": False, "exit_code": 1, "error": "test failed"})

    assert store.health()["unprocessed_events"] == 3

    # Observer leases job
    lease = observer.lease_job(force=True)
    assert lease is not None
    project_id, lease_id = lease
    assert project_id == "proj-1"

    # Second concurrent lease attempt returns None while first is in-flight
    assert observer.lease_job(force=True) is None

    # Process project
    res = observer.process_project(project_id, lease_id)
    assert res["status"] == "success"
    assert res["events_processed"] == 3
    assert res["observations_created"] >= 3
    assert res["summary_version"] == 1

    # Verify events are now marked processed
    assert store.health()["unprocessed_events"] == 0

    # Verify observations in DB
    with store.connect() as c:
        obs = c.execute("SELECT * FROM observations WHERE project_id=?", (project_id,)).fetchall()
        assert len(obs) >= 3
        types = {r["type"] for r in obs}
        assert "decision" in types
        assert "bugfix" in types

        # Check summary version
        summary = c.execute("SELECT * FROM summary_versions WHERE project_id=?", (project_id,)).fetchone()
        assert summary["version"] == 1
        assert "SQLite WAL" in summary["body"] or "TestProj" in summary["body"]

        # Check job is now done
        job = c.execute("SELECT * FROM processing_jobs WHERE project_id=?", (project_id,)).fetchone()
        assert job["state"] == "done"
        assert job["attempts"] == 0


def test_observer_failure_preserves_events(tmp_path, app_and_store):
    client, store, _ = app_and_store

    class FailingExtractor(InferenceClient):
        def generate(self, *args, **kwargs):
            raise RuntimeError("Model timeout")

    failing_observer = Observer(store, FailingExtractor())
    enqueue_event(tmp_path, store, kind="prompt_submit", payload={"prompt": "Hello", "_context": {"project_name": "TestFail"}})

    lease = failing_observer.lease_job(force=True)
    assert lease is not None
    pid, lid = lease

    res = failing_observer.process_project(pid, lid)
    assert res["status"] == "failed"
    assert "Model timeout" in res["error"]

    # Events must NOT be lost or marked processed
    assert store.health()["unprocessed_events"] == 1

    with store.connect() as c:
        attempt = c.execute("SELECT * FROM processing_attempts WHERE project_id=?", (pid,)).fetchone()
        assert attempt["status"] == "failed"
        assert "Model timeout" in attempt["error"]

        job = c.execute("SELECT * FROM processing_jobs WHERE project_id=?", (pid,)).fetchone()
        assert job["state"] == "pending"
        assert job["attempts"] == 1


def test_retrieval_context_search_timeline_and_details(tmp_path, app_and_store):
    client, store, observer = app_and_store

    # Ingest multiple events
    enqueue_event(tmp_path, store, kind="prompt_submit", payload={"prompt": "Architecture: use SQLite FTS5 for fast search", "_context": {"project_name": "HermesBrain"}})
    enqueue_event(tmp_path, store, kind="tool_result", payload={"tool": "create_schema", "success": True, "output": "schema created"})

    # Process via observer
    observer.tick(force=True)

    # 1. Test get_project_context
    ctx = get_project_context(store, "proj-1", max_tokens=800)
    assert ctx["project_id"] == "proj-1"
    assert ctx["name"] == "HermesBrain"
    assert ctx["summary"] is not None
    assert ctx["summary"]["version"] == 1
    assert len(ctx["recent_observations"]) >= 1
    assert ctx["estimated_tokens"] <= 800

    # 2. Test search_memories (FTS5)
    search_res = search_memories(store, "proj-1", query="SQLite FTS5", limit=5)
    assert search_res["total_matches"] >= 1
    assert any("SQLite" in r["title"] or "FTS5" in r["snippet"] for r in search_res["results"])

    # Search with type filter
    filtered = search_memories(store, "proj-1", query="SQLite", obs_type="decision")
    for r in filtered["results"]:
        assert r["type"] == "decision"

    # 3. Test get_memory_timeline
    timeline_res = get_memory_timeline(store, "proj-1", limit=10)
    assert timeline_res["returned_count"] >= 2
    assert len(timeline_res["timeline"]) >= 2
    assert timeline_res["estimated_tokens"] <= 1000

    # 4. Test get_memories
    obs_id = search_res["results"][0]["id"]
    detail_res = get_memories(store, "proj-1", memory_ids=[obs_id])
    assert detail_res["total_returned"] == 1
    assert detail_res["memories"][0]["id"] == obs_id
    assert len(detail_res["memories"][0]["source_event_ids"]) >= 1

    # 5. Test export_all_projects
    exported = export_all_projects(store)
    assert exported["total_projects"] >= 1
    assert any(p["project_id"] == "proj-1" for p in exported["projects"])


def test_api_retrieval_and_observer_endpoints(tmp_path, app_and_store):
    client, store, _ = app_and_store
    auth_admin = {"Authorization": "Bearer admin-key"}
    auth_read = {"Authorization": "Bearer read-key"}

    # Ingest event and tick observer via API
    enqueue_event(tmp_path, store, kind="prompt_submit", payload={"prompt": "Building MCP server interface", "_context": {"project_name": "MCPApi"}})
    tick_resp = client.post("/api/v1/observer/tick", headers=auth_admin)
    assert tick_resp.status_code == 200
    assert tick_resp.json()["result"]["status"] == "success"

    # Context endpoint
    ctx_resp = client.get("/api/v1/projects/proj-1/context", headers=auth_read)
    assert ctx_resp.status_code == 200
    assert ctx_resp.json()["name"] == "MCPApi"

    # Search endpoint
    s_resp = client.get("/api/v1/projects/proj-1/search?q=MCP", headers=auth_read)
    assert s_resp.status_code == 200
    assert s_resp.json()["total_matches"] >= 1

    # Timeline endpoint
    t_resp = client.get("/api/v1/projects/proj-1/timeline", headers=auth_read)
    assert t_resp.status_code == 200
    assert len(t_resp.json()["timeline"]) >= 1

    # Memories detail endpoint
    obs_id = s_resp.json()["results"][0]["id"]
    m_resp = client.post("/api/v1/projects/proj-1/memories", headers=auth_read, json={"memory_ids": [obs_id]})
    assert m_resp.status_code == 200
    assert m_resp.json()["memories"][0]["id"] == obs_id

    # Export all endpoint
    exp_resp = client.get("/api/v1/projects/export", headers=auth_read)
    assert exp_resp.status_code == 200
    assert exp_resp.json()["total_projects"] >= 1


def test_mcp_server_protocol_and_tool_execution(tmp_path, app_and_store):
    client, store, observer = app_and_store
    enqueue_event(tmp_path, store, kind="prompt_submit", payload={"prompt": "MCP integration test", "_context": {"project_name": "MCPProject"}})
    observer.tick(force=True)

    mcp = MemoryMCPServer(store=store)

    # 1. Initialize
    init_res = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init_res["result"]["serverInfo"]["name"] == "hermes-project-memory"

    # 2. List tools
    tools_res = mcp.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tool_names = [t["name"] for t in tools_res["result"]["tools"]]
    assert "list_projects" in tool_names
    assert "project_context" in tool_names
    assert "search_memory" in tool_names
    assert "memory_timeline" in tool_names
    assert "get_memories" in tool_names
    assert "memory_health" in tool_names

    # 3. Call tool: list_projects
    call_list = mcp.handle_request({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "list_projects", "arguments": {}}
    })
    assert call_list["result"]["isError"] is False
    content_text = call_list["result"]["content"][0]["text"]
    parsed_list = json.loads(content_text)
    assert parsed_list["total"] >= 1

    # 4. Call tool: project_context
    call_ctx = mcp.handle_request({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "project_context", "arguments": {"project_id": "proj-1"}}
    })
    assert call_ctx["result"]["isError"] is False
    parsed_ctx = json.loads(call_ctx["result"]["content"][0]["text"])
    assert parsed_ctx["name"] == "MCPProject"

    # 5. Call tool: search_memory
    call_search = mcp.handle_request({
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "search_memory", "arguments": {"project_id": "proj-1", "query": "integration"}}
    })
    assert call_search["result"]["isError"] is False
    parsed_search = json.loads(call_search["result"]["content"][0]["text"])
    assert parsed_search["total_matches"] >= 1

    # 6. Call tool: memory_health
    call_health = mcp.handle_request({
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {"name": "memory_health", "arguments": {}}
    })
    assert call_health["result"]["isError"] is False
    parsed_health = json.loads(call_health["result"]["content"][0]["text"])
    assert parsed_health["projects"] >= 1


def test_token_budget_truncation_enforcement(tmp_path, app_and_store):
    client, store, observer = app_and_store
    pid = "budget-proj"
    for i in range(15):
        enqueue_event(
            tmp_path,
            store,
            kind="prompt_submit",
            payload={"prompt": f"Task item {i} with long description " * 10, "_context": {"project_name": "BudgetTest"}},
            project_id=pid,
        )
    observer.tick(force=True)

    # Context with small token budget must truncate
    tight_ctx = get_project_context(store, pid, max_tokens=100)
    assert tight_ctx["truncated"] is True
    assert len(tight_ctx["recent_observations"]) < 15

    # Memory detail with small token budget must truncate
    with store.connect() as c:
        all_obs = [r[0] for r in c.execute("SELECT id FROM observations WHERE project_id=?", (pid,)).fetchall()]
    tight_mem = get_memories(store, pid, memory_ids=all_obs, max_tokens=150)
    assert tight_mem["truncated"] is True
    assert tight_mem["total_returned"] < len(all_obs)


def test_fts5_thai_and_special_character_search(tmp_path, app_and_store):
    client, store, observer = app_and_store
    pid = "thai-proj"
    enqueue_event(
        tmp_path,
        store,
        kind="prompt_submit",
        payload={"prompt": "สถาปัตยกรรม ฐานข้อมูล SQLite ภาษาไทย และ testing (quotes: \"test\")", "_context": {"project_name": "ThaiTest"}},
        project_id=pid,
    )
    observer.tick(force=True)

    # Search in Thai
    thai_res = search_memories(store, pid, query="สถาปัตยกรรม")
    assert thai_res["total_matches"] >= 1

    # Search with punctuation that would break raw FTS5 syntax
    punct_res = search_memories(store, pid, query="SQLite: [test] AND \"quotes\"*?")
    assert punct_res["total_matches"] >= 1


def test_proxy_inference_client_with_mock():
    import httpx
    from server.observer import ProxyInferenceClient

    mock_resp = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "observations": [
                            {
                                "type": "feature",
                                "title": "Added mock feature",
                                "narrative": "Verified mock completion",
                                "evidence_level": "verified",
                                "source_event_ids": ["e-1"],
                            }
                        ],
                        "phase": "testing",
                        "next_action": "deploy",
                        "summary": "Mock summary test.",
                        "candidate_milestones": [],
                    })
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=mock_resp))
    client = ProxyInferenceClient(base_url="http://mock.proxy/v1", api_key="test-key")

    # Monkey-patch httpx.Client in generate
    import server.observer
    original_client = httpx.Client
    try:
        server.observer.httpx.Client = lambda **kwargs: original_client(transport=transport)
        res = client.generate("MockProject", None, [], [{"event_id": "e-1", "event_type": "prompt_submit", "occurred_at": "2026-09-28T00:00:00+00:00", "payload": {}}])
        assert res["phase"] == "testing"
        assert res["observations"][0]["type"] == "feature"
        assert res["input_tokens"] == 100
    finally:
        server.observer.httpx.Client = original_client
