"""Upstream Claude-Mem transport adapter for Hermes Bridge.
Authored by: Agy (Antigravity) on 2026-09-29.

Translates local OutboxEvents into upstream Claude-Mem worker HTTP endpoints:
- SessionStart / PromptSubmit -> POST /api/sessions/init
- ToolResult                  -> POST /api/sessions/observations (with tool_use_id = event_id)
- AssistantResponse / Stop    -> POST /api/sessions/summarize
- SessionEnd                  -> POST /api/sessions/session-end
- Context & Search            -> GET /api/context/recent, GET /api/search
"""
from __future__ import annotations
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit
import httpx

from bridge.outbox import Outbox, OutboxEvent

logger = logging.getLogger(__name__)


def validate_upstream_endpoint(url: str) -> str:
    """Validate endpoint URL, restricting to HTTPS or private loopback HTTP."""
    if not url:
        return "http://127.0.0.1:37777"
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("invalid endpoint URL")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("use HTTPS or loopback HTTP through a private tunnel")
    return url.rstrip("/")


class UpstreamSyncResult:
    def __init__(
        self,
        success: bool,
        batch_size: int = 0,
        accepted: int = 0,
        duplicate: int = 0,
        rejected: int = 0,
        error: Optional[str] = None,
    ):
        self.success = success
        self.batch_size = batch_size
        self.accepted = accepted
        self.duplicate = duplicate
        self.rejected = rejected
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        return vars(self).copy()


