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

# ---------- Config ----------
DB_PATH = os.environ.get("DB_PATH", "jobs.db")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------- App & templating ----------
app = FastAPI(title="Web Watcher")
templates = Jinja2Templates(directory="templates")
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

# ---------- Scheduler ----------
scheduler = BackgroundScheduler()

def schedule_job(job):
    trigger = IntervalTrigger(minutes=job["minutes"])
    scheduler.add_job(run_job, trigger=trigger, args=[job["id"]], id=job["id"], replace_existing=True)

async def screenshot_and_ocr(url: str, save_dir: Path) -> tuple[Path, str]:
    save_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    img_path = save_dir / f"{ts}.png"
    text_path = save_dir / f"{ts}.txt"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1366, "height": 768})
        try:
            await page.goto(url, timeout=60000, wait_until="networkidle")
        except Exception:
            # Try a slower wait
            await page.wait_for_timeout(5000)
        await page.screenshot(path=str(img_path), full_page=True)
        await browser.close()

    text = pytesseract.image_to_string(Image.open(img_path))
    text_path.write_text(text, encoding="utf-8")
    return img_path, text

async def notify_discord(webhook_url: str, content: str, image_url: Optional[str] = None):
    payload = {"content": content}
    if image_url:
        payload["embeds"] = [{"image": {"url": image_url}}]
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(webhook_url, json=payload)
        r.raise_for_status()

def run_job_sync(job_id: str):
    asyncio.run(run_job(job_id))

async def run_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return
    url = job["url"]
    search_text = job["search_text"]
    webhook_url = job.get("webhook_url") or None
    job_dir = DATA_DIR / job_id
    img_path, text = await screenshot_and_ocr(url, job_dir)

    matched = search_text.lower() in text.lower()
    # Save a latest.txt summary
    summary = f"Checked: {url}\nAt (UTC): {datetime.utcnow().isoformat()}\nMatched: {matched}\nSearch text: {search_text}\n"
    (job_dir / "latest.txt").write_text(summary, encoding="utf-8")

    # If matched, send Discord notification (optional)
    if matched and webhook_url:
        # Build a public-ish URL path served by this app
        # Example: /data/<job_id>/<filename>
        image_rel = f"/data/{job_id}/{img_path.name}"
        try:
            await notify_discord(webhook_url, f"✅ Match found for '{search_text}' on {url}", image_rel)
        except Exception as e:
            (job_dir / "discord_error.log").write_text(str(e), encoding="utf-8")

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
def run_now(job_id: str):
    # fire-and-forget immediate run
    scheduler.add_job(run_job_sync, args=[job_id], replace_existing=False)
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