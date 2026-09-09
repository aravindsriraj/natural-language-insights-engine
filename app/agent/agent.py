"""Agent assembly.

A ReAct loop, constrained. The topology is deliberately not a fixed pipeline: on a CSV it
has never seen, the agent needs to look before it writes. A one-shot generate-and-execute
pipeline only reacts to errors, so a query that succeeds and returns something meaningless
reaches the user unchallenged.

The cost of that freedom is variance, which is why every limit below is explicit.
"""
from __future__ import annotations

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ClearToolUsesEdit,
    ContextEditingMiddleware,
    ModelCallLimitMiddleware,
    ModelFallbackMiddleware,
    ModelRetryMiddleware,
    PIIMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)

from app.agent.llm import fallback_models, get_model
from app.agent.middleware import (
    UsageTracker,
    grounding_middleware,
    schema_injection_middleware,
    usage_middleware,
)
from app.agent.prompts import system_prompt
from app.agent.schema import Answer
from app.agent.tools import AgentContext, build_tools, default_context
from app.config import settings


def build_agent(*, tracker: UsageTracker | None = None, checkpointer=None):
    """Construct the agent. One per request is fine; the model client is cached."""
    s = settings()
    models = fallback_models()

    middleware = [
        # Domain-specific, hand-written.
        schema_injection_middleware(system_prompt()),
        grounding_middleware(),
        # Shipped with LangChain; configuration, not code.
        ToolCallLimitMiddleware(
            tool_name="run_sql", run_limit=s.agent_max_sql_calls, exit_behavior="continue"
        ),
        ModelCallLimitMiddleware(run_limit=s.agent_max_model_calls, exit_behavior="end"),
        ModelRetryMiddleware(max_retries=2, backoff_factor=2.0, initial_delay=1.0, jitter=True),
        ToolRetryMiddleware(max_retries=1, tools=["run_sql"], on_failure="continue"),
        # Result sets from earlier turns are dead weight once summarised; evicting them
        # keeps a long follow-up thread from growing without bound.
        ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=60_000, keep=3)]),
        # Transactional data routinely contains contact details. The model has no need of them.
        PIIMiddleware("email", strategy="redact", apply_to_tool_results=True),
    ]
    if len(models) > 1:
        middleware.append(ModelFallbackMiddleware(*models[1:]))
    if tracker is not None:
        middleware.append(usage_middleware(tracker))

    return create_agent(
        model=get_model(),
        tools=build_tools(),
        system_prompt=system_prompt(),
        response_format=Answer,
        context_schema=AgentContext,
        middleware=middleware,
        checkpointer=checkpointer,
    )


def invoke_config(thread_id: str) -> dict:
    # One model turn costs roughly eight graph steps once middleware nodes are counted, so
    # the recursion limit is a coarse backstop only. ModelCallLimitMiddleware is the real
    # cap; setting this too low makes it fire first and end the run without an answer.
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": settings().agent_max_model_calls * 10,
    }


__all__ = ["build_agent", "invoke_config", "default_context", "AgentContext", "UsageTracker"]
