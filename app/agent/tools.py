"""Agent tools.

Three tools, and each earns its place. `run_sql` is the only path to data; there is no
second door, which is what makes the guard unbypassable. `describe_columns` serves cached
statistics at zero query cost so the prompt can stay compact on very wide files. `refuse`
makes declining an explicit, structured, testable action rather than an absence of output.

Two rules about inputs:

  `dataset_id` is never a tool argument. It arrives through typed runtime context set by
  the caller. A model-supplied dataset id could be hallucinated, and would let one question
  read a file it was never pointed at.

  The executed SQL trail is captured here, at the tool boundary, and emitted through the
  stream writer. The model's own account of what it ran is not evidence.
"""
from __future__ import annotations

from dataclasses import dataclass

from langchain.tools import ToolRuntime, tool

from app.config import settings
from app.core.guard import validate
from app.core.profile import describe, get_profile
from app.core.store import run_query
from app.errors import QueryTimeout, UnsafeQuery


@dataclass
class AgentContext:
    """Runtime context. Set by the job layer, never by the model."""

    dataset_id: str
    max_rows: int = 1000
    timeout_s: int = 30


def _emit(runtime: ToolRuntime, event: dict) -> None:
    writer = getattr(runtime, "stream_writer", None)
    if writer:
        try:
            writer(event)
        except Exception:  # pragma: no cover - streaming must never break the run
            pass


def _render_rows(columns: list[str], rows: list[list], limit: int = 50) -> str:
    if not rows:
        return "(0 rows)"
    head = rows[:limit]
    body = "\n".join(" | ".join("NULL" if v is None else str(v) for v in r) for r in head)
    more = f"\n… {len(rows) - len(head)} more rows returned" if len(rows) > len(head) else ""
    return f"{' | '.join(columns)}\n{'-' * 40}\n{body}{more}"


@tool
def run_sql(sql: str, purpose: str, runtime: ToolRuntime[AgentContext]) -> str:
    """Run one read-only SQL SELECT against the dataset and return the rows.

    This is the only way to see data. Use it to explore before you answer: check row
    counts, inspect distinct values, verify an assumption. Then run the query that answers
    the question.

    Args:
        sql: A single SELECT statement against the table `dataset`. Read-only.
        purpose: One short sentence saying what you expect this query to establish.
    """
    ctx = runtime.context
    _emit(runtime, {"type": "sql_start", "purpose": purpose, "sql": sql})
    try:
        safe_sql = validate(sql, max_rows=ctx.max_rows)
    except UnsafeQuery as exc:
        _emit(runtime, {"type": "sql_rejected", "sql": sql, "reason": exc.message})
        return f"QUERY REJECTED: {exc.message}\nRewrite the query and try again."

    try:
        result = run_query(ctx.dataset_id, safe_sql, max_rows=ctx.max_rows,
                           timeout_s=ctx.timeout_s)
    except QueryTimeout as exc:
        _emit(runtime, {"type": "sql_error", "sql": safe_sql, "error": exc.message})
        return (f"QUERY TIMED OUT after {ctx.timeout_s}s. {exc.message}\n"
                "Narrow the query: aggregate more, filter more, or reduce the join.")
    except Exception as exc:
        msg = str(exc).split("\n")[0][:300]
        _emit(runtime, {"type": "sql_error", "sql": safe_sql, "error": msg})
        return f"QUERY FAILED: {msg}\nFix the query and try again."

    _emit(runtime, {
        "type": "sql_ok",
        "purpose": purpose,
        "sql": safe_sql,
        "row_count": result.row_count,
        "elapsed_ms": result.elapsed_ms,
        "truncated": result.truncated,
        "columns": result.columns,
        "rows": result.rows[:200],
    })
    note = f"\n(Result truncated at {ctx.max_rows} rows.)" if result.truncated else ""
    return (f"{result.row_count} row(s) in {result.elapsed_ms}ms\n"
            f"{_render_rows(result.columns, result.rows)}{note}")


@tool
def describe_columns(columns: list[str], runtime: ToolRuntime[AgentContext]) -> str:
    """Get exact statistics for named columns: distinct counts, null rate, min, max,
    sample values and the most common values.

    These are precomputed at load time, so this costs nothing and is faster than querying.
    Use it before writing a filter, to check how a value is actually spelled in the data.

    Args:
        columns: Column names to describe. Pass an empty list for every column.
    """
    profile = get_profile(runtime.context.dataset_id)
    _emit(runtime, {"type": "describe", "columns": columns})
    return describe(profile, columns or None)


@tool
def refuse(reason: str, missing_concepts: list[str], runtime: ToolRuntime[AgentContext]) -> str:
    """Decline a question this dataset cannot answer.

    Call this when the data lacks what the question needs, for example a question about
    profit when there is no cost column, or a question about something not in this dataset
    at all. Refusing correctly is a good outcome. Do not guess, and do not substitute a
    different question you can answer.

    Args:
        reason: One or two sentences explaining, in plain language, what is missing.
        missing_concepts: The specific columns or concepts the dataset would need.
    """
    _emit(runtime, {"type": "refusal", "reason": reason, "missing": missing_concepts})
    return (f"Refusal recorded: {reason}\n"
            "Now produce your final response with refused=true, repeating this reason. "
            "Do not run further queries.")


def build_tools() -> list:
    return [run_sql, describe_columns, refuse]


def default_context(dataset_id: str) -> AgentContext:
    s = settings()
    return AgentContext(dataset_id=dataset_id, max_rows=s.max_result_rows,
                        timeout_s=s.query_timeout_s)
