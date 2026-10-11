"""Historical memory migration and backup manager.
Imports historical Claude-Mem prompts, observations, and summaries into
Hermes second-brain events with preserved provenance and rollback backups.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Dict, List, Optional
import uuid

from bridge.identity import HERMES_PROJECT_NAMESPACE
from bridge.outbox import Outbox, get_default_device_id
from bridge.sanitizer import sanitize_payload


class HistoricalMigrator:
    """Migrates historical records to Hermes bridge outbox events."""

    def __init__(self, outbox: Outbox, device_id: Optional[str] = None):
        self.outbox = outbox
        self.device_id = device_id or get_default_device_id()

    @staticmethod
    def backup_legacy_stores(source_paths: List[Path], backup_dir: Path) -> Dict[str, Any]:
        """Create restorable timestamped backups of existing memory and configuration files."""
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        backed_up = []

        for p in source_paths:
            path_obj = Path(p).resolve()
            if path_obj.exists():
                identity = hashlib.sha256(str(path_obj).encode()).hexdigest()[:12]
                dest = backup_dir / f"{path_obj.name}.{identity}.bak_{timestamp}"
                if path_obj.is_dir():
                    shutil.copytree(path_obj, dest)
                else:
                    shutil.copy2(path_obj, dest)
                backed_up.append({"source": str(path_obj), "backup": str(dest)})

        return {"timestamp": timestamp, "backup_count": len(backed_up), "backups": backed_up}

    def migrate_turn_bundle(
        self,
        turn_data: Dict[str, Any],
        project_mapping: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Migrate a single Claude-Mem turn bundle into Hermes events.
        Preserves original timestamps, platforms, and observation details.
        """
        project_mapping = project_mapping or {}
        turn_id = str(turn_data.get("turn_id") or uuid.uuid5(
            HERMES_PROJECT_NAMESPACE, json.dumps(turn_data, sort_keys=True, ensure_ascii=False)))
        platform = turn_data.get("platform", "migration")
        legacy_project = turn_data.get("project", "default")
        timestamp = turn_data.get("timestamp") or datetime.now(timezone.utc).isoformat()

        # Map project ID
        project_id = project_mapping.get(legacy_project)
        if not project_id:
            project_id = str(uuid.uuid5(HERMES_PROJECT_NAMESPACE, f"legacy_project:{legacy_project}"))

        enqueued_events: List[str] = []

        # 1. Migrate human prompt
        prompt_text = turn_data.get("prompt")
        if prompt_text:
            clean_payload, notes = sanitize_payload({
                "prompt": prompt_text,
                "_context": {"project_name": legacy_project, "legacy_turn_id": turn_id, "legacy_platform": platform},
            })
            source_id = f"claudemem:turn:{turn_id}:prompt"
            event = self.outbox.enqueue(
                device_id=self.device_id,
                project_id=project_id,
                platform="migration",
                native_session_id=turn_id,
                event_type="prompt_submit",
                raw_payload=clean_payload,
                source_identity=source_id,
                event_id=str(uuid.uuid5(HERMES_PROJECT_NAMESPACE, f"{self.device_id}:{project_id}:{source_id}")),
                occurred_at=timestamp,
            )
            enqueued_events.append(event.event_id)

        # 2. Migrate observations
        for idx, obs in enumerate(turn_data.get("observations", [])):
            clean_payload, notes = sanitize_payload({
                "observation": obs,
                "_context": {"project_name": legacy_project, "legacy_turn_id": turn_id, "legacy_platform": platform},
            })
            source_id = f"claudemem:turn:{turn_id}:obs:{idx}"
            event = self.outbox.enqueue(
                device_id=self.device_id,
                project_id=project_id,
                platform="migration",
                native_session_id=turn_id,
                event_type="tool_result",
                raw_payload=clean_payload,
                source_identity=source_id,
                event_id=str(uuid.uuid5(HERMES_PROJECT_NAMESPACE, f"{self.device_id}:{project_id}:{source_id}")),
                occurred_at=obs.get("timestamp") or timestamp,
            )
            enqueued_events.append(event.event_id)

        # 3. Migrate summary
        summary = turn_data.get("summary")
        if summary and isinstance(summary, dict):
            clean_payload, notes = sanitize_payload({
                "response": json.dumps(summary, ensure_ascii=False),
                "_context": {"project_name": legacy_project, "legacy_turn_id": turn_id, "legacy_platform": platform},
            })
            source_id = f"claudemem:turn:{turn_id}:summary"
            event = self.outbox.enqueue(
                device_id=self.device_id,
                project_id=project_id,
                platform="migration",
                native_session_id=turn_id,
                event_type="assistant_response",
                raw_payload=clean_payload,
                source_identity=source_id,
                event_id=str(uuid.uuid5(HERMES_PROJECT_NAMESPACE, f"{self.device_id}:{project_id}:{source_id}")),
                occurred_at=timestamp,
            )
            enqueued_events.append(event.event_id)

        return {
            "turn_id": turn_id,
            "project_id": project_id,
            "legacy_project": legacy_project,
            "events_enqueued": len(enqueued_events),
            "event_ids": enqueued_events,
        }

    def migrate_json_file(
        self,
        file_path: Path,
        project_mapping: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Read a JSON bundle file (single turn or array of turns) and migrate."""
        content = json.loads(file_path.read_text(encoding="utf-8"))
        if isinstance(content, list):
            results = [self.migrate_turn_bundle(t, project_mapping) for t in content]
        else:
            results = [self.migrate_turn_bundle(content, project_mapping)]

        total_events = sum(r["events_enqueued"] for r in results)
        return {
            "source_file": str(file_path),
            "turns_processed": len(results),
            "total_events_enqueued": total_events,
            "results": results,
        }
