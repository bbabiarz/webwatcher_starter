import os
import uuid
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytesseract
from PIL import Image
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import asyncio
from playwright.async_api import async_playwright
import httpx
from fastapi.responses import HTMLResponse
import shutil
import json




# ---------- Config ----------
DB_PATH = os.environ.get("DB_PATH", "jobs.db")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------- App & templating ----------
app = FastAPI(title="Web Watcher")
templates = Jinja2Templates(directory="templates")
STATIC_DIR = Path("static")
STATIC_DIR.mkdir(parents=True, exist_ok=True)  # ensure it exists
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")

# ---------- DB helpers ----------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                search_text TEXT NOT NULL,
                minutes INTEGER NOT NULL,
                webhook_url TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()

def insert_job(job):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?)",
                  (job["id"], job["url"], job["search_text"], job["minutes"], job.get("webhook_url"), job["created_at"]))
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
    
from fastapi.responses import JSONResponse
import asyncio

@app.post("/jobs/{job_id}/run-now")
async def run_now(job_id: str):
    asyncio.create_task(run_job(job_id))  # fire-and-forget
    return JSONResponse({"ok": True})

@app.get("/jobs/{job_id}/files", response_class=HTMLResponse)
def list_files(job_id: str):
    job_dir = DATA_DIR / job_id
    if not job_dir.exists():
        return HTMLResponse("<p>No files yet.</p>", status_code=404)
    items = sorted(job_dir.iterdir(), reverse=True)
    lis = "".join(f'<li><a href="/data/{job_id}/{p.name}">{p.name}</a></li>' for p in items)
    return HTMLResponse(f"<h3>Artifacts for {job_id}</h3><ul>{lis}</ul>")



# ---------- Scheduler ----------
from apscheduler.schedulers.asyncio import AsyncIOScheduler
scheduler = AsyncIOScheduler(timezone=os.getenv("TZ", "UTC"))

def schedule_job(job):
    trigger = IntervalTrigger(minutes=job["minutes"])
    # target the coroutine directly (AsyncIOScheduler will await it)
    scheduler.add_job(run_job, trigger=trigger, args=[job["id"]],
                      id=job["id"], replace_existing=True)

import logging, traceback
logging.basicConfig(level=logging.INFO)

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
            # Load page; be tolerant to failures but still take a screenshot
            try:
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            except Exception:
                # fall back: give the page a moment even if goto errored
                await page.wait_for_timeout(3000)

            # Always capture a full-page PNG for debugging/notifications
            await page.screenshot(path=str(img_path), full_page=True)

            # Try DOM text first (faster & cleaner than OCR)
            dom_text = await page.evaluate("() => document.body.innerText || ''")
            text = dom_text.strip()
            if not text:
                from PIL import Image
                import pytesseract
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

async def run_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return
    url = job["url"]
    search_text = job["search_text"]
    webhook_url = job.get("webhook_url") or None
    job_dir = DATA_DIR / job_id

    img_path, text = await screenshot_and_ocr(url, job_dir)

    latest_img = job_dir / "latest.png"
    try:
        latest_img.unlink(missing_ok=True)
    except Exception:
        pass
    shutil.copy(img_path, latest_img)

    matched = search_text.lower() in text.lower()
    summary = (
        f"Checked: {url}\n"
        f"At (UTC): {datetime.utcnow().isoformat()}\n"
        f"Matched: {matched}\n"
        f"Search text: {search_text}\n"
    )
    (job_dir / "latest.txt").write_text(summary, encoding="utf-8")

    if matched and webhook_url:
        image_rel = f"/data/{job_id}/{img_path.name}"
        try:
            await notify_discord(webhook_url, f"✅ Match found for '{search_text}' on {url}", image_rel)
        except Exception as e:
            (job_dir / "discord_error.log").write_text(str(e), encoding="utf-8")

async def notify_discord(webhook_url: str, content: str, image_path: Optional[Path] = None, external_url: Optional[str] = None):
    async with httpx.AsyncClient(timeout=30) as client:
        if image_path and image_path.exists():
            payload = {
                "content": content,
                "embeds": [{"image": {"url": f"attachment://{image_path.name}"}}]
            }
            files = {"file": (image_path.name, image_path.read_bytes(), "image/png")}
            r = await client.post(webhook_url, data={"payload_json": json.dumps(payload)}, files=files)
        else:
            # fallback: just send text (optionally include an external URL)
            payload = {"content": f"{content}\n{external_url}" if external_url else content}
            r = await client.post(webhook_url, json=payload)
        r.raise_for_status()


