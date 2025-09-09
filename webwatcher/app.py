import os
import uuid
import sqlite3
import logging
import traceback
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from playwright.async_api import async_playwright
from PIL import Image
import pytesseract
import httpx

# ---------- Config ----------
DB_PATH = os.environ.get("DB_PATH", "jobs.db")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Optional env:
#   TZ=America/Toronto
#   WAIT_SELECTOR=[css or text=...]
#   WAIT_AFTER_LOAD_MS=10000
#   PUBLIC_BASE_URL=https://your-domain
#   CURRENCY_PREFIX=C$   (defaults to "C$")
CURRENCY_PREFIX = os.getenv("CURRENCY_PREFIX", "C$")

logging.basicConfig(level=logging.INFO)

# ---------- App & templating ----------
app = FastAPI(title="Web Watcher")
templates = Jinja2Templates(directory="templates")

STATIC_DIR = Path("static")
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")

# ---------- DB helpers ----------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                search_text TEXT NOT NULL,
                minutes INTEGER NOT NULL,
                webhook_url TEXT,
                price_threshold REAL,
                created_at TEXT NOT NULL
            )
            """
        )
        # migrate: add price_threshold if missing
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
        if "price_threshold" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN price_threshold REAL")
        conn.commit()

def insert_job(job):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
            (
                job["id"],
                job["url"],
                job["search_text"],
                job["minutes"],
                job.get("webhook_url"),
                job.get("price_threshold"),
                job["created_at"],
            ),
        )
        conn.commit()

def update_job_row(job_id: str, url: str, search_text: str, minutes: int,
                   webhook_url: Optional[str], price_threshold: Optional[float]):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            UPDATE jobs
               SET url = ?, search_text = ?, minutes = ?, webhook_url = ?, price_threshold = ?
             WHERE id = ?
            """,
            (url, search_text, minutes, webhook_url, price_threshold, job_id),
        )
        conn.commit()

def delete_job(job_id):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        conn.commit()

def get_job(job_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        row = c.fetchone()
        return dict(row) if row else None

def list_jobs():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM jobs ORDER BY created_at DESC")
        rows = c.fetchall()
        return [dict(r) for r in rows]

# ---------- Scheduler ----------
scheduler = AsyncIOScheduler(timezone=os.getenv("TZ", "UTC"))

def schedule_job(job):
    trigger = IntervalTrigger(minutes=job["minutes"])
    scheduler.add_job(run_job, trigger=trigger, args=[job["id"]], id=job["id"], replace_existing=True)

def reschedule_job(job_id: str):
    # Remove then re-add with updated minutes/settings
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    updated = get_job(job_id)
    if updated:
        schedule_job(updated)

# ---------- Price extraction (C$ only by default) ----------
# Matches: "C$406" or "C$ 1,234.56" (space or NBSP allowed)
_PRICE_RE = re.compile(
    rf'{re.escape(CURRENCY_PREFIX)}[\s\u00A0]*([0-9]{{1,3}}(?:,[0-9]{{3}})*(?:\.[0-9]{{2}})?)',
    re.I,
)

def extract_prices(text: str) -> list[float]:
    vals = []
    for m in _PRICE_RE.finditer(text):
        num = m.group(1).replace(",", "")
        try:
            vals.append(float(num))
        except ValueError:
            pass
    return vals

# ---------- Core actions ----------
async def screenshot_and_ocr(url: str, save_dir: Path) -> tuple[Path, str]:
    save_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    img_path = save_dir / f"{ts}.png"
    text_path = save_dir / f"{ts}.txt"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            device_scale_factor=2,
            timezone_id=os.getenv("TZ", "UTC"),
        )
        page = await context.new_page()
        try:
            try:
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                # Let network settle
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    await page.wait_for_timeout(2000)

                # Optional target selector
                wait_selector = os.getenv("WAIT_SELECTOR", "").strip()
                if wait_selector:
                    try:
                        await page.wait_for_selector(wait_selector, timeout=15000)
                    except Exception:
                        await page.wait_for_timeout(1000)

                # Optional extra delay
                extra_ms = int(os.getenv("WAIT_AFTER_LOAD_MS", "0"))
                if extra_ms > 0:
                    await page.wait_for_timeout(extra_ms)
            except Exception:
                # Even if goto fails, try to capture something
                await page.wait_for_timeout(3000)

            # Always save a screenshot
            await page.screenshot(path=str(img_path), full_page=True)

            # Try DOM first, OCR fallback
            dom_text = await page.evaluate("() => document.body.innerText || ''")
            text = dom_text.strip()
            if not text:
                text = pytesseract.image_to_string(Image.open(img_path))

        except Exception:
            (save_dir / f"{ts}.error.log").write_text(
                f"URL: {url}\n{traceback.format_exc()}",
                encoding="utf-8",
            )
            raise
        finally:
            await context.close()
            await browser.close()

    text_path.write_text(text, encoding="utf-8")
    return img_path, text

async def notify_discord(webhook_url: str, content: str, image_path: Optional[Path] = None, external_url: Optional[str] = None):
    async with httpx.AsyncClient(timeout=30) as client:
        if image_path and image_path.exists():
            payload = {"content": content, "embeds": [{"image": {"url": f"attachment://{image_path.name}"}}]}
            files = {"file": (image_path.name, image_path.read_bytes(), "image/png")}
            r = await client.post(webhook_url, data={"payload_json": json.dumps(payload)}, files=files)
        else:
            payload = {"content": f"{content}\n{external_url}" if external_url else content}
            r = await client.post(webhook_url, json=payload)
        r.raise_for_status()

