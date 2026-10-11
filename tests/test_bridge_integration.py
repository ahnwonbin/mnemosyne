"""Acceptance tests for bridge CLI, hook context injection, local cache, installer, and probes."""
from __future__ import annotations
import json
from pathlib import Path
import sys
import pytest

from bridge.cache import LocalCache
from bridge.cli import cmd_hook, cmd_sync, build_parser
from bridge.installer import install_hooks, uninstall_hooks, run_probe, HOOK_MAPPINGS
from bridge.outbox import Outbox


@pytest.fixture
def bridge_env(tmp_path, monkeypatch):
    base_dir = tmp_path / "hermes_bridge"
    base_dir.mkdir(parents=True, exist_ok=True)
    cache = LocalCache(base_dir=base_dir)

    outbox_path = base_dir / "outbox.db"
    outbox = Outbox(outbox_path)

    monkeypatch.setattr("bridge.cli.get_default_outbox", lambda: outbox)
    monkeypatch.setattr("bridge.cache.get_bridge_dir", lambda: base_dir)

    return base_dir, cache, outbox


def test_hook_respects_payload_cwd_and_workspace_paths(bridge_env, tmp_path, monkeypatch):
    base_dir, cache, outbox = bridge_env
    custom_ws = tmp_path / "custom_project"
    custom_ws.mkdir()
    (custom_ws / ".hermes_project").write_text(json.dumps({"project_id": "proj-custom", "name": "CustomProject"}))

    # Test with payload containing cwd
    payload = {
        "session_id": "sess-cwd",
        "cwd": str(custom_ws),
        "prompt": "Working in custom dir",
    }
    monkeypatch.setattr("bridge.cli.read_hook_input", lambda: payload)

    parser = build_parser()
    args = parser.parse_args(["hook", "codex", "UserPromptSubmit"])
    res = cmd_hook(args)
    assert res == 0

    # Verify event was resolved with custom_project's project_id
    stats = outbox.get_stats()
    assert stats["pending"] == 1
    events = outbox.get_pending_batch()
    assert events[0].project_id == "proj-custom"
    assert events[0].sanitized_payload["_context"]["project_name"] == "CustomProject"


def test_hook_respects_agy_workspace_paths(bridge_env, tmp_path, monkeypatch):
    base_dir, cache, outbox = bridge_env
    agy_ws = tmp_path / "agy_project"
    agy_ws.mkdir()
    (agy_ws / ".hermes_project").write_text(json.dumps({"project_id": "proj-agy", "name": "AgyProject"}))

    transcript = agy_ws / "transcript.jsonl"
    transcript.write_text(json.dumps({"type": "USER_INPUT", "step_index": 1, "content": "Agy prompt"}) + "\n")

    payload = {
        "conversationId": "agy-conv-1",
        "workspacePaths": [str(agy_ws)],
        "transcriptPath": str(transcript),
        "invocationNum": 1,
    }
    monkeypatch.setattr("bridge.cli.read_hook_input", lambda: payload)

    parser = build_parser()
    args = parser.parse_args(["hook", "agy", "PreInvocation"])
    res = cmd_hook(args)
    assert res == 0

    events = outbox.get_pending_batch()
    assert events[0].project_id == "proj-agy"
    assert events[0].sanitized_payload["_context"]["project_name"] == "AgyProject"


def test_context_injection_claude_code_json_and_deduplication(bridge_env, tmp_path, monkeypatch, capsys):
    base_dir, cache, outbox = bridge_env
    ws = tmp_path / "my_project"
    ws.mkdir()
    (ws / ".hermes_project").write_text(json.dumps({"project_id": "proj-injection", "name": "InjectedProject"}))

    # Pre-populate local cache with project context
    cache.save_cached_context("proj-injection", {
        "project_id": "proj-injection",
        "name": "InjectedProject",
        "phase": "active_development",
        "next_action": "write tests",
        "progress_percent": 45.0,
        "summary": {"version": 2, "body": "Summary of injected project."},
        "recent_observations": [{"type": "feature", "title": "Added auth"}],
    })

    payload = {
        "session_id": "session-1",
        "cwd": str(ws),
        "prompt": "First prompt in session",
    }
    monkeypatch.setattr("bridge.cli.read_hook_input", lambda: payload)

    # First prompt: should emit Claude Code JSON instructions to stdout
    parser = build_parser()
    args = parser.parse_args(["hook", "claude_code", "UserPromptSubmit"])
    cmd_hook(args)

    captured = capsys.readouterr()
    assert '{"hookSpecificOutput":' in captured.out
    parsed_out = json.loads(captured.out.strip())
    assert "InjectedProject" in parsed_out["hookSpecificOutput"]["additionalContext"]
    assert "write tests" in parsed_out["hookSpecificOutput"]["additionalContext"]

    # Second prompt with same summary version in same session: must NOT repeat injection
    payload2 = {
        "session_id": "session-1",
        "cwd": str(ws),
        "prompt": "Second prompt in session",
    }
    monkeypatch.setattr("bridge.cli.read_hook_input", lambda: payload2)
    cmd_hook(args)

    captured2 = capsys.readouterr()
    # Output must be empty for second call because version has not changed!
    assert captured2.out.strip() == ""


