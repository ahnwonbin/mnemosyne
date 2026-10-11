"""Transactional registry, ingestion, and evidence store."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from server.models import Event

def now():
    return datetime.now(timezone.utc).isoformat()

def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

class Conflict(ValueError):
    pass

class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            migration = Path(__file__).parent / "migrations" / "001_initial.sql"
            c.executescript(migration.read_text(encoding="utf-8"))
            c.execute("INSERT OR IGNORE INTO schema_migrations VALUES (1,?)", (now(),))

    @contextmanager
    def connect(self, write=False):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=10000")
        try:
            if write:
                c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    @staticmethod
    def _revision(c):
        c.execute("UPDATE metadata SET value=value+1 WHERE key='registry_revision'")

    @staticmethod
    def _audit(c, action, entity_id, detail):
        c.execute("INSERT INTO audit_records(action,entity_id,at,detail) VALUES (?,?,?,?)",
                  (action, entity_id, now(), encode(detail)))

    def upsert_project(self, project):
        with self.connect(write=True) as c:
            existing = c.execute("SELECT * FROM projects WHERE project_id=?", (project.project_id,)).fetchone()
            if existing and existing["name"] == project.name and existing["status"] == project.status:
                return dict(existing)
            c.execute("""INSERT INTO projects(project_id,name,status,created_at,updated_at) VALUES (?,?,?,?,?)
                ON CONFLICT(project_id) DO UPDATE SET name=excluded.name,status=excluded.status,updated_at=excluded.updated_at""",
                (project.project_id, project.name, project.status, now(), now()))
            self._revision(c)
            self._audit(c, "project.upsert", project.project_id, project.model_dump())
            return dict(c.execute("SELECT * FROM projects WHERE project_id=?", (project.project_id,)).fetchone())

    def ingest(self, event: Event):
        payload_json = encode(event.payload)
        if len(payload_json.encode()) > 512 * 1024:
            raise ValueError("payload size limit")
        if hashlib.sha256(payload_json.encode()).hexdigest() != event.payload_hash:
            raise ValueError("payload hash mismatch")
        envelope_hash = hashlib.sha256(encode(event.model_dump()).encode()).hexdigest()
        with self.connect(write=True) as c:
            old = c.execute("SELECT envelope_hash FROM events WHERE event_id=?", (event.event_id,)).fetchone()
            if old:
                if old[0] != envelope_hash:
                    raise Conflict("event ID reused with different content")
                return "duplicate"
            # A sequence position has exactly one event, even after response loss.
            if c.execute("SELECT 1 FROM events WHERE device_id=? AND platform=? AND native_session_id=? AND session_sequence=?",
                         (event.device_id,event.platform,event.native_session_id,event.session_sequence)).fetchone():
                raise Conflict("session sequence already assigned")
            context = event.payload.get("_context", {})
            context = context if isinstance(context, dict) else {}
            if not c.execute("SELECT 1 FROM projects WHERE project_id=?", (event.project_id,)).fetchone():
                name = context.get("project_name")
                if not isinstance(name, str) or not name.strip() or len(name) > 200:
                    raise ValueError("unknown project requires project metadata")
                c.execute("INSERT INTO projects(project_id,name,created_at,updated_at) VALUES (?,?,?,?)",
                          (event.project_id, name, now(), now()))
                self._revision(c)
            path = context.get("canonical_path") or context.get("worktree_root") or context.get("cwd")
            if isinstance(path, str) and path:
                prior = c.execute("SELECT project_id FROM project_aliases WHERE device_id=? AND path=?", (event.device_id,path)).fetchone()
                if prior and prior[0] != event.project_id:
                    raise Conflict("path alias belongs to another project; reconcile explicitly")
                c.execute("INSERT OR IGNORE INTO project_aliases VALUES (?,?,?)", (event.device_id,path,event.project_id))
            c.execute("""INSERT INTO events(event_id,project_id,device_id,platform,native_session_id,
                session_sequence,event_type,occurred_at,received_at,payload,payload_hash,envelope_hash,
                source_identity,adapter_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event.event_id,event.project_id,event.device_id,event.platform,event.native_session_id,event.session_sequence,
                 event.event_type,event.occurred_at,now(),payload_json,event.payload_hash,envelope_hash,event.source_identity,
                 event.capture_adapter_version))
            key = (event.device_id,event.platform,event.native_session_id)
            c.execute("INSERT OR IGNORE INTO sessions(device_id,platform,native_session_id) VALUES (?,?,?)", key)
            contiguous = c.execute("SELECT contiguous_sequence FROM sessions WHERE device_id=? AND platform=? AND native_session_id=?", key).fetchone()[0]
            seqs = c.execute("SELECT session_sequence FROM events WHERE device_id=? AND platform=? AND native_session_id=? AND session_sequence>? ORDER BY session_sequence", (*key,contiguous))
            for row in seqs:
                if row[0] != contiguous + 1:
                    break
                contiguous = row[0]
            c.execute("UPDATE sessions SET contiguous_sequence=? WHERE device_id=? AND platform=? AND native_session_id=?", (contiguous,*key))
            c.execute("UPDATE projects SET updated_at=? WHERE project_id=?", (now(),event.project_id))
            self._revision(c)
            due = now() if event.event_type in {"stop","session_end"} else (datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat()
            c.execute("""INSERT INTO processing_jobs(project_id,due_at) VALUES (?,?)
                ON CONFLICT(project_id) DO UPDATE SET
                due_at=MIN(processing_jobs.due_at,excluded.due_at),
                state=CASE WHEN processing_jobs.state IN ('done','failed') THEN 'pending' ELSE processing_jobs.state END,
                attempts=CASE WHEN processing_jobs.state IN ('done','failed') THEN 0 ELSE processing_jobs.attempts END""", (event.project_id,due))
            return "accepted"

    def device_contact(self, device_id, backlog):
        with self.connect(write=True) as c:
            c.execute("""INSERT INTO devices(device_id,last_contact,reported_backlog) VALUES (?,?,?)
                ON CONFLICT(device_id) DO UPDATE SET last_contact=excluded.last_contact,reported_backlog=excluded.reported_backlog""",
                (device_id,now(),backlog))

    @staticmethod
    def _progress(c, project):
        if not project["plan_version"]:
            return None
        row = c.execute("""SELECT SUM(weight),SUM(CASE WHEN status='done' THEN weight ELSE 0 END)
            FROM milestones WHERE project_id=? AND plan_version=?""", (project["project_id"],project["plan_version"])).fetchone()
        return round(100 * row[1] / row[0], 2) if row[0] else None

    def list_projects(self, snapshot=None, offset=0, limit=50):
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("invalid pagination")
        with self.connect(write=True) as c:
            if snapshot:
                row = c.execute("SELECT * FROM registry_snapshots WHERE id=?", (snapshot,)).fetchone()
                if not row:
                    raise KeyError("snapshot not found")
                if datetime.fromisoformat(row["created_at"]) < datetime.now(timezone.utc)-timedelta(days=1):
                    raise ValueError("snapshot expired; restart listing")
                body = json.loads(row["body"])
                revision = row["revision"]
                at = row["created_at"]
            else:
                snapshot, at = str(uuid.uuid4()), now()
                revision = c.execute("SELECT value FROM metadata WHERE key='registry_revision'").fetchone()[0]
                projects = []
                for row in c.execute("SELECT * FROM projects ORDER BY name COLLATE NOCASE,project_id").fetchall():
                    project = dict(row)
                    project["progress_percent"] = self._progress(c,project)
                    project["unprocessed_events"] = c.execute("SELECT COUNT(*) FROM events WHERE project_id=? AND processed=0", (project["project_id"],)).fetchone()[0]
                    projects.append(project)
                body = {"projects":projects, "devices":[dict(r) for r in c.execute("SELECT * FROM devices ORDER BY device_id")],
                        "discovery_runs":[dict(r) for r in c.execute("SELECT run_id,device_id,scanned_at,roots,failures FROM discovery_runs ORDER BY scanned_at DESC LIMIT 20")],
                        "unresolved_candidates":c.execute("SELECT COUNT(*) FROM discovery_candidates WHERE disposition='unresolved'").fetchone()[0]}
                c.execute("INSERT INTO registry_snapshots VALUES (?,?,?,?)", (snapshot,at,revision,encode(body)))
            rows = body["projects"][offset:offset+limit]
            total = len(body["projects"])
            return {"snapshot":snapshot,"snapshot_at":at,"revision":revision,"total":total,"returned":len(rows),
                    "next_offset":offset+len(rows) if offset+len(rows)<total else None,"projects":rows,
                    "filters":{"scope":"all","include_archived":True},
                    "coverage":{k:v for k,v in body.items() if k!="projects"},
                    "completeness":"All registered projects at this snapshot; discovery/unsent offline projects may be missing."}

    def set_plan(self, project_id, plan):
        ids = [m.milestone_id for m in plan.milestones]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate milestone ID")
        with self.connect(write=True) as c:
            row = c.execute("SELECT plan_version FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if not row:
                raise KeyError("project not found")
            version = row[0]+1
            c.execute("INSERT INTO milestone_plans VALUES (?,?,?)", (project_id,version,now()))
            c.executemany("INSERT INTO milestones(project_id,plan_version,milestone_id,title,weight,status) VALUES (?,?,?,?,?,'todo')",
                          [(project_id,version,m.milestone_id,m.title,m.weight) for m in plan.milestones])
            c.execute("UPDATE projects SET plan_version=?,updated_at=? WHERE project_id=?", (version,now(),project_id))
            self._revision(c)
            self._audit(c,"plan.create",project_id,{"version":version})
            return {"plan_version":version,"progress_percent":0}

    def complete_milestone(self, project_id, version, milestone_id, completion):
        with self.connect(write=True) as c:
            project = c.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if not project:
                raise KeyError("project not found")
            if version != project["plan_version"]:
                raise Conflict("plan version changed")
            if not c.execute("SELECT 1 FROM milestones WHERE project_id=? AND plan_version=? AND milestone_id=?", (project_id,version,milestone_id)).fetchone():
                raise KeyError("milestone not found")
            evidence = []
            for eid in completion.event_ids:
                row = c.execute("SELECT project_id,event_type,payload FROM events WHERE event_id=?", (eid,)).fetchone()
                if not row or row["project_id"] != project_id:
                    raise ValueError("evidence must belong to this project")
                evidence.append(row)
            if completion.evidence_level == "verified":
                # A passing tool alone is not proof of arbitrary task completion.
                # Administrative acceptance must be explicit and identify this milestone.
                if not any(r["event_type"]=="milestone_acceptance" and json.loads(r["payload"]).get("milestone_id")==milestone_id and
                           json.loads(r["payload"]).get("plan_version")==version for r in evidence):
                    raise ValueError("verified completion requires explicit milestone acceptance evidence")
            c.execute("UPDATE milestones SET status=?,evidence_level=? WHERE project_id=? AND plan_version=? AND milestone_id=?",
                      (completion.status,completion.evidence_level,project_id,version,milestone_id))
            c.execute("DELETE FROM milestone_evidence WHERE project_id=? AND plan_version=? AND milestone_id=?", (project_id,version,milestone_id))
            c.executemany("INSERT INTO milestone_evidence VALUES (?,?,?,?)", [(project_id,version,milestone_id,eid) for eid in set(completion.event_ids)])
            self._revision(c)
            self._audit(c,"milestone.change",project_id,{"version":version,"milestone_id":milestone_id,**completion.model_dump()})
            return {"progress_percent":self._progress(c,project),"plan_version":version}

    def health(self):
        with self.connect() as c:
            return {"projects":c.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
                    "events":c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                    "unprocessed_events":c.execute("SELECT COUNT(*) FROM events WHERE processed=0").fetchone()[0],
                    "jobs":[dict(r) for r in c.execute("SELECT project_id,state,attempts,last_error FROM processing_jobs")],
                    "sessions":[dict(r) for r in c.execute("SELECT * FROM sessions")],
                    "devices":[dict(r) for r in c.execute("SELECT * FROM devices")],
                    "deployment_verified":False}

    def backup(self, destination):
        target = Path(destination).resolve()
        if target == self.path:
            raise ValueError("backup must use a separate path")
        target.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as source:
            dest = sqlite3.connect(target)
            try:
                source.backup(dest)
                if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("backup integrity check failed")
            finally:
                dest.close()

    def record_discovery_run(self, run_id: str, device_id: str, roots: list[str], failures: list[dict], candidates: list[dict]):
        with self.connect(write=True) as c:
            c.execute(
                "INSERT INTO discovery_runs(run_id,device_id,scanned_at,roots,failures) VALUES (?,?,?,?,?)",
                (run_id, device_id, now(), encode(roots), encode(failures))
            )
            for cand in candidates:
                cand_id = cand.get("candidate_id") or str(uuid.uuid4())
                disp = cand.get("disposition", "unresolved")
                pid = cand.get("project_id")
                c.execute(
                    "INSERT INTO discovery_candidates(run_id,candidate_id,body,disposition,project_id) VALUES (?,?,?,?,?)",
                    (run_id, cand_id, encode(cand), disp, pid)
                )
            self._revision(c)
            self._audit(c, "discovery.run", run_id, {"device_id": device_id, "candidates": len(candidates), "failures": len(failures)})
            return {"run_id": run_id, "candidates_recorded": len(candidates)}

    def reconcile_candidate(self, run_id: str, candidate_id: str, disposition: str, project_id: str | None = None):
        if disposition not in ("registered", "merged", "excluded", "unresolved"):
            raise ValueError(f"invalid disposition: {disposition}")
        with self.connect(write=True) as c:
            row = c.execute("SELECT * FROM discovery_candidates WHERE run_id=? AND candidate_id=?", (run_id, candidate_id)).fetchone()
            if not row:
                raise KeyError("candidate not found")
            body = json.loads(row["body"])
            if disposition == "registered" and not project_id:
                project_id = body.get("project_id") or str(uuid.uuid4())
                name = body.get("name") or "Discovered Project"
                if not c.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
                    c.execute("INSERT INTO projects(project_id,name,status,created_at,updated_at) VALUES (?,?,'active',?,?)",
                              (project_id, name, now(), now()))
            c.execute("UPDATE discovery_candidates SET disposition=?, project_id=? WHERE run_id=? AND candidate_id=?",
                      (disposition, project_id, run_id, candidate_id))
            self._revision(c)
            self._audit(c, "discovery.reconcile", candidate_id, {"run_id": run_id, "disposition": disposition, "project_id": project_id})
            return {"run_id": run_id, "candidate_id": candidate_id, "disposition": disposition, "project_id": project_id}

    def list_discovery_candidates(self, run_id: str | None = None, disposition: str | None = None):
        with self.connect() as c:
            query = "SELECT * FROM discovery_candidates WHERE 1=1"
            params = []
            if run_id:
                query += " AND run_id=?"
                params.append(run_id)
            if disposition:
                query += " AND disposition=?"
                params.append(disposition)
            query += " ORDER BY run_id DESC"
            rows = c.execute(query, params).fetchall()
            return [
                {
                    "run_id": r["run_id"],
                    "candidate_id": r["candidate_id"],
                    "body": json.loads(r["body"]),
                    "disposition": r["disposition"],
                    "project_id": r["project_id"],
                }
                for r in rows
            ]
