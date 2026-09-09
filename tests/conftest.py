from __future__ import annotations

import os
from pathlib import Path

import pytest

# Tests must never reach a real model. Clearing both names keeps the suite hermetic and
# fast, and exercises the degraded path where semantic annotation is unavailable.
os.environ["GEMINI_API_KEY"] = ""
os.environ["GOOGLE_API_KEY"] = ""


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    """Point every module at a throwaway data directory.

    Modules call `settings()` rather than holding a Settings instance, so clearing the
    cache around an env override is enough; no per-module patching needed.
    """
    from app import config

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    config.settings.cache_clear()
    yield tmp_path
    config.settings.cache_clear()


def write_csv(dirpath: Path, name: str, content: str, encoding: str = "utf-8") -> Path:
    p = Path(dirpath) / name
    p.write_bytes(content.encode(encoding))
    return p


# ------------------------------------------------------------------ hostile fixtures
# Nothing here resembles the development dataset. That is the point: the app must cope
# with column names and types it has never seen.

CSV_SIMPLE = "order_ref,when,who,widget,units,each\nA1,2024-01-05,C9,BOLT,3,1.50\nA2,2024-02-11,C4,NUT,10,0.25\nA3,2024-02-11,C9,BOLT,-1,1.50\n"

CSV_SEMICOLON = "ref;dt;amt\nX1;2024-01-01;10,5\nX2;2024-01-02;3,25\n"

CSV_LATIN1 = "ref,ville,montant\n1,Genève,12.5\n2,Zürich,8.0\n"

CSV_DUPES = "id,id,Value,Value\n1,2,3,4\n5,6,7,8\n"

CSV_WEIRD_HEADERS = '"  Order #  ",2nd col,,Total ($)\nA,1,x,9.99\nB,2,y,4.50\n'

CSV_ONE_ROW = "a,b\n1,2\n"

CSV_ALL_NULL_COL = "a,b,c\n1,,z\n2,,y\n3,,x\n"

CSV_NO_ROWS = "a,b,c\n"

CSV_MIXED_TYPES = "code,val\nA,1\nB,not_a_number\nC,3\n"


@pytest.fixture
def simple_csv(tmp_path):
    return write_csv(tmp_path, "simple.csv", CSV_SIMPLE)
