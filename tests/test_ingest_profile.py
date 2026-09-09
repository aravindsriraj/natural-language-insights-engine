"""Ingest and the statistics layer.

These are the parts that decide whether an unfamiliar CSV works at all, so the fixtures
are deliberately hostile and none of them resembles the development dataset.
"""
from __future__ import annotations

import pytest

from app.core.ingest import ingest_csv, list_datasets, normalise_column
from app.core.profile import compute_stats, describe, render_compact
from app.errors import IngestError
from tests.conftest import (
    CSV_ALL_NULL_COL,
    CSV_DUPES,
    CSV_LATIN1,
    CSV_MIXED_TYPES,
    CSV_NO_ROWS,
    CSV_ONE_ROW,
    CSV_SEMICOLON,
    CSV_SIMPLE,
    CSV_WEIRD_HEADERS,
    write_csv,
)


def load(tmp_path, name, content, encoding="utf-8"):
    return ingest_csv(write_csv(tmp_path, name, content, encoding))


# ------------------------------------------------------------------ column naming

@pytest.mark.parametrize(
    "raw,expected",
    [("Order #", "order"), ("  spaced  ", "spaced"), ("2nd col", "c_2nd_col"),
     ("", "col_0"), ("Total ($)", "total"), ("already_ok", "already_ok"),
     ("MiXeD", "mixed")],
)
def test_normalise_column(raw, expected):
    assert normalise_column(raw, 0, set()) == expected


def test_normalise_column_dedupes():
    taken = set()
    assert [normalise_column("id", i, taken) for i in range(3)] == ["id", "id_2", "id_3"]


# ------------------------------------------------------------------ ingest

def test_ingests_plain_csv(data_dir, tmp_path):
    meta = load(tmp_path, "s.csv", CSV_SIMPLE)
    assert meta.row_count == 3
    assert meta.column_count == 6
    assert meta.read_options == "utf8_auto"


def test_ingests_semicolon_delimited(data_dir, tmp_path):
    """DuckDB sniffs the delimiter; we must not assume commas."""
    meta = load(tmp_path, "semi.csv", CSV_SEMICOLON)
    assert meta.row_count == 2
    assert meta.column_count == 3


def test_ingests_latin1(data_dir, tmp_path):
    """The original UCI file is latin-1, so this fallback is real, not theoretical."""
    meta = load(tmp_path, "l1.csv", CSV_LATIN1, encoding="latin-1")
    assert meta.row_count == 2
    assert "latin1" in meta.read_options


def test_ingests_duplicate_headers(data_dir, tmp_path):
    meta = load(tmp_path, "d.csv", CSV_DUPES)
    stats = compute_stats(meta.dataset_id)
    names = [c["name"] for c in stats["columns"]]
    assert len(names) == len(set(names)), "duplicate identifiers would break every query"


def test_ingests_weird_headers(data_dir, tmp_path):
    meta = load(tmp_path, "w.csv", CSV_WEIRD_HEADERS)
    stats = compute_stats(meta.dataset_id)
    for c in stats["columns"]:
        assert c["name"].replace("_", "").isalnum()
        assert not c["name"][0].isdigit()


def test_ingests_single_row(data_dir, tmp_path):
    assert load(tmp_path, "one.csv", CSV_ONE_ROW).row_count == 1


def test_rejects_header_only(data_dir, tmp_path):
    with pytest.raises(IngestError, match="no data rows"):
        load(tmp_path, "empty.csv", CSV_NO_ROWS)


def test_rejects_empty_file(data_dir, tmp_path):
    with pytest.raises(IngestError):
        load(tmp_path, "zero.csv", "")


def test_failed_ingest_leaves_no_dataset(data_dir, tmp_path):
    with pytest.raises(IngestError):
        load(tmp_path, "bad.csv", CSV_NO_ROWS)
    assert list_datasets() == []


def test_ingest_is_idempotent_by_content(data_dir, tmp_path):
    """A retried job must converge, not create a second copy."""
    a = load(tmp_path, "a.csv", CSV_SIMPLE)
    b = ingest_csv(write_csv(tmp_path, "renamed.csv", CSV_SIMPLE))
    assert a.dataset_id == b.dataset_id
    assert len(list_datasets()) == 1


def test_different_content_gives_different_dataset(data_dir, tmp_path):
    a = load(tmp_path, "a.csv", CSV_SIMPLE)
    b = load(tmp_path, "b.csv", CSV_ONE_ROW)
    assert a.dataset_id != b.dataset_id
    assert len(list_datasets()) == 2


# ------------------------------------------------------------------ statistics

def test_stats_are_exact(data_dir, tmp_path):
    meta = load(tmp_path, "s.csv", CSV_SIMPLE)
    stats = compute_stats(meta.dataset_id)
    by = {c["name"]: c for c in stats["columns"]}
    assert stats["row_count"] == 3
    assert by["units"]["has_negatives"] is True, "a return would be invisible without this"
    assert by["each"]["has_negatives"] is False
    assert by["order_ref"]["unique"] is True
    assert by["who"]["distinct_count"] == 2
    assert by["widget"]["top_values"][0]["value"] == "BOLT"


def test_stats_handle_all_null_column(data_dir, tmp_path):
    meta = load(tmp_path, "n.csv", CSV_ALL_NULL_COL)
    by = {c["name"]: c for c in compute_stats(meta.dataset_id)["columns"]}
    assert by["b"]["null_rate"] == 1.0
    assert by["b"]["samples"] == []


def test_stats_handle_mixed_types(data_dir, tmp_path):
    """A column that defeats type inference must not lose us the whole profile."""
    meta = load(tmp_path, "m.csv", CSV_MIXED_TYPES)
    stats = compute_stats(meta.dataset_id)
    assert stats["column_count"] == 2
    assert all("type" in c for c in stats["columns"])


def test_stats_have_no_semantic_layer(data_dir, tmp_path):
    """Layer 1 is pure statistics. Roles come from the model, never from a keyword rule."""
    meta = load(tmp_path, "s.csv", CSV_SIMPLE)
    for c in compute_stats(meta.dataset_id)["columns"]:
        assert "role" not in c


# ------------------------------------------------------------------ prompt rendering

def test_compact_profile_lists_every_column(data_dir, tmp_path):
    """The agent must never have to guess what exists, however wide the file."""
    meta = load(tmp_path, "s.csv", CSV_SIMPLE)
    stats = compute_stats(meta.dataset_id)
    stats["semantics"] = None
    rendered = render_compact(stats, meta.__dict__)
    for c in stats["columns"]:
        assert c["name"] in rendered


def test_describe_unknown_column_lists_alternatives(data_dir, tmp_path):
    meta = load(tmp_path, "s.csv", CSV_SIMPLE)
    out = describe(compute_stats(meta.dataset_id), ["does_not_exist"])
    assert "No such column" in out and "widget" in out


def test_describe_is_case_insensitive(data_dir, tmp_path):
    meta = load(tmp_path, "s.csv", CSV_SIMPLE)
    assert "widget" in describe(compute_stats(meta.dataset_id), ["WIDGET"])
