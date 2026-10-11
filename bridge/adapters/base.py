"""Base capture adapter and common normalization logic for CLI clients."""

from __future__ import annotations
from abc import ABC, abstractmethod
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, Optional

from bridge.identity import resolve_project, normalize_path
from bridge.outbox import Outbox, OutboxEvent, get_default_device_id


class BaseAdapter(ABC):
    """Abstract base adapter for local CLI hook event handling."""

    platform_name: str = "base"

    def __init__(self, outbox: Optional[Outbox] = None, device_id: Optional[str] = None):
        if outbox is None:
            db_path = Path.home() / ".hermes" / "bridge" / "outbox.db"
            outbox = Outbox(db_path)
        self.outbox = outbox
        self.device_id = device_id or get_default_device_id()

    @abstractmethod
    def handle_hook(self, hook_name: str, payload_data: Dict[str, Any], cwd: Optional[str] = None) -> Optional[OutboxEvent]:
        """Process incoming hook event and enqueue normalized event."""
        pass

    def record_event(
        self,
        event_type: str,
        native_session_id: str,
        payload: Dict[str, Any],
        source_identity: str,
        cwd: Optional[str] = None,
        occurred_at: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> Optional[OutboxEvent]:
        """Resolve workspace project and commit event to outbox."""
        # Recalled evidence is already stored remotely. Do not feed it back into
        # the observer as new work (or pay to summarize it again).
        memory_reads = {"list_all_projects", "get_project_context", "get_project_status", "search_project_work"}
        tool = payload.get("tool")
        if event_type in {"tool_call", "tool_result"} and isinstance(tool, str):
            for prefix in ("mcp__hermes-memory__", "mcp__hermes_memory__"):
                if tool.startswith(prefix) and tool[len(prefix):] in memory_reads:
                    return None
        working_dir = cwd or os.getcwd()
        from bridge.cache import LocalCache
        resolved_proj = resolve_project(working_dir, known_aliases=LocalCache().get_known_aliases())

        # Annotate payload with execution context
        enriched_payload = dict(payload)
        enriched_payload["_context"] = {
            "cwd": normalize_path(working_dir),
            "project_name": resolved_proj.name,
            "canonical_path": resolved_proj.canonical_path,
            "resolution_source": resolved_proj.resolution_source,
            "branch": resolved_proj.branch,
            "worktree_root": resolved_proj.worktree_root,
        }

        return self.outbox.enqueue(
            device_id=self.device_id,
            project_id=resolved_proj.project_id,
            platform=self.platform_name,
            native_session_id=native_session_id,
            event_type=event_type,
            raw_payload=enriched_payload,
            source_identity=source_identity,
            occurred_at=occurred_at,
            event_id=event_id,
        )


def read_hook_input() -> Dict[str, Any]:
    """Helper to read hook input from stdin or argv."""
    if len(sys.argv) > 1 and sys.argv[1].startswith("{"):
        try:
            return json.loads(sys.argv[1])
        except Exception:
            pass

    if not sys.stdin.isatty():
        try:
            # Native hooks send UTF-8 JSON, independent of Windows' ANSI locale.
            stream = getattr(sys.stdin, "buffer", None)
            stdin_content = (stream.read().decode("utf-8", errors="replace")
                             if stream is not None else sys.stdin.read()).strip()
            if stdin_content:
                return json.loads(stdin_content)
        except Exception:
            pass

    return {}
