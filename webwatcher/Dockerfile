# Uses official Playwright image with Chromium already installed
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# Add Tesseract for OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persist artifacts
VOLUME ["/app/data"]

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
