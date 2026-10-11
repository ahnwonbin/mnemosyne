import json
import pytest
from bridge.adapters.codex import CodexAdapter
from bridge.adapters.agy import AntigravityAdapter
from bridge.adapters.claude_code import ClaudeCodeAdapter
from bridge.outbox import Outbox
from bridge.sanitizer import sanitize_payload

@pytest.mark.parametrize("adapter",[CodexAdapter,ClaudeCodeAdapter])
def test_native_tool_fields_and_failures(tmp_path,adapter):
    instance=adapter(Outbox(tmp_path/"out.db"),"device")
    native={"session_id":"native-session","cwd":str(tmp_path),"tool_use_id":"call-1",
            "tool_name":"Bash","tool_input":{"command":"exit 3"},
            "tool_response":{"stdout":"","stderr":"failed","exit_code":3}}
    e=instance.handle_hook("PostToolUse",native)
    assert e.sanitized_payload["input"]=={"command":"exit 3"}
    assert e.sanitized_payload["success"] is False
    assert e.sanitized_payload["exit_code"]==3
    assert instance.handle_hook("PostToolUse",native).event_id==e.event_id

def test_agy_protojson_and_public_transcript(tmp_path):
    transcript=tmp_path/"transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in [
        {"type":"USER_INPUT","step_index":1,"content":"<USER_REQUEST>Plan a database</USER_REQUEST>"},
        {"type":"THINKING","step_index":2,"content":"must never capture"},
        {"type":"PLANNER_RESPONSE","step_index":3,"content":"Planning complete; schema is next."}
    ]),encoding="utf-8")
    instance=AntigravityAdapter(Outbox(tmp_path/"out.db"),"device")
    common={"conversationId":"conversation-1","workspacePaths":[str(tmp_path)],
            "transcriptPath":str(transcript),"invocationNum":1}
    prompt=instance.handle_hook("PreInvocation",common)
    response=instance.handle_hook("PostInvocation",common)
    assert prompt.event_type=="prompt_submit"
    assert prompt.sanitized_payload["prompt"]=="Plan a database"
    assert response.event_type=="assistant_response"
    assert "schema is next" in response.sanitized_payload["response"]
    result=instance.handle_hook("PostToolUse",{**common,"stepIdx":4,"toolCall":{"name":"run_command","args":{"command":"exit 1"}},"error":"failed"})
    assert result.sanitized_payload["tool"]=="run_command"
    assert result.sanitized_payload["success"] is False

def test_missing_session_is_not_merged_into_default(tmp_path):
    adapter=CodexAdapter(Outbox(tmp_path/"out.db"),"device")
    with pytest.raises(ValueError,match="session ID"):
        adapter.handle_hook("Stop",{"cwd":str(tmp_path)})

def test_private_nested_output_and_unicode_cap():
    secret="secret-that-is-not-a-known-key-format"
    clean,notes=sanitize_payload({"input":{"file_path":"c:/repo/.env"},"output":secret})
    assert secret not in json.dumps(clean)
    clean,notes=sanitize_payload({"prompt":"before <private>hidden</private> after"})
    assert "hidden" not in clean["prompt"] and notes
    clean,notes=sanitize_payload({"output":["ภาษาไทย"*2000]*100},max_payload_bytes=1000)
    assert len(json.dumps(clean,ensure_ascii=False).encode())<=1000


def test_hook_utf8_input_ignores_windows_ansi_locale(monkeypatch):
    import io
    import sys
    from bridge.adapters.base import read_hook_input

    payload = {"output": "\u0e44\u0e17\u0e22 \u201d \U0001f600"}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    stream = io.TextIOWrapper(io.BytesIO(raw), encoding="cp1252", errors="surrogateescape")
    monkeypatch.setattr(sys, "stdin", stream)
    monkeypatch.setattr(sys, "argv", ["bridge.cli", "hook"])
    assert read_hook_input() == payload


@pytest.mark.parametrize("damaged", ["\udc9d", "\ud800"])
def test_damaged_tool_output_is_persisted_without_losing_event(tmp_path, damaged):
    outbox = Outbox(tmp_path / "out.db")
    event = outbox.enqueue(
        device_id="device", project_id="project", platform="codex",
        native_session_id="session", event_type="tool_result", source_identity="test",
        raw_payload={"output": "before " + damaged + " after \u0e44\u0e17\u0e22 \U0001f600",
                     "password": "still-secret"},
    )
    stored = outbox.get_pending_batch()[0]
    assert stored.event_id == event.event_id
    assert stored.sanitized_payload["output"] == "before \ufffd after \u0e44\u0e17\u0e22 \U0001f600"
    assert stored.sanitized_payload["password"] == "[REDACTED_BY_KEY_NAME]"
    assert "Normalized surrogate characters" in stored.sanitized_payload["_omissions"]


def test_explicit_surrogate_pair_preserves_emoji():
    clean, _ = sanitize_payload({"output": "\ud83d\ude00"})
    assert clean["output"] == "\U0001f600"

def test_codex_followup_prompt_in_same_turn_is_captured_and_retry_is_deduplicated(tmp_path):
    adapter = CodexAdapter(Outbox(tmp_path / 'followup.db'), 'device')
    common = {'session_id': 'session', 'turn_id': 'turn', 'cwd': str(tmp_path)}
    first = adapter.handle_hook('UserPromptSubmit', {**common, 'prompt': 'First instruction'})
    second_payload = {**common, 'prompt': 'New instruction in the same turn'}
    second = adapter.handle_hook('UserPromptSubmit', second_payload)
    retry = adapter.handle_hook('UserPromptSubmit', second_payload)
    assert first.event_id != second.event_id
    assert retry.event_id == second.event_id
    assert adapter.outbox.get_stats()['total'] == 2


def test_explicit_occurrence_id_conflicts_are_still_rejected(tmp_path):
    adapter = CodexAdapter(Outbox(tmp_path / 'explicit.db'), 'device')
    common = {'session_id': 'session', 'event_id': 'native-event', 'cwd': str(tmp_path)}
    adapter.handle_hook('UserPromptSubmit', {**common, 'prompt': 'First instruction'})
    with pytest.raises(ValueError, match='event ID reused'):
        adapter.handle_hook('UserPromptSubmit', {**common, 'prompt': 'Conflicting retry'})
