"""Background loop that runs due checks. One per process (run gunicorn with a single worker)."""
import logging
import threading
import time

from . import db
from .worker import run_due

log = logging.getLogger("monitor.scheduler")


def start_scheduler(db_path, snap_dir, interval_seconds=120, initial_delay=15, headless=True):
    def loop():
        time.sleep(initial_delay)          # let the web server come up first (demo pages are served by it)
        conn = db.connect(db_path)         # own connection: this thread never shares one with requests
        while True:
            try:
                results = run_due(conn, snap_dir, headless)
                if results:
                    log.info("scheduled run: %s", [(r["source_id"], r["result"]) for r in results])
            except Exception:
                log.exception("scheduled run failed")
            time.sleep(interval_seconds)

    t = threading.Thread(target=loop, daemon=True, name="scheduler")
    t.start()
    return t
