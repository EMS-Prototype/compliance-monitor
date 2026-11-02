"""Production entry point: gunicorn wsgi:app

Environment:
  ADMIN_PASSWORD   login password (user is "admin"). REQUIRED when running on Render.
  MONITOR_DB       SQLite path            (default monitor.db)
  MONITOR_SNAPSHOTS snapshot folder       (default snapshots)
  SEED_DEMO=1      add demo sources to an empty database
  AUTO_RUN=1       run due checks in the background
  SCHEDULER_SECONDS how often the scheduler wakes (default 120)
"""
import os

from monitor import db
from monitor.admin import create_app
from monitor.scheduler import start_scheduler
from monitor.seed import seed_demo

DB_PATH = os.environ.get("MONITOR_DB", "monitor.db")
SNAP_DIR = os.environ.get("MONITOR_SNAPSHOTS", "snapshots")
PASSWORD = os.environ.get("ADMIN_PASSWORD") or None

# Fail closed: never serve an open admin on a hosted platform.
if os.environ.get("RENDER") and not PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD must be set when running on Render")

app = create_app(DB_PATH, SNAP_DIR, password=PASSWORD)

if os.environ.get("SEED_DEMO") == "1":
    seed_demo(db.connect(DB_PATH), f"http://127.0.0.1:{os.environ.get('PORT', '10000')}")

if os.environ.get("AUTO_RUN") == "1":
    start_scheduler(DB_PATH, SNAP_DIR, interval_seconds=int(os.environ.get("SCHEDULER_SECONDS", "120")))