def test_context_injection_codex_text(bridge_env, tmp_path, monkeypatch, capsys):
    base_dir, cache, outbox = bridge_env
    ws = tmp_path / "codex_ws"
    ws.mkdir()
    (ws / ".hermes_project").write_text(json.dumps({"project_id": "proj-codex-test", "name": "CodexProject"}))

    cache.save_cached_context("proj-codex-test", {
        "project_id": "proj-codex-test",
        "name": "CodexProject",
        "phase": "implementation",
        "next_action": "add endpoints",
        "progress_percent": 60.0,
        "summary": {"version": 1, "body": "Codex summary"},
        "recent_observations": [],
    })

    payload = {"session_id": "codex-sess-1", "cwd": str(ws), "prompt": "Codex prompt"}
    monkeypatch.setattr("bridge.cli.read_hook_input", lambda: payload)

    parser = build_parser()
    args = parser.parse_args(["hook", "codex", "UserPromptSubmit"])
    cmd_hook(args)

    captured = capsys.readouterr()
    assert "[Hermes Memory: CodexProject]" in captured.out
    assert "add endpoints" in captured.out


def test_no_default_development_token(bridge_env, monkeypatch):
    base_dir, cache, outbox = bridge_env
    monkeypatch.delenv("HERMES_DEVICE_TOKEN", raising=False)
    # Ensure local config has no token
    cache.save_config({})

    parser = build_parser()
    args = parser.parse_args(["sync"])
    # Must fail with code 1 requiring authentication token
    assert cmd_sync(args) == 1


def test_safe_reversible_hook_installer_and_backup(tmp_path, monkeypatch):
    fake_home = tmp_path / "fake_home"
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    # 1. Install Claude Code hooks
    res = install_hooks("claude_code", python_exe="python.exe")
    assert res["status"] == "installed"
    installed_file = Path(res["installed_path"])
    assert installed_file.is_file()

    content = json.loads(installed_file.read_text(encoding="utf-8"))
    assert "UserPromptSubmit" in content["hooks"]

    # 2. Reinstallation preserves the original restore point.
    res2 = install_hooks("claude_code", python_exe="python.exe")
    assert res2["status"] == "already_installed"
    assert res2["backup_path"] == res["backup_path"]

    # 3. Uninstall reverts cleanly
    uninst = uninstall_hooks("claude_code")
    assert uninst["status"] in ("reverted_from_backup", "removed")


def test_hook_probe_across_platforms(tmp_path):
    ws = tmp_path / "probe_ws"
    ws.mkdir()
    (ws / ".hermes_project").write_text(json.dumps({"project_id": "probe-proj", "name": "ProbeWorkspace"}))

    for platform in ("claude_code", "codex", "agy"):
        res = run_probe(platform, ws)
        assert res["all_passed"] is True, f"Probe failed for {platform}: {res}"
        assert len(res["checks"]) >= 3


@pytest.mark.parametrize("platform", ["claude_code", "codex", "agy"])
def test_native_config_shape_merges_and_preserves_later_edits(tmp_path, monkeypatch, platform):
    from bridge.installer import get_hook_config_paths
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    target = get_hook_config_paths()[platform]
    target.parent.mkdir(parents=True)
    event = HOOK_MAPPINGS[platform][0]
    other_group = {"hooks": [{"type": "command", "command": "other-tool"}]}
    original = {event: [other_group]} if platform == "agy" else {"hooks": {event: [other_group]}, "other": True}
    target.write_text(json.dumps(original))
    install_hooks(platform)
    data = json.loads(target.read_text())
    hooks = data if platform == "agy" else data["hooks"]
    assert hooks[event][0] == other_group
    handler = hooks[event][1]["hooks"][0]
    assert handler["type"] == "command"
    assert isinstance(handler["command"], str)
    assert handler["timeout"] == (10000 if platform == "agy" else 10)
    if platform == "claude_code":
        assert target.name == "settings.json"
        assert isinstance(handler["args"], list)
    if platform == "agy":
        assert target.parent.name == "config"
        assert "hooks" not in data
    # Preserve settings/hooks added after installation as well as original data.
    hooks["OtherEvent"] = [other_group]
    target.write_text(json.dumps(data))
    uninstall_hooks(platform)
    restored = json.loads(target.read_text())
    restored_hooks = restored if platform == "agy" else restored["hooks"]
    assert restored_hooks[event] == [other_group]
    assert restored_hooks["OtherEvent"] == [other_group]


def test_agy_context_uses_strict_json(bridge_env, tmp_path, monkeypatch, capsys):
    _, cache, _ = bridge_env
    (tmp_path / ".hermes_project").write_text(json.dumps({"project_id": "p", "name": "Project"}))
    cache.save_cached_context("p", {"project_id": "p", "name": "Project", "summary": {"version": 1, "body": "Saved"}})
    monkeypatch.setattr("bridge.cli.read_hook_input", lambda: {"conversationId": "s", "workspacePaths": [str(tmp_path)]})
    cmd_hook(build_parser().parse_args(["hook", "agy", "PreInvocation"]))
    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"injectSteps"}
    assert "Saved" in output["injectSteps"][0]["ephemeralMessage"]
    cmd_hook(build_parser().parse_args(["hook", "agy", "PreToolUse"]))
    assert json.loads(capsys.readouterr().out) == {"decision": "allow"}


def test_absolute_hook_runner_from_another_workspace(tmp_path):
    import os
    import subprocess
    runner = Path(__file__).resolve().parents[1] / "bridge" / "hook_entry.py"
    env = os.environ.copy()
    env.update(USERPROFILE=str(tmp_path), HOME=str(tmp_path), HERMES_BRIDGE_DIR=str(tmp_path / "cache"),
               HERMES_OUTBOX=str(tmp_path / "queue.db"))
    LocalCache(tmp_path / "cache").save_config({"auto_sync": False})
    result = subprocess.run([sys.executable, str(runner), "hook", "codex", "UserPromptSubmit"],
                            cwd=tmp_path, env=env, input=json.dumps({"session_id": "runner-test", "cwd": str(tmp_path), "prompt": "Test"}),
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert Outbox(tmp_path / "queue.db").get_stats()["pending"] == 1