class UpstreamTransportClient:
    """Client for synchronizing OutboxEvents to an upstream Claude-Mem worker."""

    def __init__(
        self,
        endpoint_url: str = "http://127.0.0.1:37777",
        outbox: Optional[Outbox] = None,
        timeout: float = 15.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.endpoint_url = validate_upstream_endpoint(endpoint_url)
        self.outbox = outbox
        self.timeout = timeout
        self.transport = transport

    def map_event_to_upstream(self, event: OutboxEvent) -> Dict[str, Any]:
        """Map an OutboxEvent into (endpoint_path, json_payload)."""
        kind = event.event_type
        payload = event.sanitized_payload or {}
        ctx = payload.get("_context") if isinstance(payload.get("_context"), dict) else {}
        cwd = payload.get("cwd") or ctx.get("cwd") or ctx.get("worktree_root") or ""
        branch = payload.get("branch") or ctx.get("branch") or ""
        worktree_root = payload.get("worktree_root") or ctx.get("worktree_root") or ""

        if kind in {"session_start", "prompt_submit"}:
            prompt_text = payload.get("prompt") or payload.get("matcher") or ""
            project_name = ctx.get("project_name")
            return {
                "path": "/api/sessions/init",
                "body": {
                    "contentSessionId": event.native_session_id,
                    "project": event.project_id,
                    "prompt": prompt_text,
                    "platformSource": event.platform,
                    **({"customTitle": "Hermes project: " + str(project_name)[:120]} if project_name else {}),
                },
            }

        elif kind == "tool_result":
            tool_name = payload.get("tool") or "unknown"
            tool_input = payload.get("input")
            if tool_input is None:
                tool_input = {}
            tool_response = {
                "output": payload.get("output"),
                "error": payload.get("error"),
                "exit_code": payload.get("exit_code"),
                "success": payload.get("success"),
            }
            if branch:
                tool_response["branch"] = branch
            if worktree_root:
                tool_response["worktree_root"] = worktree_root
            return {
                "path": "/api/sessions/observations",
                "body": {
                    "contentSessionId": event.native_session_id,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "tool_response": tool_response,
                    "tool_use_id": event.event_id,
                    "cwd": cwd or event.source_identity,
                    "platformSource": event.platform,
                },
            }

        elif kind in {"assistant_response", "stop"}:
            last_message = payload.get("response") or payload.get("reason") or "Turn completed"
            return {
                "path": "/api/sessions/summarize",
                "body": {
                    "contentSessionId": event.native_session_id,
                    "last_assistant_message": str(last_message),
                    "platformSource": event.platform,
                },
            }

        elif kind == "session_end":
            return {
                "path": "/api/sessions/session-end",
                "body": {
                    "contentSessionId": event.native_session_id,
                    "platformSource": event.platform,
                },
            }

        # Events that do not trigger upstream generation (e.g. tool_call, compact)
        return {"path": None, "body": {}}

    def _has_tool_receipt(self, client: httpx.Client, event_id: str) -> bool:
        """Only the exact persisted tool ID is a receipt; endpoint errors fail closed."""
        response = client.post(f"{self.endpoint_url}/api/tool-uses/batch", json={"ids": [event_id]})
        if response.status_code != 200:
            raise RuntimeError(f"Tool receipt lookup failed: HTTP {response.status_code}")
        items = response.json()
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("Invalid tool receipt response")
        return any(item.get("tool_use_id") == event_id for item in items)

    def sync_event(self, client: httpx.Client, event: OutboxEvent) -> Dict[str, Any]:
        """Dispatch a single event to the upstream worker."""
        mapping = self.map_event_to_upstream(event)
        path = mapping["path"]
        body = mapping["body"]

        if path is None:
            # Metadata-only event; acknowledged immediately
            return {"event_id": event.event_id, "status": "accepted", "metadata_only": True}

        # A lost response can follow a committed write. On retry, check before
        # reposting evidence and potentially triggering another model request.
        if path == "/api/sessions/observations" and event.retry_count > 0:
            if self._has_tool_receipt(client, event.event_id):
                return {"event_id": event.event_id, "status": "duplicate"}

        url = f"{self.endpoint_url}{path}"
        response = client.post(url, json=body)
        if response.status_code == 503 and "initializing" in response.text.lower():
            raise RuntimeError("Upstream worker database is still initializing")
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"HTTP {response.status_code}")

        resp_json = response.json() if response.content else {}
        if not isinstance(resp_json, dict):
            raise ValueError("Invalid ingestion response")

        # Validate durable acceptance
        if resp_json.get("stored") is False:
            if resp_json.get("status") == "skipped":
                reason = str(resp_json.get("reason", "")).lower()
                if "duplicate" in reason or "already_exists" in reason:
                    status = "duplicate"
                else:
                    status = "rejected"
            else:
                # In-memory queue or uncommitted: keep pending for durable retry
                raise RuntimeError("Event was not durably stored upstream")
        elif resp_json.get("status") == "skipped":
            reason = str(resp_json.get("reason", "")).lower()
            if "duplicate" in reason or "already_exists" in reason:
                status = "duplicate"
            else:
                status = "rejected"
        else:
            status = "accepted"

        if path == "/api/sessions/observations" and status in {"accepted", "duplicate"}:
            if not self._has_tool_receipt(client, event.event_id):
                raise RuntimeError("Tool event has no durable receipt")
        if path == "/api/sessions/init" and status in {"accepted", "duplicate"}:
            session_db_id = resp_json.get("sessionDbId")
            if type(session_db_id) is not int or session_db_id <= 0:
                raise RuntimeError("Session init response lacks a valid sessionDbId")

        return {
            "event_id": event.event_id,
            "status": status,
            "reason": resp_json.get("reason"),
            "remote_response": resp_json,
        }

    def sync_batch(self, batch_size: int = 25) -> UpstreamSyncResult:
        """Sync a batch of pending events from outbox to upstream."""
        if not self.outbox:
            raise ValueError("an outbox instance is required for sync_batch")

        events = self.outbox.get_pending_batch(limit=batch_size)
        if not events:
            return UpstreamSyncResult(success=True, batch_size=0)

        in_flight_ids = [e.event_id for e in events]
        acks: List[Dict[str, Any]] = []
        errors: List[str] = []
        blocked_sessions = set()

        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport, follow_redirects=False) as client:
                for event in events:
                    session = (event.device_id, event.platform, event.native_session_id)
                    if session in blocked_sessions:
                        self.outbox.release_in_flight([event.event_id], "Waiting for earlier session event")
                        continue
                    try:
                        ack = self.sync_event(client, event)
                        self.outbox.acknowledge_results([ack])
                        acks.append(ack)
                    except Exception as exc:
                        blocked_sessions.add(session)
                        errors.append(f"{event.event_id}: {exc}")
                        self.outbox.release_in_flight([event.event_id], str(exc))

            counts = {
                "accepted": sum(1 for a in acks if a["status"] == "accepted"),
                "duplicate": sum(1 for a in acks if a["status"] == "duplicate"),
                "rejected": sum(1 for a in acks if a["status"] == "rejected"),
            }
            return UpstreamSyncResult(
                success=len(errors) == 0,
                batch_size=len(events),
                error="; ".join(errors) if errors else None,
                **counts,
            )
        except Exception as exc:
            # Blanket release for any unhandled network failure
            remaining_ids = [eid for eid in in_flight_ids if eid not in {a["event_id"] for a in acks}]
            if remaining_ids:
                self.outbox.release_in_flight(remaining_ids, str(exc))
            return UpstreamSyncResult(success=False, batch_size=len(events), error=str(exc))

    def sync_all(self, batch_size: int = 25, max_batches: int = 50) -> Dict[str, Any]:
        """Synchronize all pending batches up to max_batches."""
        result = {
            "total_batches": 0,
            "total_accepted": 0,
            "total_duplicate": 0,
            "total_rejected": 0,
            "last_error": None,
            "outbox_stats": self.outbox.get_stats() if self.outbox else {},
        }
        for _ in range(max_batches):
            res = self.sync_batch(batch_size=batch_size)
            if res.batch_size == 0:
                break
            result["total_batches"] += 1
            result["total_accepted"] += res.accepted
            result["total_duplicate"] += res.duplicate
            result["total_rejected"] += res.rejected
            if not res.success:
                result["last_error"] = res.error
                break
        if self.outbox:
            result["outbox_stats"] = self.outbox.get_stats()
        return result

    def fetch_recent_context(self, project_id: str, http_client=None) -> Dict[str, Any]:
        """Fetch recent context packet from upstream worker."""
        url = f"{self.endpoint_url}/api/context/recent"
        if http_client is not None:
            resp = http_client.get(url, params={"project": project_id})
            resp.raise_for_status()
            return resp.json()
        with httpx.Client(timeout=self.timeout, transport=self.transport, follow_redirects=False) as client:
            resp = client.get(url, params={"project": project_id})
            resp.raise_for_status()
            return resp.json()

    def search_memories(self, query: str, project_id: str) -> Dict[str, Any]:
        """Search upstream worker memory via SQLite FTS5."""
        url = f"{self.endpoint_url}/api/search"
        with httpx.Client(timeout=self.timeout, transport=self.transport, follow_redirects=False) as client:
            resp = client.get(url, params={"query": query, "project": project_id})
            resp.raise_for_status()
            return resp.json()


