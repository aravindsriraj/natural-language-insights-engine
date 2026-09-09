"""DuckDB access.

One database file per dataset. Rationale: DuckDB allows a single writer per file, so
isolating datasets means an ingest can never contend with, block, or corrupt a query
against a different dataset. The cost is no cross-dataset joins, which nothing needs yet.

The table inside every dataset file is always named `dataset`. Prompts and the SQL guard
therefore reference one stable identifier regardless of the uploaded file's name.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import duckdb

from app.config import settings
from app.errors import NotFound, QueryTimeout

TABLE = "dataset"


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    row_count: int
    elapsed_ms: int
    truncated: bool


def db_path(dataset_id: str) -> Path:
    return settings().datasets_dir / f"{dataset_id}.duckdb"


def exists(dataset_id: str) -> bool:
    return db_path(dataset_id).exists()


def writer(dataset_id: str) -> duckdb.DuckDBPyConnection:
    """Exclusive read-write connection. Only ingest uses this."""
    p = db_path(dataset_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(p))


def reader(dataset_id: str) -> duckdb.DuckDBPyConnection:
    """Read-only connection.

    This is the enforcement boundary for query safety. A parse check can be fooled by a
    construct we failed to anticipate; a read-only connection cannot write regardless.
    """
    if not exists(dataset_id):
        raise NotFound(f"Dataset '{dataset_id}' not found")
    return duckdb.connect(str(db_path(dataset_id)), read_only=True)


def run_query(
    dataset_id: str,
    sql: str,
    *,
    max_rows: int | None = None,
    timeout_s: int | None = None,
) -> QueryResult:
    """Execute read-only with a wall-clock timeout and a row cap."""
    cfg = settings()
    max_rows = max_rows or cfg.max_result_rows
    timeout_s = timeout_s or cfg.query_timeout_s

    con = reader(dataset_id)
    timed_out = threading.Event()

    def _interrupt() -> None:
        timed_out.set()
        try:
            con.interrupt()
        except Exception:  # pragma: no cover - connection already closed
            pass

    timer = threading.Timer(timeout_s, _interrupt)
    timer.start()
    started = time.perf_counter()
    try:
        cur = con.execute(sql)
        columns = [d[0] for d in (cur.description or [])]
        # Fetch one extra row so truncation is detectable rather than guessed.
        fetched = cur.fetchmany(max_rows + 1)
    except duckdb.InterruptException as exc:
        raise QueryTimeout(f"Query exceeded {timeout_s}s and was cancelled") from exc
    except Exception as exc:
        if timed_out.is_set():
            raise QueryTimeout(f"Query exceeded {timeout_s}s and was cancelled") from exc
        raise
    finally:
        timer.cancel()
        con.close()

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    truncated = len(fetched) > max_rows
    rows = [list(r) for r in fetched[:max_rows]]
    return QueryResult(columns, rows, len(rows), elapsed_ms, truncated)
