"""Versioned API contracts. Event IDs and payload hashes are immutable."""
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=128)
    schema_version: Literal[1]
    device_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    platform: Literal["codex", "claude_code", "agy", "migration", "manual"]
    native_session_id: str = Field(min_length=1, max_length=256)
    session_sequence: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=80)
    occurred_at: str
    received_at: str
    source_identity: str = Field(min_length=1, max_length=1024)
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    payload: dict[str, Any]
    capture_adapter_version: str = Field(min_length=1, max_length=80)

    @field_validator("occurred_at", "received_at")
    @classmethod
    def timestamp(cls, value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return value

class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    status: Literal["active", "blocked", "paused", "complete", "archived"] = "active"

class Batch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(min_length=1, max_length=128)
    events: list[dict[str, Any]] = Field(max_length=100)
    reported_backlog: int | None = Field(default=None, ge=0)

class Milestone(BaseModel):
    model_config = ConfigDict(extra="forbid")
    milestone_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=300)
    weight: float = Field(gt=0, le=1000000, allow_inf_nan=False)

class MilestonePlan(BaseModel):
    milestones: list[Milestone] = Field(min_length=1, max_length=100)

class Completion(BaseModel):
    status: Literal["todo", "in_progress", "done"]
    evidence_level: Literal["reported", "verified"]
    event_ids: list[str] = Field(min_length=1, max_length=100)

class MemoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_ids: list[str] = Field(min_length=1, max_length=100)
    max_tokens: int = Field(default=1500, ge=50, le=10000)

class DiscoveryRunSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    roots: list[str]
    failures: list[dict[str, Any]] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)

class CandidateReconcile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=128)
    disposition: Literal["registered", "merged", "excluded", "unresolved"]
    project_id: str | None = None
