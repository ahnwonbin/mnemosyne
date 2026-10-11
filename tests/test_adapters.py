"""Unit tests for Claude Code, Codex, and Agy capture adapters."""

from pathlib import Path
from bridge.adapters.claude_code import ClaudeCodeAdapter
from bridge.adapters.codex import CodexAdapter
from bridge.adapters.agy import AntigravityAdapter
from bridge.outbox import Outbox


def test_claude_code_adapter_flow(tmp_path: Path):
    db_file = tmp_path / "outbox.db"
    outbox = Outbox(db_file)
    adapter = ClaudeCodeAdapter(outbox=outbox, device_id="test-dev")

    workspace = tmp_path / "workspace_claude"
    workspace.mkdir()

    # 1. Session start
    evt_start = adapter.handle_hook("SessionStart", {"sessionId": "c-sess-1", "matcher": "startup"}, cwd=str(workspace))
    assert evt_start is not None
    assert evt_start.event_type == "session_start"
    assert evt_start.platform == "claude_code"
    assert evt_start.session_sequence == 1

    # 2. Prompt submit with secret
    evt_prompt = adapter.handle_hook(
        "UserPromptSubmit",
        {"sessionId": "c-sess-1", "prompt": "Please test key sk-proj-1234567890123456789012345678901234567890123456"},
        cwd=str(workspace),
    )
    assert evt_prompt.event_type == "prompt_submit"
    assert evt_prompt.session_sequence == 2
    assert "[REDACTED_OPENAI_KEY]" in evt_prompt.sanitized_payload["prompt"]

    # 3. PreToolUse
    evt_pre = adapter.handle_hook(
        "PreToolUse",
        {"sessionId": "c-sess-1", "tool": "Read", "input": {"file_path": "main.py"}},
        cwd=str(workspace),
    )
    assert evt_pre.event_type == "tool_call"
    assert evt_pre.sanitized_payload["tool"] == "Read"

    # 4. PostToolUse (success)
    evt_post = adapter.handle_hook(
        "PostToolUse",
        {"sessionId": "c-sess-1", "tool": "Read", "output": "print('hello')"},
        cwd=str(workspace),
    )
    assert evt_post.event_type == "tool_result"
    assert evt_post.sanitized_payload["success"] is None  # No exit/status evidence in this fixture

    # 5. PostToolUse (failure)
    evt_post_err = adapter.handle_hook(
        "PostToolUse",
        {"sessionId": "c-sess-1", "tool": "Bash", "error": "command failed", "is_error": True},
        cwd=str(workspace),
    )
    assert evt_post_err.event_type == "tool_result"
    assert evt_post_err.sanitized_payload["success"] is False


def test_codex_adapter_flow(tmp_path: Path):
    db_file = tmp_path / "outbox.db"
    outbox = Outbox(db_file)
    adapter = CodexAdapter(outbox=outbox, device_id="test-dev")

    workspace = tmp_path / "workspace_codex"
    workspace.mkdir()

    # Prompt submit
    evt_prompt = adapter.handle_hook(
        "UserPromptSubmit",
        {"thread_id": "codex-th-1", "prompt": "Run tests"},
        cwd=str(workspace),
    )
    assert evt_prompt.event_type == "prompt_submit"
    assert evt_prompt.platform == "codex"
    assert evt_prompt.native_session_id == "codex-th-1"

    # Tool result with exit code != 0
    evt_tool = adapter.handle_hook(
        "PostToolUse",
        {"thread_id": "codex-th-1", "tool": "exec", "exit_code": 1, "output": "Exit with 1"},
        cwd=str(workspace),
    )
    assert evt_tool.sanitized_payload["success"] is False


def test_agy_adapter_flow(tmp_path: Path):
    db_file = tmp_path / "outbox.db"
    outbox = Outbox(db_file)
    adapter = AntigravityAdapter(outbox=outbox, device_id="test-dev")

    workspace = tmp_path / "workspace_agy"
    workspace.mkdir()

    evt_start = adapter.handle_hook(
        "start",
        {"conversation_id": "agy-conv-1", "model": "gemini-3.8-flash-high"},
        cwd=str(workspace),
    )
    assert evt_start.event_type == "session_start"
    assert evt_start.platform == "agy"
    assert evt_start.sanitized_payload["model"] == "gemini-3.8-flash-high"
