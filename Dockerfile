FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    VT_DB_PATH=/data/tiles.db \
    VT_WORKERS=2

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source.
COPY app ./app
COPY data ./data
COPY scripts ./scripts

RUN mkdir -p /data && \
    groupadd --system tiles && \
    useradd --system --gid tiles --home-dir /app tiles && \
    chown -R tiles:tiles /data /app
USER tiles

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=5s --start-period=15s --retries=10 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

# Two workers share the same SQLite file; cross-process optimistic
# concurrency is serialised inside BEGIN IMMEDIATE transactions (see
# README "并发发布（跨进程竞态复核）").
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${VT_WORKERS:-2}"]
