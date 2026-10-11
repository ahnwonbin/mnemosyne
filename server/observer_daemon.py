"""Background observer daemon for Hermes Cloud Service.
Runs as a continuous service on the GCP VM, leasing pending jobs and generating observations/summaries.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import signal
import threading
from typing import Optional

from server.db import Store
from server.observer import Observer, ProxyInferenceClient, DeterministicExtractor


def run_daemon(
    db_path: Path,
    proxy_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: str = "gemini-3.8-flash-high",
    poll_interval: float = 3.0,
    batch_size: int = 50,
    stop_event: Optional[threading.Event] = None,
) -> None:
    if poll_interval <= 0 or not 1 <= batch_size <= 100:
        raise ValueError("positive polling interval and batch size between 1 and 100 required")
    stop_event = stop_event or threading.Event()
    store = Store(db_path)

    if proxy_url and api_key:
        client = ProxyInferenceClient(base_url=proxy_url, api_key=api_key, model=model)
        print(f"[hermes-observer] Configured ProxyInferenceClient: {proxy_url} (model: {model})")
    else:
        client = DeterministicExtractor()
        print("[hermes-observer] Running in local DeterministicExtractor mode (no proxy configured)")

    observer = Observer(store=store, inference_client=client)
    print(f"[hermes-observer] Daemon started on database {db_path} (interval: {poll_interval}s)")

    while not stop_event.is_set():
        try:
            res = observer.tick(batch_size=batch_size)
            if res:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                if res.get("status") == "success":
                    print(
                        f"[{ts}] Processed project '{res['project_id']}': "
                        f"{res['events_processed']} events -> {res['observations_created']} obs (v{res['summary_version']})"
                    )
                elif res.get("status") == "failed":
                    print(f"[{ts}] Processing failed for project '{res.get('project_id')}': {res.get('error')}", file=sys.stderr)
            stop_event.wait(poll_interval)
        except KeyboardInterrupt:
            print("[hermes-observer] Daemon stopped.")
            break
        except Exception as exc:
            print(f"[hermes-observer error] {exc}", file=sys.stderr)
            stop_event.wait(poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Memory Observer Daemon")
    parser.add_argument("--db", help="Path to SQLite memory database")
    parser.add_argument("--proxy-url", help="OpenAI-compatible inference proxy URL (e.g. http://127.0.0.1:8317/v1)")
    parser.add_argument("--api-key", help="Inference proxy API key (e.g. example-proxy-key)")
    parser.add_argument("--model", default=os.environ.get("HERMES_MODEL", "gemini-3.8-flash-high"))
    parser.add_argument("--interval", type=float, default=3.0, help="Polling interval in seconds")
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()

    db_path = Path(args.db or os.environ.get("HERMES_MEMORY_DB", str(Path.home() / ".hermes" / "server" / "memory.db"))).resolve()
    proxy_url = args.proxy_url or os.environ.get("HERMES_PROXY_URL")
    api_key = args.api_key or os.environ.get("HERMES_PROXY_KEY")
    if bool(proxy_url) != bool(api_key):
        parser.error("configure both proxy URL and API key, or neither for offline extraction")
    stop_event = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop_event.set())

    run_daemon(
        db_path=db_path,
        proxy_url=proxy_url,
        api_key=api_key,
        model=args.model,
        poll_interval=args.interval,
        batch_size=args.batch_size,
        stop_event=stop_event,
    )


if __name__ == "__main__":
    main()
