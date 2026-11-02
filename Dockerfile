FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    MONITOR_DB=/data/monitor.db \
    MONITOR_SNAPSHOTS=/data/snapshots \
    SEED_DEMO=1 \
    AUTO_RUN=1

WORKDIR /app

COPY requirements.txt .
# Chromium plus the system libraries it needs, installed as root, then made readable to the app user.
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /ms-playwright \
 && rm -rf /var/lib/apt/lists/*

COPY monitor ./monitor
COPY wsgi.py .

RUN useradd --create-home app && mkdir -p /data && chown app:app /data
USER app

# ONE worker on purpose: it owns the background scheduler and the single-browser lock.
# Threads handle concurrent page views; --timeout is generous because "Check now" drives a real browser.
CMD ["sh", "-c", "exec gunicorn wsgi:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT:-10000}"]
