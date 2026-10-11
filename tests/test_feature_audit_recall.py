"""Isolated acceptance gaps for recall-tool dispatch and reinjection."""
import json
import os
import subprocess
import sys

import pytest

from bridge.cache import LocalCache
from bridge.cli import build_parser, cmd_hook
from bridge.outbox import Outbox
from tests.test_project_recall import store


def test_all_four_recall_tools_over_stdio(store):
    names = {"list_all_projects", "get_project_context", "get_project_status", "search_project_work"}
    requests = [{"jsonrpc": "2.0", "id": 0, "method": "tools/list"}]
    for number, name in enumerate(sorted(names), 1):
        args = {} if name == "list_all_projects" else {"project": "p-a"}
        if name == "search_project_work":
            args["query"] = "401"
        requests.append({"jsonrpc": "2.0", "id": number, "method": "tools/call",
                         "params": {"name": name, "arguments": args}})
    env = {**os.environ, "HERMES_UPSTREAM_DB": str(store.database),
           "HERMES_PROJECT_NAMES": str(store.names_file)}
    result = subprocess.run([sys.executable, "-B", "scripts/secretary_projects_mcp.py"],
                            env=env, input="\n".join(json.dumps(r) for r in requests) + "\n",
                            capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert {tool["name"] for tool in replies[0]["result"]["tools"]} == names
    for reply in replies[1:]:
        assert "isError" not in reply["result"]
        assert json.loads(reply["result"]["content"][0]["text"])


def test_all_continuation_fields_and_source_ids(store):
    work = store.context({"project": "p-a", "work_id": "session:1"})["subwork"][0]
    assert work["goal"] == "Build login"
    assert work["recorded_progress"] == "Login attempted"
    assert work["learned"] == "API returns 401"
    assert work["notes"] == "Not deployed"
    assert work["next_step"] == "Fix token"
    assert work["source"] == "codex"
    assert work["summary_id"] == 1
    assert work["evidence"][0]["observation_id"] == 1


@pytest.mark.parametrize("platform", ["codex", "claude_code"])
@pytest.mark.parametrize("source", ["resume", "compact"])
def test_resume_compaction_reinject_unchanged_context(tmp_path, monkeypatch, capsys, platform, source):
    cache = LocalCache(tmp_path / "bridge")
    cache.save_config({"auto_sync": False})
    cache.save_cached_context("p", {"project": "p", "content": [{"type": "text", "text": "Remember auth"}]})
    (tmp_path / ".hermes_project").write_text(json.dumps({"project_id": "p", "name": "Project"}))
    box = Outbox(tmp_path / "queue.db")
    monkeypatch.setattr("bridge.cli.LocalCache", lambda: cache)
    monkeypatch.setattr("bridge.cli.get_default_outbox", lambda: box)
    monkeypatch.setattr("bridge.adapters.base.get_default_device_id", lambda: "audit")
    payload = {"session_id": "s", "cwd": str(tmp_path), "source": "startup"}
    monkeypatch.setattr("bridge.cli.read_hook_input", lambda: payload)
    args = build_parser().parse_args(["hook", platform, "SessionStart"])
    assert cmd_hook(args) == 0
    assert "Remember auth" in json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert cmd_hook(args) == 0
    assert capsys.readouterr().out == ""
    payload["source"] = source
    assert cmd_hook(args) == 0
    assert "Remember auth" in json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("platform,hook", [("codex", "UserPromptSubmit"), ("claude_code", "UserPromptSubmit"), ("agy", "PreInvocation")])
def test_changed_memory_reinjects_in_same_session(tmp_path, monkeypatch, capsys, platform, hook):
    cache = LocalCache(tmp_path / "bridge")
    cache.save_config({"auto_sync": False})
    (tmp_path / ".hermes_project").write_text(json.dumps({"project_id": "p", "name": "Project"}))
    box = Outbox(tmp_path / "queue.db")
    monkeypatch.setattr("bridge.cli.LocalCache", lambda: cache)
    monkeypatch.setattr("bridge.cli.get_default_outbox", lambda: box)
    monkeypatch.setattr("bridge.adapters.base.get_default_device_id", lambda: "audit")
    monkeypatch.setattr("bridge.cli.read_hook_input", lambda: {"session_id": "s", "cwd": str(tmp_path), "prompt": "Continue"})
    args = build_parser().parse_args(["hook", platform, hook])
    for text in ("Earlier memory", "New observation"):
        cache.save_cached_context("p", {"project": "p", "content": [{"type": "text", "text": text}]})
        assert cmd_hook(args) == 0
        output = json.loads(capsys.readouterr().out)
        context = output["injectSteps"][0]["ephemeralMessage"] if platform == "agy" else output["hookSpecificOutput"]["additionalContext"]
        assert text in context
