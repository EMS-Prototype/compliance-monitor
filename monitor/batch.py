"""Daily batch run, built for GitHub Actions.

    python -m monitor.batch

1. Reads sources.toml (the list of pages to watch) and syncs it into the SQLite database.
2. Scrapes every active source once with Playwright (same worker as the web app).
3. Writes site/data/results.json (+ screenshots) for the static frontend.
4. Prunes old snapshot files so the git repo stays small.

The database and snapshots are committed back to the repo by the workflow, which is how
yesterday's page is available to diff against today's.
"""
import argparse
import json
import os
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .security import validate_public_url
from .worker import CHECK_LOCK, Checker


# ------------------------------------------------------------------ sources.toml
def load_sources(path):
    with open(path, "rb") as f:
        items = tomllib.load(f).get("source", [])
    seen = set()
    for i, s in enumerate(items, 1):
        for key in ("name", "url"):
            if not str(s.get(key, "")).strip():
                raise ValueError(f"source #{i} in {path}: missing '{key}'")
        if s["name"] in seen:
            raise ValueError(f"duplicate source name {s['name']!r} in {path}")
        seen.add(s["name"])
    return items


def sync_sources(conn, items):
    """Make the database match sources.toml. Sources removed from the file are deactivated, not deleted."""
    for s in items:
        vals = (s.get("authority", ""), s.get("state", "CENTRAL"), s["url"], s.get("selector", ""),
                1 if s.get("respect_robots", True) else 0)
        if conn.execute("SELECT 1 FROM sources WHERE name=?", (s["name"],)).fetchone():
            conn.execute("UPDATE sources SET authority=?, state=?, url=?, selector=?, respect_robots=?, active=1"
                         " WHERE name=?", (*vals, s["name"]))
        else:
            db.add_source(conn, s["name"], s["url"], authority=vals[0], state=vals[1], selector=vals[3],
                          respect_robots=bool(vals[4]))
    names = [s["name"] for s in items]
    if names:
        conn.execute(f"UPDATE sources SET active=0 WHERE name NOT IN ({','.join('?' * len(names))})", names)
    else:
        conn.execute("UPDATE sources SET active=0")
    conn.commit()


# ------------------------------------------------------------------------ run
def run(conn, snap_dir, allow_private=False, headless=True):
    rows = conn.execute("SELECT * FROM sources WHERE active=1 ORDER BY id").fetchall()
    results = []
    with CHECK_LOCK, Checker(conn, snap_dir, headless, shot_ext="jpg") as checker:
        for r in rows:
            if not allow_private:
                try:
                    validate_public_url(r["url"])
                except ValueError as e:
                    conn.execute("UPDATE sources SET status='error', last_checked_at=?, last_error=? WHERE id=?",
                                 (db.now(), f"ValueError: {e}", r["id"]))
                    conn.commit()
                    results.append({"result": "error", "source_id": r["id"], "error": str(e)})
                    continue
            results.append(checker.check(r["id"]))
    return results


