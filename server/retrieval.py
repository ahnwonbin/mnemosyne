"""Bounded retrieval, full-text search, and compact context generation.
Enforces token budgets and deterministic enumeration.
"""
from __future__ import annotations
import math
import re
from typing import Any, Dict, List, Optional

from server.db import Store, encode


def _bounded(result, max_tokens, list_key, count_key=None):
    """Bound the entire serialized response, including accounting fields.

    This is an estimate, not a model-specific tokenizer guarantee. Refuse budgets
    too small for the response envelope rather than silently exceeding them.
    """
    if not isinstance(max_tokens, int) or not 50 <= max_tokens <= 10000:
        raise ValueError("max_tokens must be between 50 and 10000")
    result["estimated_tokens"] = 0
    while True:
        if count_key:
            result[count_key] = len(result[list_key])
        for _ in range(4):
            result["estimated_tokens"] = estimate_tokens(result)
        if estimate_tokens(result) <= max_tokens:
            return result
        result["truncated"] = True
        if result[list_key]:
            result[list_key].pop()
            continue
        # Trim only descriptive fields; preserve identifiers and version metadata.
        candidates = [(result, k) for k in ("name", "phase", "next_action", "query")
                      if isinstance(result.get(k), str) and result[k]]
        if isinstance(result.get("summary"), dict) and result["summary"].get("body"):
            candidates.append((result["summary"], "body"))
        if candidates:
            obj, key = max(candidates, key=lambda pair: len(pair[0][pair[1]]))
            obj[key] = obj[key][:len(obj[key]) // 2]
        elif result.get("summary") is not None:
            result["summary"] = None
        else:
            raise ValueError("max_tokens is too small for the response metadata")


def estimate_tokens(content: Any) -> int:
    """Estimate token count for a string or JSON-serializable object.
    Uses a UTF-8 byte/4 heuristic, not a model-specific tokenizer guarantee.
    """
    if content is None:
        return 0
    if isinstance(content, str):
        text = content
    else:
        text = encode(content)
    if not text:
        return 0
    # Byte accounting includes multibyte text; actual token counts vary by model.
    return max(1, math.ceil(len(text.encode("utf-8")) / 4))


def _sanitize_fts_query(query: str) -> str:
    """Sanitize user search queries to prevent FTS5 syntax errors.
    Filters out standalone boolean operators, quotes terms, and joins with OR
    so relevant keyword matches surface and are ranked by BM25.
    """
    stopwords = {"and", "or", "not", "to", "the", "a", "an", "in", "of", "on"}
    raw_terms = re.findall(r"[\w\u0E00-\u0E7F]+", query)
    terms = [t for t in raw_terms if t.lower() not in stopwords]
    if not terms:
        terms = raw_terms
    if not terms:
        return '""'
    return " OR ".join(f'"{t}"' for t in terms)



def get_project_context(store: Store, project_id: str, max_tokens: int = 800) -> Dict[str, Any]:
    """Retrieve compact project context within a specified token budget (default 800).
    Includes project metadata, plan progress, latest summary, and a compact index
    of recent observations.
    """
    with store.connect() as c:
        project_row = c.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not project_row:
            raise KeyError("project not found")
        project = dict(project_row)

        progress = store._progress(c, project)
        latest_summary_row = c.execute(
            "SELECT version, body, covered_events, created_at, model FROM summary_versions "
            "WHERE project_id=? ORDER BY version DESC LIMIT 1",
            (project_id,)
        ).fetchone()

        summary_data = None
        if latest_summary_row:
            summary_data = {
                "version": latest_summary_row["version"],
                "body": latest_summary_row["body"],
                "created_at": latest_summary_row["created_at"],
                "model": latest_summary_row["model"],
            }

        # Fetch recent observations for compact index
        obs_rows = c.execute(
            "SELECT id, type, title, created_at, narrative FROM observations "
            "WHERE project_id=? ORDER BY created_at DESC LIMIT 20",
            (project_id,)
        ).fetchall()

        recent_obs_index: List[Dict[str, Any]] = []
        for row in obs_rows:
            obs_item = {
                "id": row["id"],
                "type": row["type"],
                "title": row["title"],
                "created_at": row["created_at"],
                "size_estimate": estimate_tokens(row["narrative"]),
            }
            recent_obs_index.append(obs_item)

        # Build context object and enforce max_tokens budget
        result = {
            "project_id": project["project_id"],
            "name": project["name"],
            "status": project["status"],
            "phase": project["phase"],
            "next_action": project["next_action"],
            "progress_percent": progress,
            "plan_version": project["plan_version"],
            "summary": summary_data,
            "recent_observations": recent_obs_index,
            "budget_tokens": max_tokens,
            "truncated": False,
        }

        # Check budget and prune observations if necessary
        current_tokens = estimate_tokens(result)
        while current_tokens > max_tokens and len(result["recent_observations"]) > 1:
            result["recent_observations"].pop()
            result["truncated"] = True
            current_tokens = estimate_tokens(result)

        return _bounded(result, max_tokens, "recent_observations")


def search_memories(
    store: Store,
    project_id: str,
    query: str,
    limit: int = 5,
    obs_type: Optional[str] = None,
    max_tokens: int = 500,
) -> Dict[str, Any]:
    """Perform full-text search over observations using FTS5 within a token budget."""
    safe_query = _sanitize_fts_query(query)
    limit = max(1, min(limit, 20))

    with store.connect() as c:
        project_exists = c.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not project_exists:
            raise KeyError("project not found")

        params: List[Any] = [safe_query, project_id]
        type_filter = ""
        if obs_type:
            type_filter = "AND o.type = ?"
            params.append(obs_type)

        sql = f"""
            SELECT o.id, o.type, o.title, o.narrative, o.evidence_level, o.created_at,
                   snippet(observations_fts, 1, '[', ']', '...', 10) as title_snippet,
                   snippet(observations_fts, 2, '[', ']', '...', 25) as narrative_snippet,
                   bm25(observations_fts) as rank
            FROM observations_fts
            JOIN observations o ON observations_fts.id = o.id
            WHERE observations_fts MATCH ? AND o.project_id = ? {type_filter}
            ORDER BY rank ASC
            LIMIT ?
        """
        params.append(limit * 2)

        try:
            rows = c.execute(sql, params).fetchall()
        except Exception:
            # Fallback if FTS matching fails on unusual characters
            rows = []

        total_matches = len(rows)
        results: List[Dict[str, Any]] = []
        for r in rows:
            snippet = r["narrative_snippet"] or r["title_snippet"] or (r["narrative"][:100] + "...")
            item = {
                "id": r["id"],
                "type": r["type"],
                "title": r["title"],
                "snippet": snippet,
                "evidence_level": r["evidence_level"],
                "created_at": r["created_at"],
                "detail_size_estimate": estimate_tokens(r["narrative"]),
            }
            results.append(item)

        response = {
            "project_id": project_id,
            "query": query,
            "total_matches": total_matches,
            "returned": len(results),
            "results": results[:limit],
            "budget_tokens": max_tokens,
            "truncated": False,
        }

        # Truncate results if exceeding token budget
        while estimate_tokens(response) > max_tokens and len(response["results"]) > 1:
            response["results"].pop()
            response["truncated"] = True

        response["returned"] = len(response["results"])
        return _bounded(response, max_tokens, "results", "returned")


def get_memory_timeline(
    store: Store,
    project_id: str,
    near_event_id: Optional[str] = None,
    limit: int = 10,
    max_tokens: int = 1000,
) -> Dict[str, Any]:
    """Retrieve chronological neighboring events and linked observations."""
    limit = max(1, min(limit, 50))
    with store.connect() as c:
        if not c.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
            raise KeyError("project not found")

        target_ordinal = None
        if near_event_id:
            row = c.execute("SELECT ordinal FROM events WHERE event_id=? AND project_id=?", (near_event_id, project_id)).fetchone()
            if row:
                target_ordinal = row[0]

        if target_ordinal is not None:
            half = limit // 2
            before_rows = c.execute(
                "SELECT event_id, event_type, platform, occurred_at, session_sequence, payload "
                "FROM events WHERE project_id=? AND ordinal < ? ORDER BY ordinal DESC LIMIT ?",
                (project_id, target_ordinal, half)
            ).fetchall()[::-1]

            target_row = c.execute(
                "SELECT event_id, event_type, platform, occurred_at, session_sequence, payload "
                "FROM events WHERE project_id=? AND ordinal = ?",
                (project_id, target_ordinal)
            ).fetchall()

            after_rows = c.execute(
                "SELECT event_id, event_type, platform, occurred_at, session_sequence, payload "
                "FROM events WHERE project_id=? AND ordinal > ? ORDER BY ordinal ASC LIMIT ?",
                (project_id, target_ordinal, limit - len(before_rows) - len(target_row))
            ).fetchall()

            event_rows = before_rows + target_row + after_rows
        else:
            event_rows = c.execute(
                "SELECT event_id, event_type, platform, occurred_at, session_sequence, payload "
                "FROM events WHERE project_id=? ORDER BY ordinal DESC LIMIT ?",
                (project_id, limit)
            ).fetchall()[::-1]

        timeline_items = []
        event_ids = [r["event_id"] for r in event_rows]

        # Fetch linked observations
        obs_map: Dict[str, List[Dict[str, Any]]] = {}
        if event_ids:
            placeholders = ",".join("?" for _ in event_ids)
            linked_obs = c.execute(
                f"""SELECT os.event_id, o.id, o.type, o.title, o.evidence_level
                    FROM observation_sources os
                    JOIN observations o ON os.observation_id = o.id
                    WHERE os.event_id IN ({placeholders})""",
                event_ids
            ).fetchall()
            for lo in linked_obs:
                obs_map.setdefault(lo["event_id"], []).append({
                    "id": lo["id"],
                    "type": lo["type"],
                    "title": lo["title"],
                    "evidence_level": lo["evidence_level"],
                })

        for er in event_rows:
            timeline_items.append({
                "event_id": er["event_id"],
                "event_type": er["event_type"],
                "platform": er["platform"],
                "occurred_at": er["occurred_at"],
                "sequence": er["session_sequence"],
                "linked_observations": obs_map.get(er["event_id"], []),
            })

        result = {
            "project_id": project_id,
            "near_event_id": near_event_id,
            "returned_count": len(timeline_items),
            "timeline": timeline_items,
            "budget_tokens": max_tokens,
            "truncated": False,
        }

        while estimate_tokens(result) > max_tokens and len(result["timeline"]) > 1:
            result["timeline"].pop()
            result["truncated"] = True

        result["returned_count"] = len(result["timeline"])
        return _bounded(result, max_tokens, "timeline", "returned_count")


def get_memories(
    store: Store,
    project_id: str,
    memory_ids: List[str],
    max_tokens: int = 1500,
) -> Dict[str, Any]:
    """Retrieve full detail for specific memory/observation IDs, including source event links."""
    # Bounded input list
    target_ids = memory_ids[:50]
    with store.connect() as c:
        if not c.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
            raise KeyError("project not found")

        placeholders = ",".join("?" for _ in target_ids)
        obs_rows = c.execute(
            f"SELECT * FROM observations WHERE project_id=? AND id IN ({placeholders})",
            (project_id, *target_ids)
        ).fetchall()

        # Fetch source events
        sources_rows = c.execute(
            f"SELECT observation_id, event_id FROM observation_sources WHERE observation_id IN ({placeholders})",
            target_ids
        ).fetchall()

        sources_map: Dict[str, List[str]] = {}
        for sr in sources_rows:
            sources_map.setdefault(sr["observation_id"], []).append(sr["event_id"])

        memories = []
        for r in obs_rows:
            memories.append({
                "id": r["id"],
                "project_id": r["project_id"],
                "type": r["type"],
                "title": r["title"],
                "narrative": r["narrative"],
                "evidence_level": r["evidence_level"],
                "created_at": r["created_at"],
                "source_event_ids": sources_map.get(r["id"], []),
            })

        response = {
            "project_id": project_id,
            "memories": memories,
            "total_requested": len(memory_ids),
            "total_returned": len(memories),
            "budget_tokens": max_tokens,
            "truncated": False,
        }

        while estimate_tokens(response) > max_tokens and len(response["memories"]) > 1:
            response["memories"].pop()
            response["truncated"] = True

        if len(response["memories"]) < len(memory_ids):
            response["truncated"] = True

        response["total_returned"] = len(response["memories"])
        return _bounded(response, max_tokens, "memories", "total_returned")


def export_all_projects(store: Store) -> Dict[str, Any]:
    """Exhaustive, deterministic enumeration of all registered projects.
    Follows all pages from list_projects snapshot to guarantee 100% completeness.
    """
    initial = store.list_projects(limit=100)
    snapshot = initial["snapshot"]
    all_projects = list(initial["projects"])
    next_offset = initial["next_offset"]

    while next_offset is not None:
        page = store.list_projects(snapshot=snapshot, offset=next_offset, limit=100)
        all_projects.extend(page["projects"])
        next_offset = page["next_offset"]

    return {
        "snapshot": snapshot,
        "snapshot_at": initial["snapshot_at"],
        "revision": initial["revision"],
        "total_projects": len(all_projects),
        "projects": all_projects,
        "coverage": initial["coverage"],
        "completeness": "All registered projects at this snapshot; discovery/unsent offline projects may be missing.",
    }
