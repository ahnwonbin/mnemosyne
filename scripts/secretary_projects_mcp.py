#!/usr/bin/env python3
"""Bounded project/work recall over the pinned upstream SQLite store. Read-only; no inference."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import sys

PROJECT_TITLE_PREFIX = "Hermes project: "

def compact(value, length):
    text = " ".join(str(value or "").split())
    return text if len(text) <= length else text[:length-1] + "…"

def integer(args, key, default, maximum):
    value = args.get(key, default)
    minimum = 0 if key == "offset" else 1
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be an integer from {minimum} to {maximum}")
    return value

class Projects:
    def __init__(self, database=None, names_file=None):
        self.database = Path(database or os.environ.get("HERMES_UPSTREAM_DB", str(Path.home()/".claude-mem/claude-mem.db")))
        self.names_file = Path(names_file or os.environ.get("HERMES_PROJECT_NAMES", str(self.database.parent/"hermes-project-names.json")))

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.database.resolve().as_uri()+"?mode=ro", uri=True, timeout=3)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=ON")
        c.execute("BEGIN")
        try:
            yield c
        finally:
            c.close()

    def catalog(self, c):
        names = json.loads(self.names_file.read_text(encoding="utf-8-sig")) if self.names_file.is_file() else {}
        ids = [r[0] for r in c.execute("""SELECT project FROM sdk_sessions
            UNION SELECT project FROM observations UNION SELECT project FROM session_summaries""") if r[0]]
        projects = []
        for pid in ids:
            title = c.execute("""SELECT custom_title FROM sdk_sessions WHERE project=?
                AND substr(custom_title,1,?)=? ORDER BY id DESC LIMIT 1""",
                (pid,len(PROJECT_TITLE_PREFIX),PROJECT_TITLE_PREFIX)).fetchone()
            name = title[0][len(PROJECT_TITLE_PREFIX):] if title else names.get(pid,{}).get("name",pid)
            count, latest = c.execute("SELECT count(*),max(started_at) FROM sdk_sessions WHERE project=?", (pid,)).fetchone()
            platforms = [r[0] for r in c.execute("SELECT DISTINCT platform_source FROM sdk_sessions WHERE project=? ORDER BY platform_source",(pid,))]
            projects.append(dict(project_id=pid,name=compact(name,120),work_count=count,last_session_at=latest,platforms=platforms))
        return sorted(projects,key=lambda p:(p["name"].casefold(),p["project_id"]))

    def resolve(self,c,requested):
        if not isinstance(requested,str) or not requested.strip():
            raise ValueError("project is required: use an exact project_id or unique display name")
        projects = self.catalog(c)
        matches = [p for p in projects if p["project_id"]==requested] or [p for p in projects if p["name"].casefold()==requested.strip().casefold()]
        if not matches:
            raise ValueError("Project not found. Use list_all_projects; do not guess project IDs.")
        if len(matches)>1:
            raise ValueError("Ambiguous project name; choose project_id: "+", ".join(p["project_id"] for p in matches))
        return matches[0]

    def rows(self,c,pid,limit,offset=0,work_id=None,query=None):
        where,params = ["s.project=?"],[pid]
        if work_id is not None:
            if not isinstance(work_id,str) or not work_id.startswith("session:") or not work_id[8:].isdigit():
                raise ValueError("work_id must be a session:ID returned by a project tool")
            where.append("s.id=?")
            params.append(int(work_id[8:]))
        if query:
            pattern = "%"+query.replace("\\","\\\\").replace("%","\\%").replace("_","\\_")+"%"
            where.append("""(coalesce(s.user_prompt,'') LIKE ? ESCAPE '\\'
                OR coalesce(m.request,'') LIKE ? ESCAPE '\\'
                OR coalesce(m.completed,'') LIKE ? ESCAPE '\\'
                OR coalesce(m.next_steps,'') LIKE ? ESCAPE '\\'
                OR EXISTS(SELECT 1 FROM user_prompts u WHERE u.session_db_id=s.id
                  AND coalesce(u.prompt_text,'') LIKE ? ESCAPE '\\')
                OR EXISTS(SELECT 1 FROM session_summaries h WHERE h.project=s.project
                  AND h.memory_session_id=s.memory_session_id
                  AND (coalesce(h.request,'') LIKE ? ESCAPE '\\'
                    OR coalesce(h.completed,'') LIKE ? ESCAPE '\\'
                    OR coalesce(h.learned,'') LIKE ? ESCAPE '\\'
                    OR coalesce(h.next_steps,'') LIKE ? ESCAPE '\\'
                    OR coalesce(h.notes,'') LIKE ? ESCAPE '\\'))
                OR EXISTS(SELECT 1 FROM observations o WHERE o.project=s.project
                  AND o.memory_session_id=s.memory_session_id
                  AND (coalesce(o.title,'') LIKE ? ESCAPE '\\' OR coalesce(o.narrative,'') LIKE ? ESCAPE '\\')))""")
            params.extend([pattern]*12)
        sql = """SELECT s.id AS session_id,s.memory_session_id,s.platform_source,s.started_at,
            s.user_prompt,m.id AS summary_id,m.request,m.completed,m.learned,m.next_steps,m.notes,
            m.created_at AS summary_at FROM sdk_sessions s LEFT JOIN session_summaries m ON m.id=(
              SELECT id FROM session_summaries WHERE memory_session_id=s.memory_session_id
              AND project=s.project ORDER BY created_at_epoch DESC,id DESC LIMIT 1)
            WHERE """+" AND ".join(where)+" ORDER BY s.started_at_epoch DESC,s.id DESC LIMIT ? OFFSET ?"
        return c.execute(sql,(*params,limit,offset)).fetchall()

    def work(self,row,detail=False):
        item = dict(work_id=f"session:{row['session_id']}",
            goal=compact(row["request"] or row["user_prompt"] or "Captured session",300 if detail else 140),
            source=row["platform_source"],started_at=row["started_at"],
            memory_status="summarized" if row["summary_id"] is not None else "summary_pending")
        if row["summary_id"] is not None:
            item.update(recorded_progress=compact(row["completed"],500 if detail else 160),
                next_step=compact(row["next_steps"],400 if detail else 140),
                summary_id=row["summary_id"],summary_at=row["summary_at"])
            if detail:
                item.update(learned=compact(row["learned"],400),notes=compact(row["notes"],300))
        return item

    def list_projects(self,args):
        limit,offset = integer(args,"limit",5,5),integer(args,"offset",0,1000000)
        per_project = integer(args,"work_limit",2,5)
        with self.connection() as c:
            projects = self.catalog(c)
            page = projects[offset:offset+limit]
            for p in page:
                rows = self.rows(c,p["project_id"],per_project)
                p["subwork"] = [self.work(r) for r in rows]
                p["more_work"] = p["work_count"]>len(rows)
            return dict(projects=page,total_projects=len(projects),
                next_offset=offset+len(page) if offset+len(page)<len(projects) else None,
                grouping="Project -> captured work session; session goals are not inferred task completion.",
                coverage="Captured upstream memory only; unsent local work is absent.")

    def context(self,args):
        limit,offset = integer(args,"limit",3,5),integer(args,"offset",0,1000000)
        with self.connection() as c:
            p = self.resolve(c,args.get("project"))
            rows = self.rows(c,p["project_id"],limit+1,offset,args.get("work_id"))
            if args.get("work_id") and not rows:
                raise ValueError("Work not found within the selected project")
            work = []
            for r in rows[:limit]:
                item = self.work(r,True)
                obs = c.execute("""SELECT id,type,title,narrative FROM observations
                    WHERE project=? AND memory_session_id=? ORDER BY created_at_epoch DESC,id DESC LIMIT 3""",
                    (p["project_id"],r["memory_session_id"])).fetchall()
                item["evidence"] = [dict(observation_id=o["id"],type=o["type"],title=compact(o["title"],140),detail=compact(o["narrative"],240)) for o in obs]
                work.append(item)
            unlinked = []
            if not work and not args.get("work_id"):
                unlinked = [dict(observation_id=r["id"],title=compact(r["title"],140)) for r in c.execute(
                    "SELECT id,title FROM observations WHERE project=? ORDER BY id DESC LIMIT 5",(p["project_id"],))]
            return dict(project=p,subwork=work,unlinked_observations=unlinked,
                next_offset=offset+len(work) if len(rows)>limit else None,
                continuation="Use goals, recorded progress, learned facts, notes and next steps; verify current files before acting. Summaries are historical claims, not proof of completion. Missing blockers are unknown, not none.")

    def search(self,args):
        query = args.get("query")
        if not isinstance(query,str) or not 1<=len(query.strip())<=200:
            raise ValueError("query must contain 1-200 characters")
        limit,offset = integer(args,"limit",5,5),integer(args,"offset",0,1000000)
        with self.connection() as c:
            p = self.resolve(c,args.get("project"))
            rows = self.rows(c,p["project_id"],limit+1,offset,query=query.strip())
            pattern = "%"+query.strip().replace("\\","\\\\").replace("%","\\%").replace("_","\\_")+"%"
            matches = []
            for row in rows[:limit]:
                item = self.work(row)
                prompts = c.execute("""SELECT id,prompt_number,prompt_text FROM user_prompts
                    WHERE session_db_id=? AND coalesce(prompt_text,'') LIKE ? ESCAPE '\\'
                    ORDER BY id DESC LIMIT 2""", (row["session_id"],pattern)).fetchall()
                if prompts:
                    item["matched_prompts"] = [dict(prompt_id=r["id"],prompt_number=r["prompt_number"],
                        excerpt=compact(r["prompt_text"],240)) for r in prompts]
                matches.append(item)
            return dict(project=p,query=query,matches=matches,
                next_offset=offset+limit if len(rows)>limit else None,
                search_scope="Literal text in session goals, prompt history, all summaries and linked observations; no semantic inference.")

def schema(properties,required=()):
    return dict(type="object",properties=properties,required=list(required),additionalProperties=False)

PROJECT = dict(type="string",description="Exact project_id or unique display name from list_all_projects")
LIMIT = dict(type="integer",minimum=1,maximum=5)
OFFSET = dict(type="integer",minimum=0,description="Use next_offset for another page")
CONTEXT_SCHEMA = schema(dict(project=PROJECT,work_id=dict(type="string"),limit=LIMIT,offset=OFFSET),["project"])
TOOLS = [
    dict(name="list_all_projects",description="Show projects grouped with recent subwork, recorded progress and next steps. Follow next_offset for more projects; never merge equal names.",
         inputSchema=schema(dict(limit=LIMIT,offset=OFFSET,work_limit=LIMIT))),
    dict(name="get_project_status",description="Get bounded project or selected subwork context, with source IDs and pending-summary labels.",inputSchema=CONTEXT_SCHEMA),
    dict(name="get_project_context",description="Before continuing a project, retrieve its goals, recorded progress, learned facts, notes and next steps. Optional work_id focuses one subwork item.",inputSchema=CONTEXT_SCHEMA),
    dict(name="search_project_work",description="Find older subwork within one project by literal Thai/English text; then get_project_context with work_id for detail.",
         inputSchema=schema(dict(project=PROJECT,query=dict(type="string",maxLength=200),limit=LIMIT,offset=OFFSET),["project","query"]))
]

def call_tool(name,args,store=None):
    try:
        if not isinstance(args,dict):
            raise ValueError("arguments must be an object")
        store = store or Projects()
        handlers = dict(list_all_projects=store.list_projects,get_project_status=store.context,
                        get_project_context=store.context,search_project_work=store.search)
        if name not in handlers:
            raise ValueError("Unknown tool")
        result = handlers[name](args)
        return dict(content=[dict(type="text",text=json.dumps(result,ensure_ascii=False,separators=(",",":")))])
    except (ValueError,sqlite3.Error,OSError) as exc:
        message = str(exc) if isinstance(exc,ValueError) else "Upstream memory unavailable or schema incompatible; no data was changed."
        return dict(content=[dict(type="text",text=message)],isError=True)

def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            req = json.loads(line)
            if not isinstance(req,dict) or "id" not in req:
                continue
            method = req.get("method")
            if method=="initialize":
                result = dict(protocolVersion="2024-11-05",capabilities=dict(tools={}),
                              serverInfo=dict(name="hermes-secretary-projects",version="2.0.0"))
            elif method=="tools/list":
                result = dict(tools=TOOLS)
            elif method=="tools/call":
                params = req.get("params",{})
                result = call_tool(params.get("name"),params.get("arguments",{}))
            elif method=="ping":
                result = {}
            else:
                print(json.dumps(dict(jsonrpc="2.0",id=req["id"],error=dict(code=-32601,message="Method not found"))),flush=True)
                continue
            print(json.dumps(dict(jsonrpc="2.0",id=req["id"],result=result),ensure_ascii=False),flush=True)
        except (ValueError,TypeError,AttributeError):
            print(json.dumps(dict(jsonrpc="2.0",id=None,error=dict(code=-32600,message="Invalid request"))),flush=True)

if __name__=="__main__":
    main()