def refresh_recent_context_cache(
    client: UpstreamTransportClient,
    cache: Any,
    project_ids: List[str],
    min_interval: float = 0,
) -> Dict[str, Any]:
    """Refresh local project context caches from the upstream worker."""
    result = {"refreshed": 0, "errors": []}
    with httpx.Client(timeout=client.timeout, transport=client.transport, follow_redirects=False) as http:
        for pid in dict.fromkeys(project_ids):
            existing = cache.get_cached_context(pid)
            now = time.time()
            attempted = (existing or {}).get("_hermes_refresh", {}).get("attempted_at", 0)
            if min_interval and 0 <= now - attempted < min_interval:
                continue
            try:
                ctx = client.fetch_recent_context(pid, http_client=http)
                if not isinstance(ctx, dict):
                    raise ValueError("invalid context response")
                if ctx.get("project") not in (None, pid):
                    raise ValueError("context project mismatch")
                ctx["project"] = pid
                ctx["stale"] = False
                ctx["_hermes_refresh"] = {"attempted_at": now, "refreshed_at": now}
                cache.save_cached_context(pid, ctx)
                result["refreshed"] += 1
            except Exception as exc:
                # Keep last good content; failed first attempts also get a cooldown.
                existing = existing or {"project": pid, "content": []}
                existing["stale"] = True
                existing.setdefault("_hermes_refresh", {})["attempted_at"] = now
                cache.save_cached_context(pid, existing)
                result["errors"].append({"project_id": pid, "error": type(exc).__name__})
    return result
