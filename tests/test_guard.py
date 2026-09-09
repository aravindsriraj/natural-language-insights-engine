"""The SQL guard. If this regresses, the agent can reach outside the dataset."""
from __future__ import annotations

import pytest

from app.core.guard import validate
from app.errors import UnsafeQuery

ATTACKS = [
    ("write", "DROP TABLE dataset"),
    ("write", "DELETE FROM dataset"),
    ("write", "UPDATE dataset SET quantity = 0"),
    ("write", "INSERT INTO dataset VALUES (1)"),
    ("write", "CREATE TABLE evil AS SELECT 1"),
    ("write", "ALTER TABLE dataset ADD COLUMN x INT"),
    ("stacked", "SELECT 1 FROM dataset; DROP TABLE dataset"),
    ("stacked", "SELECT 1 FROM dataset;SELECT 2 FROM dataset"),
    ("file_read", "SELECT * FROM read_csv('/etc/passwd')"),
    ("file_read", "SELECT * FROM read_csv_auto('/etc/passwd')"),
    ("file_read", "SELECT * FROM read_parquet('/tmp/x.parquet')"),
    ("file_read", "SELECT * FROM read_json('/tmp/x.json')"),
    ("file_read", "SELECT read_text('/etc/passwd')"),
    ("file_read", "SELECT * FROM glob('/**')"),
    ("file_read", "SELECT * FROM dataset WHERE a IN (SELECT * FROM read_csv('/etc/passwd'))"),
    ("exfiltrate", "COPY dataset TO '/tmp/leak.csv'"),
    ("attach", "ATTACH '/tmp/other.db' AS other"),
    ("extension", "INSTALL httpfs"),
    ("extension", "LOAD httpfs"),
    ("cross_table", "SELECT * FROM secrets"),
    ("cross_table", "SELECT * FROM dataset JOIN secrets ON 1=1"),
    ("cross_table", "SELECT * FROM information_schema.columns"),
    ("empty", ""),
    ("empty", "   "),
    ("garbage", "this is not sql at all !!!"),
]


@pytest.mark.parametrize("kind,sql", ATTACKS, ids=[f"{k}:{s[:28]}" for k, s in ATTACKS])
def test_rejects(kind, sql):
    with pytest.raises(UnsafeQuery):
        validate(sql, max_rows=100)


LEGITIMATE = [
    "SELECT * FROM dataset",
    "SELECT country, sum(revenue) FROM dataset GROUP BY 1 ORDER BY 2 DESC",
    "WITH t AS (SELECT a FROM dataset) SELECT count(*) FROM t",
    "SELECT a.x FROM dataset a JOIN dataset b ON a.id = b.id",
    "SELECT * FROM dataset WHERE d BETWEEN DATE '2024-01-01' AND DATE '2024-03-31'",
    "(SELECT 1 FROM dataset) UNION ALL (SELECT 2 FROM dataset)",
    "SELECT strftime(d, '%Y-%m') m, sum(v) FROM dataset GROUP BY 1",
    "SELECT * FROM dataset QUALIFY row_number() OVER (PARTITION BY a ORDER BY b) = 1",
]


@pytest.mark.parametrize("sql", LEGITIMATE)
def test_allows(sql):
    assert "LIMIT" in validate(sql, max_rows=100).upper()


def test_injects_limit_when_absent():
    assert validate("SELECT * FROM dataset", max_rows=25).upper().endswith("LIMIT 25")


def test_clamps_limit_above_cap():
    out = validate("SELECT * FROM dataset LIMIT 999999", max_rows=25)
    assert "LIMIT 25" in out.upper()
    assert "999999" not in out


def test_preserves_smaller_limit():
    assert "LIMIT 5" in validate("SELECT * FROM dataset LIMIT 5", max_rows=100).upper()


def test_rejection_message_is_actionable():
    """The message goes to the agent as a tool result, so it has to say what to do."""
    with pytest.raises(UnsafeQuery) as exc:
        validate("SELECT * FROM other", max_rows=10)
    assert "dataset" in str(exc.value)
