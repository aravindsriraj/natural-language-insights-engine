"""Schema context built from the file itself.

Two layers, deliberately separated:

  Layer 1 (statistics) is pure SQL and contains no judgment. It is what the agent would
  otherwise have to rediscover with five to ten exploratory queries on every question, so
  we compute it once at ingest and cache it. This layer is deterministic and unit-tested.

  Layer 2 (semantics) is a single model call at ingest that reads layer 1 and assigns a
  role and description to each column plus table-level notes. It is deliberately NOT a
  keyword or regex heuristic: a hardcoded list of English money words would be exactly the
  kind of dataset-specific assumption this system must not contain, and it misleads the
  agent when wrong. Running it once per dataset rather than per question means every
  question shares one stable interpretation.

Layer 2 fails soft. Without an API key, or on a malformed response, the dataset keeps
layer 1, which is the floor the agent actually needs.
"""
from __future__ import annotations

import json
from typing import Any

from app.core.ingest import load_doc, meta_path
from app.core.store import TABLE, reader

MAX_TOP_VALUES = 10
TOP_VALUE_CARDINALITY_LIMIT = 50
MAX_TOP_VALUE_COLUMNS = 40

NUMERIC_PREFIXES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT",
                    "USMALLINT", "UINTEGER", "UBIGINT", "FLOAT", "DOUBLE", "DECIMAL", "REAL")
TEMPORAL_PREFIXES = ("DATE", "TIMESTAMP", "TIME")


def is_numeric(duck_type: str) -> bool:
    return duck_type.upper().startswith(NUMERIC_PREFIXES)


def is_temporal(duck_type: str) -> bool:
    return duck_type.upper().startswith(TEMPORAL_PREFIXES)


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------- layer 1: statistics

def compute_stats(dataset_id: str) -> dict:
    """Single-pass column statistics. No model, no heuristics, no judgment.

    Distinct counts are exact rather than approximate. HyperLogLog is cheaper but can
    report more distinct values than there are rows, which makes uniqueness detection
    unreliable and confuses anything reading the profile. On a file small enough to upload,
    the exact count costs milliseconds.
    """
    doc = load_doc(dataset_id) if meta_path(dataset_id).exists() else {}
    display_names = doc.get("display_names", {})

    con = reader(dataset_id)
    try:
        desc = con.execute(f"SELECT * FROM {TABLE} LIMIT 0").description
        columns = [(d[0], str(d[1])) for d in desc]
        types = dict(
            con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                f"WHERE table_name = '{TABLE}' ORDER BY ordinal_position"
            ).fetchall()
        )
        row_count = con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]

        parts: list[str] = []
        for name, _ in columns:
            c = _q(name)
            parts += [
                f"count({c})",
                f"count(DISTINCT {c})",
                f"min({c})::VARCHAR",
                f"max({c})::VARCHAR",
            ]
        agg: list[Any] = []
        try:
            agg = list(con.execute(f"SELECT {', '.join(parts)} FROM {TABLE}").fetchone())
        except Exception:
            # A type that resists min/max (nested, blob). Fall back per column so one bad
            # column cannot cost us the profile for every other column.
            for name, _ in columns:
                c = _q(name)
                row: list[Any] = []
                for expr in (f"count({c})", f"count(DISTINCT {c})",
                             f"min({c})::VARCHAR", f"max({c})::VARCHAR"):
                    try:
                        row.append(con.execute(f"SELECT {expr} FROM {TABLE}").fetchone()[0])
                    except Exception:
                        row.append(None)
                agg += row

        sample_rows = con.execute(f"SELECT * FROM {TABLE} LIMIT 5").fetchall()

        cols: list[dict] = []
        for i, (name, _pytype) in enumerate(columns):
            non_null, distinct, cmin, cmax = agg[i * 4: i * 4 + 4]
            dtype = types.get(name, "VARCHAR")
            samples = [_jsonable(r[i]) for r in sample_rows if r[i] is not None][:3]
            col = {
                "name": name,
                "display_name": display_names.get(name, name),
                "type": dtype,
                "null_count": row_count - (non_null or 0),
                "null_rate": round(1 - (non_null or 0) / row_count, 4) if row_count else 0.0,
                "distinct_count": distinct,
                "min": _jsonable(cmin),
                "max": _jsonable(cmax),
                "samples": samples,
                "top_values": [],
                "has_negatives": False,
                "unique": bool(distinct and row_count and distinct >= row_count * 0.99),
            }
            if is_numeric(dtype):
                try:
                    col["has_negatives"] = bool(cmin is not None and float(cmin) < 0)
                except (TypeError, ValueError):
                    col["has_negatives"] = False
            cols.append(col)

        # Top values only where they are informative and cheap.
        budget = MAX_TOP_VALUE_COLUMNS
        for col in cols:
            if budget <= 0:
                break
            d = col["distinct_count"] or 0
            if d == 0 or d > TOP_VALUE_CARDINALITY_LIMIT or col["unique"]:
                continue
            try:
                rows = con.execute(
                    f"SELECT {_q(col['name'])} AS v, count(*) AS n FROM {TABLE} "
                    f"WHERE {_q(col['name'])} IS NOT NULL "
                    f"GROUP BY 1 ORDER BY n DESC LIMIT {MAX_TOP_VALUES}"
                ).fetchall()
                col["top_values"] = [{"value": _jsonable(v), "count": n} for v, n in rows]
                budget -= 1
            except Exception:
                continue
    finally:
        con.close()

    return {"row_count": row_count, "column_count": len(cols), "columns": cols}


