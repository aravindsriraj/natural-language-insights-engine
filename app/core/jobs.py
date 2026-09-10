"""Async job manager.

Submit returns an id immediately; work happens on a bounded pool. Design notes that matter
under questioning:

  Concurrency. A semaphore caps in-flight work. Queued jobs wait in `queued` state rather
  than being rejected, so a burst is absorbed rather than dropped. Because each dataset is
  its own DuckDB file, two ingests never contend, and queries never contend with ingest.

  Partial failure. Job state lives in SQLite, not in memory, so it survives the process. On
  startup any job still marked `running` is marked `interrupted` with an explanatory error,
  because we cannot know whether it completed. Ingest is idempotent by content hash, so
  resubmitting an interrupted ingest converges rather than duplicating.

  Backpressure on events. Each job has a bounded event queue. A slow or absent SSE consumer
  cannot make a producer block or grow memory without limit; oldest events are dropped and
  the terminal state is always readable from the database.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import traceback
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from app.config import settings
from app.errors import AppError, NotFound

Status = Literal["queued", "running", "succeeded", "failed", "interrupted"]
TERMINAL = ("succeeded", "failed", "interrupted")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    dataset_id   TEXT,
    status       TEXT NOT NULL,
    stage        TEXT,
    payload      TEXT,
    result       TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at DESC);
CREATE TABLE IF NOT EXISTS answer_cache (
    key         TEXT PRIMARY KEY,
    dataset_id  TEXT NOT NULL,
    question    TEXT NOT NULL,
    result      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class JobManager:
    def __init__(self, *, concurrency: int | None = None, db_path=None):
        self.db_path = str(db_path or settings().jobs_db)
        self.concurrency = concurrency or settings().job_concurrency
        self._sem = asyncio.Semaphore(self.concurrency)
        self._queues: dict[str, asyncio.Queue] = {}
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ storage
    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def setup(self) -> None:
        with self._conn() as con:
            # WAL is a persistent property of the database file, so it is set once here
            # rather than on every connection.
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_SCHEMA)

    def recover(self) -> int:
        """Mark jobs that were mid-flight when the process died. Returns how many."""
        with self._conn() as con:
            cur = con.execute(
                "UPDATE jobs SET status='interrupted', finished_at=?, "
                "error=? WHERE status IN ('running','queued')",
                (
                    _now(),
                    json.dumps({
                        "code": "interrupted",
                        "message": "Server restarted while this job was in flight. "
                                   "Resubmit it; ingestion is idempotent.",
                    }),
                ),
            )
            return cur.rowcount

    def create(self, kind: str, *, dataset_id: str | None = None, payload: dict | None = None) -> str:
        job_id = uuid.uuid4().hex[:16]
        with self._conn() as con:
            con.execute(
                "INSERT INTO jobs (id, kind, dataset_id, status, created_at, payload) "
                "VALUES (?,?,?,?,?,?)",
                (job_id, kind, dataset_id, "queued", _now(), json.dumps(payload or {})),
            )
        return job_id

    def get(self, job_id: str) -> dict:
        with self._conn() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFound(f"Job '{job_id}' not found")
        d = dict(row)
        for field in ("payload", "result", "error"):
            d[field] = json.loads(d[field]) if d[field] else None
        return d

    def list(self, limit: int = 50) -> list[dict]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT id, kind, dataset_id, status, stage, created_at, finished_at "
                "FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as con:
            con.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    # ------------------------------------------------------------------ events
    def _queue(self, job_id: str) -> asyncio.Queue:
        # No lock: the event loop is single threaded and there is no await between the
        # check and the insert, so this cannot interleave with another caller.
        if job_id not in self._queues:
            self._queues[job_id] = asyncio.Queue(maxsize=256)
        return self._queues[job_id]

    async def emit(self, job_id: str, event: dict) -> None:
        q = self._queue(job_id)
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest rather than block the producer. Terminal state is always
            # recoverable from the database, so a dropped progress event costs nothing.
            try:
                q.get_nowait()
                q.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def events(self, job_id: str, *, poll_s: float = 1.0):
        """Yield events until the job reaches a terminal state.

        Events arrive through the queue with no delay. `poll_s` only governs how often we
        fall back to the database, which matters solely when a job dies without emitting a
        terminal event, so it is deliberately not tuned for latency.
        """
        job = self.get(job_id)
        q = self._queue(job_id)
        if job["status"] in TERMINAL:
            yield {"type": "status", "status": job["status"], "job": job}
            return
        yield {"type": "status", "status": job["status"]}
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=poll_s)
                yield event
                if event.get("type") == "done":
                    return
            except TimeoutError:
                current = self.get(job_id)
                if current["status"] in TERMINAL:
                    while not q.empty():
                        yield q.get_nowait()
                    yield {"type": "done", "status": current["status"], "job": current}
                    return

    async def _cleanup(self, job_id: str) -> None:
        await asyncio.sleep(30)  # let a late SSE consumer drain
        self._queues.pop(job_id, None)

    # ------------------------------------------------------------------ execution
    def submit(
        self,
        kind: str,
        handler: Callable[[str, Callable[[dict], Awaitable[None]]], Awaitable[dict]],
        *,
        dataset_id: str | None = None,
        payload: dict | None = None,
    ) -> str:
        job_id = self.create(kind, dataset_id=dataset_id, payload=payload)
        task = asyncio.create_task(self._run(job_id, handler))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job_id

    async def _run(self, job_id: str, handler) -> None:
        async def emit(event: dict) -> None:
            if stage := event.get("stage"):
                self._update(job_id, stage=stage)
            await self.emit(job_id, event)

        async with self._sem:
            self._update(job_id, status="running", started_at=_now(), stage="starting")
            await self.emit(job_id, {"type": "status", "status": "running"})
            try:
                result = await handler(job_id, emit)
                self._update(
                    job_id, status="succeeded", finished_at=_now(), stage="done",
                    result=json.dumps(result, default=str),
                )
                await self.emit(job_id, {"type": "done", "status": "succeeded", "result": result})
            except AppError as exc:
                self._update(job_id, status="failed", finished_at=_now(),
                             error=json.dumps(exc.to_dict()["error"]))
                await self.emit(job_id, {"type": "done", "status": "failed",
                                         "error": exc.to_dict()["error"]})
            except Exception as exc:
                # Unexpected. Full detail to the log, an opaque reference to the caller.
                ref = uuid.uuid4().hex[:8]
                print(f"[job {job_id}] unhandled error ref={ref}\n{traceback.format_exc()}", flush=True)
                err = {"code": "internal_error",
                       "message": f"An unexpected error occurred (reference {ref}).",
                       "detail": type(exc).__name__}
                self._update(job_id, status="failed", finished_at=_now(), error=json.dumps(err))
                await self.emit(job_id, {"type": "done", "status": "failed", "error": err})
            finally:
                asyncio.create_task(self._cleanup(job_id))

    async def shutdown(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # ------------------------------------------------------------------ threads
    # Threads are derived from the jobs table rather than stored separately. Every question
    # is already a job row carrying its thread id, its text and its answer, so the jobs
    # table is the conversation log. A second table would be duplicate state to keep in
    # sync, and could disagree with the log it was copied from.

    def list_threads(self, dataset_id: str | None = None, limit: int = 100) -> list[dict]:
        where = "kind='query' AND json_extract(payload,'$.thread_id') IS NOT NULL"
        params: list[Any] = []
        if dataset_id:
            where += " AND dataset_id = ?"
            params.append(dataset_id)
        with self._conn() as con:
            rows = con.execute(
                f"""
                SELECT json_extract(j.payload,'$.thread_id') AS thread_id,
                       j.dataset_id,
                       count(*)            AS message_count,
                       min(j.created_at)   AS created_at,
                       max(j.created_at)   AS updated_at,
                       sum(j.status = 'failed') AS failed_count,
                       (
                         SELECT json_extract(f.payload,'$.question') FROM jobs f
                         WHERE f.kind='query'
                           AND json_extract(f.payload,'$.thread_id')
                               = json_extract(j.payload,'$.thread_id')
                         ORDER BY f.created_at ASC, f.rowid ASC LIMIT 1
                       ) AS title
                FROM jobs j
                WHERE {where}
                GROUP BY thread_id, j.dataset_id
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def thread_turns(self, thread_id: str) -> list[dict]:
        """Every question in a thread, oldest first, with its answer if it produced one."""
        with self._conn() as con:
            rows = con.execute(
                """
                SELECT id, status, payload, result, error, created_at
                FROM jobs
                WHERE kind='query' AND json_extract(payload,'$.thread_id') = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (thread_id,),
            ).fetchall()
        turns = []
        for r in rows:
            payload = json.loads(r["payload"]) if r["payload"] else {}
            turns.append({
                "job_id": r["id"],
                "status": r["status"],
                "question": payload.get("question"),
                "created_at": r["created_at"],
                "answer": json.loads(r["result"]) if r["result"] else None,
                "error": json.loads(r["error"]) if r["error"] else None,
            })
        return turns

    def delete_thread(self, thread_id: str) -> int:
        """Forget a conversation. The checkpointer's copy is removed by the caller."""
        with self._conn() as con:
            cur = con.execute(
                "DELETE FROM jobs WHERE kind='query' "
                "AND json_extract(payload,'$.thread_id') = ?",
                (thread_id,),
            )
            return cur.rowcount

    # ------------------------------------------------------------------ answer cache
    @staticmethod
    def cache_key(dataset_id: str, question: str) -> str:
        return f"{dataset_id}:{' '.join(question.lower().split())}"

    def cache_get(self, dataset_id: str, question: str) -> dict | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT result FROM answer_cache WHERE key=?",
                (self.cache_key(dataset_id, question),),
            ).fetchone()
        return json.loads(row["result"]) if row else None

    def cache_put(self, dataset_id: str, question: str, result: dict) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT OR REPLACE INTO answer_cache (key, dataset_id, question, result, created_at) "
                "VALUES (?,?,?,?,?)",
                (self.cache_key(dataset_id, question), dataset_id, question,
                 json.dumps(result, default=str), _now()),
            )

    def cache_clear(self, dataset_id: str | None = None) -> None:
        with self._conn() as con:
            if dataset_id:
                con.execute("DELETE FROM answer_cache WHERE dataset_id=?", (dataset_id,))
            else:
                con.execute("DELETE FROM answer_cache")
