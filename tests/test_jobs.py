"""Job lifecycle. If this regresses, work is silently lost or a caller hangs forever."""
from __future__ import annotations

import asyncio

import pytest

from app.core.jobs import JobManager
from app.errors import BadRequest, NotFound


@pytest.fixture
def manager(data_dir):
    m = JobManager(concurrency=2, db_path=data_dir / "jobs.sqlite")
    m.setup()
    return m


async def _drain(m: JobManager, job_id: str, timeout: float = 5.0) -> dict:
    async with asyncio.timeout(timeout):
        while m.get(job_id)["status"] not in ("succeeded", "failed", "interrupted"):
            await asyncio.sleep(0.02)
    return m.get(job_id)


async def test_submit_returns_immediately_and_completes(manager):
    started = asyncio.Event()

    async def handler(job_id, emit):
        started.set()
        await emit({"type": "stage", "stage": "working"})
        await asyncio.sleep(0.05)
        return {"value": 42}

    job_id = manager.submit("test", handler)
    assert manager.get(job_id)["status"] in ("queued", "running")
    job = await _drain(manager, job_id)
    assert job["status"] == "succeeded"
    assert job["result"] == {"value": 42}


async def test_typed_error_reaches_the_caller_intact(manager):
    async def handler(job_id, emit):
        raise BadRequest("that column does not exist")

    job = await _drain(manager, manager.submit("test", handler))
    assert job["status"] == "failed"
    assert job["error"]["code"] == "bad_request"
    assert "column" in job["error"]["message"]


async def test_unexpected_error_is_opaque_to_the_caller(manager):
    """A crash must not leak internals; the caller gets a reference to quote."""

    async def handler(job_id, emit):
        raise RuntimeError("connection string postgres://user:hunter2@internal")

    job = await _drain(manager, manager.submit("test", handler))
    assert job["status"] == "failed"
    assert job["error"]["code"] == "internal_error"
    assert "hunter2" not in str(job["error"])
    assert "reference" in job["error"]["message"]


async def test_concurrency_is_bounded(manager):
    """A burst must queue, not stampede. Concurrency here is two."""
    live = 0
    peak = 0
    release = asyncio.Event()

    async def handler(job_id, emit):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await release.wait()
        live -= 1
        return {}

    ids = [manager.submit("test", handler) for _ in range(6)]
    await asyncio.sleep(0.1)
    assert peak <= 2, f"ran {peak} concurrently with a limit of 2"
    release.set()
    for i in ids:
        assert (await _drain(manager, i))["status"] == "succeeded"


async def test_restart_marks_inflight_jobs_interrupted(manager, data_dir):
    """Partial failure: state is on disk, so a crash is visible rather than a silent hang."""
    job_id = manager.create("ingest", payload={})
    manager._update(job_id, status="running")

    fresh = JobManager(concurrency=2, db_path=data_dir / "jobs.sqlite")
    fresh.setup()
    assert fresh.recover() == 1
    job = fresh.get(job_id)
    assert job["status"] == "interrupted"
    assert "idempotent" in job["error"]["message"]


async def test_events_stream_terminates_on_completion(manager):
    async def handler(job_id, emit):
        for i in range(3):
            await emit({"type": "stage", "stage": f"step{i}"})
            await asyncio.sleep(0.01)
        return {"ok": True}

    job_id = manager.submit("test", handler)
    seen = []
    async with asyncio.timeout(5):
        async for event in manager.events(job_id):
            seen.append(event)
    assert seen[-1]["type"] == "done"
    assert seen[-1]["status"] == "succeeded"


async def test_events_on_already_finished_job_do_not_hang(manager):
    async def handler(job_id, emit):
        return {"ok": True}

    job_id = manager.submit("test", handler)
    await _drain(manager, job_id)
    async with asyncio.timeout(2):
        events = [e async for e in manager.events(job_id)]
    assert events and events[0]["status"] == "succeeded"


async def test_slow_consumer_does_not_block_producer(manager):
    """Backpressure: a full event queue drops the oldest rather than stalling the job."""
    async def handler(job_id, emit):
        for i in range(1000):
            await emit({"type": "stage", "stage": str(i)})
        return {"emitted": 1000}

    job = await _drain(manager, manager.submit("test", handler), timeout=10)
    assert job["status"] == "succeeded"
    assert job["result"]["emitted"] == 1000


def test_unknown_job_is_not_found(manager):
    with pytest.raises(NotFound):
        manager.get("nope")
