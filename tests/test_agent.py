"""Agent guardrails, exercised without a model.

The interlock and the refusal path are the two behaviours the whole trust story rests on,
so they are tested against constructed message histories rather than a live model.
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.middleware import (
    GROUNDING_NUDGE,
    _messages_this_run,
    grounding_middleware,
    refusal_recorded,
    successful_sql_calls,
)
from app.agent.tools import AgentContext
from tests.conftest import CSV_SIMPLE, write_csv


def tool_msg(name: str, content: str) -> ToolMessage:
    return ToolMessage(content=content, name=name, tool_call_id="x")


# ------------------------------------------------------------------ evidence counting

def test_successful_sql_counted_from_tool_results_not_model_claims():
    msgs = [
        HumanMessage(content="q"),
        AIMessage(content="I ran a query and revenue was 5 million"),  # claim, not evidence
        tool_msg("run_sql", "12 row(s) in 4ms\nfoo"),
    ]
    assert successful_sql_calls(msgs) == 1


@pytest.mark.parametrize(
    "content", ["QUERY REJECTED: DROP is not permitted", "QUERY FAILED: no such column",
                "QUERY TIMED OUT after 30s"]
)
def test_failed_queries_are_not_evidence(content):
    assert successful_sql_calls([tool_msg("run_sql", content)]) == 0


def test_describe_columns_is_not_evidence():
    """Reading the profile is not the same as retrieving a number."""
    assert successful_sql_calls([tool_msg("describe_columns", "a :: VARCHAR")]) == 0


def test_refusal_is_detected():
    assert refusal_recorded([tool_msg("refuse", "Refusal recorded: no cost column")])
    assert not refusal_recorded([tool_msg("run_sql", "1 row")])


# ------------------------------------------------------------------ run boundary

def test_run_boundary_ignores_the_interlocks_own_nudge():
    """If the nudge counted as a turn boundary it would hide the evidence and loop forever."""
    msgs = [
        HumanMessage(content="real question"),
        AIMessage(content="", tool_calls=[]),
        tool_msg("run_sql", "5 row(s) in 2ms"),
        HumanMessage(content="you have not run a query", name=GROUNDING_NUDGE),
        AIMessage(content="answer"),
    ]
    assert successful_sql_calls(_messages_this_run(msgs)) == 1


def test_run_boundary_excludes_a_previous_question():
    msgs = [
        HumanMessage(content="first question"),
        tool_msg("run_sql", "5 row(s)"),
        AIMessage(content="first answer"),
        HumanMessage(content="second question"),
        AIMessage(content="second answer"),
    ]
    assert successful_sql_calls(_messages_this_run(msgs)) == 0


# ------------------------------------------------------------------ the interlock

def _check(messages):
    mw = grounding_middleware()
    hook = mw.after_model
    return hook({"messages": messages}, None)


def test_interlock_blocks_an_answer_with_no_query():
    out = _check([HumanMessage(content="what is revenue?"),
                  AIMessage(content="Revenue was about 9 million.")])
    assert out is not None and out["jump_to"] == "model"
    assert out["messages"][0].name == GROUNDING_NUDGE


def test_interlock_allows_an_answer_backed_by_a_query():
    msgs = [HumanMessage(content="q"), tool_msg("run_sql", "1 row(s) in 2ms\n9000000"),
            AIMessage(content="Revenue was 9,000,000.")]
    assert _check(msgs) is None


def test_interlock_allows_an_explicit_refusal():
    msgs = [HumanMessage(content="what is our profit?"),
            tool_msg("refuse", "Refusal recorded: no cost column"),
            AIMessage(content="I cannot answer that.")]
    assert _check(msgs) is None


def test_interlock_ignores_a_turn_that_is_still_working():
    msgs = [HumanMessage(content="q"),
            AIMessage(content="", tool_calls=[{"name": "run_sql", "args": {}, "id": "1"}])]
    assert _check(msgs) is None


def test_interlock_gives_up_rather_than_looping():
    """Two nudges is the ceiling; after that the call limits end the run."""
    msgs = [HumanMessage(content="q")]
    for _ in range(2):
        msgs += [AIMessage(content="answer"),
                 HumanMessage(content="nudge", name=GROUNDING_NUDGE)]
    msgs.append(AIMessage(content="answer"))
    assert _check(msgs) is None


# ------------------------------------------------------------------ tools

def test_run_sql_rejects_unsafe_query_as_a_tool_result(data_dir, tmp_path):
    """A rejection is a turn the agent can learn from, not an exception that kills the run."""
    from app.agent.tools import run_sql
    from app.core.ingest import ingest_csv

    meta = ingest_csv(write_csv(tmp_path, "s.csv", CSV_SIMPLE))
    out = run_sql.invoke({
        "sql": "DROP TABLE dataset", "purpose": "drop it",
        "runtime": _runtime(meta.dataset_id),
    })
    assert out.startswith("QUERY REJECTED")
    assert "try again" in out


def test_run_sql_returns_rows(data_dir, tmp_path):
    from app.agent.tools import run_sql
    from app.core.ingest import ingest_csv

    meta = ingest_csv(write_csv(tmp_path, "s.csv", CSV_SIMPLE))
    out = run_sql.invoke({
        "sql": "SELECT count(*) AS n FROM dataset", "purpose": "count",
        "runtime": _runtime(meta.dataset_id),
    })
    assert "3" in out and "row(s)" in out


@pytest.mark.parametrize("tool_name", ["run_sql", "describe_columns", "refuse"])
def test_no_tool_lets_the_model_choose_the_dataset(tool_name):
    """dataset_id is never a tool argument, so a model cannot point one at another file."""
    import app.agent.tools as tools

    assert "dataset_id" not in getattr(tools, tool_name).args


def _runtime(dataset_id: str):
    from langchain.tools import ToolRuntime

    return ToolRuntime(
        state={"messages": []},
        context=AgentContext(dataset_id=dataset_id),
        config={},
        stream_writer=lambda _e: None,
        tool_call_id="t",
        store=None,
    )



# ------------------------------------------------------------------ tool failure

def _tool_error_middleware(monkeypatch):
    """Build the middleware list. A dummy key lets the model objects construct; nothing
    reaches the network, so the test stays hermetic."""
    from langchain.agents.middleware import ToolErrorMiddleware

    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-not-used")
    from app.agent.agent import build_middleware

    return next(m for m in build_middleware() if isinstance(m, ToolErrorMiddleware))


def test_every_tool_is_covered_by_error_handling(monkeypatch):
    """An exception in any tool must cost one tool call, not the whole run.

    run_sql catches its own failures and returns something the agent can act on.
    describe_columns and refuse do not, so this middleware has to be present and unscoped.
    Without it a corrupt profile turns an answerable question into a 500.
    """
    mw = _tool_error_middleware(monkeypatch)
    assert getattr(mw, "tools", None) in (None, [], ()), (
        "ToolErrorMiddleware must cover every tool, not a subset"
    )


def test_tool_error_message_names_the_type_not_the_detail(monkeypatch):
    """The handler must not pass an exception message through: it can carry paths."""
    mw = _tool_error_middleware(monkeypatch)
    request = type("R", (), {"tool_call": {"name": "describe_columns"}})()
    out = mw.on_error(RuntimeError("/srv/data/secret.json is unreadable"), request)
    assert "RuntimeError" in out
    assert "secret.json" not in out


# ------------------------------------------------------------------ wall-clock bounds

def test_model_calls_are_bounded_in_time_and_retry_only_once(monkeypatch):
    """Every other limit counts things. These are the only two that watch a clock.

    The provider SDK retries by default, underneath ModelRetryMiddleware. Left on, one
    model call could make fourteen attempts with backoff the middleware cannot see, which
    is how a request stalls for minutes with nothing to stop it.
    """
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-not-used")
    from app.agent.llm import get_model
    from app.config import settings

    get_model.cache_clear()
    settings.cache_clear()
    model = get_model()
    assert model.timeout, "a model call with no timeout can hang a worker slot forever"
    assert model.max_retries == 0, "retry policy must live in one place, not two"
    get_model.cache_clear()
