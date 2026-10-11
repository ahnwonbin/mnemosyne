"""Safe, reversible hook installer and diagnostic probes for Claude Code, Codex, and Agy.
Backs up existing configuration files before modification. Probes use synthetic payloads.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
from typing import Any, Dict, List, Optional

from bridge.adapters.claude_code import ClaudeCodeAdapter
from bridge.adapters.codex import CodexAdapter
from bridge.adapters.agy import AntigravityAdapter
from bridge.identity import resolve_project
from bridge.outbox import Outbox


HOOK_MAPPINGS = {
    "claude_code": [
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
        "SessionEnd",
    ],
    "codex": [
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SessionEnd",
    ],
    "agy": [
        "PreInvocation",
        "PreToolUse",
        "PostToolUse",
        "PostInvocation",
        "Stop",
    ],
}


def get_hook_config_paths() -> Dict[str, Path]:
    """Get standard configuration paths for each client."""
    home = Path.home()
    return {
        "claude_code": home / ".claude" / "settings.json",
        "codex": home / ".codex" / "hooks.json",
        "agy": home / ".gemini" / "config" / "hooks.json",
    }


def install_hooks(platform: str, python_exe: Optional[str] = None) -> Dict[str, Any]:
    """Install Hermes Bridge hooks for the specified platform, backing up existing configuration."""
    platform = platform.lower()
    if platform not in HOOK_MAPPINGS:
        raise ValueError(f"Unknown platform '{platform}'. Must be one of: {list(HOOK_MAPPINGS.keys())}")

    target_path = get_hook_config_paths()[platform]
    target_path.parent.mkdir(parents=True, exist_ok=True)

    py_cmd = python_exe or sys.executable
    manifest_path = target_path.with_name(target_path.name + ".hermes-install.json")
    config_content = json.loads(target_path.read_text(encoding="utf-8")) if target_path.exists() else {}
    if not isinstance(config_content, dict):
        raise ValueError("Hook configuration must be an object; existing file preserved")
    hooks = config_content if platform == "agy" else config_content.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Invalid hooks object; existing file preserved")
    backup_path = None

    hooks_list = HOOK_MAPPINGS[platform]
    hook_definitions: Dict[str, Any] = {}
    runner = str(Path(__file__).with_name("hook_entry.py").resolve())
    for h in hooks_list:
        if not isinstance(hooks.get(h, []), list):
            raise ValueError(f"Invalid {h} hook groups; existing file preserved")
        argv = [py_cmd, runner, "hook", platform, h]
        handler = {"type": "command", "timeout": 3 if h == "SessionEnd" else 10}
        if platform == "claude_code":
            handler.update(command=py_cmd, args=argv[1:])
        elif platform == "agy":
            # Agy splits on spaces without shell unquoting.
            if any(any(ch.isspace() for ch in arg) for arg in argv):
                raise ValueError("Agy requires Python and bridge paths without spaces")
            handler.update(name="hermes-bridge", command=" ".join(a.replace("\\", "/") for a in argv), timeout=10000)
        else:
            handler.update(command=shlex.join(argv), commandWindows=subprocess.list2cmdline(argv))
        group = {"hooks": [handler], "matcher": "*"}
        hook_definitions[h] = group

    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("platform") != platform:
            raise ValueError("Hook ownership mismatch")
        if all(group in hooks.get(event, []) for event, group in manifest["groups"].items()):
            return {"platform": platform, "installed_path": str(target_path),
                    "backup_path": manifest.get("backup_file"), "hooks_count": len(hooks_list),
                    "status": "already_installed"}
        raise ValueError("Installed hooks changed; uninstall before reinstalling")

    if target_path.exists():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        backup_path = target_path.parent / f"{target_path.name}.hermes_bak_{timestamp}"
        shutil.copy2(target_path, backup_path)
    for event, group in hook_definitions.items():
        hooks.setdefault(event, []).append(group)
    manifest = {"platform": platform, "groups": hook_definitions,
                "backup_file": str(backup_path) if backup_path else None}
    # Metadata lives outside the native config, especially Agy's strict event map.
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    target_path.write_text(json.dumps(config_content, indent=2), encoding="utf-8")

    return {
        "platform": platform,
        "installed_path": str(target_path),
        "backup_path": str(backup_path) if backup_path else None,
        "hooks_count": len(hooks_list),
        "status": "installed",
    }


def uninstall_hooks(platform: str) -> Dict[str, Any]:
    """Revert installed hooks for the specified platform by restoring backup or removing file."""
    platform = platform.lower()
    if platform not in HOOK_MAPPINGS:
        raise ValueError(f"Unknown platform '{platform}'")

    target_path = get_hook_config_paths()[platform]
    if not target_path.exists():
        return {"platform": platform, "status": "not_installed"}

    manifest_path = target_path.with_name(target_path.name + ".hermes-install.json")
    if not manifest_path.is_file():
        return {"platform": platform, "status": "not_installed"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("platform") != platform:
        raise ValueError("Hook ownership mismatch")
    data = json.loads(target_path.read_text(encoding="utf-8"))
    backup_file = manifest.get("backup_file")
    if backup_file and not Path(backup_file).is_file():
        raise FileNotFoundError("Hook backup is missing; current configuration preserved")
    hooks = data if platform == "agy" else data.get("hooks", {})
    original = json.loads(Path(backup_file).read_text(encoding="utf-8")) if backup_file else {}
    original_hooks = original if platform == "agy" else original.get("hooks", {})
    for event, group in manifest["groups"].items():
        if group in hooks.get(event, []):
            hooks[event].remove(group)
            if not hooks[event] and event not in original_hooks:
                del hooks[event]
    if platform != "agy" and not hooks and "hooks" not in original:
        data.pop("hooks", None)
    if backup_file and data == original:
        shutil.copy2(backup_file, target_path)
        status = "reverted_from_backup"
    elif not data and not backup_file:
        target_path.unlink()
        status = "removed"
    else:
        target_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        status = "removed"
    manifest_path.unlink()
    return {"platform": platform, "status": status}


def run_probe(platform: str, workspace_dir: Path, outbox: Optional[Outbox] = None) -> Dict[str, Any]:
    """Probe hook handlers for a given platform using synthetic payloads and check verification."""
    platform = platform.lower()
    if platform not in HOOK_MAPPINGS:
        raise ValueError(f"Unsupported probe platform: {platform}")

    test_outbox = outbox or Outbox(workspace_dir / "probe_outbox.db")
    results: List[Dict[str, Any]] = []

    # 1. Project identity probe
    proj = resolve_project(workspace_dir)
    results.append({
        "check": "project_identity",
        "project_id": proj.project_id,
        "name": proj.name,
        "success": bool(proj.project_id and proj.name),
    })

    # 2. Platform adapter probe
    adapter = None
    if platform == "claude_code":
        adapter = ClaudeCodeAdapter(outbox=test_outbox, device_id="probe-device")
        p_event = adapter.handle_hook("UserPromptSubmit", {
            "session_id": "probe-sess-claude",
            "cwd": str(workspace_dir),
            "prompt": "Test Claude Code probe prompt",
        })
        results.append({
            "check": "prompt_hook",
            "event_type": p_event.event_type if p_event else None,
            "success": p_event is not None and p_event.event_type == "prompt_submit",
        })
        t_event = adapter.handle_hook("PostToolUse", {
            "session_id": "probe-sess-claude",
            "cwd": str(workspace_dir),
            "tool_name": "Bash",
            "tool_input": {"command": "echo probe"},
            "tool_response": {"exit_code": 0, "stdout": "probe"},
        })
        results.append({
            "check": "tool_hook",
            "event_type": t_event.event_type if t_event else None,
            "success": t_event is not None and t_event.sanitized_payload["success"] is True,
        })

    elif platform == "codex":
        adapter = CodexAdapter(outbox=test_outbox, device_id="probe-device")
        p_event = adapter.handle_hook("UserPromptSubmit", {
            "session_id": "probe-sess-codex",
            "cwd": str(workspace_dir),
            "prompt": "Test Codex probe prompt",
        })
        results.append({
            "check": "prompt_hook",
            "event_type": p_event.event_type if p_event else None,
            "success": p_event is not None and p_event.event_type == "prompt_submit",
        })
        t_event = adapter.handle_hook("PostToolUse", {
            "session_id": "probe-sess-codex",
            "cwd": str(workspace_dir),
            "tool_name": "shell",
            "tool_input": {"command": "echo probe"},
            "tool_response": {"exit_code": 0, "stdout": "probe"},
        })
        results.append({
            "check": "tool_hook",
            "event_type": t_event.event_type if t_event else None,
            "success": t_event is not None and t_event.sanitized_payload["success"] is True,
        })

    elif platform == "agy":
        transcript = workspace_dir / "probe_transcript.jsonl"
        transcript.write_text(json.dumps({
            "type": "USER_INPUT",
            "step_index": 1,
            "content": "<USER_REQUEST>Test Agy probe</USER_REQUEST>",
        }) + "\n", encoding="utf-8")
        adapter = AntigravityAdapter(outbox=test_outbox, device_id="probe-device")
        p_event = adapter.handle_hook("PreInvocation", {
            "conversationId": "probe-conv-agy",
            "workspacePaths": [str(workspace_dir)],
            "transcriptPath": str(transcript),
            "invocationNum": 1,
        })
        results.append({
            "check": "prompt_hook",
            "event_type": p_event.event_type if p_event else None,
            "success": p_event is not None and p_event.sanitized_payload.get("prompt") == "Test Agy probe",
        })
        t_event = adapter.handle_hook("PostToolUse", {
            "conversationId": "probe-conv-agy",
            "workspacePaths": [str(workspace_dir)],
            "stepIdx": 2,
            "toolCall": {"name": "run_command", "args": {"command": "echo probe"}},
        })
        results.append({
            "check": "tool_hook",
            "event_type": t_event.event_type if t_event else None,
            "success": t_event is not None and t_event.sanitized_payload.get("tool") == "run_command",
        })

    # 3. Outbox status check
    stats = test_outbox.get_stats()
    all_passed = all(r.get("success", False) for r in results)

    return {
        "platform": platform,
        "checks": results,
        "outbox_stats": stats,
        "all_passed": all_passed,
    }
