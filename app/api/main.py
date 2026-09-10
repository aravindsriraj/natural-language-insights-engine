"""HTTP surface.

Every ingest and every question is a job: submit, get an id, poll or stream, retrieve.
Errors leave here as a structured envelope with a status code. Stack traces never do.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, File, Query, Request, UploadFile
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.schemas import ColumnRoleUpdate, QueryRequest
from app.config import llm_configured, settings
from app.core import ingest as ing
from app.core.jobs import JobManager
from app.core.profile import build_profile, get_profile, set_column_role
from app.errors import AppError, BadRequest, NotFound, PayloadTooLarge, QueryTimeout

jobs = JobManager()
UI_DIST = Path(__file__).resolve().parent.parent.parent / "ui" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    jobs.setup()
    if (n := jobs.recover()):
        print(f"[startup] marked {n} in-flight job(s) as interrupted", flush=True)
    yield
    await jobs.shutdown()


app = FastAPI(
    title="Natural Language Insights Engine",
    version="0.1.0",
    description="Ask questions in plain English about any transactional CSV.",
    lifespan=lifespan,
)


# --------------------------------------------------------------------- error handling

@app.exception_handler(AppError)
async def _app_error(request: Request, exc: AppError):
    return JSONResponse(status_code=exc.status, content=exc.to_dict())


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    fields = [
        {"field": ".".join(str(p) for p in e["loc"][1:]) or "body", "problem": e["msg"]}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation_error",
                           "message": "Request body failed validation.",
                           "fields": fields}},
    )


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": "http_error", "message": str(exc.detail)}},
    )


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """Last resort. The caller gets a reference; the log gets the trace."""
    import traceback

    ref = uuid.uuid4().hex[:8]
    print(f"[api] unhandled {type(exc).__name__} ref={ref} path={request.url.path}\n"
          f"{traceback.format_exc()}", flush=True)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error",
                           "message": f"An unexpected error occurred (reference {ref})."}},
    )


# --------------------------------------------------------------------- health

@app.get("/health", tags=["meta"])
async def health():
    return {
        "status": "ok",
        "llm_configured": llm_configured(),
        "model": settings().llm_model,
        "datasets": len(ing.list_datasets()),
        "job_concurrency": settings().job_concurrency,
    }


# --------------------------------------------------------------------- datasets

def _accepted(job_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "queued",
            "poll_url": f"/api/jobs/{job_id}",
            "stream_url": f"/api/jobs/{job_id}/events",
        },
    )


@app.post("/api/datasets", status_code=202, tags=["datasets"])
async def upload_dataset(file: UploadFile = File(...), name: str | None = Query(None)):
    """Upload a CSV. Returns a job id; profiling happens as part of the job."""
    if not file.filename:
        raise BadRequest("A file is required")
    if not file.filename.lower().endswith((".csv", ".tsv", ".txt")):
        raise BadRequest("Expected a .csv file", detail=f"Received '{file.filename}'")

    limit = settings().max_upload_mb * 1024 * 1024
    tmp_dir = Path(tempfile.mkdtemp(prefix="nlie-upload-"))
    tmp = tmp_dir / Path(file.filename).name
    size = 0
    try:
        with tmp.open("wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise PayloadTooLarge(
                        f"File exceeds the {settings().max_upload_mb} MB limit"
                    )
                out.write(chunk)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    if size == 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise BadRequest("Uploaded file is empty")

    display_name = name or Path(file.filename).stem

    async def handler(job_id: str, emit):
        try:
            await emit({"type": "stage", "stage": "loading", "message": "Loading CSV into DuckDB"})
            meta = await asyncio.to_thread(ing.ingest_csv, tmp, name=display_name)
            await emit({"type": "stage", "stage": "profiling",
                        "message": f"Profiling {meta.row_count:,} rows across {meta.column_count} columns"})
            profile = await asyncio.to_thread(lambda: build_profile(meta.dataset_id, use_model=True))
            return {
                "dataset_id": meta.dataset_id,
                "name": meta.name,
                "row_count": meta.row_count,
                "column_count": meta.column_count,
                "read_options": meta.read_options,
                "semantics_error": profile.get("semantics_error"),
            }
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return _accepted(jobs.submit("ingest", handler, payload={"filename": file.filename, "bytes": size}))


@app.get("/api/datasets", tags=["datasets"])
async def list_datasets():
    return {"datasets": ing.list_datasets()}


@app.get("/api/datasets/{dataset_id}", tags=["datasets"])
async def get_dataset(dataset_id: str = PathParam(..., min_length=1, max_length=128)):
    meta = ing.load_meta(dataset_id)
    return {"meta": meta.__dict__, "profile": get_profile(dataset_id)}


@app.patch("/api/datasets/{dataset_id}/columns/{column}", tags=["datasets"])
async def update_column_role(dataset_id: str, column: str, body: ColumnRoleUpdate = Body(...)):
    """Correct an inferred role. Schema inference is visible and fixable, not silent."""
    profile = set_column_role(dataset_id, column, body.role, body.description)
    return {"profile": profile}


@app.delete("/api/datasets/{dataset_id}", status_code=204, tags=["datasets"])
async def delete_dataset(dataset_id: str):
    ing.delete_dataset(dataset_id)
    return JSONResponse(status_code=204, content=None)


# --------------------------------------------------------------------- questions

@app.post("/api/query", status_code=202, tags=["questions"])
async def ask(body: QueryRequest):
    """Ask a question. Returns a job id; poll or stream it."""
    ing.load_meta(body.dataset_id)  # 404 early rather than inside the job
    if not llm_configured():
        raise AppError("No model API key is configured on the server.")

    thread_id = body.thread_id or uuid.uuid4().hex[:16]

    async def handler(job_id: str, emit):
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        from app.agent.run import answer_question

        await emit({"type": "stage", "stage": "thinking", "message": "Reading the schema"})
        cap = settings().question_timeout_s
        try:
            async with AsyncSqliteSaver.from_conn_string(str(settings().checkpoints_db)) as saver:
                result = await asyncio.wait_for(
                    answer_question(
                        body.dataset_id, body.question,
                        thread_id=thread_id, checkpointer=saver, on_event=emit,
                    ),
                    timeout=cap,
                )
        except TimeoutError as exc:
            # The per-request timeout should catch a hung provider long before this. This
            # is the backstop that stops one question holding a worker slot indefinitely.
            raise QueryTimeout(
                f"The question was still running after {cap}s and was stopped. "
                "Try narrowing it, or ask again."
            ) from exc
        result["thread_id"] = thread_id
        return result

    return _accepted(jobs.submit("query", handler, dataset_id=body.dataset_id,
                                 payload={"question": body.question, "thread_id": thread_id}))


# --------------------------------------------------------------------- threads

@app.get("/api/threads", tags=["threads"])
async def list_threads(dataset_id: str | None = Query(None), limit: int = Query(100, ge=1, le=500)):
    """Conversations, most recently used first. Titled by the question that started them."""
    return {"threads": jobs.list_threads(dataset_id, limit)}


@app.get("/api/threads/{thread_id}", tags=["threads"])
async def get_thread(thread_id: str):
    """Replay a conversation: every question with the answer it produced."""
    turns = jobs.thread_turns(thread_id)
    if not turns:
        raise NotFound(f"Thread '{thread_id}' not found")
    return {"thread_id": thread_id, "turns": turns}


async def _forget_checkpoints(thread_ids: list[str]) -> None:
    """Drop the agent's memory of these conversations. One saver for the whole batch."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    if not thread_ids:
        return
    try:
        async with AsyncSqliteSaver.from_conn_string(str(settings().checkpoints_db)) as saver:
            for tid in thread_ids:
                try:
                    await saver.adelete_thread(tid)
                except Exception as exc:  # pragma: no cover - the visible log is already gone
                    print(f"[threads] checkpoint delete failed for {tid}: {exc}", flush=True)
    except Exception as exc:  # pragma: no cover
        print(f"[threads] could not open the checkpoint store: {exc}", flush=True)


