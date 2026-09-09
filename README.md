# Natural Language Insights Engine

Ask questions about a transactional CSV in plain English. Get an answer with the SQL that
produced it, or a refusal that says what the data would need.

Point it at a CSV it has never seen and it will work out the schema for itself. No code
changes, no configuration, nothing about any particular dataset baked in.

**[System design and architecture →](DESIGN.md)**

### Asking a question, and being refused

![Asking a question](docs/demo-ask.gif)

A question it can answer, with the queries that produced it. Then a question about profit
margin, which this data cannot support, so it declines and says what is missing.

### Loading a CSV it has never seen

![Loading a second CSV](docs/demo-second-csv.gif)

A different domain, semicolon delimited, `DD/MM/YYYY` dates, and no revenue column. The
schema panel fills in on its own and the next question is answered against it. No code
changes, no restart.

---

## Quick start

Requires [uv](https://docs.astral.sh/uv/), Node 20 or newer, and a Google Gemini API key.

```bash
git clone <this-repo> && cd nl-insights-engine
make setup                       # venv, dependencies, UI build, .env
echo "GEMINI_API_KEY=your-key" >> .env
make dev                         # http://localhost:8000
```

In another terminal, load the sample data:

```bash
make seed
```

Then open http://localhost:8000, pick a dataset, and ask something.

### Or with Docker

```bash
echo "GEMINI_API_KEY=your-key" > .env
docker compose up --build        # http://localhost:8000
```

---

## Asking a question

Through the UI, or over HTTP. Every long operation is a job: submit, get an id, poll or
stream, retrieve.

```bash
# 1. Load a CSV
curl -X POST localhost:8000/api/datasets -F "file=@samples/plant_hire.csv"
# → 202 {"job_id": "...", "poll_url": "...", "stream_url": "..."}

# 2. Ask
curl -X POST localhost:8000/api/query \
  -H 'content-type: application/json' \
  -d '{"dataset_id":"<id>","question":"Which depot generated the most revenue?"}'
# → 202 {"job_id": "..."}

# 3. Watch it work
curl -N localhost:8000/api/jobs/<job_id>/events

# …or just poll
curl localhost:8000/api/jobs/<job_id>
```

A repeated question comes back from the cache as `200` with the answer inline rather than
`202`. Pass `"use_cache": false` to force a fresh run, or `"thread_id": "<id>"` from a
previous answer to ask a follow-up in the same conversation.

Interactive API documentation is at `/docs`.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/datasets` | Upload a CSV. Returns a job. |
| `GET` | `/api/datasets` | List loaded datasets |
| `GET` | `/api/datasets/{id}` | Full profile: statistics and inferred meaning |
| `PATCH` | `/api/datasets/{id}/columns/{column}` | Correct an inferred column role |
| `DELETE` | `/api/datasets/{id}` | Remove a dataset |
| `POST` | `/api/query` | Ask a question. Returns a job, or a cached answer. |
| `GET` | `/api/jobs/{id}` | Job status and result |
| `GET` | `/api/jobs/{id}/events` | Server-sent progress events |
| `DELETE` | `/api/cache` | Clear cached answers |
| `GET` | `/health` | Readiness and configuration |

Errors are always shaped the same way, and never contain a stack trace:

```json
{"error": {"code": "not_found", "message": "Dataset 'abc' not found"}}
```

---

## What it does about being wrong

**It shows its working.** Every answer carries the queries that produced it, their row
counts and timings, and the assumptions the agent made. An answer you can check is worth
more than one you cannot.

**It cannot answer without querying.** A middleware hook blocks any final answer unless a
query actually succeeded in that run, or the agent explicitly refused. This is enforced in
code and counted from tool results, not asked for in a prompt.

**Refusing is a real action.** `refuse` is a tool the agent calls, so a refusal is
structured, logged, and testable. Eight of the twenty-one evaluation questions pass only by
being refused.

**SQL cannot escape the dataset.** Queries run on a read-only connection, which is the
enforcement, behind parse checks that reject writes, stacked statements, file-reading
functions and any table other than the dataset. Twenty-five attack strings are in the tests.

---

## Any CSV

`samples/plant_hire.csv` deliberately shares nothing with the development dataset: different
domain, different column names, semicolon delimited, `DD/MM/YYYY` dates, no precomputed
revenue column, and cancellations expressed as negative day counts. Loading it produces:

```
revenue_expression : hire_days * day_rate_gbp     ← derived, not read from a column
has_returns        : true, negative hire_days are cancellations
grain              : one row per equipment hire booking
time_column        : booked_on
entities           : customer=account_ref, product=asset_ref, location=depot
```

The inferred roles appear in the schema panel and can be corrected there. Schema inference
that is visible and fixable beats schema inference that is silently wrong.

---

## Development

```bash
make test        # 112 tests, no network, about a second
make lint        # ruff
make eval        # 21 questions against a running server
make ui          # Vite dev server on :5173 with hot reload
make help        # everything else
```

The test suite makes no model calls. A test that needs the network is a test that fails on
someone else's machine.

`make eval` needs a running server and a key. It exits non-zero on regression, and asserts
on values with a tolerance rather than on SQL text, because the agent writes a different but
equivalent query on every run.

---

## Configuration

Everything is environment driven. See `.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | — | Required |
| `LLM_MODEL` | `google_genai:gemini-3.8-flash` | Any provider string LangChain accepts |
| `LLM_FALLBACK_MODEL` | `google_genai:gemini-2.5-flash` | Used if the primary fails |
| `JOB_CONCURRENCY` | `4` | In-flight jobs; the rest queue |
| `MAX_RESULT_ROWS` | `1000` | Row cap per query |
| `QUERY_TIMEOUT_S` | `30` | Wall-clock cap per query |
| `AGENT_MAX_SQL_CALLS` | `6` | Queries per question |
| `AGENT_MAX_MODEL_CALLS` | `16` | Model turns per question |
| `MAX_UPLOAD_MB` | `512` | Upload size limit |
| `LANGSMITH_TRACING` | `false` | Set with `LANGSMITH_API_KEY` for traces |

Switching provider is one line. `LLM_MODEL=anthropic:claude-sonnet-5` with the matching key
and package works without touching any code.

---

## Layout

```
app/core/      store, ingest, profile, guard, jobs   — no model in the path except profile layer 2
app/agent/     tools, middleware, prompts, assembly
app/api/       FastAPI service
ui/            React + Vite
eval/          questions.yaml + run_eval.py
tests/         112 tests, hermetic
docs/          architecture diagram
```

## Known limits

Stated plainly, and expanded in [DESIGN.md](DESIGN.md).

- One process. Concurrency is a bounded pool, not horizontal workers.
- One table per dataset. No joins across files.
- An interrupted question is reported as interrupted, not resumed automatically.
- Very wide files put every column in the prompt; past a few hundred columns that needs ranking.
- Semantic annotation is a single point of judgment. It is shown to the user and correctable,
  which is the mitigation, not a fix.
