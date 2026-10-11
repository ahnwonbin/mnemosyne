"""Authenticated HTTP API. Start with python -m server.api."""
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from server.db import Store, Conflict
from server.models import (
    Batch, Event, Project, MilestonePlan, Completion, MemoryQuery,
    DiscoveryRunSubmission, CandidateReconcile
)
from server.retrieval import (
    get_project_context,
    search_memories,
    get_memory_timeline,
    get_memories,
    export_all_projects,
)
from server.observer import Observer, InferenceClient

@dataclass(frozen=True)
class Principal:
    token_hash: str
    role: Literal["ingest","read","admin"]
    device_id: str | None = None

def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()

def create_app(database, principals, inference_client: InferenceClient | None = None):
    if not principals:
        raise ValueError("configure at least one credential; there are no default tokens")
    store = Store(database)
    observer = Observer(store, inference_client)
    app = FastAPI(title="Hermes project memory", docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.observer = observer


    @app.middleware("http")
    async def size_limit(request, call_next):
        # Count received bytes, including chunked requests, before JSON parsing.
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 2 * 1024 * 1024:
                return JSONResponse({"detail":"request too large"}, status_code=413)
        body = bytes(content)
        request._body = body
        return await call_next(request)

    def auth(request: Request):
        scheme, _, token = request.headers.get("authorization","").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401,"authentication required")
        digest = token_hash(token)
        for principal in principals:
            if hmac.compare_digest(digest, principal.token_hash):
                return principal
        raise HTTPException(401,"invalid credential")

    def require(*roles):
        def check(p=Depends(auth)):
            if p.role not in roles:
                raise HTTPException(403,"insufficient scope")
            return p
        return check

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail":str(exc)},status_code=409)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail":"not found"},status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail":str(exc)},status_code=400)

    @app.get("/healthz")
    def healthz():
        return {"status":"ok","schema_version":1}

    @app.get("/api/v1/health")
    def health(p=Depends(require("read","admin"))):
        return store.health()

    @app.post("/api/v1/projects")
    def upsert(project: Project,p=Depends(require("admin"))):
        return store.upsert_project(project)

    @app.get("/api/v1/projects")
    def projects(snapshot: str | None=None,offset: int=0,limit: int=50,p=Depends(require("read","admin"))):
        return store.list_projects(snapshot,offset,limit)

    @app.post("/api/v1/events/batch")
    def ingest(batch: Batch,p=Depends(require("ingest","admin"))):
        if p.role=="ingest" and p.device_id != batch.device_id:
            raise HTTPException(403,"device credential mismatch")
        acks=[]
        for raw in batch.events:
            eid = raw.get("event_id")
            try:
                event=Event.model_validate(raw)
                if event.device_id != batch.device_id:
                    raise ValueError("batch device mismatch")
                if event.event_type=="milestone_acceptance" and p.role!="admin":
                    raise ValueError("milestone acceptance requires administrator")
                status=store.ingest(event)
                acks.append({"event_id":eid,"status":status})
            except (ValueError,ValidationError) as exc:
                # Do not echo payloads or validation input values back into logs.
                reason = "schema validation failed" if isinstance(exc,ValidationError) else str(exc)
                acks.append({"event_id":eid,"status":"rejected","reason":reason})
        store.device_contact(batch.device_id,batch.reported_backlog)
        return {"acknowledgments":acks}

    @app.post("/api/v1/projects/{project_id}/plans")
    def plan(project_id: str,body: MilestonePlan,p=Depends(require("admin"))):
        return store.set_plan(project_id,body)

    @app.put("/api/v1/projects/{project_id}/plans/{version}/milestones/{milestone_id}")
    def milestone(project_id: str,version: int,milestone_id: str,body: Completion,p=Depends(require("admin"))):
        return store.complete_milestone(project_id,version,milestone_id,body)

    @app.get("/api/v1/projects/export")

    def export_projects(p=Depends(require("read","admin"))):
        return export_all_projects(store)

    @app.get("/api/v1/projects/{project_id}/context")
    def context(project_id: str,max_tokens: int=800,p=Depends(require("read","admin"))):
        return get_project_context(store, project_id, max_tokens=max_tokens)

    @app.get("/api/v1/projects/{project_id}/search")
    def search(project_id: str,q: str,limit: int=5,type: str | None=None,max_tokens: int=500,p=Depends(require("read","admin"))):
        return search_memories(store, project_id, query=q, limit=limit, obs_type=type, max_tokens=max_tokens)

    @app.get("/api/v1/projects/{project_id}/timeline")
    def timeline(project_id: str,near_event_id: str | None=None,limit: int=10,max_tokens: int=1000,p=Depends(require("read","admin"))):
        return get_memory_timeline(store, project_id, near_event_id=near_event_id, limit=limit, max_tokens=max_tokens)

    @app.post("/api/v1/projects/{project_id}/memories")
    def memories(project_id: str,body: MemoryQuery,p=Depends(require("read","admin"))):
        return get_memories(store, project_id, memory_ids=body.memory_ids, max_tokens=body.max_tokens)

    @app.post("/api/v1/observer/tick")
    def observer_tick(batch_size: int=50,force: bool=True,p=Depends(require("admin"))):
        result = observer.tick(batch_size=batch_size, force=force)
        return {"result": result}

    @app.post("/api/v1/discovery/runs")
    def record_discovery(body: DiscoveryRunSubmission,p=Depends(require("ingest","admin"))):
        if p.role == "ingest" and p.device_id != body.device_id:
            raise HTTPException(403, "device credential mismatch")
        return store.record_discovery_run(
            run_id=body.run_id,
            device_id=body.device_id,
            roots=body.roots,
            failures=body.failures,
            candidates=body.candidates,
        )

    @app.get("/api/v1/discovery/candidates")
    def list_candidates(run_id: str | None=None,disposition: str | None=None,p=Depends(require("read","admin"))):
        return store.list_discovery_candidates(run_id=run_id, disposition=disposition)

    @app.put("/api/v1/discovery/candidates/{candidate_id}/reconcile")
    def reconcile(candidate_id: str,body: CandidateReconcile,p=Depends(require("admin"))):
        return store.reconcile_candidate(
            run_id=body.run_id,
            candidate_id=candidate_id,
            disposition=body.disposition,
            project_id=body.project_id,
        )

    return app

def app_from_environment():
    credentials = Path(os.environ["HERMES_MEMORY_CREDENTIALS"]).read_text(encoding="utf-8")
    principals = [Principal(**p) for p in json.loads(credentials)]
    return create_app(os.environ["HERMES_MEMORY_DB"],principals)

if __name__=="__main__":
    import uvicorn
    uvicorn.run(app_from_environment(),host="127.0.0.1",port=int(os.environ.get("HERMES_MEMORY_PORT","8787")))
