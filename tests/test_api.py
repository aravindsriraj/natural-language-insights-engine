"""API contract: status codes and the error envelope. No LLM calls."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import CSV_NO_ROWS, CSV_SIMPLE


@pytest.fixture
def client(data_dir, monkeypatch):
    import app.api.main as main
    from app.core.jobs import JobManager

    monkeypatch.setattr(main, "jobs", JobManager(concurrency=2, db_path=data_dir / "jobs.sqlite"))
    # Server exceptions must surface as responses so the 500 envelope is testable.
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c


def upload(client, content=CSV_SIMPLE, filename="orders.csv"):
    return client.post("/api/datasets", files={"file": (filename, content.encode(), "text/csv")})


def wait(client, job_id, tries=200):
    for _ in range(tries):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "interrupted"):
            return job
    raise AssertionError("job never finished")


# ------------------------------------------------------------------ health

def test_health_reports_readiness(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "llm_configured" in body and "model" in body


# ------------------------------------------------------------------ upload

def test_upload_returns_202_with_a_job_handle(client):
    r = upload(client)
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"job_id", "status", "poll_url", "stream_url"}


def test_upload_then_poll_yields_a_dataset(client):
    job = wait(client, upload(client).json()["job_id"])
    assert job["status"] == "succeeded"
    assert job["result"]["row_count"] == 3


def test_upload_rejects_non_csv(client):
    r = client.post("/api/datasets", files={"file": ("x.pdf", b"%PDF", "application/pdf")})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_upload_rejects_empty_file(client):
    r = client.post("/api/datasets", files={"file": ("x.csv", b"", "text/csv")})
    assert r.status_code == 400


def test_unparseable_csv_fails_the_job_not_the_request(client):
    """Submission succeeded, so the failure belongs on the job, not on the POST."""
    r = upload(client, CSV_NO_ROWS, "headers_only.csv")
    assert r.status_code == 202
    job = wait(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "ingest_failed"


def test_upload_respects_size_limit(client, monkeypatch):
    import app.config as config

    monkeypatch.setenv("MAX_UPLOAD_MB", "0")
    config.settings.cache_clear()
    r = client.post("/api/datasets", files={"file": ("big.csv", b"a,b\n1,2\n" * 100, "text/csv")})
    config.settings.cache_clear()
    assert r.status_code == 413


# ------------------------------------------------------------------ datasets

def test_list_and_get_dataset(client):
    ds = wait(client, upload(client).json()["job_id"])["result"]["dataset_id"]
    assert any(d["dataset_id"] == ds for d in client.get("/api/datasets").json()["datasets"])
    body = client.get(f"/api/datasets/{ds}").json()
    assert body["profile"]["row_count"] == 3
    assert len(body["profile"]["columns"]) == 6


def test_get_unknown_dataset_is_404(client):
    r = client.get("/api/datasets/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_delete_dataset(client):
    ds = wait(client, upload(client).json()["job_id"])["result"]["dataset_id"]
    assert client.delete(f"/api/datasets/{ds}").status_code == 204
    assert client.get(f"/api/datasets/{ds}").status_code == 404


def test_column_role_can_be_corrected(client):
    """Inferred schema must be fixable in place, not silently wrong."""
    ds = wait(client, upload(client).json()["job_id"])["result"]["dataset_id"]
    r = client.patch(f"/api/datasets/{ds}/columns/each", json={"role": "money"})
    assert r.status_code == 200
    col = next(c for c in r.json()["profile"]["columns"] if c["name"] == "each")
    assert col["role"] == "money" and col["role_source"] == "user"


def test_invalid_role_is_rejected(client):
    ds = wait(client, upload(client).json()["job_id"])["result"]["dataset_id"]
    r = client.patch(f"/api/datasets/{ds}/columns/each", json={"role": "banana"})
    assert r.status_code == 400


def test_unknown_column_is_404(client):
    ds = wait(client, upload(client).json()["job_id"])["result"]["dataset_id"]
    assert client.patch(f"/api/datasets/{ds}/columns/nope", json={"role": "money"}).status_code == 404


# ------------------------------------------------------------------ query validation

def test_query_validates_body(client):
    r = client.post("/api/query", json={"dataset_id": "x"})
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "validation_error"
    assert any(f["field"] == "question" for f in body["fields"])


def test_query_rejects_blank_question(client):
    ds = wait(client, upload(client).json()["job_id"])["result"]["dataset_id"]
    assert client.post("/api/query", json={"dataset_id": ds, "question": "   "}).status_code == 422


def test_query_on_unknown_dataset_is_404(client):
    r = client.post("/api/query", json={"dataset_id": "nope", "question": "how many rows?"})
    assert r.status_code == 404


# ------------------------------------------------------------------ jobs

def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/nope").status_code == 404


def test_stream_of_unknown_job_is_404_not_an_open_stream(client):
    assert client.get("/api/jobs/nope/events").status_code == 404


def test_job_list(client):
    upload(client)
    assert len(client.get("/api/jobs").json()["jobs"]) >= 1


def test_no_stack_trace_ever_reaches_the_caller(client, monkeypatch):
    import app.api.main as main

    def boom(*a, **k):
        raise RuntimeError("secret internal detail at /srv/app/private.py:42")

    monkeypatch.setattr(main.ing, "list_datasets", boom)
    r = client.get("/api/datasets")
    assert r.status_code == 500
    text = r.text
    assert "Traceback" not in text and "private.py" not in text
    assert r.json()["error"]["code"] == "internal_error"


# ------------------------------------------------------------------ threads

def _job_row(client, dataset_id, question, thread_id, status="succeeded", result=None):
    """Insert a query job the way /api/query does, without needing a model."""
    import app.api.main as main

    jid = main.jobs.create("query", dataset_id=dataset_id,
                           payload={"question": question, "thread_id": thread_id})
    import json as _json
    main.jobs._update(jid, status=status,
                      result=_json.dumps(result or {"answer": f"answer to {question}"}))
    return jid


def test_threads_are_derived_from_the_job_log(client):
    ds = wait(client, upload(client).json()["job_id"])["result"]["dataset_id"]
    _job_row(client, ds, "first question", "t-alpha")
    _job_row(client, ds, "second question", "t-alpha")
    _job_row(client, ds, "unrelated", "t-beta")

    threads = {t["thread_id"]: t for t in client.get("/api/threads").json()["threads"]}
    assert threads["t-alpha"]["message_count"] == 2
    assert threads["t-beta"]["message_count"] == 1


def test_thread_title_is_the_question_that_started_it(client):
    """Not an arbitrary row. SQLite's bare-column behaviour cannot be relied on here."""
    ds = wait(client, upload(client).json()["job_id"])["result"]["dataset_id"]
    _job_row(client, ds, "the opening question", "t-title")
    _job_row(client, ds, "a later follow-up", "t-title")
    _job_row(client, ds, "a later one still", "t-title")

    t = next(t for t in client.get("/api/threads").json()["threads"] if t["thread_id"] == "t-title")
    assert t["title"] == "the opening question"


