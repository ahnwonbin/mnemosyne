"""Explicit native event normalization. Missing evidence remains unknown."""
import json
import hashlib
from pathlib import Path
import re
import uuid

EVENTS = {
    "SessionStart":"session_start","UserPromptSubmit":"prompt_submit",
    "PreToolUse":"tool_call","PostToolUse":"tool_result","PostToolUseFailure":"tool_result",
    "Stop":"stop","SessionEnd":"session_end","PreCompact":"pre_compact","PostCompact":"post_compact",
    "PreInvocation":"prompt_submit","PostInvocation":"assistant_response",
}
AGY_LEGACY = {"start":"SessionStart","init":"SessionStart","prompt":"UserPromptSubmit",
              "toolcall":"PreToolUse","toolresult":"PostToolUse","stop":"Stop"}

def first(data,*keys,default=None):
    return next((data[k] for k in keys if k in data and data[k] is not None),default)

def text_content(content):
    if isinstance(content,str):
        return content
    if isinstance(content,list):
        return "\n".join(v["text"] for v in content if isinstance(v,dict) and
                         v.get("type","text") in {"text","output_text","input_text"} and isinstance(v.get("text"),str))
    return ""

def last_public_message(transcript,role,platform):
    if not transcript:
        return "",None
    path=Path(transcript)
    # Read only a bounded tail. Never expose reasoning/thinking nodes.
    with path.open("rb") as f:
        f.seek(0,2)
        size=f.tell()
        start=max(0,size-2*1024*1024)
        f.seek(start)
        if start:
            f.readline()
        lines=f.read().splitlines()
    for line in reversed(lines):
        try:
            record=json.loads(line)
        except (ValueError,UnicodeDecodeError):
            continue
        if platform=="agy":
            expected="USER_INPUT" if role=="user" else "PLANNER_RESPONSE"
            if record.get("type")!=expected:
                continue
            content=text_content(record.get("content"))
            if role=="user":
                match=re.search(r"<USER_REQUEST>([\s\S]*?)</USER_REQUEST>",content)
                content=match.group(1).strip() if match else re.sub(r"<(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>[\s\S]*?</(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>","",content).strip()
            return content,record.get("step_index")
        message=record.get("message",{})
        if record.get("type")==role and isinstance(message,dict):
            text=text_content(message.get("content"))
            if text:
                return text,record.get("uuid")
    return "",None

def normalize(platform,hook,data,cwd=None):
    if not isinstance(data,dict):
        raise ValueError("hook payload must be an object")
    if platform=="agy":
        hook=AGY_LEGACY.get(hook,hook)
    if hook not in EVENTS:
        raise ValueError("unsupported hook: "+hook)
    session=first(data,"conversationId","session_id","thread_id","conversation_id","sessionId")
    if not isinstance(session,str) or not session:
        raise ValueError("native session ID required; refusing to merge unrelated sessions")
    workspaces=data.get("workspacePaths",[])
    workspace=cwd or (workspaces[0] if isinstance(workspaces,list) and workspaces else None) or first(data,"cwd","workspace","workspace_path")
    if not isinstance(workspace,str) or not Path(workspace).is_dir():
        raise ValueError("valid workspace required")
    kind=EVENTS[hook]
    tool=data.get("toolCall") if isinstance(data.get("toolCall"),dict) else {}
    native_id=first(data,"event_id","eventId")
    turn=first(data,"turn_id","invocationNum")
    call=first(data,"tool_use_id","call_id","stepIdx")
    timestamp=first(data,"timestamp","occurred_at")
    payload={"native_hook":hook}
    if kind=="session_start":
        payload.update(matcher=first(data,"source","matcher",default="startup"),model=data.get("model"))
    elif kind in {"prompt_submit","assistant_response","stop"}:
        if kind=="prompt_submit":
            text=first(data,"prompt","text","message",default="")
            key="prompt"
        else:
            text=first(data,"last_assistant_message","response","summary",default="")
            key="response"
        transcript=first(data,"transcriptPath","transcript_path")
        if not text and transcript:
            try:
                text,message_id=last_public_message(transcript,"user" if kind=="prompt_submit" else "assistant",platform)
                native_id=native_id or (str(message_id) if message_id is not None else None)
            except (OSError,ValueError):
                payload["_capture_gaps"]=["Transcript message unavailable"]
        payload[key]=text
        if not text:
            payload.setdefault("_capture_gaps",[]).append("No public message available for this event")
        if kind=="stop":
            payload["reason"]=first(data,"terminationReason","reason",default="unknown")
    elif kind in {"tool_call","tool_result"}:
        payload.update(tool=tool.get("name") or first(data,"tool_name","tool",default=""),
                       input=tool.get("args") if "args" in tool else first(data,"tool_input","input","parameters","args","arguments",default={}))
        if kind=="tool_result":
            output=first(data,"tool_response","output","result")
            error=data.get("error")
            exit_code=data.get("exit_code")
            is_error=bool(data.get("is_error") or hook=="PostToolUseFailure" or error)
            if isinstance(output,dict):
                error=error or output.get("error")
                exit_code=first(output,"exit_code","exitCode",default=exit_code)
                is_error=is_error or bool(output.get("isError") or output.get("is_error") or error)
            # Text terminal results may carry the actual process exit code.
            if exit_code is None and isinstance(output,str):
                match=re.search(r"(?:Process exited with code|exit code[: ]+)\s*(-?\d+)",output,re.I)
                if match:
                    exit_code=int(match.group(1))
            success=False if is_error or (exit_code is not None and exit_code!=0) else (
                True if exit_code==0 or (platform=="agy" and "error" in data and not error) else None)
            payload.update(output=output,error=error,exit_code=exit_code,success=success)
            if output is None:
                payload["_capture_gaps"]=["Tool result body absent; only native outcome metadata captured"]
    else:
        payload["reason"]=first(data,"trigger","reason",default="unknown")
    # A native occurrence identity deduplicates retries, not repeated identical user messages.
    occurrence=native_id
    if occurrence is None and call is not None:
        occurrence=str(call)
    if occurrence is None and turn is not None and kind in {"prompt_submit","assistant_response","stop"}:
        occurrence=str(turn)
        # Codex can submit new steering messages within the same turn.
        # A turn is not a prompt occurrence; keep different messages distinct.
        if platform == "codex" and kind == "prompt_submit":
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
            occurrence += ":" + digest
    if occurrence is None and timestamp:
        occurrence=str(timestamp)
    source="hook:"+platform+":"+hook+(":"+str(occurrence) if occurrence is not None else ":"+str(uuid.uuid4()))
    event_id=str(uuid.uuid5(uuid.NAMESPACE_URL,platform+":"+session+":"+source))
    return dict(event_type=kind,native_session_id=session,payload=payload,source_identity=source,
                cwd=workspace,occurred_at=timestamp,event_id=event_id)
