# Web Watcher (Screenshots + OCR + Discord alert)

Poll any webpage on a schedule, capture a full-page screenshot, OCR it, and notify a Discord webhook when your target text appears.

## Quick start (Docker)

```bash
# 1) Unzip this project, then from the project folder:
docker compose up --build
# 2) Open http://localhost:8000
```

## Local (no Docker)
- Python 3.11+ recommended
- System package: `tesseract-ocr`
- Install dependencies:
```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium
```
- Run:
```bash
uvicorn app:app --reload
```

## How it works
- **Scheduler**: APScheduler runs each job every N minutes.
- **Screenshot**: Playwright (Chromium) loads the page and captures a full-page PNG.
- **OCR**: Tesseract via `pytesseract` parses the image to text.
- **Match**: Case-insensitive contains check for your search text.
- **Notify**: If matched and a Discord webhook is provided, the app posts a message with the screenshot.

Artifacts live in `data/<job_id>/` and are served at `http://localhost:8000/data/<job_id>/`.

## Discord webhook
Create one under **Server Settings → Integrations → Webhooks**, copy the URL, and paste it in the job form.

## Notes & tips
- Some sites need more time/interaction. You can extend `screenshot_and_ocr` to click/scroll/login.
- For stability, keep intervals ≥ 1 minute. Be respectful of target sites' terms of use & robots.txt.
- To persist jobs/data across container restarts, the compose file mounts `./data`.

## Security
This demo stores jobs in a local SQLite DB and serves artifacts without auth for simplicity. Add auth if exposing publicly.