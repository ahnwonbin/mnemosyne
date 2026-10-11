"""Offline capture, processing, and recall using temporary SQLite databases."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from bridge.outbox import Outbox
from server.db import Store
from server.models import Event
from server.observer import Observer
from server.retrieval import search_memories


def main():
    with TemporaryDirectory(prefix="mnemosyne-demo-") as directory:
        folder = Path(directory)
        box = Outbox(folder / "outbox.db")
        record = box.enqueue(
            "demo-device", "demo-project", "manual", "session-one",
            "prompt_submit",
            {"prompt": "Decide to use SQLite WAL for durable project memory.",
             "_context": {"project_name": "Local recall demo"}},
            "examples/local_recall.py",
        )
        store = Store(folder / "memory.db")
        status = store.ingest(Event.model_validate(record.to_transmission_dict()))
        observer = Observer(store)
        lease = observer.lease_job(force=True)
        if lease is None:
            raise RuntimeError("No processing job was created")
        processed = observer.process_project(*lease)
        if processed["status"] != "success":
            raise RuntimeError(f"Processing failed: {processed}")
        # Reopen persisted storage, as a subsequent session would.
        recalled = search_memories(Store(folder / "memory.db"), "demo-project", "SQLite")
        if not recalled["results"]:
            raise RuntimeError("The captured event could not be recalled")
        print(f"Ingestion: {status}")
        print(f"Observations created: {processed['observations_created']}")
        print("Recall after reopening the database:")
        print(json.dumps(recalled["results"], indent=2))
        print("Demo complete; temporary databases are removed on exit.")


if __name__ == "__main__":
    main()