@app.delete("/api/threads", tags=["threads"])
async def delete_all_threads(dataset_id: str | None = Query(None)):
    """Delete every conversation, or every conversation for one dataset.

    Returns the count rather than 204 so the caller can tell the user what happened.
    """
    thread_ids = jobs.all_thread_ids(dataset_id)
    for tid in thread_ids:
        jobs.delete_thread(tid)
    await _forget_checkpoints(thread_ids)
    return {"deleted": len(thread_ids)}


@app.delete("/api/threads/{thread_id}", status_code=204, tags=["threads"])
async def delete_thread(thread_id: str):
    """Forget a conversation, both the visible log and the agent's memory of it."""
    if not jobs.thread_turns(thread_id):
        raise NotFound(f"Thread '{thread_id}' not found")
    jobs.delete_thread(thread_id)
    # Without this the log would be gone but the agent would still remember the
    # conversation the next time that id came round.
    await _forget_checkpoints([thread_id])
    return JSONResponse(status_code=204, content=None)


# --------------------------------------------------------------------- jobs

@app.get("/api/jobs", tags=["jobs"])
async def list_jobs(limit: int = Query(50, ge=1, le=200)):
    return {"jobs": jobs.list(limit)}


@app.get("/api/jobs/{job_id}", tags=["jobs"])
async def get_job(job_id: str):
    return jobs.get(job_id)


@app.get("/api/jobs/{job_id}/events", tags=["jobs"])
async def stream_job(job_id: str, request: Request):
    """Server-sent events for one job. Terminates when the job reaches a terminal state."""
    jobs.get(job_id)  # 404 before opening the stream

    async def gen():
        try:
            async for event in jobs.events(job_id):
                if await request.is_disconnected():
                    return
                yield f"data: {json.dumps(event, default=str)}\n\n"
        except asyncio.CancelledError:  # pragma: no cover
            return
        except Exception as exc:  # pragma: no cover
            yield f"data: {json.dumps({'type': 'done', 'status': 'failed', 'error': {'code': 'stream_error', 'message': str(exc)[:200]}})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------- UI

if UI_DIST.exists():
    app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.startswith("api/"):
            raise NotFound("Unknown endpoint")
        candidate = UI_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(UI_DIST / "index.html")
else:
    @app.get("/", include_in_schema=False)
    async def no_ui():
        return {"message": "API is running. Build the UI with: cd ui && npm install && npm run build",
                "docs": "/docs", "health": "/health"}
