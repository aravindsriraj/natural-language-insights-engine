#!/usr/bin/env python3
"""Load CSVs into a running server. Repeatable: re-running converges rather than duplicating.

    python scripts/seed.py                       # every CSV in samples/ plus the retail file
    python scripts/seed.py path/to/other.csv     # a specific file
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "http://localhost:8000"


def post_file(path: Path, name: str) -> dict:
    boundary = uuid.uuid4().hex
    body = b"".join([
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: text/csv\r\n\r\n".encode(),
        path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        f"{API}/api/datasets?name={urllib.parse.quote(name)}", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def wait(job_id: str) -> dict:
    while True:
        with urllib.request.urlopen(f"{API}/api/jobs/{job_id}", timeout=30) as r:
            job = json.loads(r.read())
        if job["status"] in ("succeeded", "failed", "interrupted"):
            return job
        time.sleep(1)


def main() -> int:
    if len(sys.argv) > 1:
        files = [Path(a) for a in sys.argv[1:]]
    else:
        files = sorted((ROOT / "samples").glob("*.csv"))
        if (retail := ROOT / "online_retail_clean.csv").exists():
            files.insert(0, retail)
    if not files:
        print("Nothing to load. Put a CSV in samples/ or pass a path.")
        return 1

    try:
        urllib.request.urlopen(f"{API}/health", timeout=5)
    except Exception:
        print(f"Cannot reach the API at {API}. Start it with: make dev")
        return 1

    failed = 0
    for path in files:
        if not path.exists():
            print(f"  missing: {path}")
            failed += 1
            continue
        name = path.stem.replace("_", " ").title()
        size = path.stat().st_size / 1e6
        print(f"  loading {path.name} ({size:.1f} MB) as '{name}' …", flush=True)
        job = wait(post_file(path, name)["job_id"])
        if job["status"] == "succeeded":
            r = job["result"]
            note = f", semantics unavailable ({r['semantics_error']})" if r.get("semantics_error") else ""
            print(f"    ok  id={r['dataset_id']}  {r['row_count']:,} rows  "
                  f"{r['column_count']} cols  read={r['read_options']}{note}")
        else:
            print(f"    failed: {(job.get('error') or {}).get('message')}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
