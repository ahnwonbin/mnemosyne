import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import httpx

from bridge.cache import LocalCache
from bridge.cli import _trigger_background_sync, build_parser, cmd_sync, cmd_hook
from bridge.cli import _run_daemon, cmd_daemon
from bridge.outbox import Outbox
from bridge.upstream_transport import UpstreamTransportClient, refresh_recent_context_cache
from bridge.worker import file_lock, sender_lock_path
from scripts.retire_local_claudemem import retire


def test_hook_import_does_not_load_network_stack():
    run = subprocess.run([sys.executable, "-B", "-c",
        "import bridge.cli,sys; assert 'httpx' not in sys.modules; assert 'pydantic' not in sys.modules"],
        capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr


def test_hook_burst_spawns_one_sender_and_existing_sender_suppresses_launch(tmp_path, monkeypatch):
    cache = LocalCache(tmp_path / "cache")
    cache.save_config({"upstream": True})
    box = Outbox(tmp_path / "queue.db")
    popen = Mock()
    monkeypatch.setattr("bridge.cli.subprocess.Popen", popen)
    for _ in range(30):
        _trigger_background_sync(cache, box)
    assert popen.call_count == 1
    assert "--idle-timeout" in popen.call_args.args[0]
    box.db_path.with_suffix(".sender-launch").unlink()
    with file_lock(sender_lock_path(box)) as acquired:
        assert acquired
        _trigger_background_sync(cache, box)
    assert popen.call_count == 1
    _trigger_background_sync(cache, box)
    assert popen.call_count == 2


def test_failed_launch_can_retry(tmp_path, monkeypatch):
    cache = LocalCache(tmp_path / "cache")
    cache.save_config({"upstream": True})
    box = Outbox(tmp_path / "queue.db")
    popen = Mock(side_effect=[OSError("fixture"), Mock()])
    monkeypatch.setattr("bridge.cli.subprocess.Popen", popen)
    _trigger_background_sync(cache, box)
    _trigger_background_sync(cache, box)
    assert popen.call_count == 2


def test_sender_lock_excludes_another_process_and_releases(tmp_path):
    lock = tmp_path / "sender.lock"
    code = "from bridge.worker import file_lock; import sys\nwith file_lock(sys.argv[1]) as acquired: print(acquired)"
    def probe():
        return subprocess.run([sys.executable, "-B", "-c", code, str(lock)],
                              capture_output=True, text=True, timeout=15)
    with file_lock(lock) as acquired:
        assert acquired
        child = probe()
        assert child.returncode == 0, child.stderr
        assert child.stdout.strip() == "False"
    assert probe().stdout.strip() == "True"


def test_daemon_sleeps_and_exits_after_idle_window(tmp_path, monkeypatch):
    cache = LocalCache(tmp_path / "cache")
    cache.save_config({"upstream": True})
    box = Outbox(tmp_path / "queue.db")
    clock = [0]
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr("bridge.cli.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("bridge.cli.time.sleep", sleep)
    client = Mock()
    monkeypatch.setattr("bridge.cli.UpstreamTransportClient", lambda **kw: client)
    monkeypatch.setattr("bridge.cli.refresh_recent_context_cache", lambda *a, **kw: None)
    args = build_parser().parse_args(["daemon", "--idle-timeout", "300"])
    assert _run_daemon(args, cache, box) == 0
    assert sleeps == [60] * 5
    client.sync_all.assert_not_called()


def test_interrupted_auto_sender_does_not_respawn(tmp_path, monkeypatch):
    cache = LocalCache(tmp_path / "cache")
    cache.save_config({"upstream": True})
    box = Outbox(tmp_path / "queue.db")
    box.enqueue("d", "p", "codex", "s", "prompt_submit", {"prompt": "x"}, "test")
    monkeypatch.setattr("bridge.cli.LocalCache", lambda: cache)
    monkeypatch.setattr("bridge.cli.get_default_outbox", lambda: box)
    client = Mock()
    client.sync_all.side_effect = KeyboardInterrupt
    monkeypatch.setattr("bridge.cli.UpstreamTransportClient", lambda **kw: client)
    spawn = Mock()
    monkeypatch.setattr("bridge.cli._trigger_background_sync", spawn)
    assert cmd_daemon(build_parser().parse_args(["daemon", "--idle-timeout", "300"])) == 130
    spawn.assert_not_called()


def test_refresh_cooldown_success_failure_and_delayed_summary(tmp_path, monkeypatch):
    cache = LocalCache(tmp_path)
    clock = [1000]
    calls = []
    fail = [False]
    monkeypatch.setattr("bridge.upstream_transport.time.time", lambda: clock[0])
    def handler(req):
        calls.append(req.url.params["project"])
        return httpx.Response(503) if fail[0] else httpx.Response(200, json={
            "content": [{"type": "text", "text": str(clock[0])}]})
    client = UpstreamTransportClient(transport=httpx.MockTransport(handler))
    refresh_recent_context_cache(client, cache, ["p", "p"], min_interval=300)
    clock[0] = 1100
    refresh_recent_context_cache(client, cache, ["p"], min_interval=300)
    assert calls == ["p"]
    clock[0] = 1301
    refresh_recent_context_cache(client, cache, ["p"], min_interval=300)
    assert cache.get_cached_context("p")["content"][0]["text"] == "1301"
    fail[0] = True
    clock[0] = 1602
    refresh_recent_context_cache(client, cache, ["p", "new"], min_interval=300)
    assert cache.get_cached_context("p")["stale"]
    assert cache.get_cached_context("p")["content"][0]["text"] == "1301"
    n = len(calls)
    clock[0] = 1650
    refresh_recent_context_cache(client, cache, ["p", "new"], min_interval=300)
    assert len(calls) == n


def test_active_project_selection_keeps_old_unsent_events(tmp_path):
    box = Outbox(tmp_path / "queue.db")
    for pid in ("old-sent", "old-pending", "recent"):
        box.enqueue("d", pid, "codex", pid, "prompt_submit", {"prompt": "x"}, "fixture")
    c = box._get_connection()
    try:
        with c:
            c.execute("UPDATE outbox_events SET created_at='2000-01-01' WHERE project_id LIKE 'old-%'")
            c.execute("UPDATE outbox_events SET status='sent' WHERE project_id='old-sent'")
    finally:
        c.close()
    assert box.active_project_ids() == ["old-pending", "recent"]


def test_upstream_selection_ignores_legacy_token(tmp_path, monkeypatch):
    cache = LocalCache(tmp_path / "cache")
    cache.save_config({"upstream": True, "token": "old-token"})
    box = Outbox(tmp_path / "queue.db")
    client = Mock()
    client.sync_all.return_value = dict(total_batches=0, total_accepted=0, total_duplicate=0,
                                       total_rejected=0, last_error=None, outbox_stats={})
    upstream = Mock(return_value=client)
    monkeypatch.setattr("bridge.cli.LocalCache", lambda: cache)
    monkeypatch.setattr("bridge.cli.get_default_outbox", lambda: box)
    monkeypatch.setattr("bridge.cli.UpstreamTransportClient", upstream)
    monkeypatch.setattr("bridge.cli.SyncClient", Mock(side_effect=AssertionError("wrong backend")))
    monkeypatch.setattr("bridge.cli.refresh_recent_context_cache", lambda *a, **k: {"refreshed": 0, "errors": []})
    assert cmd_sync(build_parser().parse_args(["sync", "--upstream"])) == 0
    upstream.assert_called_once()


def test_context_tail_change_does_not_repeat_injection(tmp_path, monkeypatch, capsys):
    cache = LocalCache(tmp_path / "cache")
    cache.save_config({"auto_sync": False})
    box = Outbox(tmp_path / "queue.db")
    (tmp_path / ".hermes_project").write_text("project")
    monkeypatch.setenv("HERMES_BRIDGE_DIR", str(cache.base_dir))
    monkeypatch.setattr("bridge.cli.LocalCache", lambda: cache)
    monkeypatch.setattr("bridge.cli.get_default_outbox", lambda: box)
    monkeypatch.setattr("bridge.adapters.base.get_default_device_id", lambda: "fixture")
    monkeypatch.setattr("bridge.cli.read_hook_input", lambda: {"session_id": "s", "prompt": "x", "cwd": str(tmp_path)})
    args = build_parser().parse_args(["hook", "codex", "UserPromptSubmit"])
    for tail in ("old", "new"):
        cache.save_cached_context("project", {"project": "project", "content": [{"type": "text", "text": "A" * 3300 + tail}]})
        cmd_hook(args)
        out = capsys.readouterr().out
        assert bool(out.strip()) == (tail == "old")


def test_retirement_preserves_other_hooks_files_and_memory(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Keep this.\n<!-- claudemem-agent-driven -->Old recorder<!-- /claudemem-agent-driven -->\n<claude-mem-context>Old memory</claude-mem-context>")
    hooks = tmp_path / ".gemini/config/hooks.json"
    hooks.parent.mkdir(parents=True)
    hermes = {"name": "hermes-bridge", "command": "python hook_entry.py"}
    other = {"name": "other", "command": "other-command"}
    hooks.write_text(json.dumps({"Stop": [{"matcher": "*", "hooks": [hermes, other,
        {"name": "claude-mem", "command": "bun /worker-service.cjs hook antigravity-cli summarize"}]}]}))
    skill = tmp_path / ".codex/skills/claudemem"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("legacy skill")
    db = tmp_path / ".claude-mem/claude-mem.db"
    db.parent.mkdir()
    db.write_bytes(b"history stays untouched")
    preview = retire(tmp_path)
    assert preview["legacy_hooks_removed"] == 1
    assert skill.exists()
    result = retire(tmp_path, apply=True)
    assert (tmp_path / "AGENTS.md").read_text().strip() == "Keep this."
    assert json.loads(hooks.read_text())["Stop"][0]["hooks"] == [hermes, other]
    assert db.read_bytes() == b"history stays untouched"
    assert not skill.exists()
    assert (Path(result["backup"]) / ".codex/skills/claudemem/SKILL.md").read_text() == "legacy skill"
    assert "Old recorder" in (Path(result["backup"]) / "AGENTS.md").read_text()
    assert "backup" not in retire(tmp_path, apply=True)
