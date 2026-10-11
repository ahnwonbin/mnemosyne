CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO metadata VALUES ('registry_revision',0);
CREATE TABLE IF NOT EXISTS projects (
 project_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
 phase TEXT, next_action TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 summary_version INTEGER NOT NULL DEFAULT 0, plan_version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS project_aliases (
 device_id TEXT NOT NULL, path TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(project_id),
 PRIMARY KEY(device_id,path));
CREATE TABLE IF NOT EXISTS devices (
 device_id TEXT PRIMARY KEY, last_contact TEXT NOT NULL, reported_backlog INTEGER,
 adapter_version TEXT);
CREATE TABLE IF NOT EXISTS sessions (
 device_id TEXT NOT NULL, platform TEXT NOT NULL, native_session_id TEXT NOT NULL,
 contiguous_sequence INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(device_id,platform,native_session_id));
CREATE TABLE IF NOT EXISTS events (
 ordinal INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
 project_id TEXT NOT NULL REFERENCES projects(project_id), device_id TEXT NOT NULL,
 platform TEXT NOT NULL, native_session_id TEXT NOT NULL, session_sequence INTEGER NOT NULL,
 event_type TEXT NOT NULL, occurred_at TEXT NOT NULL, received_at TEXT NOT NULL,
 payload TEXT NOT NULL, payload_hash TEXT NOT NULL, envelope_hash TEXT NOT NULL,
 source_identity TEXT NOT NULL, adapter_version TEXT NOT NULL,
 processed INTEGER NOT NULL DEFAULT 0,
 UNIQUE(device_id,platform,native_session_id,session_sequence));
CREATE INDEX IF NOT EXISTS events_pending ON events(project_id,processed,ordinal);
CREATE TABLE IF NOT EXISTS processing_jobs (
 project_id TEXT PRIMARY KEY REFERENCES projects(project_id), state TEXT NOT NULL DEFAULT 'pending',
 due_at TEXT NOT NULL, lease_id TEXT, lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS processing_attempts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, status TEXT NOT NULL, error TEXT, model TEXT,
 input_tokens INTEGER, output_tokens INTEGER);
CREATE TABLE IF NOT EXISTS observations (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
 type TEXT NOT NULL, title TEXT NOT NULL, narrative TEXT NOT NULL, evidence_level TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observation_sources (
 observation_id TEXT NOT NULL REFERENCES observations(id), event_id TEXT NOT NULL REFERENCES events(event_id),
 PRIMARY KEY(observation_id,event_id));
CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(id UNINDEXED, title, narrative, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS summary_versions (
 project_id TEXT NOT NULL REFERENCES projects(project_id), version INTEGER NOT NULL,
 body TEXT NOT NULL, covered_events TEXT NOT NULL, created_at TEXT NOT NULL, model TEXT NOT NULL,
 PRIMARY KEY(project_id,version));
CREATE TABLE IF NOT EXISTS milestone_plans (
 project_id TEXT NOT NULL REFERENCES projects(project_id), version INTEGER NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(project_id,version));
CREATE TABLE IF NOT EXISTS milestones (
 project_id TEXT NOT NULL, plan_version INTEGER NOT NULL, milestone_id TEXT NOT NULL,
 title TEXT NOT NULL, weight REAL NOT NULL CHECK(weight>0), status TEXT NOT NULL,
 evidence_level TEXT, PRIMARY KEY(project_id,plan_version,milestone_id));
CREATE TABLE IF NOT EXISTS milestone_evidence (
 project_id TEXT NOT NULL, plan_version INTEGER NOT NULL, milestone_id TEXT NOT NULL,
 event_id TEXT NOT NULL REFERENCES events(event_id), PRIMARY KEY(project_id,plan_version,milestone_id,event_id));
CREATE TABLE IF NOT EXISTS registry_snapshots (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS discovery_runs (
 run_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, scanned_at TEXT NOT NULL,
 roots TEXT NOT NULL, failures TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS discovery_candidates (
 run_id TEXT NOT NULL REFERENCES discovery_runs(run_id), candidate_id TEXT NOT NULL, body TEXT NOT NULL,
 disposition TEXT NOT NULL DEFAULT 'unresolved', project_id TEXT,
 PRIMARY KEY(run_id,candidate_id));
CREATE TABLE IF NOT EXISTS audit_records (
 id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, entity_id TEXT, at TEXT NOT NULL, detail TEXT NOT NULL);
