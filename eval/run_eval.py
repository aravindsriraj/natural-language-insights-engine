#!/usr/bin/env python3
"""Run the evaluation set against a running API.

    python eval/run_eval.py                       # everything
    python eval/run_eval.py --only refuse         # ids containing "refuse"
    python eval/run_eval.py --api http://host:8000

Assertions compare values, never SQL text. The agent writes a different but equivalent
query every run, so pinning the query would test the wrong thing.

Exits non-zero on any failure, which is what makes it usable in CI.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).resolve().parent.parent
NUMBER = re.compile(r"-?\d[\d,]*\.?\d*")


def http(method: str, url: str, body: dict | None = None, timeout: int = 300) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method=method,
                  headers={"content-type": "application/json"} if data else {})
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except HTTPError as e:
        return json.loads(e.read() or b"{}")


def numbers_in(text: str) -> list[float]:
    out = []
    for m in NUMBER.finditer(text or ""):
        try:
            out.append(float(m.group().replace(",", "")))
        except ValueError:
            continue
    return out


def numbers_in_result(result: dict | None) -> list[float]:
    if not result:
        return []
    return [float(v) for row in result.get("rows", []) for v in row
            if isinstance(v, (int, float)) and not isinstance(v, bool)]


def check(expect: dict, answer: dict) -> tuple[bool, str]:
    kind = expect["kind"]
    refused = bool(answer.get("refused"))
    text = answer.get("answer") or ""

    if kind == "refusal":
        if refused:
            return True, "refused"
        return False, "answered a question it should have refused"

    if kind == "refusal_or_caveat":
        # Two answers are acceptable and one is not. Refusing is fine. Answering while
        # naming the limitation is fine. Answering as though the limitation were not there
        # is the failure, because that is the answer a reader would act on and be wrong.
        if refused:
            return True, "refused"
        low = text.lower()
        hit = next((v for v in expect["values"] if v.lower() in low), None)
        if hit:
            return True, f"answered but flagged the limitation ({hit!r})"
        return False, "answered without acknowledging the limitation"

    if refused:
        return False, f"refused unexpectedly: {answer.get('refusal_reason')}"

    if kind == "no_refusal":
        if not answer.get("queries"):
            return False, "answered without running a query"
        return True, f"answered from {len(answer['queries'])} quer(y/ies)"

    if kind == "contains":
        low = text.lower()
        missing = [v for v in expect["values"] if v.lower() not in low]
        if missing:
            return False, f"answer omits {missing}"
        return True, "all expected terms present"

    if kind == "numeric":
        want = float(expect["value"])
        tol = float(expect.get("tolerance", 0.01))
        window = abs(want) * tol if tol else 0.0
        found = numbers_in(text) + numbers_in_result(answer.get("result"))
        near = [n for n in found if abs(n - want) <= window]
        if near:
            return True, f"found {near[0]:,.2f} (expected {want:,.2f} ±{tol:.0%})"
        closest = min(found, key=lambda n: abs(n - want), default=None)
        return False, (f"expected {want:,.2f} ±{tol:.0%}, closest value seen was "
                       f"{closest:,.2f}" if closest is not None else
                       f"expected {want:,.2f}, no numbers in the answer")

    return False, f"unknown assertion kind '{kind}'"


def ensure_dataset(api: str, spec: dict) -> str:
    """Find the loaded dataset by its source file, not by a display name.

    `make seed` derives the name from the filename, and a person uploading through the UI
    can call it anything. The file it came from is the stable identity.
    """
    wanted = Path(spec["path"]).name
    loaded = http("GET", f"{api}/api/datasets").get("datasets", [])
    for d in loaded:
        if d.get("source_filename") == wanted:
            return d["dataset_id"]
    for d in loaded:
        if spec["name"].lower() in d["name"].lower():
            return d["dataset_id"]
    names = ", ".join(f"{d['name']} ({d.get('source_filename')})" for d in loaded) or "none"
    sys.exit(f"Could not find a dataset loaded from '{wanted}'.\n"
             f"Currently loaded: {names}\n"
             f"Load it with:  make seed")


def ask(api: str, dataset_id: str, question: str, timeout: int) -> dict:
    r = http("POST", f"{api}/api/query",
             {"dataset_id": dataset_id, "question": question})
    if "job_id" not in r:
        raise RuntimeError(r.get("error", {}).get("message", str(r)))
    if r.get("status") == "succeeded":
        return r["result"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = http("GET", f"{api}/api/jobs/{r['job_id']}")
        if job.get("status") == "succeeded":
            return job["result"]
        if job.get("status") in ("failed", "interrupted"):
            raise RuntimeError((job.get("error") or {}).get("message", "job failed"))
        time.sleep(1.0)
    raise TimeoutError(f"no answer within {timeout}s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--file", default=str(Path(__file__).parent / "questions.yaml"))
    ap.add_argument("--only", help="Run only questions whose id contains this substring")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--json", help="Write full results to this path")
    args = ap.parse_args()

    try:
        health = http("GET", f"{args.api}/health", timeout=10)
    except (URLError, TimeoutError):
        return int(bool(print(f"Cannot reach the API at {args.api}. Start it with: make dev")))
    if not health.get("llm_configured"):
        print("The server has no model API key configured; every question will fail.")
        return 1

    spec = yaml.safe_load(Path(args.file).read_text())
    dataset_id = ensure_dataset(args.api, spec["dataset"])
    questions = [q for q in spec["questions"] if not args.only or args.only in q["id"]]

    print(f"\n{len(questions)} question(s) against '{spec['dataset']['name']}' "
          f"({dataset_id}) via {args.api}\n")
    print(f"{'':2} {'id':<28} {'secs':>6}  detail")
    print("-" * 100)

    results, passed = [], 0
    for q in questions:
        started = time.time()
        try:
            answer = ask(args.api, dataset_id, q["question"], args.timeout)
            ok, detail = check(q["expect"], answer)
        except Exception as exc:
            answer, ok, detail = {}, False, f"{type(exc).__name__}: {exc}"
        secs = time.time() - started
        passed += ok
        print(f"{'PASS' if ok else 'FAIL':<2} {q['id']:<28} {secs:>6.1f}  {detail}")
        if not ok and answer.get("answer"):
            print(f"{'':38}answer: {answer['answer'][:150].replace(chr(10), ' ')}")
        results.append({"id": q["id"], "question": q["question"], "passed": ok,
                        "detail": detail, "seconds": round(secs, 1),
                        "answer": answer.get("answer"),
                        "refused": answer.get("refused"),
                        "queries": [x["sql"] for x in answer.get("queries", [])]})

    print("-" * 100)
    print(f"{passed}/{len(questions)} passed")
    refusals = [r for r, q in zip(results, questions, strict=True) if q["expect"]["kind"] == "refusal"]
    if refusals:
        print(f"  refusals correct: {sum(r['passed'] for r in refusals)}/{len(refusals)}")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
        print(f"  wrote {args.json}")
    return 0 if passed == len(questions) else 1


if __name__ == "__main__":
    sys.exit(main())