def test_threads_can_be_filtered_by_dataset(client):
    a = wait(client, upload(client).json()["job_id"])["result"]["dataset_id"]
    b = wait(client, upload(client, CSV_SIMPLE.replace("BOLT", "SCREW")).json()["job_id"])["result"]["dataset_id"]
    _job_row(client, a, "about a", "t-a")
    _job_row(client, b, "about b", "t-b")

    got = [t["thread_id"] for t in client.get(f"/api/threads?dataset_id={a}").json()["threads"]]
    assert got == ["t-a"]


def test_thread_replays_in_order(client):
    ds = wait(client, upload(client).json()["job_id"])["result"]["dataset_id"]
    for i in range(3):
        _job_row(client, ds, f"question {i}", "t-order")
    turns = client.get("/api/threads/t-order").json()["turns"]
    assert [t["question"] for t in turns] == ["question 0", "question 1", "question 2"]
    assert turns[0]["answer"]["answer"] == "answer to question 0"


def test_unknown_thread_is_404(client):
    assert client.get("/api/threads/nope").status_code == 404
    assert client.delete("/api/threads/nope").status_code == 404


def test_deleting_a_thread_removes_its_turns(client):
    ds = wait(client, upload(client).json()["job_id"])["result"]["dataset_id"]
    _job_row(client, ds, "doomed", "t-del")
    _job_row(client, ds, "survivor", "t-keep")

    assert client.delete("/api/threads/t-del").status_code == 204
    assert client.get("/api/threads/t-del").status_code == 404
    remaining = [t["thread_id"] for t in client.get("/api/threads").json()["threads"]]
    assert "t-del" not in remaining and "t-keep" in remaining


def test_delete_all_threads_for_one_dataset(client):
    a = wait(client, upload(client).json()["job_id"])["result"]["dataset_id"]
    b = wait(client, upload(client, CSV_SIMPLE.replace("BOLT", "SCREW")).json()["job_id"])["result"]["dataset_id"]
    _job_row(client, a, "q1", "t-a1")
    _job_row(client, a, "q2", "t-a2")
    _job_row(client, b, "q3", "t-b1")

    r = client.request("DELETE", "/api/threads", params={"dataset_id": a})
    assert r.status_code == 200 and r.json()["deleted"] == 2

    left = [t["thread_id"] for t in client.get("/api/threads").json()["threads"]]
    assert left == ["t-b1"], "deleting one dataset's chats must not touch another's"


def test_delete_all_threads_everywhere(client):
    ds = wait(client, upload(client).json()["job_id"])["result"]["dataset_id"]
    for i in range(3):
        _job_row(client, ds, f"q{i}", f"t-{i}")

    assert client.request("DELETE", "/api/threads").json()["deleted"] == 3
    assert client.get("/api/threads").json()["threads"] == []


def test_delete_all_threads_when_there_are_none(client):
    assert client.request("DELETE", "/api/threads").json()["deleted"] == 0
