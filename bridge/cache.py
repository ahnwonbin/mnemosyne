"""Local persistent registry, alias store, and context cache for Hermes Bridge.
Handles offline caching, session injection deduplication, and local configuration.
"""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from bridge.worker import file_lock


def get_bridge_dir() -> Path:
    """Get the base bridge directory in ~/.hermes/bridge."""
    d = Path(os.environ.get("HERMES_BRIDGE_DIR", str(Path.home() / ".hermes" / "bridge")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """Replace JSON atomically so readers never see a partial document."""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False)
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


class LocalCache:
    """Manages local alias persistence, context caching, and session tracking."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or get_bridge_dir()
        self.aliases_file = self.base_dir / "aliases.json"
        self.config_file = self.base_dir / "config.json"
        self.cache_dir = self.base_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_file = self.base_dir / "sessions.json"

    def get_config(self) -> Dict[str, Any]:
        """Read local config, or return defaults."""
        if self.config_file.is_file():
            try:
                return json.loads(self.config_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def save_config(self, cfg: Dict[str, Any]) -> None:
        """Write local config."""
        self.config_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def get_token(self) -> Optional[str]:
        """Retrieve token from env or local config. No hardcoded default token."""
        return os.environ.get("HERMES_DEVICE_TOKEN") or self.get_config().get("token")

    def get_read_token(self) -> Optional[str]:
        return os.environ.get("HERMES_READ_TOKEN") or self.get_config().get("read_token")

    def get_endpoint(self) -> str:
        """Retrieve endpoint from env or local config, defaulting to loopback."""
        return os.environ.get("HERMES_SYNC_URL") or self.get_config().get("endpoint") or "http://127.0.0.1:8787"

    def get_known_aliases(self) -> Dict[str, Dict[str, Any]]:
        """Read persistent path and git aliases."""
        if self.aliases_file.is_file():
            try:
                return json.loads(self.aliases_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def save_alias(self, path: str, project_id: str, name: str) -> None:
        """Save a path alias mapping."""
        aliases = self.get_known_aliases()
        aliases[path.lower().rstrip("/")] = {
            "project_id": project_id,
            "name": name,
        }
        self.aliases_file.write_text(json.dumps(aliases, indent=2), encoding="utf-8")

    def get_cached_context(self, project_id: str) -> Optional[Dict[str, Any]]:
        """Read cached project context."""
        cache_path = self.cache_dir / f"{project_id}.json"
        if cache_path.resolve().parent != self.cache_dir.resolve():
            raise ValueError("invalid cache project identifier")
        if cache_path.is_file():
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None

    def save_cached_context(self, project_id: str, context_data: Dict[str, Any]) -> None:
        """Save project context to local disk cache."""
        cache_path = self.cache_dir / f"{project_id}.json"
        if cache_path.resolve().parent != self.cache_dir.resolve():
            raise ValueError("invalid cache project identifier")
        _write_json_atomic(cache_path, context_data)

    def _get_session_states(self) -> Dict[str, Any]:
        if self.sessions_file.is_file():
            try:
                return json.loads(self.sessions_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def should_inject_context(self, session_key: str, project_id: str, current_version: Optional[int]) -> bool:
        """Determine if context should be injected.
        Only injects on session start, after an actual memory change, or when resuming.
        Avoids repeatedly appending unchanged packets.
        """
        states = self._get_session_states()
        sess_data = states.get(session_key)
        if not sess_data:
            return True
        if sess_data.get("project_id") != project_id:
            return True
        last_version = sess_data.get("injected_version")
        if current_version is not None and current_version != last_version:
            return True
        return False

    def mark_injected(self, session_key: str, project_id: str, version: Optional[int]) -> None:
        """Record that context was injected for a session."""
        with file_lock(self.base_dir / "sessions.lock", blocking=True):
            self._mark_injected(session_key, project_id, version)

    def _mark_injected(self, session_key, project_id, version):
        states = self._get_session_states()
        states[session_key] = {
            "project_id": project_id,
            "injected_version": version,
        }
        # Keep only last 100 sessions
        if len(states) > 100:
            keys_to_remove = list(states.keys())[:-100]
            for k in keys_to_remove:
                del states[k]
        _write_json_atomic(self.sessions_file, states)


def format_context_prompt(ctx: Dict[str, Any], max_chars: int = 3200) -> str:
    """Format compact context into standard memory instruction banner."""
    stale_banner = ""
    if ctx.get("stale") or ctx.get("is_stale"):
        stale_banner = "\n[NOTE: Cached offline context — upstream refresh pending]"

    # Support upstream Claude-Mem context envelope
    if "content" in ctx and isinstance(ctx["content"], list):
        text_parts = [
            item.get("text", "")
            for item in ctx["content"]
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        body = "\n".join(part for part in text_parts if part).strip()
        if body:
            if len(body) > max_chars:
                body = body[:max_chars].rstrip() + "\n[... truncated remaining context to fit budget]"
            proj = ctx.get("project") or "Current Project"
            return f"[Hermes Memory: {proj}]{stale_banner}\n{body}"

    name = ctx.get("name", "Project")
    phase = ctx.get("phase") or "planning"
    next_action = ctx.get("next_action") or "none specified"
    progress = ctx.get("progress_percent")
    progress_str = f"{progress}%" if progress is not None else "not planned"

    summary_text = ""
    summary_obj = ctx.get("summary")
    if summary_obj and isinstance(summary_obj, dict):
        summary_text = summary_obj.get("body", "")

    observations = ctx.get("recent_observations", [])
    obs_lines = []
    for obs in observations[:5]:
        obs_lines.append(f"- [{obs.get('type', 'note')}] {obs.get('title', '')}")

    obs_block = "\n".join(obs_lines) if obs_lines else "- No recorded observations yet"

    out = (
        f"[Hermes Memory: {name}]{stale_banner}\n"
        f"Phase: {phase} | Next: {next_action} | Progress: {progress_str}\n"
        f"Summary: {summary_text}\n"
        f"Recent observations:\n{obs_block}"
    )
    if len(out) > max_chars:
        out = out[:max_chars].rstrip() + "\n[... truncated remaining context to fit budget]"
    return out
