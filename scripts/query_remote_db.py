import sqlite3
from pathlib import Path

conn = sqlite3.connect(str(Path.home() / ".claude-mem" / "claude-mem.db"))
print("=== SESSIONS ===")
for r in conn.execute("SELECT id, project, user_prompt, status FROM sdk_sessions").fetchall():
    print(r)

print("=== TOOL USES ===")
for r in conn.execute("SELECT id, tool_name, tool_use_id FROM tool_uses").fetchall():
    print(r)

print("=== OBSERVATIONS ===")
for r in conn.execute("SELECT id, type, title, project FROM observations").fetchall():
    print(r)
