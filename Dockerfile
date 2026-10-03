# Tender Synopsis Generator - cloud image (Railway / any Docker host)
FROM python:3.11-slim

# Tesseract = the OCR that always works, even when the cloud OCR is down or out of quota.
# English + Hindi language data (bilingual tenders).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data OMP_THREAD_LIMIT=1
WORKDIR /app
COPY requirements-web.txt .
RUN pip install -r requirements-web.txt
COPY . .

# One process (jobs run in its threads and share one SQLite file); threads serve the pages.
CMD gunicorn wsgi:app --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 8 --timeout 120 --graceful-timeout 30
