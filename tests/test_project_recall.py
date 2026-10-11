import json
import os
import sqlite3
import subprocess
import sys

import pytest

from scripts.secretary_projects_mcp import Projects, call_tool
from bridge.upstream_transport import UpstreamTransportClient
from tests.test_upstream_transport import make_test_event


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "upstream.db"
    with sqlite3.connect(db) as c:
        c.executescript("""
            CREATE TABLE sdk_sessions(id INTEGER PRIMARY KEY,project TEXT,memory_session_id TEXT,
                platform_source TEXT,user_prompt TEXT,started_at TEXT,started_at_epoch INTEGER,custom_title TEXT);
            CREATE TABLE session_summaries(id INTEGER PRIMARY KEY,project TEXT,memory_session_id TEXT,
                request TEXT,completed TEXT,learned TEXT,next_steps TEXT,notes TEXT,created_at TEXT,created_at_epoch INTEGER);
            CREATE TABLE user_prompts(id INTEGER PRIMARY KEY,session_db_id INTEGER,prompt_number INTEGER,prompt_text TEXT);
            CREATE TABLE observations(id INTEGER PRIMARY KEY,project TEXT,memory_session_id TEXT,
                type TEXT,title TEXT,narrative TEXT,created_at_epoch INTEGER);
        """)
        sessions = [
            (1,"p-a","m1","codex","Build login","2026-09-01",1,"Hermes project: Project A"),
            (2,"p-a","m2","agy","Fix tests","2026-09-02",2,"Hermes project: Project A"),
            (3,"p-b","m3","codex","Plan launch","2026-09-03",3,"Hermes project: Project B"),
        ]
        c.executemany("INSERT INTO sdk_sessions VALUES (?,?,?,?,?,?,?,?)",sessions)
        c.executemany("INSERT INTO session_summaries VALUES (?,?,?,?,?,?,?,?,?,?)",[
            (1,"p-a","m1","Build login","Login attempted","API returns 401","Fix token","Not deployed","2026-09-01",1),
            (2,"p-a","m2","Fix tests","Three tests pass","Missing migration","Add migration","Deployment unknown","2026-09-02",2)])
        c.executemany("INSERT INTO observations VALUES (?,?,?,?,?,?,?)",[
            (1,"p-a","m1","discovery","Login error","401 error verified",1),
            (2,"p-a","m2","decision","ทดสอบ","ทดสอบ migration 50%_done",2),
            (3,"p-b","m2","discovery","Other project private","Do not leak",3)])
    return Projects(db,tmp_path / "names.json")


def test_project_hierarchy_and_pending_work(store):
    before = store.database.read_bytes()
    result = store.list_projects({})
    assert result["total_projects"] == 2
    a,b = result["projects"]
    assert a["name"] == "Project A" and a["work_count"] == 2
    assert [w["work_id"] for w in a["subwork"]] == ["session:2","session:1"]
    assert b["subwork"][0]["memory_status"] == "summary_pending"
    assert "recorded_progress" not in b["subwork"][0]
    assert store.database.read_bytes() == before
    with store.connection() as c:
        with pytest.raises(sqlite3.OperationalError):
            c.execute("DELETE FROM observations")


def test_project_and_work_pagination(store):
    page = store.list_projects({"limit":1,"work_limit":1})
    assert page["next_offset"] == 1
    assert page["projects"][0]["more_work"]
    assert store.list_projects({"offset":1})["projects"][0]["project_id"] == "p-b"
    work = store.context({"project":"Project A","limit":1})
    assert work["next_offset"] == 1
    assert store.context({"project":"p-a","offset":1})["subwork"][0]["work_id"] == "session:1"


def test_continuation_is_project_scoped_and_has_sources(store):
    packet = store.context({"project":"project a","work_id":"session:2"})
    work = packet["subwork"][0]
    assert work["next_step"] == "Add migration"
    assert work["notes"] == "Deployment unknown"
    assert work["summary_id"] == 2
    assert [e["observation_id"] for e in work["evidence"]] == [2]
    with pytest.raises(ValueError,match="within the selected project"):
        store.context({"project":"p-b","work_id":"session:2"})
    with pytest.raises(ValueError,match="not found"):
        store.context({"project":"invented"})


