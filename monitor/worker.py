"""Playwright worker: fetch a source, detect changes, queue them for human review.

Behaviour on a gated page (CAPTCHA / OTP / login / 403 / 429): stop, take a screenshot
for the human, open a handoff, mark the source blocked. Never attempts to get past it.
"""
import difflib
import hashlib
import re
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from .challenge import detect_challenge
from .db import now

USER_AGENT = "ComplianceMonitorBot/0.1 (learning project; polite, low-frequency)"

# Only one browser at a time: keeps memory low on small hosts and avoids overlapping runs
# when the scheduler and a manual "Check now" fire together.
CHECK_LOCK = threading.Lock()

_EXTRACT_JS = r"""
(sel) => {
  const root = sel ? document.querySelector(sel) : document.body;
  if (!root) return null;
  const lines = root.innerText.split('\n').map(s => s.trim()).filter(Boolean);
  // Include links so a newly published PDF/notification shows up as a change.
  const links = [...root.querySelectorAll('a[href]')]
    .map(a => 'LINK: ' + a.innerText.trim() + ' -> ' + a.href);
  return lines.concat(links);
}
"""


class SelectorMissing(Exception):
    pass


def draft_summary(old_lines, new_lines):
    """Hook for an LLM drafter. Default: count added/removed lines and show the first few.

    A draft is only ever a suggestion for the reviewer, never published automatically.
    """
    old, new = set(old_lines), set(new_lines)
    added = [l for l in new_lines if l not in old]
    removed = [l for l in old_lines if l not in new]
    parts = [f"{len(added)} line(s) added, {len(removed)} removed."]
    if added:
        parts.append("Added: " + " | ".join(added[:3]))
    if removed:
        parts.append("Removed: " + " | ".join(removed[:3]))
    return " ".join(parts)


def _normalize(lines):
    return [re.sub(r"\s+", " ", l).strip() for l in lines if l.strip()]


def is_due(source, at=None):
    if not source["active"]:
        return False
    if not source["last_checked_at"]:
        return True
    at = at or datetime.now(timezone.utc)
    last = datetime.fromisoformat(source["last_checked_at"])
    return at - last >= timedelta(minutes=source["check_every_minutes"])


