"""Agent constraints.

Most production hardening here is configuration of middleware that ships with LangChain:
call limits, retry with backoff, model fallback, context eviction, PII redaction. Only the
pieces below are hand-written, because only they are specific to this domain.
"""
from __future__ import annotations

from langchain.agents.middleware import after_model, wrap_model_call
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.core.ingest import load_meta
from app.core.profile import get_profile, render_compact

FAILURE_PREFIXES = ("QUERY REJECTED", "QUERY FAILED", "QUERY TIMED OUT")


def successful_sql_calls(messages: list) -> int:
    """Count run_sql tool results that actually returned data.

    Deliberately derived from tool results rather than from anything the model asserts.
    """
    n = 0
    for m in messages:
        if isinstance(m, ToolMessage) and m.name == "run_sql":
            content = str(m.content)
            if not content.startswith(FAILURE_PREFIXES):
                n += 1
    return n


def refusal_recorded(messages: list) -> bool:
    return any(isinstance(m, ToolMessage) and m.name == "refuse" for m in messages)


GROUNDING_NUDGE = "grounding_check"


def _messages_this_run(messages: list) -> list:
    """Messages since the last real human turn, so limits are per question, not per thread.

    The interlock's own nudge is a HumanMessage, so it must be excluded here. Counting it
    as a turn boundary would hide the tool results that satisfy the check, and the
    interlock would nudge forever.
    """
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, HumanMessage) and getattr(m, "name", None) != GROUNDING_NUDGE:
            return messages[i:]
    return messages


# ------------------------------------------------------------------ schema injection

def schema_injection_middleware(system_prompt: str):
    """Put the dataset profile in front of the model on every turn.

    The agent is never asked to remember or infer the schema, so it cannot invent a column.
    """

    @wrap_model_call
    async def _inject(request, handler):
        dataset_id = getattr(request.runtime.context, "dataset_id", None)
        if dataset_id:
            try:
                profile = get_profile(dataset_id)
                meta = load_meta(dataset_id)
                context = render_compact(profile, meta.__dict__)
                request = request.override(system_prompt=f"{system_prompt}\n\n---\n\n{context}")
            except Exception:
                pass  # a missing profile must not stop the run; the tools still work
        return await handler(request)

    return _inject


# ------------------------------------------------------------------ grounding interlock

def grounding_middleware():
    """No successful query and no explicit refusal means no answer.

    This is the anti-invention rule, and it is mechanical rather than prompted. A model
    that tries to answer from memory is sent back to work instead of being trusted.
    """

    @after_model(can_jump_to=["model"])
    def _check(state, runtime):
        messages = state["messages"]
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None  # still working

        run = _messages_this_run(messages)
        if successful_sql_calls(run) or refusal_recorded(run):
            return None

        nudges = sum(
            1 for m in run if isinstance(m, HumanMessage) and getattr(m, "name", None) == GROUNDING_NUDGE
        )
        if nudges >= 2:
            return None  # pushed back twice already; let the call limits end it

        return {
            "messages": [
                HumanMessage(
                    name=GROUNDING_NUDGE,
                    content=(
                        "You have not run a successful query, so you cannot answer yet. "
                        "Either call run_sql to get the numbers from the dataset, or call "
                        "refuse if the dataset genuinely cannot answer this question. "
                        "Do not answer from prior knowledge."
                    )
                )
            ],
            "jump_to": "model",
        }

    return _check


# ------------------------------------------------------------------ cost accounting

class UsageTracker:
    """Accumulates token usage across a run. One instance per question."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.model_calls = 0

    def as_dict(self) -> dict:
        return {
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
        }


def usage_middleware(tracker: UsageTracker):
    @wrap_model_call
    async def _track(request, handler):
        response = await handler(request)
        tracker.model_calls += 1
        msg = getattr(response, "result", None) or []
        for m in msg if isinstance(msg, list) else [msg]:
            usage = getattr(m, "usage_metadata", None)
            if usage:
                tracker.input_tokens += usage.get("input_tokens", 0) or 0
                tracker.output_tokens += usage.get("output_tokens", 0) or 0
        return response

    return _track
