"""Cloud observer and memory background processor.
Extracts structured observations, updates versioned project summaries,
links milestone evidence, and handles durable background jobs.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
import json
import re
from typing import Any, Dict, List, Optional, Tuple
import uuid
from typing import Literal
from pydantic import BaseModel, Field

import httpx

from server.db import Store, encode, now, Conflict


class ObservationOutput(BaseModel):
    type: Literal["decision", "discovery", "change", "bugfix", "feature", "refactor"]
    title: str
    narrative: str
    evidence_level: Literal["reported", "verified"] = "reported"
    source_event_ids: List[str] = Field(min_length=1)


class MilestoneOutput(BaseModel):
    milestone_id: str
    status: Literal["todo", "in_progress", "done"]
    evidence_level: Literal["reported", "verified"] = "reported"
    event_ids: List[str] = Field(min_length=1)


class ExtractionOutput(BaseModel):
    observations: List[ObservationOutput]
    summary: str
    candidate_milestones: List[MilestoneOutput] = Field(default_factory=list)
    phase: Optional[str] = None
    next_action: Optional[str] = None
    model: str = "observer-v1"
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class InferenceClient:
    """Interface for model inference."""
    def generate(
        self,
        project_name: str,
        current_summary: Optional[str],
        current_plan: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        raise NotImplementedError


class DeterministicExtractor(InferenceClient):
    """Rule-based extractor for local/offline operation and tests.
    Identifies decisions, changes, discoveries, and bugfixes from native event streams.
    """
    def generate(
        self,
        project_name: str,
        current_summary: Optional[str],
        current_plan: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        observations = []
        phase = None
        next_action = None
        candidate_milestones = []

        for e in events:
            eid = e["event_id"]
            kind = e["event_type"]
            payload = json.loads(e["payload"]) if isinstance(e["payload"], str) else e.get("payload", {})

            if kind in ("prompt_submit", "user_prompt"):
                prompt = payload.get("prompt", "")
                if any(w in prompt.lower() for w in ["decide", "use ", "architecture", "choice", "switch"]):
                    observations.append({
                        "type": "decision",
                        "title": f"Decision: {prompt[:60]}",
                        "narrative": prompt,
                        "evidence_level": "reported",
                        "source_event_ids": [eid],
                    })
                else:
                    observations.append({
                        "type": "discovery",
                        "title": f"Task: {prompt[:60]}",
                        "narrative": prompt,
                        "evidence_level": "reported",
                        "source_event_ids": [eid],
                    })
                if not phase:
                    phase = "active_development"

            elif kind in ("tool_call", "tool_result", "post_tool_use"):
                tool = payload.get("tool") or payload.get("tool_name", "tool")
                success = payload.get("success")
                exit_code = payload.get("exit_code")
                if kind == "tool_call" or (success is None and exit_code is None):
                    observations.append({
                        "type": "discovery", "title": f"Unconfirmed action: {tool}",
                        "narrative": f"Tool {tool} was recorded without a confirmed outcome.",
                        "evidence_level": "reported", "source_event_ids": [eid],
                    })
                elif success is False or (exit_code is not None and exit_code != 0):
                    observations.append({
                        "type": "bugfix",
                        "title": f"Tool failure in {tool}",
                        "narrative": f"Tool {tool} failed with exit code {payload.get('exit_code')}: {encode(payload.get('error') or payload.get('output'))[:200]}",
                        "evidence_level": "verified",
                        "source_event_ids": [eid],
                    })
                else:
                    obs_type = "change"
                    if any(t in tool.lower() for t in ["edit", "replace", "write"]):
                        obs_type = "change"
                    elif any(t in tool.lower() for t in ["find", "search", "read", "view"]):
                        obs_type = "discovery"
                    observations.append({
                        "type": obs_type,
                        "title": f"Action: {tool}",
                        "narrative": f"Executed {tool} successfully.",
                        "evidence_level": "verified",
                        "source_event_ids": [eid],
                    })

            elif kind in ("assistant_response", "post_invocation"):
                resp = payload.get("response", "")
                if "next:" in resp.lower() or "next step" in resp.lower():
                    # Extract tentative next action
                    match = re.search(r"(?:next(?:\s+action|\s+step)?[:\s]+)([^.\n]+)", resp, re.IGNORECASE)
                    if match:
                        next_action = match.group(1).strip()

            elif kind == "milestone_acceptance":
                mid = payload.get("milestone_id")
                if mid:
                    candidate_milestones.append({
                        "milestone_id": mid,
                        "status": "done",
                        "evidence_level": "verified",
                        "event_ids": [eid],
                    })

        # Synthesize concise summary
        total_obs = len(observations)
        prev = f"Previous summary: {current_summary[-600:]}. " if current_summary else ""
        summary_text = (
            f"{prev}Project '{project_name}' processed {len(events)} events with {total_obs} observations. "
            f"Phase: {phase or 'steady_state'}."
        )
        if next_action:
            summary_text += f" Next action: {next_action}."

        return {
            "observations": observations,
            "phase": phase,
            "next_action": next_action,
            "summary": summary_text,
            "candidate_milestones": candidate_milestones,
            "model": "deterministic-extractor-v1",
            "input_tokens": sum(len(encode(e)) for e in events) // 4,
            "output_tokens": len(summary_text) // 4,
        }


class ProxyInferenceClient(InferenceClient):
    """Calls an OpenAI-compatible proxy (e.g., local Gemini CLI proxy at 127.0.0.1:8317)."""
    def __init__(self, base_url: str, api_key: str, model: str = "gemini-3.8-flash-high", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def generate(
        self,
        project_name: str,
        current_summary: Optional[str],
        current_plan: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        system_prompt = (
            "You are the Hermes Cloud Observer. Analyze new project events and produce a structured JSON object with:\n"
            "- 'observations': list of objects with 'type' (one of: decision, discovery, change, bugfix, feature, refactor), "
            "'title' (short string), 'narrative' (explanation), 'evidence_level' ('reported' or 'verified'), "
            "'source_event_ids' (list of event IDs that support this observation).\n"
            "- 'phase': current development phase (e.g. planning, active_development, testing, paused, completed).\n"
            "- 'next_action': clear single next action if identified.\n"
            "- 'summary': updated compact project summary (under 200 words).\n"
            "- 'candidate_milestones': list of milestone objects with 'milestone_id', 'status' ('todo'|'in_progress'|'done'), 'evidence_level', 'event_ids'.\n"
            "Return ONLY valid JSON matching this structure."
        )

        user_content = {
            "project_name": project_name,
            "current_summary": current_summary,
            "milestone_plan": current_plan,
            "new_events": [
                {
                    "event_id": e["event_id"],
                    "event_type": e["event_type"],
                    "occurred_at": e["occurred_at"],
                    "payload": json.loads(e["payload"]) if isinstance(e["payload"], str) else e.get("payload", {})
                }
                for e in events
            ]
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_content, ensure_ascii=False)}
            ],
            "temperature": 0.2,
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
            if resp.status_code != 200:
                raise RuntimeError(f"Proxy returned HTTP {resp.status_code}: {resp.text}")
            data = resp.json()
            raw_text = data["choices"][0]["message"]["content"]
            # Clean markdown fences if present
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.MULTILINE)
            raw_text = re.sub(r"```$", "", raw_text.strip(), flags=re.MULTILINE)
            parsed = json.loads(raw_text)
            parsed["model"] = self.model
            parsed["input_tokens"] = data.get("usage", {}).get("prompt_tokens", 0)
            parsed["output_tokens"] = data.get("usage", {}).get("completion_tokens", 0)
            return parsed


class Observer:
    """Manages job leasing, observation generation, and project progress updates."""
    def __init__(self, store: Store, inference_client: Optional[InferenceClient] = None):
        self.store = store
        self.inference_client = inference_client or DeterministicExtractor()

    def lease_job(self, lease_duration_seconds: int = 60, max_attempts: int = 5, force: bool = False) -> Optional[Tuple[str, str]]:
        """Atomically lease one pending or expired in-flight processing job."""
        current_time = now()
        with self.store.connect(write=True) as c:
            if force:
                where_clause = """WHERE (
                       (state = 'pending')
                       OR
                       (state = 'in_flight' AND lease_until < ?)
                   ) AND attempts < ?"""
                params = (current_time, max_attempts)
            else:
                where_clause = """WHERE (
                       (state = 'pending' AND due_at <= ?)
                       OR
                       (state = 'in_flight' AND lease_until < ?)
                   ) AND attempts < ?"""
                params = (current_time, current_time, max_attempts)

            row = c.execute(
                f"""SELECT project_id, attempts FROM processing_jobs
                   {where_clause}
                   ORDER BY due_at ASC
                   LIMIT 1""",
                params
            ).fetchone()

            if not row:
                return None

            project_id = row["project_id"]
            lease_id = str(uuid.uuid4())
            lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_duration_seconds)).isoformat()

            c.execute(
                """UPDATE processing_jobs
                   SET state='in_flight', lease_id=?, lease_until=?, attempts=attempts+1
                   WHERE project_id=?""",
                (lease_id, lease_until, project_id)
            )
            return project_id, lease_id

    def process_project(self, project_id: str, lease_id: str, batch_size: int = 50) -> Dict[str, Any]:
        """Process unhandled events for a leased project."""
        if not 1 <= batch_size <= 100:
            raise ValueError("batch_size must be between 1 and 100")
        # 1. Fetch project info and events under read check
        with self.store.connect() as c:
            job = c.execute("SELECT * FROM processing_jobs WHERE project_id=?", (project_id,)).fetchone()
            if not job or job["lease_id"] != lease_id or job["state"] != "in_flight" or job["lease_until"] <= now():
                raise Conflict("invalid or expired lease ID")

            project = c.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if not project:
                raise KeyError(f"project {project_id} not found")

            # Fetch unhandled events
            events = [
                dict(r) for r in c.execute(
                    "SELECT * FROM events WHERE project_id=? AND processed=0 ORDER BY ordinal ASC LIMIT ?",
                    (project_id, batch_size)
                ).fetchall()
            ]

            if not events:
                # Mark job as done
                with self.store.connect(write=True) as wc:
                    wc.execute(
                        "UPDATE processing_jobs SET state='done', lease_id=NULL, lease_until=NULL WHERE project_id=? AND lease_id=? AND NOT EXISTS (SELECT 1 FROM events WHERE project_id=? AND processed=0)",
                        (project_id, lease_id, project_id)
                    )
                return {"status": "no_events", "processed_count": 0}

            # Fetch current latest summary
            summary_row = c.execute(
                "SELECT body FROM summary_versions WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (project_id,)
            ).fetchone()
            current_summary = summary_row["body"] if summary_row else None

            # Fetch milestones for current plan
            milestones = []
            if project["plan_version"] > 0:
                milestones = [
                    dict(r) for r in c.execute(
                        "SELECT * FROM milestones WHERE project_id=? AND plan_version=?",
                        (project_id, project["plan_version"])
                    ).fetchall()
                ]

        # 2. Call inference / extractor outside DB write transaction
        started_at = now()
        error_msg = None
        extracted = None

        try:
            extracted = self.inference_client.generate(
                project_name=project["name"],
                current_summary=current_summary,
                current_plan=milestones,
                events=events,
            )
            extracted = ExtractionOutput.model_validate(extracted).model_dump()
            by_id = {e["event_id"]: e for e in events}
            for obs in extracted["observations"]:
                if not set(obs["source_event_ids"]).issubset(by_id):
                    raise ValueError("observation sources must belong to the current event batch")
            milestone_ids = {m["milestone_id"] for m in milestones}
            for candidate in extracted["candidate_milestones"]:
                if candidate["milestone_id"] not in milestone_ids or not set(candidate["event_ids"]).issubset(by_id):
                    raise ValueError("invalid milestone or evidence outside the current event batch")
                if candidate["evidence_level"] == "verified":
                    evidence = [by_id[eid] for eid in candidate["event_ids"]]
                    if not any(e["event_type"] == "milestone_acceptance"
                               and json.loads(e["payload"]).get("milestone_id") == candidate["milestone_id"]
                               and json.loads(e["payload"]).get("plan_version") == project["plan_version"]
                               for e in evidence):
                        raise ValueError("verified completion requires explicit milestone acceptance evidence")
        except Exception as exc:
            error_msg = str(exc)

        # 3. Apply results or record failure
        with self.store.connect(write=True) as c:
            # Re-verify lease
            current_job = c.execute("SELECT * FROM processing_jobs WHERE project_id=?", (project_id,)).fetchone()
            if not current_job or current_job["lease_id"] != lease_id or current_job["state"] != "in_flight" or current_job["lease_until"] <= now():
                raise Conflict("lease was revoked or expired during processing")

            current_project = c.execute("SELECT plan_version, summary_version FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if current_project["plan_version"] != project["plan_version"] or current_project["summary_version"] != project["summary_version"]:
                error_msg = "project plan or summary changed during inference; retry required"

            if error_msg:
                # Record attempt failure
                c.execute(
                    """INSERT INTO processing_attempts(project_id, started_at, finished_at, status, error, model)
                       VALUES (?,?,?,?,?,?)""",
                    (project_id, started_at, now(), "failed", error_msg, getattr(self.inference_client, "model", "unknown"))
                )
                new_state = "failed" if current_job["attempts"] >= 5 else "pending"
                # Back off due_at by 30 seconds on failure
                retry_due = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
                c.execute(
                    """UPDATE processing_jobs
                       SET state=?, due_at=?, lease_id=NULL, lease_until=NULL, last_error=?
                       WHERE project_id=?""",
                    (new_state, retry_due, error_msg, project_id)
                )
                return {"status": "failed", "error": error_msg}

            # Inference succeeded: insert observations and update summary
            event_ids = [e["event_id"] for e in events]
            created_observations = []

            for obs in extracted.get("observations", []):
                obs_id = str(uuid.uuid4())
                obs_type = obs.get("type", "discovery")
                title = obs.get("title", "Observation")
                narrative = obs.get("narrative", "")
                evidence_level = obs.get("evidence_level", "reported")
                obs_created_at = now()

                c.execute(
                    """INSERT INTO observations(id, project_id, type, title, narrative, evidence_level, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (obs_id, project_id, obs_type, title, narrative, evidence_level, obs_created_at)
                )
                c.execute(
                    "INSERT INTO observations_fts(id, title, narrative) VALUES (?,?,?)",
                    (obs_id, title, narrative)
                )

                # Link source events
                for src_eid in obs.get("source_event_ids", []):
                    c.execute(
                        "INSERT OR IGNORE INTO observation_sources(observation_id, event_id) VALUES (?,?)",
                        (obs_id, src_eid)
                    )
                created_observations.append(obs_id)

            # Update summary version
            current_v = c.execute("SELECT summary_version FROM projects WHERE project_id=?", (project_id,)).fetchone()[0]
            next_v = current_v + 1
            summary_body = extracted.get("summary", "")
            model_name = extracted.get("model", "observer-v1")

            c.execute(
                """INSERT INTO summary_versions(project_id, version, body, covered_events, created_at, model)
                   VALUES (?,?,?,?,?,?)""",
                (project_id, next_v, summary_body, encode(event_ids), now(), model_name)
            )

            # Update project phase, next action, and version
            new_phase = extracted.get("phase") or project["phase"]
            new_next = extracted.get("next_action") or project["next_action"]
            c.execute(
                """UPDATE projects
                   SET summary_version=?, phase=?, next_action=?, updated_at=?
                   WHERE project_id=?""",
                (next_v, new_phase, new_next, now(), project_id)
            )

            # Mark processed events
            placeholders = ",".join("?" for _ in event_ids)
            c.execute(f"UPDATE events SET processed=1 WHERE event_id IN ({placeholders})", event_ids)

            # Handle candidate milestones if applicable
            for cand in extracted.get("candidate_milestones", []):
                mid = cand.get("milestone_id")
                c_status = cand.get("status", "in_progress")
                c_evidence = cand.get("evidence_level", "reported")
                c_events = cand.get("event_ids", [])
                if project["plan_version"] > 0:
                    exists = c.execute(
                        "SELECT 1 FROM milestones WHERE project_id=? AND plan_version=? AND milestone_id=?",
                        (project_id, project["plan_version"], mid)
                    ).fetchone()
                    if exists and c_status in ("todo", "in_progress", "done"):
                        c.execute(
                            "UPDATE milestones SET status=?, evidence_level=? WHERE project_id=? AND plan_version=? AND milestone_id=?",
                            (c_status, c_evidence, project_id, project["plan_version"], mid)
                        )
                        for ceid in c_events:
                            c.execute(
                                "INSERT OR IGNORE INTO milestone_evidence VALUES (?,?,?,?)",
                                (project_id, project["plan_version"], mid, ceid)
                            )

            # Record attempt success
            c.execute(
                """INSERT INTO processing_attempts(project_id, started_at, finished_at, status, model, input_tokens, output_tokens)
                   VALUES (?,?,?,?,?,?,?)""",
                (project_id, started_at, now(), "success", model_name, extracted.get("input_tokens", 0), extracted.get("output_tokens", 0))
            )

            # Check if more events remain for this project
            unprocessed_count = c.execute(
                "SELECT COUNT(*) FROM events WHERE project_id=? AND processed=0",
                (project_id,)
            ).fetchone()[0]

            job_state = "pending" if unprocessed_count > 0 else "done"
            c.execute(
                """UPDATE processing_jobs
                   SET state=?, due_at=?, lease_id=NULL, lease_until=NULL, attempts=0, last_error=NULL
                   WHERE project_id=?""",
                (job_state, now(), project_id)
            )

            self.store._revision(c)
            self.store._audit(c, "observer.processed", project_id, {
                "summary_version": next_v,
                "events_processed": len(event_ids),
                "observations_created": len(created_observations),
            })

            return {
                "status": "success",
                "project_id": project_id,
                "summary_version": next_v,
                "events_processed": len(event_ids),
                "observations_created": len(created_observations),
                "remaining_unprocessed": unprocessed_count,
            }

    def tick(self, batch_size: int = 50, force: bool = False) -> Optional[Dict[str, Any]]:
        """Execute one processing iteration if any leased job is available."""
        if not 1 <= batch_size <= 100:
            raise ValueError("batch_size must be between 1 and 100")
        lease = self.lease_job(force=force)
        if not lease:
            return None
        project_id, lease_id = lease
        return self.process_project(project_id, lease_id, batch_size=batch_size)
