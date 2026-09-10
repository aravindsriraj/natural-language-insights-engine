#!/usr/bin/env python3
"""Record the demo GIFs by driving the real UI in a browser.

Every answer in these recordings is produced live. There is no cache, so what you see is
the real wait. To keep the recordings short, run the server with LLM_REASONING_EFFORT=low,
which is measured at roughly half the latency with no loss on the evaluation set:

    LLM_REASONING_EFFORT=low make dev
    python scripts/record_demo.py
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
API = "http://localhost:8000"
VIEWPORT = {"width": 1440, "height": 900}
FPS = 8

RETAIL_QUESTIONS = [
    "What are the top 10 products by revenue?",
    "What is our profit margin by product category?",
]
HIRE_QUESTIONS = [
    "Which depot generated the most hire revenue?",
]


# ---------------------------------------------------------------- helpers

def http(method: str, path: str, body: dict | None = None, timeout: int = 300) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"content-type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def find_dataset(name_part: str) -> str | None:
    for d in http("GET", "/api/datasets")["datasets"]:
        if name_part.lower() in d["name"].lower():
            return d["dataset_id"]
    return None


def save_gif(frames: list[bytes], path: Path, scale: float = 0.6) -> None:
    if not frames:
        print(f"  no frames for {path.name}")
        return
    images = []
    for raw in frames:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
        images.append(im.quantize(colors=180, method=Image.MEDIANCUT))
    images[0].save(path, save_all=True, append_images=images[1:],
                   duration=int(1000 / FPS), loop=0, optimize=True)
    print(f"  wrote {path.relative_to(ROOT)}  {len(images)} frames  "
          f"{path.stat().st_size / 1e6:.1f} MB")


class Recorder:
    """Captures frames on a timer while the main coroutine drives the page."""

    def __init__(self, page):
        self.page = page
        self.frames: list[bytes] = []
        self._task: asyncio.Task | None = None

    async def _loop(self):
        try:
            while True:
                try:
                    self.frames.append(await self.page.screenshot(type="png"))
                except Exception:
                    pass
                await asyncio.sleep(1 / FPS)
        except asyncio.CancelledError:
            pass

    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> list[bytes]:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        return self.frames


async def select_dataset(page, name_part: str):
    import re

    await page.locator("button.ds").filter(
        has_text=re.compile(name_part, re.IGNORECASE)
    ).first.click()
    await page.wait_for_timeout(700)


async def ask(page, question: str, settle_ms: int = 2600):
    box = page.locator("textarea")
    await box.click()
    await box.type(question, delay=22)
    await page.wait_for_timeout(250)
    await box.press("Enter")
    # Wait for the answer card, then for the badges that only render when it is complete.
    try:
        await page.locator(".card .badges").last.wait_for(timeout=180_000)
    except Exception:
        pass
    await page.wait_for_timeout(settle_ms)


async def reveal_sql(page):
    sql = page.locator("details.sql > summary").last
    if await sql.count():
        await sql.click()
        await page.wait_for_timeout(2200)


# ---------------------------------------------------------------- scenes

async def scene_ask(browser, retail_id: str) -> list[bytes]:
    """Ask an answerable question, show the query, then ask one that must be refused."""
    page = await browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
    rec = Recorder(page)
    await page.goto(API, wait_until="networkidle")
    await page.wait_for_timeout(900)
    rec.start()
    await page.wait_for_timeout(600)
    await select_dataset(page, "retail")
    await ask(page, RETAIL_QUESTIONS[0])
    await reveal_sql(page)
    await page.mouse.wheel(0, 500)
    await page.wait_for_timeout(1800)
    await ask(page, RETAIL_QUESTIONS[1])
    await page.wait_for_timeout(2600)
    frames = await rec.stop()
    await page.close()
    return frames


async def scene_second_csv(browser, hire_csv: Path) -> list[bytes]:
    """Load a CSV the app has never seen, then query it. No code changes in between."""
    page = await browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
    rec = Recorder(page)
    await page.goto(API, wait_until="networkidle")
    await page.wait_for_timeout(900)
    rec.start()
    await page.wait_for_timeout(600)
    await page.locator("#csv").set_input_files(str(hire_csv))
    # Ingest plus profiling is a real model call; wait for the dataset to appear and be selected.
    await page.wait_for_timeout(2000)
    for _ in range(120):
        if await page.locator(".sem").count():
            break
        await page.wait_for_timeout(1000)
    await page.wait_for_timeout(2500)
    await ask(page, HIRE_QUESTIONS[0])
    await reveal_sql(page)
    await page.wait_for_timeout(2200)
    frames = await rec.stop()
    await page.close()
    return frames


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=0.6)
    args = ap.parse_args()
    DOCS.mkdir(exist_ok=True)

    try:
        http("GET", "/health", timeout=5)
    except Exception:
        print(f"Cannot reach {API}. Start it with: make dev")
        return 1

    retail_id = find_dataset("retail")
    if not retail_id:
        print("Load the retail dataset first:  make seed")
        return 1

    hire_csv = ROOT / "samples" / "plant_hire.csv"
    if not hire_csv.exists():
        print(f"Missing {hire_csv}")
        return 1

    # The second-CSV scene must start from a state where that dataset is absent.
    if (existing := find_dataset("plant")) is not None:
        http("DELETE", f"/api/datasets/{existing}")
        print("  removed the previously loaded second dataset")

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--force-color-profile=srgb"])
        print("Recording: asking questions…")
        save_gif(await scene_ask(browser, retail_id), DOCS / "demo-ask.gif", args.scale)
        print("Recording: loading a second CSV…")
        save_gif(await scene_second_csv(browser, hire_csv), DOCS / "demo-second-csv.gif", args.scale)
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