async def run_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return

    url = job["url"]
    search_text = job["search_text"]
    webhook_url = job.get("webhook_url") or None
    price_threshold = job.get("price_threshold")
    job_dir = DATA_DIR / job_id

    img_path, text = await screenshot_and_ocr(url, job_dir)

    # Maintain latest.png
    latest_img = job_dir / "latest.png"
    try:
        if latest_img.exists():
            latest_img.unlink()
        shutil.copy(img_path, latest_img)
    except Exception as e:
        (job_dir / "latest_copy_error.log").write_text(str(e), encoding="utf-8")

    # Evaluate matches
    text_match = bool(search_text) and (search_text.lower() in text.lower())
    found_prices = extract_prices(text)
    min_price = min(found_prices) if found_prices else None
    price_ok = (min_price is not None and price_threshold is not None and min_price <= float(price_threshold))

    # Notify logic (price-gated if threshold set)
    if price_threshold is not None:
        matched_for_notify = price_ok
        trigger_reason = "price≤threshold" if price_ok else "none"
    else:
        matched_for_notify = text_match
        trigger_reason = "text" if text_match else "none"

    # Summary
    summary_lines = [
        f"Checked: {url}",
        f"At (UTC): {datetime.utcnow().isoformat()}",
        f"Notify condition: {'price≤threshold' if price_threshold is not None else 'text match'}",
        f"Matched (notify): {matched_for_notify}",
        f"Search text: {search_text}",
        f"Threshold: {price_threshold if price_threshold is not None else '—'}",
        f"Currency prefix: {CURRENCY_PREFIX}",
        f"Found prices: {', '.join(f'{p:.2f}' for p in found_prices) if found_prices else 'none'}",
        f"Min price: {min_price:.2f}" if min_price is not None else "Min price: —",
        f"Trigger reason: {trigger_reason}",
    ]
    (job_dir / "latest.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    # Notify (price-gated or text-only as configured)
    if matched_for_notify and webhook_url:
        base = os.getenv("PUBLIC_BASE_URL")
        external_url = f"{base}/data/{job_id}/{img_path.name}" if base else None
        msg = f"✅ Match ({trigger_reason}) for '{search_text}' on {url}"
        if min_price is not None:
            msg += f"\nLowest detected price: {min_price:.2f}"
            if price_threshold is not None:
                msg += f" (threshold: {float(price_threshold):.2f})"
        try:
            await notify_discord(webhook_url, msg, image_path=img_path, external_url=external_url)
        except Exception as e:
            (job_dir / "discord_error.log").write_text(str(e), encoding="utf-8")

# ---------- Routes ----------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "jobs": list_jobs()})

@app.get("/jobs/{job_id}/edit", response_class=HTMLResponse)
def edit_job(job_id: str, request: Request):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return templates.TemplateResponse("edit.html", {"request": request, "job": job})

@app.post("/jobs/{job_id}/update", response_class=RedirectResponse)
def update_job(
    job_id: str,
    url: str = Form(...),
    search_text: str = Form(""),
    minutes: int = Form(...),
    webhook_url: Optional[str] = Form(None),
    price_threshold: Optional[float] = Form(None),
):
    if minutes < 1:
        raise HTTPException(400, "Minutes must be >= 1")
    update_job_row(
        job_id=job_id,
        url=url.strip(),
        search_text=(search_text or "").strip(),
        minutes=int(minutes),
        webhook_url=webhook_url.strip() if webhook_url else None,
        price_threshold=float(price_threshold) if price_threshold not in (None, "") else None,
    )
    reschedule_job(job_id)
    return RedirectResponse(url="/", status_code=303)

@app.post("/jobs", response_class=RedirectResponse)
def create_job(
    url: str = Form(...),
    search_text: str = Form(""),
    minutes: int = Form(...),
    webhook_url: Optional[str] = Form(None),
    price_threshold: Optional[float] = Form(None),
):
    if minutes < 1:
        raise HTTPException(400, "Minutes must be >= 1")
    job = {
        "id": str(uuid.uuid4()),
        "url": url.strip(),
        "search_text": (search_text or "").strip(),
        "minutes": minutes,
        "webhook_url": webhook_url.strip() if webhook_url else None,
        "price_threshold": float(price_threshold) if price_threshold not in (None, "") else None,
        "created_at": datetime.utcnow().isoformat(),
    }
    insert_job(job)
    schedule_job(job)
    return RedirectResponse(url="/", status_code=303)

@app.post("/jobs/{job_id}/run-now")
async def run_now(job_id: str):
    # fire-and-forget
    import asyncio
    asyncio.create_task(run_job(job_id))
    return JSONResponse({"ok": True})

@app.post("/jobs/{job_id}/delete", response_class=RedirectResponse)
def remove_job(job_id: str):
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    delete_job(job_id)
    return RedirectResponse(url="/", status_code=303)

@app.get("/jobs/{job_id}/latest", response_class=FileResponse)
def latest_text(job_id: str):
    f = (DATA_DIR / job_id) / "latest.txt"
    if f.exists():
        return FileResponse(str(f))
    raise HTTPException(404, "No latest summary yet.")

@app.get("/jobs/{job_id}/files", response_class=HTMLResponse)
def list_files(job_id: str):
    job_dir = DATA_DIR / job_id
    if not job_dir.exists():
        return HTMLResponse("<p>No files yet.</p>", status_code=404)
    items = sorted(job_dir.iterdir(), reverse=True)
    lis = "".join(f'<li><a href="/data/{job_id}/{p.name}">{p.name}</a></li>' for p in items)
    return HTMLResponse(f"<h3>Artifacts for {job_id}</h3><ul>{lis}</ul>")

# ---------- Startup / Shutdown ----------
@app.on_event("startup")
def on_startup():
    init_db()
    for job in list_jobs():
        schedule_job(job)
    scheduler.start()
    logging.info("Scheduler started")

@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown()