def run_job_sync(job_id: str):
    asyncio.run(run_job(job_id))



# ---------- Routes ----------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    rows = list_jobs()
    return templates.TemplateResponse("index.html", {"request": request, "jobs": rows})

@app.post("/jobs", response_class=RedirectResponse)
def create_job(url: str = Form(...),
               search_text: str = Form(...),
               minutes: int = Form(...),
               webhook_url: Optional[str] = Form(None)):
    if minutes < 1:
        raise HTTPException(400, "Minutes must be >= 1")
    job = {
        "id": str(uuid.uuid4()),
        "url": url.strip(),
        "search_text": search_text.strip(),
        "minutes": minutes,
        "webhook_url": webhook_url.strip() if webhook_url else None,
        "created_at": datetime.utcnow().isoformat()
    }
    insert_job(job)
    schedule_job(job)
    return RedirectResponse(url="/", status_code=303)

@app.post("/jobs/{job_id}/run-now")
async def run_now(job_id: str):
    await run_job(job_id)
    return JSONResponse({"ok": True})

@app.post("/jobs/{job_id}/delete", response_class=RedirectResponse)
def remove_job(job_id: str):
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    delete_job(job_id)
    # Optionally keep artifacts in data/
    return RedirectResponse(url="/", status_code=303)

@app.get("/jobs/{job_id}/latest", response_class=FileResponse)
def latest_text(job_id: str):
    job_dir = DATA_DIR / job_id
    f = job_dir / "latest.txt"
    if f.exists():
        return FileResponse(str(f))
    raise HTTPException(404, "No latest summary yet.")

from fastapi.responses import HTMLResponse

@app.get("/jobs/{job_id}/files", response_class=HTMLResponse)
def list_files(job_id: str):
    job_dir = DATA_DIR / job_id
    if not job_dir.exists():
        return HTMLResponse("<p>No files yet.</p>", status_code=404)
    items = sorted(job_dir.iterdir(), reverse=True)
    lis = "".join(f'<li><a href="/data/{job_id}/{p.name}">{p.name}</a></li>' for p in items)
    return HTMLResponse(f"<h3>Artifacts for {job_id}</h3><ul>{lis}</ul>")

async def run_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return

    url = job["url"]
    search_text = job["search_text"]
    webhook_url = job.get("webhook_url") or None
    job_dir = DATA_DIR / job_id

    # Take screenshot + get text (DOM first, OCR fallback)
    img_path, text = await screenshot_and_ocr(url, job_dir)

    # Keep a stable pointer for easy viewing
    try:
        latest_img = job_dir / "latest.png"
        if latest_img.exists():
            latest_img.unlink()
        shutil.copy(img_path, latest_img)
    except Exception as e:
        (job_dir / "latest_copy_error.log").write_text(str(e), encoding="utf-8")

    # Did we find the text?
    matched = search_text.lower() in text.lower()

    # Update latest.txt
    summary = (
        f"Checked: {url}\n"
        f"At (UTC): {datetime.utcnow().isoformat()}\n"
        f"Matched: {matched}\n"
        f"Search text: {search_text}\n"
    )
    (job_dir / "latest.txt").write_text(summary, encoding="utf-8")

    # If matched, notify Discord (upload PNG; optionally include a public URL)
    if matched and webhook_url:
        # If you have a domain or reverse proxy, set PUBLIC_BASE_URL in your stack/env
        base = os.getenv("PUBLIC_BASE_URL")
        external_url = f"{base}/data/{job_id}/{img_path.name}" if base else None
        try:
            await notify_discord(
                webhook_url,
                f"✅ Match found for '{search_text}' on {url}",
                image_path=img_path,            # upload file to Discord
                external_url=external_url       # optional clickable link
            )
        except Exception as e:
            (job_dir / "discord_error.log").write_text(str(e), encoding="utf-8")



# ---------- Startup ----------
@app.on_event("startup")
def on_startup():
    init_db()
    # reload jobs
    for job in list_jobs():
        schedule_job(job)
    scheduler.start()

@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown()