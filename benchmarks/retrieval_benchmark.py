"""Reproducible synthetic FTS5 benchmark; no network calls or live database."""
import argparse
import json
import math
from pathlib import Path
import platform
import sqlite3
import statistics
from tempfile import TemporaryDirectory
from time import perf_counter

from server.db import Store
from server.models import Project
from server.retrieval import search_memories


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=positive_int, default=10000)
    parser.add_argument("--queries", type=positive_int, default=60)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="mnemosyne-benchmark-") as directory:
        path = Path(directory) / "memory.db"
        store = Store(path)
        store.upsert_project(Project(project_id="benchmark", name="Synthetic benchmark"))
        with store.connect(write=True) as connection:
            # Seed observations and their FTS index directly; ingestion is not timed.
            for index in range(args.records):
                identity = f"observation-{index}"
                title = f"SQLite decision {index} topic{index % 100}"
                narrative = (
                    f"Project memory topic{index % 100}: use SQLite WAL for durable "
                    f"event storage and ordered delivery. Synthetic observation {index}."
                )
                connection.execute(
                    "INSERT INTO observations VALUES (?,?,?,?,?,?,?)",
                    (identity, "benchmark", "decision", title, narrative,
                     "reported", "2026-01-01T00:00:00+00:00"),
                )
                connection.execute("INSERT INTO observations_fts VALUES (?,?,?)",
                                   (identity, title, narrative))
        def search():
            result = search_memories(store, "benchmark", "topic0", limit=5, max_tokens=1000)
            if not result["results"]:
                raise RuntimeError("FTS search returned no results")
            return result
        for _ in range(10):
            search()
        durations = []
        for _ in range(args.queries):
            start = perf_counter()
            result = search()
            durations.append((perf_counter() - start) * 1000)
        with store.connect() as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        report = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "processor": platform.processor(),
            "records": args.records,
            "query": "topic0",
            "warmup_queries": 10,
            "measured_queries": args.queries,
            "limit": 5,
            "max_tokens": 1000,
            "returned_results": len(result["results"]),
            "median_ms": round(statistics.median(durations), 3),
            "p95_ms": round(sorted(durations)[math.ceil(0.95 * len(durations)) - 1], 3),
            "database_mib": round(path.stat().st_size / (1024 * 1024), 3),
        }
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