def test_search_thai_and_literal_wildcards(store):
    assert store.search({"project":"p-a","query":"ทดสอบ"})["matches"][0]["work_id"] == "session:2"
    assert store.search({"project":"p-a","query":"50%_done"})["matches"][0]["work_id"] == "session:2"
    assert store.search({"project":"p-a","query":"50%done"})["matches"] == []
    assert store.search({"project":"p-b","query":"401"})["matches"] == []


def test_equal_names_not_merged_and_legacy_names_resolved(store):
    with sqlite3.connect(store.database) as c:
        c.execute("UPDATE sdk_sessions SET custom_title=NULL")
    store.names_file.write_text(json.dumps({"p-a":{"name":"Same"},"p-b":{"name":"Same"}}))
    assert len(store.list_projects({})["projects"]) == 2
    with pytest.raises(ValueError,match="Ambiguous"):
        store.context({"project":"Same"})
    assert store.context({"project":"p-a"})["project"]["project_id"] == "p-a"


def test_long_memory_is_bounded(store):
    with sqlite3.connect(store.database) as c:
        c.execute("UPDATE session_summaries SET request=?,completed=?,next_steps=?,learned=?,notes=?",["ก"*100000]*5)
        c.execute("UPDATE observations SET narrative=?",("x"*100000,))
    result = call_tool("get_project_context",{"project":"p-a"},store)
    assert len(result["content"][0]["text"]) < 8000
    assert "…" in result["content"][0]["text"]


def test_missing_database_does_not_create_it(tmp_path):
    db = tmp_path/"absent.db"
    result = call_tool("list_all_projects",{},Projects(db))
    assert result["isError"] and not db.exists()


def test_stdio_protocol_and_tools(store):
    env = {**os.environ,"HERMES_UPSTREAM_DB":str(store.database),"HERMES_PROJECT_NAMES":str(store.names_file)}
    requests = [{"jsonrpc":"2.0","id":1,"method":"tools/list"},
                {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_project_context","arguments":{"project":"p-a"}}}]
    proc = subprocess.run([sys.executable,"-B","scripts/secretary_projects_mcp.py"],env=env,
                          input="\n".join(json.dumps(r) for r in requests)+"\n",capture_output=True,text=True,encoding="utf-8",timeout=10)
    assert proc.returncode == 0,proc.stderr
    results = [json.loads(line) for line in proc.stdout.splitlines()]
    assert {t["name"] for t in results[0]["result"]["tools"]} >= {"get_project_context","search_project_work"}
    assert "isError" not in results[1]["result"]


def test_new_session_transports_display_name_without_changing_identity():
    event = make_test_event("e","prompt_submit",project_id="stable-uuid",payload={"prompt":"Do work","_context":{"project_name":"Readable Name"}})
    body = UpstreamTransportClient().map_event_to_upstream(event)["body"]
    assert body["project"] == "stable-uuid"
    assert body["customTitle"] == "Hermes project: Readable Name"

def test_search_retains_older_prompt_and_summary_history(store):
    with sqlite3.connect(store.database) as c:
        c.execute('INSERT INTO user_prompts VALUES (1,1,2,?)', ('Older request orchid-restore-50%_done',))
        c.execute('INSERT INTO user_prompts VALUES (2,3,2,?)', ('Foreign request foreign-only',))
        c.execute('INSERT INTO session_summaries VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (3,'p-a','m1','Earlier request','Earlier result','historical-learning','Earlier step','Earlier note','2026-08-31',0))
    result = store.search({'project':'p-a','query':'orchid-restore-50%_done'})
    assert result['matches'][0]['work_id'] == 'session:1'
    assert result['matches'][0]['matched_prompts'][0]['prompt_id'] == 1
    assert store.search({'project':'p-a','query':'historical-learning'})['matches'][0]['work_id'] == 'session:1'
    assert store.search({'project':'p-a','query':'foreign-only'})['matches'] == []
    assert store.search({'project':'p-a','query':'orchid-restore-50%done'})['matches'] == []
