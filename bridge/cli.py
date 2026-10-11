"""Hermes Second-Brain Bridge CLI.
Provides hook handlers, sync runners, outbox status, project discovery,
hook installation, diagnostics, and context injection.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, Optional

from bridge.adapters.base import read_hook_input
from bridge.adapters.claude_code import ClaudeCodeAdapter
from bridge.adapters.codex import CodexAdapter
from bridge.adapters.agy import AntigravityAdapter
from bridge.cache import LocalCache, format_context_prompt
from bridge.identity import normalize_path, resolve_project
from bridge.installer import install_hooks, uninstall_hooks, run_probe, HOOK_MAPPINGS
from bridge.outbox import Outbox, get_default_device_id
from bridge.worker import file_lock, sender_lock_path
from bridge.tunnel import ensure_tunnel


# Hooks only need the standard library. Load network dependencies in senders.
def SyncClient(*args, **kwargs):
    from bridge.sync import SyncClient as Client
    return Client(*args, **kwargs)


def UpstreamTransportClient(*args, **kwargs):
    from bridge.upstream_transport import UpstreamTransportClient as Client
    return Client(*args, **kwargs)


def refresh_context_cache(*args, **kwargs):
    from bridge.sync import refresh_context_cache as refresh
    return refresh(*args, **kwargs)


def refresh_recent_context_cache(*args, **kwargs):
    from bridge.upstream_transport import refresh_recent_context_cache as refresh
    return refresh(*args, **kwargs)


def get_default_outbox() -> Outbox:
    db_path = Path(os.environ.get("HERMES_OUTBOX", str(LocalCache().base_dir / "outbox.db")))
    return Outbox(db_path)


def _trigger_background_sync(cache: LocalCache, outbox: Outbox) -> None:
    """Wake a single, idle-expiring sender without launching one process per event."""
    token = cache.get_token()
    endpoint = cache.get_endpoint()
    is_upstream = is_upstream_worker_endpoint(endpoint, cache.get_config())
    if not token and not is_upstream:
        return
    env = os.environ.copy()
    if token:
        env["HERMES_DEVICE_TOKEN"] = token
    env.update(HERMES_SYNC_URL=endpoint,
               HERMES_BRIDGE_DIR=str(cache.base_dir.resolve()), HERMES_OUTBOX=str(outbox.db_path))
    options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    try:
        with file_lock(sender_lock_path(outbox)) as available:
            if not available:
                return
            # Covers the short window before a newly spawned sender takes its lock.
            launch_file = outbox.db_path.with_suffix(".sender-launch")
            if launch_file.exists() and 0 <= time.time() - launch_file.stat().st_mtime < 5:
                return
            with (cache.base_dir / "sync.log").open("a", encoding="utf-8") as log:
                subprocess.Popen([sys.executable, "-u", "-m", "bridge.cli", "daemon",
                                  "--interval", "60", "--idle-timeout", "300"],
                                 cwd=str(Path(__file__).resolve().parent.parent), env=env,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 close_fds=True, **options)
            launch_file.touch()
    except OSError as exc:
        print(f"[hermes-bridge] Could not start sync: {type(exc).__name__}", file=sys.stderr)


def cmd_hook(args: argparse.Namespace) -> int:
    platform = args.platform.lower()
    hook_name = args.hook_name
    payload = read_hook_input()

    # Respect hook-provided cwd from args, payload "cwd", or Agy "workspacePaths"
    workspace_paths = payload.get("workspacePaths")
    payload_cwd = payload.get("cwd") or (workspace_paths[0] if workspace_paths and isinstance(workspace_paths, list) else None)
    cwd = args.cwd or payload_cwd or os.getcwd()

    cache = LocalCache()
    outbox = get_default_outbox()

    adapter = None
    if platform in ("claude", "claude_code", "claude-code"):
        platform_norm = "claude_code"
        adapter = ClaudeCodeAdapter(outbox=outbox)
    elif platform in ("codex", "openai_codex"):
        platform_norm = "codex"
        adapter = CodexAdapter(outbox=outbox)
    elif platform in ("agy", "antigravity", "gemini"):
        platform_norm = "agy"
        adapter = AntigravityAdapter(outbox=outbox)
    else:
        platform_norm = "claude_code"
        adapter = ClaudeCodeAdapter(outbox=outbox)

    event = None
    try:
        event = adapter.handle_hook(hook_name=hook_name, payload_data=payload, cwd=cwd)
        if event and args.verbose:
            print(f"[hermes-bridge] Enqueued {event.platform} {event.event_type} ({event.event_id[:8]})", file=sys.stderr)
    except Exception as exc:
        # Report capture failures to error log and stderr (if verbose) without crashing host CLI
        err_log = cache.base_dir / "capture_errors.log"
        try:
            with open(err_log, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now(timezone.utc).isoformat()}] [{platform_norm}] [{hook_name}] {str(exc)}\n")
        except Exception:
            pass
        if args.verbose:
            print(f"[hermes-bridge capture error] {exc}", file=sys.stderr)

    # Context injection for prompt / session initialization hooks
    is_injection_hook = (hook_name == "PreInvocation" if platform_norm == "agy" else
                         hook_name in ("SessionStart", "UserPromptSubmit", "PreToolUse"))

    emitted_context = False
    if is_injection_hook:
        try:
            project_id = event.project_id if event else resolve_project(
                cwd, known_aliases=cache.get_known_aliases()).project_id
            session_id = (
                payload.get("session_id")
                or payload.get("conversationId")
                or (event.native_session_id if event else "global")
            )
            session_key = f"{platform_norm}:{session_id}"

            # Retrieve context from cache
            ctx = cache.get_cached_context(project_id)
            if ctx and ("content" not in ctx or ctx["content"]):
                import hashlib
                context_prompt = format_context_prompt(ctx)
                # Changes outside the bounded injected text must not spend tokens again.
                current_v = hashlib.sha256(context_prompt.encode()).hexdigest()[:16]
                resumed = hook_name == "SessionStart" and payload.get("source") in ("resume", "compact")
                if resumed or cache.should_inject_context(session_key, project_id, current_v):
                    # Emit client-specific hook response
                    if platform_norm == "agy":
                        hook_out = {"injectSteps": [{"ephemeralMessage": context_prompt}]}
                    else:
                        hook_out = {"hookSpecificOutput": {"hookEventName": hook_name,
                                                          "additionalContext": context_prompt}}
                    sys.stdout.write(json.dumps(hook_out, ensure_ascii=False) + "\n")
                    sys.stdout.flush()
                    emitted_context = True

                    cache.mark_injected(session_key, project_id, current_v)
        except Exception as exc:
            if args.verbose:
                print(f"[hermes-bridge injection error] {exc}", file=sys.stderr)

    if platform_norm == "agy" and not emitted_context:
        sys.stdout.write(json.dumps({"decision": "allow"} if hook_name == "PreToolUse" else {}) + "\n")
        sys.stdout.flush()

    # Trigger background sync if auto_sync enabled
    if event and cache.get_config().get("auto_sync", True):
        _trigger_background_sync(cache, outbox)

    return 0


def is_upstream_worker_endpoint(endpoint: str, config: Optional[Dict[str, Any]] = None) -> bool:
    """Check if endpoint is an upstream Claude-Mem worker."""
    if not endpoint:
        return False
    if config and config.get("upstream"):
        return True
    return any(p in endpoint for p in (":37777", ":37778"))


def cmd_sync(args: argparse.Namespace) -> int:
    cache = LocalCache()
    outbox = get_default_outbox()
    with file_lock(sender_lock_path(outbox)) as acquired:
        if not acquired:
            print("A sender is already running for this outbox.")
            return 0
        return _sync_once(args, cache, outbox)


def _sync_once(args, cache, outbox):
    endpoint = args.endpoint or cache.get_endpoint()
    token = args.token or cache.get_token()

    is_upstream = (
        getattr(args, "upstream", False)
        or is_upstream_worker_endpoint(endpoint, cache.get_config())
    )

    if is_upstream:
        ensure_tunnel(cache.get_config(), endpoint, cache.base_dir)
        print(f"Syncing with upstream Claude-Mem worker: {endpoint}")
        client = UpstreamTransportClient(endpoint_url=endpoint, outbox=outbox)
        res = client.sync_all(batch_size=args.batch_size, max_batches=args.max_batches)
        # Refresh recent context for known projects
        known_projects = outbox.active_project_ids()
        cache_result = refresh_recent_context_cache(client, cache, known_projects)
        print(f"Sync completed. Batches: {res['total_batches']}, Accepted: {res['total_accepted']}, Duplicate: {res['total_duplicate']}, Rejected: {res['total_rejected']}")
        if res["last_error"]:
            print(f"Sync notice/error: {res['last_error']}")
        print(f"Current outbox: {res['outbox_stats']}")
        print(f"Context cache: {cache_result}")
        return 1 if res["last_error"] or res["total_rejected"] or cache_result["errors"] else 0

    if not token:
        print("[hermes-bridge] Error: Authentication token required. Set HERMES_DEVICE_TOKEN or run 'hermes-bridge config --token <TOKEN>'", file=sys.stderr)
        return 1

    print(f"Syncing with cloud endpoint: {endpoint}")
    client = SyncClient(endpoint_url=endpoint, auth_token=token, outbox=outbox)
    res = client.sync_all(batch_size=args.batch_size, max_batches=args.max_batches)
    cache_result = refresh_context_cache(cache, outbox, endpoint)

    print(f"Sync completed. Batches: {res['total_batches']}, Accepted: {res['total_accepted']}, Duplicate: {res['total_duplicate']}, Rejected: {res['total_rejected']}")
    if res["last_error"]:
        print(f"Sync notice/error: {res['last_error']}")
    print(f"Current outbox: {res['outbox_stats']}")
    print(f"Context cache: {cache_result}")
    return 1 if res["last_error"] or res["total_rejected"] or cache_result["errors"] else 0


def cmd_status(args: argparse.Namespace) -> int:
    outbox = get_default_outbox()
    stats = outbox.get_stats()
    device_id = get_default_device_id()
    cache = LocalCache()
    token_status = "configured" if cache.get_token() else "missing"
    print("================ Hermes Bridge Status ================")
    print(f"Device ID:     {device_id}")
    print(f"Outbox DB:     {outbox.db_path}")
    print(f"Cloud Target:  {cache.get_endpoint()}")
    print(f"Device Token:  [{token_status}]")
    print(f"Pending:       {stats['pending']}")
    print(f"In Flight:     {stats['in_flight']}")
    print(f"Sent:          {stats['sent']}")
    print(f"Quarantined:   {stats['quarantined']}")
    print(f"Total Events:  {stats['total']}")
    print("======================================================")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    from bridge.discovery import DiscoveryEngine
    roots = args.roots or [os.environ.get("HERMES_PROJECTS_ROOT", str(Path.home() / "projects"))]
    engine = DiscoveryEngine(workspace_roots=roots)
    res = engine.scan(max_depth=args.max_depth)

    print(f"Discovered {res['total_found']} candidate projects across {len(res['roots'])} roots:")
    for cand in res["candidates"]:
        git_mark = "[GIT]" if cand["is_git"] else "[DIR]"
        print(f" - {git_mark} {cand['name']:<25} ID: {cand['project_id']} ({cand['path']})")
    return 0


def cmd_bind(args: argparse.Namespace) -> int:
    target_dir = Path(args.path).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    override_file = target_dir / ".hermes_project"

    name = args.name or target_dir.name
    normalized = normalize_path(target_dir)
    data = {
        "project_id": args.project_id,
        "name": name,
        "path": normalized,
    }
    override_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Also persist to local aliases
    cache = LocalCache()
    cache.save_alias(normalized, args.project_id, name)

    print(f"Bound project '{name}' ({args.project_id}) to {target_dir}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cache = LocalCache()
    cfg = cache.get_config()
    changed = False

    if args.token is not None:
        cfg["token"] = args.token
        changed = True
        print("Updated token.")
    if args.read_token is not None:
        cfg["read_token"] = args.read_token
        changed = True
        print("Updated read token.")
    if args.endpoint is not None:
        cfg["endpoint"] = args.endpoint
        changed = True
        print(f"Updated endpoint to {args.endpoint}")
    if args.auto_sync is not None:
        cfg["auto_sync"] = args.auto_sync.lower() in ("true", "1", "yes")
        changed = True
        print(f"Set auto_sync = {cfg['auto_sync']}")
    if getattr(args, "upstream", None) is not None:
        cfg["upstream"] = args.upstream.lower() in ("true", "1", "yes")
        changed = True
        print(f"Set upstream = {cfg['upstream']}")

    if changed:
        cache.save_config(cfg)
    else:
        print("Current Bridge Configuration:")
        print(json.dumps({k: "[configured]" if "token" in k.lower() or "key" in k.lower() else v
                          for k, v in cfg.items()}, indent=2))
    return 0


def cmd_install_hooks(args: argparse.Namespace) -> int:
    platforms = [args.platform] if args.platform != "all" else list(HOOK_MAPPINGS.keys())
    for p in platforms:
        res = install_hooks(p, python_exe=args.python)
        print(f"[{p}] Installed {res['hooks_count']} hooks to {res['installed_path']}")
        if res["backup_path"]:
            print(f"      Backup saved to {res['backup_path']}")
    return 0


def cmd_uninstall_hooks(args: argparse.Namespace) -> int:
    platforms = [args.platform] if args.platform != "all" else list(HOOK_MAPPINGS.keys())
    for p in platforms:
        res = uninstall_hooks(p)
        print(f"[{p}] Status: {res['status']}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    platforms = [args.platform] if args.platform != "all" else list(HOOK_MAPPINGS.keys())
    workspace = Path(args.workspace or os.getcwd()).resolve()

    print(f"Running Hermes Hook Probes on workspace: {workspace}")
    all_ok = True
    for p in platforms:
        res = run_probe(p, workspace)
        status_sym = "[OK]" if res["all_passed"] else "[FAIL]"
        print(f"\n{status_sym} Platform: {p}")
        for chk in res["checks"]:
            mark = "PASS" if chk.get("success") else "FAIL"
            print(f"   [{mark}] {chk['check']}")
        if not res["all_passed"]:
            all_ok = False

    return 0 if all_ok else 1


def cmd_daemon(args: argparse.Namespace) -> int:
    cache = LocalCache()
    outbox = get_default_outbox()
    if args.interval <= 0 or getattr(args, "idle_timeout", 0) < 0:
        raise ValueError("interval must be positive and idle timeout non-negative")
    # Allow the spawning hook to release its lock; don't accumulate waiting senders.
    for attempt in range(10):
        with file_lock(sender_lock_path(outbox)) as acquired:
            if acquired:
                result = _run_daemon(args, cache, outbox)
                break
        time.sleep(0.1)
    else:
        return 0
    # Close the idle-exit race: an event may have arrived after our final poll.
    if result == 0 and getattr(args, "idle_timeout", 0) and cache.get_config().get("auto_sync", True):
        stats = outbox.get_stats()
        if stats["pending"] or stats["in_flight"]:
            _trigger_background_sync(cache, outbox)
    return result


def _run_daemon(args, cache, outbox):
    endpoint = args.endpoint or cache.get_endpoint()
    token = args.token or cache.get_token()
    is_upstream = is_upstream_worker_endpoint(endpoint, cache.get_config())

    if not is_upstream and not token:
        print("[hermes-bridge daemon] Error: Authentication token required.", file=sys.stderr)
        return 1

    print(f"[hermes-bridge daemon] Starting background sync daemon for {endpoint} (poll: {args.interval}s, upstream: {is_upstream})", flush=True)
    if is_upstream:
        client = UpstreamTransportClient(endpoint_url=endpoint, outbox=outbox)
    else:
        client = SyncClient(endpoint_url=endpoint, auth_token=token, outbox=outbox)

    idle_timeout = getattr(args, "idle_timeout", 0)
    last_activity = time.monotonic()
    last_total = None
    while True:
        try:
            if is_upstream:
                ensure_tunnel(cache.get_config(), endpoint, cache.base_dir)
            stats = outbox.get_stats()
            if stats["total"] != last_total or stats["pending"] or stats["in_flight"]:
                last_activity = time.monotonic()
            last_total = stats["total"]
            if stats["pending"] > 0 or stats["in_flight"] > 0:
                res = client.sync_all(batch_size=50, max_batches=10)
                if res["last_error"]:
                    print(f"[hermes-bridge sync error] {res['last_error']}", file=sys.stderr, flush=True)
                if res["total_accepted"] > 0 or res["total_rejected"] > 0:
                    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Synced: accepted={res['total_accepted']}, rejected={res['total_rejected']}")
            if is_upstream:
                refreshed = refresh_recent_context_cache(client, cache, outbox.active_project_ids(), min_interval=300)
                if refreshed and refreshed["errors"]:
                    print(f"[hermes-bridge context error] {refreshed['errors']}", file=sys.stderr, flush=True)
            else:
                refresh_context_cache(cache, outbox, endpoint)
            if idle_timeout and time.monotonic() - last_activity >= idle_timeout:
                break
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("[hermes-bridge daemon] Stopped.")
            return 130
        except Exception as exc:
            print(f"[hermes-bridge daemon error] {exc}", file=sys.stderr)
            time.sleep(args.interval)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-bridge", description="Hermes Second-Brain Bridge CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # hook command
    hook_parser = subparsers.add_parser("hook", help="Handle CLI lifecycle hook")
    hook_parser.add_argument("platform", choices=["claude_code", "claude", "codex", "agy", "antigravity"])
    hook_parser.add_argument("hook_name", help="Name of the hook (e.g. SessionStart, PreToolUse, PostToolUse, Stop)")
    hook_parser.add_argument("--cwd", help="Override working directory", default=None)
    hook_parser.add_argument("-v", "--verbose", action="store_true", help="Print debug information")
    hook_parser.set_defaults(func=cmd_hook)

    # sync command
    sync_parser = subparsers.add_parser("sync", help="Synchronize outbox with cloud backend")
    sync_parser.add_argument("--endpoint", help="Cloud API endpoint URL")
    sync_parser.add_argument("--token", help="Device bearer token")
    sync_parser.add_argument("--upstream", action="store_true", help="Sync directly to upstream Claude-Mem worker")
    sync_parser.add_argument("--batch-size", type=int, default=50)
    sync_parser.add_argument("--max-batches", type=int, default=50)
    sync_parser.set_defaults(func=cmd_sync)

    # status command
    status_parser = subparsers.add_parser("status", help="Show outbox and device status")
    status_parser.set_defaults(func=cmd_status)

    # discover command
    disc_parser = subparsers.add_parser("discover", help="Discover projects in workspace roots")
    disc_parser.add_argument("--roots", nargs="*", help="Workspace root paths to scan")
    disc_parser.add_argument("--max-depth", type=int, default=2)
    disc_parser.set_defaults(func=cmd_discover)

    # bind command
    bind_parser = subparsers.add_parser("bind", help="Explicitly bind a directory to a project ID")
    bind_parser.add_argument("path", help="Target directory path")
    bind_parser.add_argument("project_id", help="Hermes Project UUID")
    bind_parser.add_argument("--name", help="Display name for project")
    bind_parser.set_defaults(func=cmd_bind)

    # config command
    cfg_parser = subparsers.add_parser("config", help="View or update local bridge configuration")
    cfg_parser.add_argument("--token", help="Set device authentication token")
    cfg_parser.add_argument("--read-token", help="Set separate context-read token")
    cfg_parser.add_argument("--endpoint", help="Set cloud endpoint URL")
    cfg_parser.add_argument("--auto-sync", help="Enable or disable auto-sync (true/false)")
    cfg_parser.add_argument("--upstream", help="Enable or disable upstream mode (true/false)")
    cfg_parser.set_defaults(func=cmd_config)

    # install-hooks command
    inst_parser = subparsers.add_parser("install-hooks", help="Safely install hooks for local CLIs")
    inst_parser.add_argument("platform", choices=["claude_code", "codex", "agy", "all"])
    inst_parser.add_argument("--python", help="Path to python executable to invoke")
    inst_parser.set_defaults(func=cmd_install_hooks)

    # uninstall-hooks command
    uninst_parser = subparsers.add_parser("uninstall-hooks", help="Safely uninstall hooks and revert backups")
    uninst_parser.add_argument("platform", choices=["claude_code", "codex", "agy", "all"])
    uninst_parser.set_defaults(func=cmd_uninstall_hooks)

    # probe command
    probe_parser = subparsers.add_parser("probe", help="Run diagnostic probes on hook adapters")
    probe_parser.add_argument("--platform", choices=["claude_code", "codex", "agy", "all"], default="all")
    probe_parser.add_argument("--workspace", help="Target workspace path to probe")
    probe_parser.set_defaults(func=cmd_probe)

    # daemon command
    daemon_parser = subparsers.add_parser("daemon", help="Run background synchronization daemon")
    daemon_parser.add_argument("--endpoint", help="Cloud API endpoint URL")
    daemon_parser.add_argument("--token", help="Device bearer token")
    daemon_parser.add_argument("--interval", type=float, default=60.0, help="Polling interval in seconds")
    daemon_parser.add_argument("--idle-timeout", type=float, default=0, help="Exit after idle seconds; 0 keeps retry daemon running")
    daemon_parser.set_defaults(func=cmd_daemon)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
