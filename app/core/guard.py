"""SQL guard.

Layered, and the layers are not equally important. The outermost layer is the read-only
DuckDB connection in `store.reader`: it cannot be argued around by a prompt injection or by
a syntax we failed to anticipate. The parse checks here exist to reject bad SQL with a
message the agent can act on, and to stop a runaway scan before it starts.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from app.core.store import TABLE
from app.errors import UnsafeQuery

DIALECT = "duckdb"

# Any of these appearing anywhere in the parsed tree is a hard reject.
FORBIDDEN = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.TruncateTable, exp.Grant, exp.Copy, exp.Command, exp.Attach, exp.Detach,
)

# DuckDB functions that reach outside the database file.
FORBIDDEN_FUNCTIONS = {
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "read_text", "read_blob", "glob", "parquet_scan", "csv_scan", "install", "load",
    "sniff_csv", "delta_scan", "iceberg_scan", "postgres_scan", "sqlite_scan", "mysql_scan",
}

ALLOWED_TABLES = {TABLE}

_FORBIDDEN_TEXT = re.compile(
    r"\b(" + "|".join(sorted(FORBIDDEN_FUNCTIONS)) + r")\s*\(", re.IGNORECASE
)


def _limit_of(node: exp.Expression) -> int | None:
    lim = node.args.get("limit")
    if lim is None:
        return None
    try:
        return int(lim.expression.this)
    except (AttributeError, TypeError, ValueError):
        return None


def validate(sql: str, *, max_rows: int) -> str:
    """Parse, reject anything that is not a bounded read, and return SQL with a LIMIT.

    Raises UnsafeQuery with a message written for the agent, not for an end user.
    """
    if not sql or not sql.strip():
        raise UnsafeQuery("Empty query")

    try:
        statements = sqlglot.parse(sql, dialect=DIALECT)
    except Exception as exc:
        raise UnsafeQuery(f"Could not parse SQL: {exc}") from exc

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsafeQuery(
            f"Expected exactly one statement, found {len(statements)}. "
            "Send a single SELECT."
        )

    stmt = statements[0]

    for node in stmt.walk():
        if isinstance(node, FORBIDDEN):
            raise UnsafeQuery(
                f"{type(node).__name__.upper()} is not permitted. This is a read-only "
                "interface; use SELECT."
            )
        if isinstance(node, exp.Anonymous):
            fn = (node.this or "").lower()
            if fn in FORBIDDEN_FUNCTIONS:
                raise UnsafeQuery(
                    f"Function {fn}() reads outside the dataset and is not permitted. "
                    f"Query the `{TABLE}` table."
                )

    # Belt and braces. sqlglot gives some file-reading functions dedicated node classes
    # (read_csv becomes exp.ReadCSV, not exp.Anonymous), and a future dialect update could
    # add more. A textual check catches whatever the structural walk classifies unexpectedly.
    if (hit := _FORBIDDEN_TEXT.search(sql)) is not None:
        raise UnsafeQuery(
            f"Function {hit.group(1)}() reads outside the dataset and is not permitted. "
            f"Query the `{TABLE}` table."
        )

    if not isinstance(stmt, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise UnsafeQuery(
            f"Only SELECT statements are permitted, got {type(stmt).__name__.upper()}."
        )

    # Every referenced table must be the dataset table. CTE names are local and allowed.
    cte_names = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
    for table in stmt.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            # A table function, e.g. FROM read_parquet(...). Never legitimate here.
            raise UnsafeQuery(
                f"Table functions are not permitted. The only table available is `{TABLE}`."
            )
        name = (table.name or "").lower()
        if not name or name in cte_names:
            continue
        if name not in ALLOWED_TABLES:
            raise UnsafeQuery(
                f"Unknown table '{table.name}'. The only table available is `{TABLE}`."
            )

    existing = _limit_of(stmt)
    if existing is None:
        stmt.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    elif existing > max_rows:
        stmt.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))

    return stmt.sql(dialect=DIALECT)
