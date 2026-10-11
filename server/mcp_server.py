"""Hermes Model Context Protocol (MCP) Server.
Exposes bounded project memory and registry tools to Hermes Agent via JSON-RPC 2.0 stdio.
Supports direct Store access or remote HTTP endpoint mode.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, Optional

import httpx

from server.db import Store, encode
from server.retrieval import (
    get_project_context,
    search_memories,
    get_memory_timeline,
    get_memories,
)


TOOL_DEFINITIONS = [
    {
        "name": "list_projects",
        "description": "List all registered projects in the memory registry. Exhaustive snapshot-consistent enumeration.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot": {"type": "string", "description": "Optional snapshot ID for paginated continuation"},
                "offset": {"type": "integer", "default": 0, "description": "Offset in the snapshot"},
                "limit": {"type": "integer", "default": 50, "description": "Max projects per page (1-200)"},
            },
        },
    },
    {
        "name": "project_context",
        "description": "Get compact current context, latest summary, active phase, next action, and recent memory index for a project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Hermes Project UUID"},
                "max_tokens": {"type": "integer", "default": 800, "description": "Token budget limit"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "search_memory",
        "description": "Search historical observations and memories for a project using full-text search.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Hermes Project UUID"},
                "query": {"type": "string", "description": "Search query terms"},
                "limit": {"type": "integer", "default": 5, "description": "Max results to return"},
                "type": {
                    "type": "string",
                    "description": "Filter by observation type: decision, discovery, change, bugfix, feature, refactor",
                },
                "max_tokens": {"type": "integer", "default": 500, "description": "Token budget limit"},
            },
            "required": ["project_id", "query"],
        },
    },
    {
        "name": "memory_timeline",
        "description": "Get chronological neighboring events and observations around a target event or latest activity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Hermes Project UUID"},
                "near_event_id": {"type": "string", "description": "Optional target event ID"},
                "limit": {"type": "integer", "default": 10, "description": "Number of events to retrieve"},
                "max_tokens": {"type": "integer", "default": 1000, "description": "Token budget limit"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "get_memories",
        "description": "Fetch full detailed memories for specific observation IDs with linked source event IDs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Hermes Project UUID"},
                "memory_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of observation IDs to fetch",
                },
                "max_tokens": {"type": "integer", "default": 1500, "description": "Token budget limit"},
            },
            "required": ["project_id", "memory_ids"],
        },
    },
    {
        "name": "memory_health",
        "description": "Check status of memory store, devices, pending jobs, and ingestion health.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


class MemoryMCPServer:
    """MCP Protocol Server handler."""

    def __init__(
        self,
        store: Optional[Store] = None,
        endpoint_url: Optional[str] = None,
        auth_token: Optional[str] = None,
    ):
        self.store = store
        self.endpoint_url = endpoint_url.rstrip("/") if endpoint_url else None
        self.auth_token = auth_token

    def handle_request(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = req.get("method")
        msg_id = req.get("id")

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "hermes-project-memory",
                        "version": "1.0.0",
                    },
                },
            }

        elif method == "notifications/initialized":
            return None

        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": TOOL_DEFINITIONS},
            }

        elif method == "tools/call":
            params = req.get("params", {})
            tool_name = params.get("name")
            args = params.get("arguments", {})

            try:
                res_data = self.execute_tool(tool_name, args)
                text_content = encode(res_data)
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": text_content}],
                        "isError": False,
                    },
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error: {str(exc)}"}],
                        "isError": True,
                    },
                }

        elif method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

        else:
            if msg_id is not None:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            return None

    def execute_tool(self, name: str, args: Dict[str, Any]) -> Any:
        if self.store is not None:
            return self._execute_local(name, args)
        elif self.endpoint_url is not None:
            return self._execute_remote(name, args)
        else:
            raise RuntimeError("No store or endpoint configured for MCP server")

    def _execute_local(self, name: str, args: Dict[str, Any]) -> Any:
        assert self.store is not None
        if name == "list_projects":
            snapshot = args.get("snapshot")
            offset = int(args.get("offset", 0))
            limit = int(args.get("limit", 50))
            return self.store.list_projects(snapshot=snapshot, offset=offset, limit=limit)

        elif name == "project_context":
            project_id = args["project_id"]
            max_tokens = int(args.get("max_tokens", 800))
            return get_project_context(self.store, project_id=project_id, max_tokens=max_tokens)

        elif name == "search_memory":
            project_id = args["project_id"]
            query = args["query"]
            limit = int(args.get("limit", 5))
            obs_type = args.get("type")
            max_tokens = int(args.get("max_tokens", 500))
            return search_memories(
                self.store,
                project_id=project_id,
                query=query,
                limit=limit,
                obs_type=obs_type,
                max_tokens=max_tokens,
            )

        elif name == "memory_timeline":
            project_id = args["project_id"]
            near_event_id = args.get("near_event_id")
            limit = int(args.get("limit", 10))
            max_tokens = int(args.get("max_tokens", 1000))
            return get_memory_timeline(
                self.store,
                project_id=project_id,
                near_event_id=near_event_id,
                limit=limit,
                max_tokens=max_tokens,
            )

        elif name == "get_memories":
            project_id = args["project_id"]
            memory_ids = args["memory_ids"]
            max_tokens = int(args.get("max_tokens", 1500))
            return get_memories(
                self.store,
                project_id=project_id,
                memory_ids=memory_ids,
                max_tokens=max_tokens,
            )

        elif name == "memory_health":
            return self.store.health()

        else:
            raise ValueError(f"Unknown tool: {name}")

    def _execute_remote(self, name: str, args: Dict[str, Any]) -> Any:
        assert self.endpoint_url is not None
        headers = {"Authorization": f"Bearer {self.auth_token}"} if self.auth_token else {}

        with httpx.Client(timeout=30.0) as client:
            if name == "list_projects":
                params = {}
                if "snapshot" in args:
                    params["snapshot"] = args["snapshot"]
                if "offset" in args:
                    params["offset"] = args["offset"]
                if "limit" in args:
                    params["limit"] = args["limit"]
                r = client.get(f"{self.endpoint_url}/api/v1/projects", params=params, headers=headers)

            elif name == "project_context":
                pid = args["project_id"]
                params = {"max_tokens": args.get("max_tokens", 800)}
                r = client.get(f"{self.endpoint_url}/api/v1/projects/{pid}/context", params=params, headers=headers)

            elif name == "search_memory":
                pid = args["project_id"]
                params = {
                    "q": args["query"],
                    "limit": args.get("limit", 5),
                    "max_tokens": args.get("max_tokens", 500),
                }
                if "type" in args and args["type"]:
                    params["type"] = args["type"]
                r = client.get(f"{self.endpoint_url}/api/v1/projects/{pid}/search", params=params, headers=headers)

            elif name == "memory_timeline":
                pid = args["project_id"]
                params = {
                    "limit": args.get("limit", 10),
                    "max_tokens": args.get("max_tokens", 1000),
                }
                if "near_event_id" in args and args["near_event_id"]:
                    params["near_event_id"] = args["near_event_id"]
                r = client.get(f"{self.endpoint_url}/api/v1/projects/{pid}/timeline", params=params, headers=headers)

            elif name == "get_memories":
                pid = args["project_id"]
                body = {
                    "memory_ids": args["memory_ids"],
                    "max_tokens": args.get("max_tokens", 1500),
                }
                r = client.post(f"{self.endpoint_url}/api/v1/projects/{pid}/memories", json=body, headers=headers)

            elif name == "memory_health":
                r = client.get(f"{self.endpoint_url}/api/v1/health", headers=headers)

            else:
                raise ValueError(f"Unknown tool: {name}")

            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
            return r.json()

    def run_stdio(self) -> None:
        """Run loop reading JSON-RPC from stdin and writing to stdout."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                resp = self.handle_request(req)
                if resp is not None:
                    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                    sys.stdout.flush()
            except Exception as exc:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {str(exc)}"},
                }
                sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Hermes Memory MCP Server")
    parser.add_argument("--db", help="Path to local SQLite database file")
    parser.add_argument("--endpoint", help="Cloud API endpoint URL")
    parser.add_argument("--token", help="Bearer authentication token")
    args = parser.parse_args()

    db_path = args.db or os.environ.get("HERMES_MEMORY_DB")
    endpoint = args.endpoint or os.environ.get("HERMES_MEMORY_URL")
    token = args.token or os.environ.get("HERMES_MEMORY_TOKEN")

    if db_path:
        server = MemoryMCPServer(store=Store(db_path))
    elif endpoint:
        server = MemoryMCPServer(endpoint_url=endpoint, auth_token=token)
    else:
        # Default to local db in ~/.hermes/cloud.db if neither specified
        default_db = Path.home() / ".hermes" / "server" / "memory.db"
        server = MemoryMCPServer(store=Store(default_db))

    server.run_stdio()


if __name__ == "__main__":
    main()