# ---------------------------------------------------------------------- export
def export(conn, snap_dir, out_path):
    """Write the JSON the static frontend reads. Screenshot paths are relative to the JSON file."""
    snap_dir, out_path = Path(snap_dir), Path(out_path)
    rel = Path(os.path.relpath(snap_dir, out_path.parent)).as_posix()

    sources = []
    for s in conn.execute("SELECT * FROM sources WHERE active=1 ORDER BY id").fetchall():
        snap = conn.execute("SELECT * FROM snapshots WHERE source_id=? ORDER BY id DESC LIMIT 1",
                            (s["id"],)).fetchone()
        lines = []
        if snap:
            try:
                lines = (snap_dir / snap["text_path"]).read_text(encoding="utf-8").splitlines()
            except OSError:
                pass
        handoffs = [{"kind": h["kind"], "detail": h["detail"], "created_at": h["created_at"],
                     "screenshot": f"{rel}/{h['screenshot_path']}" if h["screenshot_path"] else None}
                    for h in conn.execute("SELECT * FROM handoffs WHERE source_id=? AND status='open' ORDER BY id DESC",
                                          (s["id"],)).fetchall()]
        sources.append({
            "id": s["id"], "name": s["name"], "authority": s["authority"], "state": s["state"], "url": s["url"],
            "selector": s["selector"], "status": s["status"], "last_checked_at": s["last_checked_at"],
            "last_error": s["last_error"],
            "latest": {"taken_at": snap["taken_at"], "line_count": len(lines), "lines": lines[:40],
                       "screenshot": f"{rel}/{snap['screenshot_path']}" if snap and snap["screenshot_path"] else None}
                      if snap else None,
            "pending_changes": conn.execute("SELECT COUNT(*) FROM changes WHERE source_id=? AND status='pending'",
                                            (s["id"],)).fetchone()[0],
            "handoffs": handoffs,
        })

    changes = [{"id": c["id"], "source_id": c["source_id"], "source_name": c["source_name"],
                "detected_at": c["detected_at"], "summary": c["summary"], "status": c["status"],
                "diff": c["diff"][:4000]}
               for c in conn.execute(
                   "SELECT c.*, s.name AS source_name FROM changes c JOIN sources s ON s.id=c.source_id"
                   " WHERE s.active=1 ORDER BY c.id DESC LIMIT 100").fetchall()]

    count = lambda st: sum(1 for x in sources if x["status"] == st)  # noqa: E731
    payload = {
        "generated_at": db.now(),
        "run_url": (f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
                    f"{os.environ['GITHUB_RUN_ID']}") if os.environ.get("GITHUB_RUN_ID") else None,
        "summary": {"sources": len(sources), "ok": count("ok"), "blocked": count("blocked"),
                    "error": count("error"), "pending_changes": sum(x["pending_changes"] for x in sources)},
        "sources": sources,
        "changes": changes,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out_path)
    return payload


# ----------------------------------------------------------------------- prune
def prune(conn, snap_dir, keep=2):
    """Delete snapshot files that are no longer needed. Keeps the newest `keep` per source
    (the newest is what tomorrow's run diffs against) and any open handoff screenshot."""
    snap_dir = Path(snap_dir)
    keep_files = set()
    for (sid,) in conn.execute("SELECT id FROM sources").fetchall():
        for r in conn.execute("SELECT text_path, screenshot_path FROM snapshots WHERE source_id=?"
                              " ORDER BY id DESC LIMIT ?", (sid, keep)).fetchall():
            keep_files.update(x for x in (r["text_path"], r["screenshot_path"]) if x)
    for r in conn.execute("SELECT screenshot_path FROM handoffs WHERE status='open'").fetchall():
        if r["screenshot_path"]:
            keep_files.add(r["screenshot_path"])
    removed = 0
    for p in snap_dir.iterdir():
        if p.is_file() and p.suffix in (".txt", ".png", ".jpg") and p.name not in keep_files:
            p.unlink()
            removed += 1
    return removed


# ------------------------------------------------------------------------ main
def _esc(text, prop=False):
    """Escape text for a GitHub Actions workflow command (::warning ...::message)."""
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def main(argv=None):
    ap = argparse.ArgumentParser(prog="monitor.batch")
    ap.add_argument("--sources", default="sources.toml")
    ap.add_argument("--db", default="data/monitor.db")
    ap.add_argument("--snaps", default="site/data/snaps")
    ap.add_argument("--out", default="site/data/results.json")
    ap.add_argument("--keep", type=int, default=2, help="snapshots to keep per source")
    ap.add_argument("--allow-private", action="store_true",
                    default=os.environ.get("ALLOW_PRIVATE_URLS") == "1",
                    help="allow localhost/private URLs (local testing only)")
    args = ap.parse_args(argv)

    items = load_sources(args.sources)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(args.db)
    try:
        sync_sources(conn, items)
        results = run(conn, args.snaps, allow_private=args.allow_private)
        payload = export(conn, args.snaps, args.out)
        removed = prune(conn, args.snaps, args.keep)
    finally:
        conn.execute("PRAGMA journal_mode=DELETE")   # fold the WAL in: one clean file to commit
        conn.close()

    s = payload["summary"]
    print(f"{s['sources']} source(s): {s['ok']} ok, {s['blocked']} blocked, {s['error']} error; "
          f"{s['pending_changes']} change(s) awaiting review; pruned {removed} old file(s)")
    for src in payload["sources"]:
        line = f"  [{src['status']:7}] {src['name']}"
        if src["last_error"]:
            line += f"  <- {src['last_error']}"
        print(line)
        if src["status"] in ("blocked", "error"):   # shows as an annotation on the Actions run page
            print(f"::warning title={_esc(src['name'], prop=True)}::{_esc(src['status'] + ': ' + str(src['last_error']))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