# ---------------------------------------------------------------- layer 2: semantics

ROLES = ["date", "money", "quantity", "identifier", "category", "boolean", "text", "other"]

_ANNOTATE_PROMPT = """You are profiling an unfamiliar transactional dataset so a SQL agent can query it.

Table name: `dataset`
Row count: {row_count}

Columns (name, SQL type, distinct values, null rate, min, max, sample values, most common values):
{columns}

For EVERY column return:
  - name: exactly as given
  - role: one of {roles}
  - description: one short sentence saying what this column holds, based only on the evidence above

Then return table-level notes:
  - revenue_expression: a SQL expression over these columns giving per-row monetary value,
    or null if this dataset has no monetary measure. Prefer an existing total/amount column
    over recomputing it from components.
  - has_returns: true if negative quantities or negative amounts indicate returns, refunds or cancellations
  - returns_note: one sentence on how returns should be treated in revenue totals, or null
  - grain: one sentence describing what a single row represents
  - time_column: the column best used for time-series analysis, or null
  - entity_columns: object mapping any of customer/product/location to the best column name, omit what is absent
  - caveats: list of short warnings a query author should know (mixed types, high null rates, pre-aggregation, ambiguity)

Base every statement on the evidence given. Do not invent columns. If evidence is weak, say so in caveats."""


def _column_line(c: dict) -> str:
    bits = [f"- {c['name']} ({c['type']})",
            f"distinct={c['distinct_count']}",
            f"nulls={c['null_rate']:.1%}"]
    if c["min"] is not None:
        bits.append(f"min={c['min']!r} max={c['max']!r}")
    if c["samples"]:
        bits.append(f"samples={c['samples']!r}")
    if c["top_values"]:
        top = ", ".join(f"{t['value']!r}({t['count']})" for t in c["top_values"][:5])
        bits.append(f"common=[{top}]")
    if c["has_negatives"]:
        bits.append("contains negatives")
    return " | ".join(bits)


def annotate(stats: dict) -> dict:
    """One model call per dataset. Raises on failure; callers decide whether to degrade."""
    from pydantic import BaseModel, Field

    from app.agent.llm import get_model

    class ColumnNote(BaseModel):
        name: str
        role: str
        description: str

    class Semantics(BaseModel):
        columns: list[ColumnNote]
        revenue_expression: str | None = None
        has_returns: bool = False
        returns_note: str | None = None
        grain: str | None = None
        time_column: str | None = None
        entity_columns: dict[str, str] = Field(default_factory=dict)
        caveats: list[str] = Field(default_factory=list)

    prompt = _ANNOTATE_PROMPT.format(
        row_count=stats["row_count"],
        columns="\n".join(_column_line(c) for c in stats["columns"]),
        roles=", ".join(ROLES),
    )
    result = get_model().with_structured_output(Semantics).invoke(prompt)
    return json.loads(result.model_dump_json())


def build_profile(dataset_id: str, *, use_model: bool = True) -> dict:
    """Compute and persist the profile. Layer 2 failure degrades, it does not raise."""
    profile = compute_stats(dataset_id)
    profile["semantics"] = None
    profile["semantics_error"] = None

    if use_model:
        from app.config import llm_configured

        if not llm_configured():
            profile["semantics_error"] = "no_api_key"
        else:
            try:
                sem = annotate(profile)
                by_name = {c["name"]: c for c in profile["columns"]}
                for note in sem.get("columns", []):
                    if (col := by_name.get(note["name"])) is not None:
                        col["role"] = note["role"] if note["role"] in ROLES else "other"
                        col["description"] = note["description"]
                profile["semantics"] = {k: v for k, v in sem.items() if k != "columns"}
            except Exception as exc:  # degrade, never block ingest
                profile["semantics_error"] = f"{type(exc).__name__}: {exc}"[:300]

    doc = load_doc(dataset_id)
    doc["profile"] = profile
    meta_path(dataset_id).write_text(json.dumps(doc, indent=2, default=str))
    return profile


