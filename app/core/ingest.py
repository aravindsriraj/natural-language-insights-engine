"""CSV to queryable table.

Repeatable, not manual: the same call is used by the HTTP upload path, the test suite,
and the seed script. Idempotent by file content hash, so a retried or duplicated job
converges on the same dataset instead of creating a second copy.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from app.config import settings
from app.core.store import TABLE, db_path, writer
from app.errors import IngestError, NotFound

_SAFE = re.compile(r"[^0-9a-zA-Z_]+")


@dataclass
class DatasetMeta:
    dataset_id: str
    name: str
    source_filename: str
    content_hash: str
    row_count: int
    column_count: int
    created_at: str
    read_options: str


def meta_path(dataset_id: str) -> Path:
    return settings().datasets_dir / f"{dataset_id}.json"


def normalise_column(name: str, index: int, taken: set[str]) -> str:
    """Make a header safe to use as a SQL identifier without losing the original.

    The original is preserved in the profile as `display_name`; only the queryable
    identifier is rewritten, and only when it has to be.
    """
    base = _SAFE.sub("_", (name or "").strip()).strip("_").lower()
    if not base or base[0].isdigit():
        base = f"col_{index}" if not base else f"c_{base}"
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}_{n}"
        n += 1
    taken.add(candidate)
    return candidate


def content_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


# Tried in order. DuckDB sniffs delimiter and quoting itself; what it cannot recover from
# on its own is a non-UTF-8 file or a column whose values defeat type inference.
_READ_ATTEMPTS: list[tuple[str, dict]] = [
    ("utf8_auto", {"sample_size": -1}),
    ("latin1_auto", {"sample_size": -1, "encoding": "'latin-1'"}),
    ("utf8_all_varchar", {"sample_size": -1, "all_varchar": "true"}),
    ("latin1_all_varchar", {"sample_size": -1, "encoding": "'latin-1'", "all_varchar": "true"}),
]


def _read_expr(csv_path: Path, opts: dict) -> str:
    parts = [f"'{str(csv_path).replace(chr(39), chr(39) * 2)}'"]
    parts += [f"{k}={v}" for k, v in opts.items()]
    return f"read_csv({', '.join(parts)})"


def _existing_with_hash(h: str) -> str | None:
    for f in settings().datasets_dir.glob("*.json"):
        try:
            if json.loads(f.read_text()).get("meta", {}).get("content_hash") == h:
                return f.stem
        except (json.JSONDecodeError, OSError):
            continue
    return None


def ingest_csv(csv_path: Path, *, name: str | None = None, dataset_id: str | None = None) -> DatasetMeta:
    """Load a CSV into its own DuckDB file. Returns metadata; profiling is a separate step."""
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise NotFound(f"File not found: {csv_path}")
    if csv_path.stat().st_size == 0:
        raise IngestError("File is empty")

    h = content_hash(csv_path)
    if dataset_id is None:
        if (prior := _existing_with_hash(h)) is not None:
            return load_meta(prior)
        dataset_id = h

    target = db_path(dataset_id)
    if target.exists():
        target.unlink()

    con = writer(dataset_id)
    last_error: Exception | None = None
    used: str | None = None
    try:
        for label, opts in _READ_ATTEMPTS:
            try:
                con.execute(f"CREATE OR REPLACE VIEW _raw AS SELECT * FROM {_read_expr(csv_path, opts)}")
                con.execute("SELECT * FROM _raw LIMIT 1").fetchall()
                used = label
                break
            except duckdb.Error as exc:
                last_error = exc
                continue
        if used is None:
            raise IngestError("Could not parse CSV", detail=str(last_error)[:400])

        raw_cols = [d[0] for d in con.execute("SELECT * FROM _raw LIMIT 0").description]
        if not raw_cols:
            raise IngestError("CSV has no columns")

        taken: set[str] = set()
        pairs = [(orig, normalise_column(orig, i, taken)) for i, orig in enumerate(raw_cols)]
        select = ", ".join(f'"{o}" AS "{n}"' for o, n in pairs)
        con.execute(f"CREATE OR REPLACE TABLE {TABLE} AS SELECT {select} FROM _raw")
        con.execute("DROP VIEW IF EXISTS _raw")

        row_count = con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
        if row_count == 0:
            raise IngestError("CSV parsed but contains no data rows")
    except IngestError:
        con.close()
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        con.close()
        target.unlink(missing_ok=True)
        raise IngestError("Ingest failed", detail=str(exc)[:400]) from exc
    finally:
        try:
            con.close()
        except Exception:
            pass

    meta = DatasetMeta(
        dataset_id=dataset_id,
        name=name or csv_path.stem,
        source_filename=csv_path.name,
        content_hash=h,
        row_count=row_count,
        column_count=len(pairs),
        created_at=datetime.now(UTC).isoformat(),
        read_options=used,
    )
    save_meta(meta, display_names={n: o for o, n in pairs})
    return meta


def save_meta(meta: DatasetMeta, *, display_names: dict[str, str] | None = None) -> None:
    p = meta_path(meta.dataset_id)
    doc = json.loads(p.read_text()) if p.exists() else {}
    doc["meta"] = asdict(meta)
    if display_names is not None:
        doc["display_names"] = display_names
    p.write_text(json.dumps(doc, indent=2, default=str))


def load_doc(dataset_id: str) -> dict:
    p = meta_path(dataset_id)
    if not p.exists():
        raise NotFound(f"Dataset '{dataset_id}' not found")
    return json.loads(p.read_text())


def load_meta(dataset_id: str) -> DatasetMeta:
    return DatasetMeta(**load_doc(dataset_id)["meta"])


def list_datasets() -> list[dict]:
    out = []
    for f in sorted(settings().datasets_dir.glob("*.json")):
        try:
            doc = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        m = doc.get("meta")
        if m and db_path(m["dataset_id"]).exists():
            m = dict(m)
            m["profiled"] = "profile" in doc
            out.append(m)
    return sorted(out, key=lambda d: d["created_at"], reverse=True)


def delete_dataset(dataset_id: str) -> None:
    if not meta_path(dataset_id).exists():
        raise NotFound(f"Dataset '{dataset_id}' not found")
    db_path(dataset_id).unlink(missing_ok=True)
    meta_path(dataset_id).unlink(missing_ok=True)
    shutil.rmtree(settings().datasets_dir / dataset_id, ignore_errors=True)
