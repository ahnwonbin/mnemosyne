import uuid

import pytest
from fastapi.testclient import TestClient
from server.api import create_app, Principal, token_hash
from server.db import Store
from server.models import Project
from bridge.outbox import Outbox

@pytest.fixture
def cloud(tmp_path):
    app=create_app(tmp_path/"cloud.db",[
        Principal(token_hash("admin-fixture"),"admin"),
        Principal(token_hash("device-fixture"),"ingest","device-1"),
        Principal(token_hash("read-fixture"),"read")])
    with TestClient(app) as client:
        yield client,app.state.store

def headers(token="admin-fixture"):
    return {"Authorization":"Bearer "+token}

def event(tmp_path,session="session-1",kind="prompt_submit",payload=None,project="project-1"):
    box=Outbox(tmp_path/"local.db")
    return box.enqueue("device-1",project,"codex",session,kind,
                       payload or {"prompt":"planning database","_context":{"project_name":"Example"}},
                       "fixture:"+str(uuid.uuid4())).to_transmission_dict()

def post(client,events,token="device-fixture",device="device-1"):
    return client.post("/api/v1/events/batch",headers=headers(token),
                       json={"device_id":device,"events":events,"reported_backlog":len(events)})

def test_auth_and_device_boundaries(cloud,tmp_path):
    c,s=cloud
    assert c.get("/api/v1/projects").status_code==401
    assert c.get("/api/v1/projects",headers=headers("device-fixture")).status_code==403
    assert post(c,[],device="another-device").status_code==403
    assert post(c,[],token="read-fixture").status_code==403
    assert c.post("/api/v1/projects",headers=headers("device-fixture"),
                  json={"project_id":"x","name":"x"}).status_code==403

def test_ingestion_idempotency_conflict_and_sequence_gaps(cloud,tmp_path):
    c,s=cloud
    first=event(tmp_path)
    second=event(tmp_path)
    assert post(c,[second]).json()["acknowledgments"][0]["status"]=="accepted"
    assert s.health()["sessions"][0]["contiguous_sequence"]==0
    assert post(c,[first]).json()["acknowledgments"][0]["status"]=="accepted"
    assert s.health()["sessions"][0]["contiguous_sequence"]==2
    assert post(c,[first]).json()["acknowledgments"][0]["status"]=="duplicate"
    changed={**first,"project_id":"different"}
    assert post(c,[changed]).json()["acknowledgments"][0]["status"]=="rejected"
    colliding={**first,"event_id":str(uuid.uuid4())}
    assert post(c,[colliding]).json()["acknowledgments"][0]["status"]=="rejected"
    assert s.health()["events"]==2

def test_mixed_batch_preserves_valid_and_rejects_corrupt(cloud,tmp_path):
    c,s=cloud
    good=event(tmp_path)
    bad={**event(tmp_path),"payload_hash":"0"*64}
    acks=post(c,[good,bad]).json()["acknowledgments"]
    assert [a["status"] for a in acks]==["accepted","rejected"]
    assert s.health()["events"]==1

def test_snapshot_returns_exact_ids_despite_changes(cloud):
    c,s=cloud
    original={str(uuid.uuid4()) for _ in range(17)}
    for i,pid in enumerate(sorted(original)):
        s.upsert_project(Project(project_id=pid,name=f"Project {i}",status="archived" if i%2 else "complete"))
    page=c.get("/api/v1/projects?limit=3",headers=headers("read-fixture")).json()
    snapshot=page["snapshot"]
    # Concurrent additions and renames must not change this enumeration.
    s.upsert_project(Project(project_id="new",name="New"))
    s.upsert_project(Project(project_id=next(iter(original)),name="Renamed"))
    found=[p["project_id"] for p in page["projects"]]
    while page["next_offset"] is not None:
        page=c.get("/api/v1/projects",params={"snapshot":snapshot,"offset":page["next_offset"],"limit":3},
                   headers=headers("read-fixture")).json()
        assert page["total"]==17
        found.extend(p["project_id"] for p in page["projects"])
    assert len(found)==len(set(found))==17
    assert set(found)==original

def test_no_plan_and_no_inferred_completion(cloud,tmp_path):
    c,s=cloud
    e=event(tmp_path,kind="stop")
    post(c,[e])
    project=c.get("/api/v1/projects",headers=headers()).json()["projects"][0]
    assert project["progress_percent"] is None
    c.post("/api/v1/projects/project-1/plans",headers=headers(),
                json={"milestones":[{"milestone_id":"planning","title":"Plan","weight":10},
                                    {"milestone_id":"database","title":"Database","weight":90}]}).json()
    route="/api/v1/projects/project-1/plans/1/milestones/planning"
    # Generic session completion is not milestone verification.
    denied=c.put(route,headers=headers(),json={"status":"done","evidence_level":"verified","event_ids":[e["event_id"]]})
    assert denied.status_code==400
    result=c.put(route,headers=headers(),json={"status":"done","evidence_level":"reported","event_ids":[e["event_id"]]})
    assert result.json()["progress_percent"]==10
    c.post("/api/v1/projects/project-1/plans",headers=headers(),
           json={"milestones":[{"milestone_id":"new-scope","title":"Expanded","weight":1}]})
    assert c.put(route,headers=headers(),json={"status":"done","evidence_level":"reported","event_ids":[e["event_id"]]}).status_code==409

def test_verified_milestone_requires_admin_acceptance(cloud,tmp_path):
    c,s=cloud
    post(c,[event(tmp_path)])
    c.post("/api/v1/projects/project-1/plans",headers=headers(),
           json={"milestones":[{"milestone_id":"plan","title":"Plan","weight":1}]})
    e=event(tmp_path,kind="milestone_acceptance",payload={"milestone_id":"plan","plan_version":1})
    assert post(c,[e]).json()["acknowledgments"][0]["status"]=="rejected"
    assert post(c,[e],token="admin-fixture").json()["acknowledgments"][0]["status"]=="accepted"
    result=c.put("/api/v1/projects/project-1/plans/1/milestones/plan",headers=headers(),
                 json={"status":"done","evidence_level":"verified","event_ids":[e["event_id"]]})
    assert result.json()["progress_percent"]==100

def test_backup_restore_registry_and_events(cloud,tmp_path):
    c,s=cloud
    post(c,[event(tmp_path)])
    s.backup(tmp_path/"backup.db")
    restored=Store(tmp_path/"backup.db")
    assert restored.health()["events"]==1
    assert restored.list_projects()["projects"][0]["name"]=="Example"

def test_body_limit_counts_actual_bytes(cloud):
    c,s=cloud
    assert c.post("/api/v1/events/batch",headers=headers(),content=b"x"*(2*1024*1024+1)).status_code==413

def test_metadata_alias_collision_not_silent(cloud,tmp_path):
    c,s=cloud
    first=event(tmp_path,payload={"_context":{"project_name":"First","cwd":"c:/same"}})
    second=event(tmp_path,project="project-2",payload={"_context":{"project_name":"Second","cwd":"c:/same"}})
    assert post(c,[first]).json()["acknowledgments"][0]["status"]=="accepted"
    assert post(c,[second]).json()["acknowledgments"][0]["status"]=="rejected"
    assert s.health()["projects"]==1
