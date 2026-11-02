"""Command line: python -m monitor.cli <init|add|run-due|serve> [options]"""
import argparse
import json
import os

from . import db
from .worker import run_due

DB_PATH = os.environ.get("MONITOR_DB", "monitor.db")
SNAP_DIR = os.environ.get("MONITOR_SNAPSHOTS", "snapshots")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="monitor")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database")

    a = sub.add_parser("add", help="add a source")
    a.add_argument("name")
    a.add_argument("url")
    a.add_argument("--authority", default="")
    a.add_argument("--state", default="CENTRAL")
    a.add_argument("--selector", default="")
    a.add_argument("--every", type=int, default=360, help="check interval in minutes")
    a.add_argument("--ignore-robots", action="store_true")

    sub.add_parser("run-due", help="check every source whose interval has elapsed")

    s = sub.add_parser("serve", help="start the admin UI on 127.0.0.1")
    s.add_argument("--port", type=int, default=5055)

    args = ap.parse_args(argv)
    conn = db.connect(DB_PATH)

    if args.cmd == "init":
        print(f"database ready: {DB_PATH}")
    elif args.cmd == "add":
        sid = db.add_source(conn, args.name, args.url, args.authority, args.state, args.selector,
                            args.every, not args.ignore_robots)
        print(f"added source {sid}")
    elif args.cmd == "run-due":
        print(json.dumps(run_due(conn, SNAP_DIR), indent=2))
    elif args.cmd == "serve":
        from .admin import create_app
        create_app(DB_PATH, SNAP_DIR, password=os.environ.get("ADMIN_PASSWORD") or None
                   ).run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
