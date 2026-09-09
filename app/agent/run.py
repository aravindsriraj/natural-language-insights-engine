"""Single entry point for asking a question. Used by the API, the CLI and the eval harness."""
from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from app.agent.agent import build_agent, invoke_config
from app.agent.middleware import UsageTracker
from app.agent.schema import Answer
from app.agent.tools import default_context
from app.core.ingest import load_meta  # noqa: F401  (validates dataset exists early)


def _finalise(state: dict, trail: list[dict], tracker: UsageTracker, elapsed_ms: int) -> dict:
    structured: Answer | None = state.get("structured_response")
    if structured is None:
        msgs = state.get("messages", [])
        text = next(
            (m.content for m in reversed(msgs) if getattr(m, "type", "") == "ai" and m.content),
            "",
        )
        structured = Answer(
            answer=str(text) or "The agent stopped before producing an answer.",
            refused=not text,
            refusal_reason=None if text else "Agent halted without an answer.",
            confidence="low",
        )
    body = structured.model_dump()
    executed = [e for e in trail if e.get("type") == "sql_ok"]
    body["queries"] = [
        {k: e[k] for k in ("sql", "purpose", "row_count", "elapsed_ms", "truncated") if k in e}
        for e in executed
    ]
    body["result"] = (
        {"columns": executed[-1]["columns"], "rows": executed[-1]["rows"]} if executed else None
    )
    body["rejected_queries"] = [
        {"sql": e["sql"], "reason": e.get("reason") or e.get("error")}
        for e in trail
        if e.get("type") in ("sql_rejected", "sql_error")
    ]
    body["usage"] = tracker.as_dict()
    body["elapsed_ms"] = elapsed_ms
    return body


async def answer_question(
    dataset_id: str,
    question: str,
    *,
    thread_id: str,
    checkpointer=None,
    on_event=None,
) -> dict:
    """Run the agent to completion. `on_event` receives progress events as they happen."""
    load_meta(dataset_id)
    tracker = UsageTracker()
    agent = build_agent(tracker=tracker, checkpointer=checkpointer)
    trail: list[dict] = []
    started = time.perf_counter()

    final_state: dict[str, Any] = {}
    async for mode, chunk in agent.astream(
        {"messages": [{"role": "user", "content": question}]},
        config=invoke_config(thread_id),
        context=default_context(dataset_id),
        stream_mode=["custom", "values"],
    ):
        if mode == "custom":
            trail.append(chunk)
            if on_event:
                await on_event(chunk)
        elif mode == "values":
            final_state = chunk

    return _finalise(final_state, trail, tracker, int((time.perf_counter() - started) * 1000))


async def stream_events(*args, **kwargs) -> AsyncIterator[dict]:  # pragma: no cover
    raise NotImplementedError
