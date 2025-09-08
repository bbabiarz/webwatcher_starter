# Dockerfile
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# Avoid tzdata interactive prompt, set a default TZ
ENV DEBIAN_FRONTEND=noninteractive TZ=UTC

# Add tzdata (for zoneinfo) + tesseract
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

VOLUME ["/app/data"]
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