class Checker:
    """Owns one Playwright browser. Use as a context manager."""

    def __init__(self, conn, snap_dir, headless=True, shot_ext="png"):
        self.conn = conn
        self.snap_dir = Path(snap_dir)
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        # "png" = full-page screenshots (admin app). "jpg" = small top-of-page JPEGs (daily batch,
        # keeps the git repo light because screenshots are committed).
        self.shot_ext = shot_ext
        self.shot_kwargs = ({"type": "jpeg", "quality": 60, "full_page": False}
                            if shot_ext == "jpg" else {"full_page": True})

    def __enter__(self):
        self._pw = sync_playwright().start()
        # --disable-dev-shm-usage: containers often have a tiny /dev/shm, which crashes Chromium
        self._browser = self._pw.chromium.launch(headless=self.headless,
                                                 args=["--disable-dev-shm-usage"])
        self._ctx = self._browser.new_context(user_agent=USER_AGENT)
        return self

    def __exit__(self, *exc):
        self._ctx.close()
        self._browser.close()
        self._pw.stop()

    # ------------------------------------------------------------------ helpers
    def _robots_allows(self, url):
        p = urlparse(url)
        robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
        try:
            resp = self._ctx.request.get(robots_url, timeout=10000)
            if resp.status != 200:
                return True  # no robots.txt published
            rp = RobotFileParser()
            rp.parse(resp.text().splitlines())
            return rp.can_fetch(USER_AGENT, url)
        except Exception:
            return True  # robots unreachable: proceed, the fetch itself will surface real problems

    def _set_status(self, sid, status, error=None, hash_=None):
        sets, vals = ["status=?", "last_checked_at=?", "last_error=?"], [status, now(), error]
        if hash_ is not None:
            sets.append("last_hash=?")
            vals.append(hash_)
        self.conn.execute(f"UPDATE sources SET {','.join(sets)} WHERE id=?", (*vals, sid))
        self.conn.commit()

    def _save_snapshot(self, sid, lines, hash_, page):
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        text_path = self.snap_dir / f"s{sid}_{ts}.txt"
        text_path.write_text("\n".join(lines), encoding="utf-8")
        shot = self.snap_dir / f"s{sid}_{ts}.{self.shot_ext}"
        page.screenshot(path=str(shot), **self.shot_kwargs)
        cur = self.conn.execute(
            "INSERT INTO snapshots (source_id, taken_at, content_hash, text_path, screenshot_path)"
            " VALUES (?,?,?,?,?)", (sid, now(), hash_, text_path.name, shot.name))
        self.conn.commit()
        return cur.lastrowid

    def _open_handoff(self, sid, kind, detail, page):
        existing = self.conn.execute(
            "SELECT id FROM handoffs WHERE source_id=? AND kind=? AND status='open'", (sid, kind)).fetchone()
        if existing:
            return existing["id"]
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        shot = self.snap_dir / f"handoff_s{sid}_{ts}.{self.shot_ext}"
        try:
            page.screenshot(path=str(shot), **self.shot_kwargs)  # for the human reviewer to look at
            shot_name = shot.name
        except Exception:
            shot_name = None
        cur = self.conn.execute(
            "INSERT INTO handoffs (source_id, created_at, kind, detail, screenshot_path) VALUES (?,?,?,?,?)",
            (sid, now(), kind, detail, shot_name))
        self.conn.commit()
        return cur.lastrowid

    # --------------------------------------------------------------------- main
    def check(self, sid):
        src = self.conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        if src is None:
            raise KeyError(f"no source {sid}")

        if src["respect_robots"] and not self._robots_allows(src["url"]):
            self._set_status(sid, "blocked", "robots.txt disallows this URL")
            return {"result": "skipped_robots", "source_id": sid}

        page = self._ctx.new_page()
        try:
            resp = page.goto(src["url"], wait_until="domcontentloaded", timeout=30000)
            status = resp.status if resp else None
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PWTimeout:
                pass  # long-polling pages: carry on with what has rendered

            challenge = detect_challenge(page, status)
            if challenge:
                kind, detail = challenge
                hid = self._open_handoff(sid, kind, detail, page)
                self._set_status(sid, "blocked", f"{kind}: {detail}")
                return {"result": "blocked", "source_id": sid, "kind": kind, "handoff_id": hid}

            raw = page.evaluate(_EXTRACT_JS, src["selector"] or None)
            if raw is None:
                raise SelectorMissing(f"selector {src['selector']!r} matched nothing")
            lines = _normalize(raw)
            if not lines:
                raise SelectorMissing("watched region is empty")
            hash_ = hashlib.sha256("\n".join(lines).encode()).hexdigest()

            prev = self.conn.execute(
                "SELECT * FROM snapshots WHERE source_id=? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
            if prev and prev["content_hash"] == hash_:
                self._set_status(sid, "ok", None, hash_)
                return {"result": "unchanged", "source_id": sid}

            new_id = self._save_snapshot(sid, lines, hash_, page)
            self._set_status(sid, "ok", None, hash_)
            if prev is None:
                return {"result": "baseline", "source_id": sid, "snapshot_id": new_id}

            old_lines = (self.snap_dir / prev["text_path"]).read_text(encoding="utf-8").splitlines()
            diff = "\n".join(difflib.unified_diff(old_lines, lines, "previous", "current", lineterm="", n=1))
            cur = self.conn.execute(
                "INSERT INTO changes (source_id, detected_at, old_snapshot_id, new_snapshot_id, summary, diff)"
                " VALUES (?,?,?,?,?,?)",
                (sid, now(), prev["id"], new_id, draft_summary(old_lines, lines), diff))
            self.conn.commit()
            return {"result": "changed", "source_id": sid, "change_id": cur.lastrowid}
        except Exception as e:
            # First line only: Playwright appends a multi-line "Call log" that is noise on a dashboard.
            msg = (str(e).strip().splitlines() or [""])[0]
            self._set_status(sid, "error", f"{type(e).__name__}: {msg}"[:300])
            return {"result": "error", "source_id": sid, "error": msg[:300]}
        finally:
            page.close()


def run_due(conn, snap_dir, headless=True):
    due = [r["id"] for r in conn.execute("SELECT * FROM sources").fetchall() if is_due(r)]
    if not due:
        return []
    with CHECK_LOCK, Checker(conn, snap_dir, headless) as c:
        return [c.check(sid) for sid in due]