def get_profile(dataset_id: str) -> dict:
    doc = load_doc(dataset_id)
    if "profile" not in doc:
        return build_profile(dataset_id, use_model=False)
    return doc["profile"]


def set_column_role(dataset_id: str, column: str, role: str, description: str | None = None) -> dict:
    """User correction of an inferred role. Inference is visible and fixable, not silent."""
    from app.errors import BadRequest, NotFound

    if role not in ROLES:
        raise BadRequest(f"role must be one of: {', '.join(ROLES)}")
    doc = load_doc(dataset_id)
    profile = doc.get("profile") or build_profile(dataset_id, use_model=False)
    for col in profile["columns"]:
        if col["name"] == column:
            col["role"] = role
            col["role_source"] = "user"
            if description:
                col["description"] = description
            doc["profile"] = profile
            meta_path(dataset_id).write_text(json.dumps(doc, indent=2, default=str))
            return profile
    raise NotFound(f"Column '{column}' not found in dataset '{dataset_id}'")


# ---------------------------------------------------------------- prompt rendering

def render_compact(profile: dict, meta: dict) -> str:
    """What the agent sees on every turn.

    Every column name and type is always listed, so the agent never has to guess what
    exists. Per-column statistics are pulled on demand via describe_columns, which keeps
    this bounded for very wide files.
    """
    sem = profile.get("semantics") or {}
    lines = [
        f"Table: `dataset`  ({profile['row_count']:,} rows, {profile['column_count']} columns)",
        f"Source file: {meta.get('source_filename', 'unknown')}",
        "",
        "Columns:",
    ]
    for c in profile["columns"]:
        bits = [f"  {c['name']} :: {c['type']}"]
        if c.get("role"):
            bits.append(f"[{c['role']}]")
        if c.get("description"):
            bits.append(f"- {c['description']}")
        lines.append(" ".join(bits))

    if sem:
        lines += ["", "Dataset notes:"]
        if sem.get("grain"):
            lines.append(f"  Grain: {sem['grain']}")
        if sem.get("revenue_expression"):
            lines.append(f"  Monetary value per row: {sem['revenue_expression']}")
        if sem.get("time_column"):
            col = next((c for c in profile["columns"] if c["name"] == sem["time_column"]), None)
            span = f" spanning {col['min']} to {col['max']}" if col and col.get("min") else ""
            lines.append(f"  Time column: {sem['time_column']}{span}")
            if span:
                lines.append("  Check this range covers any period asked about before answering.")
        if sem.get("entity_columns"):
            ents = ", ".join(f"{k}={v}" for k, v in sem["entity_columns"].items())
            lines.append(f"  Entities: {ents}")
        if sem.get("has_returns"):
            lines.append(f"  Returns present. {sem.get('returns_note') or ''}".rstrip())
        for c in sem.get("caveats", [])[:6]:
            lines.append(f"  Caveat: {c}")
    elif profile.get("semantics_error"):
        lines += ["", f"(Semantic annotation unavailable: {profile['semantics_error']}. "
                      "Statistics below are still exact; infer meaning from the data itself.)"]
    return "\n".join(lines)


def describe(profile: dict, columns: list[str] | None = None) -> str:
    """Full statistics for named columns. Backs the describe_columns tool."""
    wanted = {c.lower() for c in columns} if columns else None
    out = []
    for c in profile["columns"]:
        if wanted and c["name"].lower() not in wanted:
            continue
        out.append(
            f"{c['name']} :: {c['type']}"
            + (f" [{c['role']}]" if c.get("role") else "")
            + f"\n  distinct={c['distinct_count']}  nulls={c['null_rate']:.1%}"
            + (f"  min={c['min']!r}  max={c['max']!r}" if c["min"] is not None else "")
            + ("  contains negatives" if c["has_negatives"] else "")
            + (f"\n  samples: {c['samples']!r}" if c["samples"] else "")
            + ("\n  most common: " + ", ".join(f"{t['value']!r} ({t['count']:,})"
                                                for t in c["top_values"]) if c["top_values"] else "")
            + (f"\n  {c['description']}" if c.get("description") else "")
        )
    if not out:
        known = ", ".join(c["name"] for c in profile["columns"])
        return f"No such column(s). Available columns: {known}"
    return "\n\n".join(out)
