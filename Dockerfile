# UWAGA: to jest monolityczny obraz (instaluje całe ML przy każdym buildzie →
# deploy 7-8 min). Szybszy wariant „Poziom 0" rozbija to na obraz bazowy z ML
# (Dockerfile.base, publikowany do GHCR) + cienki obraz aplikacji (Dockerfile.slim,
# deploy ~1 min). Jak przełączyć: docs/OBRAZ_BAZOWY_ML.md
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

# All system deps, Python packages, and cleanup in a single layer so that
# build-only tools (git, curl) and bytecode caches never bloat the exported
# image.  Separate layers would embed git+curl permanently even if removed
# later; a single RUN avoids that.
#
# torchaudio removed — not used by any pipeline in this application.
# git/curl are purged after pip finishes; they are only needed for the two
# git+https:// installs (LightGlue, GroundingDINO).
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-pol \
    tesseract-ocr-eng \
    tesseract-ocr-ukr \
    tesseract-ocr-rus \
    tesseract-ocr-deu \
    tesseract-ocr-fra \
    tesseract-ocr-spa \
    tesseract-ocr-ita \
    tesseract-ocr-nld \
    tesseract-ocr-por \
    tesseract-ocr-ces \
    tesseract-ocr-slk \
    tesseract-ocr-ron \
    tesseract-ocr-hun \
    tesseract-ocr-hrv \
    tesseract-ocr-slv \
    tesseract-ocr-bul \
    tesseract-ocr-est \
    tesseract-ocr-lav \
    tesseract-ocr-lit \
    tesseract-ocr-fin \
    tesseract-ocr-swe \
    tesseract-ocr-nor \
    tesseract-ocr-dan \
    poppler-utils \
    ghostscript \
    libgl1 \
    libglib2.0-0 \
    libzbar0 \
    fonts-dejavu-core \
    fonts-liberation \
    curl \
    git \
    # CPU-only PyTorch — must come before requirements.txt to prevent pip
    # from resolving CUDA variants (~2.5 GB) from the default index.
    && pip install --no-cache-dir --retries 5 --timeout 120 \
        "torch>=2.1,<3.0" \
        "torchvision>=0.16,<1.0" \
        --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir --retries 5 --timeout 120 -r requirements.txt \
    # Remove build-only tools so they don't appear in the exported layer.
    && apt-get purge -y --auto-remove curl git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    # Strip bytecode caches — saves ~150-200 MB across all ML packages.
    && find /usr/local/lib/python3.11 -name "*.pyc" -delete \
    && find /usr/local/lib/python3.11 -name "__pycache__" -type d \
         -exec rm -rf {} + 2>/dev/null || true \
    && rm -rf /root/.cache /tmp/pip* /tmp/*.whl

COPY . .

EXPOSE 5000

# Migration runs at container start so it targets the runtime DB path
# (which may be on a persistent mounted volume, not available at build time).
# Workers: 2 × 3 threads — each worker loads its own model copy (~2 GB),
# 3 workers exhausted 8 GB RAM and triggered OOM. 2 workers × 3 threads
# serves ~5 concurrent users with overlap during Claude API I/O waits.
CMD python migrate_db.py && \
    gunicorn app:app \
    --workers 2 \
    --worker-class gthread \
    --threads 3 \
    --timeout 300 \
    --bind 0.0.0.0:5000
