import pytest
from scripts.connect_local_memory import with_instructions, remote_args
from scripts.retire_local_claudemem import retire
from bridge.adapters.claude_code import ClaudeCodeAdapter
from bridge.adapters.codex import CodexAdapter
from bridge.adapters.agy import AntigravityAdapter
from bridge.outbox import Outbox


def test_instructions_preserve_existing_rules_and_are_idempotent():
    text = with_instructions("Keep my custom rules.\n")
    assert text.startswith("Keep my custom rules.")
    assert with_instructions(text) == text
    assert "Work stays in this local agent" in text
    assert "get_project_context once" in text


def test_remote_transport_has_no_local_worker():
    args = remote_args("owner@memory.example", "/home/owner/.claude-mem/scripts/secretary_projects_mcp.py")
    assert "BatchMode=yes" in args
    assert args[-3:] == ["python3", "-u", "/home/owner/.claude-mem/scripts/secretary_projects_mcp.py"]
    assert not any("worker-service" in a for a in args)
    with pytest.raises(ValueError):
        remote_args("-oProxyCommand=bad", "/tmp/script.py")
    with pytest.raises(ValueError):
        remote_args("owner@host", "/tmp/script.py;bad")


def test_agy_actual_skill_directory_is_retired(tmp_path):
    for name in ("claudemem", "mem-search", "other-skill"):
        p = tmp_path / ".gemini/config/skills" / name
        p.mkdir(parents=True)
        (p / "SKILL.md").write_text(name)
    report = retire(tmp_path, apply=True)
    assert len(report["skills_to_archive"]) == 2
    assert (tmp_path / ".gemini/config/skills/other-skill/SKILL.md").is_file()
    assert not (tmp_path / ".gemini/config/skills/claudemem").exists()


@pytest.mark.parametrize("adapter_cls", [ClaudeCodeAdapter, CodexAdapter, AntigravityAdapter])
@pytest.mark.parametrize("prefix", ["mcp__hermes-memory__", "mcp__hermes_memory__"])
def test_memory_reads_are_not_recaptured(tmp_path, monkeypatch, adapter_cls, prefix):
    adapter = adapter_cls(outbox=Outbox(tmp_path / "outbox.db"), device_id="test")
    def unexpected_resolution(*args, **kwargs):
        pytest.fail("Memory reads should skip project resolution and enqueue")
    with monkeypatch.context() as patch:
        patch.setattr("bridge.adapters.base.resolve_project", unexpected_resolution)
        patch.setattr(adapter.outbox, "enqueue", unexpected_resolution)
        for name in ("list_all_projects", "get_project_context", "get_project_status", "search_project_work"):
            for hook in ("PreToolUse", "PostToolUse"):
                event = adapter.handle_hook(hook, {
                    "session_id": "s", "tool_name": prefix + name,
                    "tool_response": "Historical project context",
                }, cwd=str(tmp_path))
                assert event is None
    # Unqualified tools and other servers still represent capturable work.
    for name in ("get_project_context", "mcp__other__get_project_context", prefix + "write_note"):
        assert adapter.handle_hook("PostToolUse", {
            "session_id": "s", "tool_name": name, "tool_response": "new work",
        }, cwd=str(tmp_path)) is not None
