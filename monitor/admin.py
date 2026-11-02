"""Flask app: results dashboard, source management, review queue, handoffs, JSON API.

Security model (needed once this is on the public internet):
  * HTTP Basic auth on everything except /healthz and the public /demo pages (set ADMIN_PASSWORD).
  * POSTs carrying a cross-site Origin are rejected (CSRF; browsers re-send cached Basic credentials).
  * New source URLs must resolve to public addresses (see security.py).
"""
import hmac
import os
import threading
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, redirect, render_template_string, request, send_from_directory, url_for

from . import db
from .demo import bp as demo_bp
from .security import validate_public_url
from .worker import CHECK_LOCK, Checker

PAGE = """
<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Compliance Monitor</title>
<style>
 body{font:14px system-ui;margin:24px auto;max-width:1100px;padding:0 16px;color:#222}
 table{border-collapse:collapse;width:100%;margin:8px 0 24px} th,td{border:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}
 th{background:#f5f5f5} .ok{color:#0a7} .blocked{color:#c60;font-weight:600} .error{color:#c00;font-weight:600} .new{color:#666}
 pre{background:#f8f8f8;padding:8px;overflow:auto;max-height:220px;white-space:pre-wrap} input,button{font:inherit;padding:4px 6px}
 nav a{margin-right:14px} .pill{background:#eee;border-radius:10px;padding:1px 8px}
 .card{border:1px solid #ddd;border-radius:6px;padding:12px;margin-bottom:14px} .row{display:flex;gap:16px;flex-wrap:wrap}
 .grow{flex:1;min-width:280px} .muted{color:#666} img.shot{max-width:100%;border:1px solid #ccc;max-height:240px}
</style>
<nav><a href="/">Results</a><a href="/sources">Sources</a>
<a href="/review">Review queue <span class="pill">{{ n_pending }}</span></a>
<a href="/handoffs">Handoffs <span class="pill">{{ n_open }}</span></a><a href="/api/results">JSON</a></nav>
{% block body %}{% endblock %}
"""


def _tpl(body):
    return PAGE.replace("{% block body %}{% endblock %}", body)


RESULTS = _tpl("""
<meta http-equiv="refresh" content="60">
<h2>Latest scraper results</h2>
<p class="muted">{{ results|length }} source(s) &middot; {{ n_pending }} change(s) waiting for review &middot;
{{ n_open }} page(s) needing a human. Auto-refreshes every minute.</p>
{% for r in results %}<div class="card">
 <b>{{ r.name }}</b> <span class="muted">({{ r.authority or '-' }} / {{ r.state }})</span>
 &middot; <span class="{{ r.status }}">{{ r.status }}</span>
 &middot; <span class="muted">last checked {{ r.last_checked_at or 'never' }}</span><br>
 <small class="muted">{{ r.url }}</small>
 {% if r.last_error %}<p class="{{ r.status }}">{{ r.last_error }}</p>{% endif %}
 {% if r.open_handoffs %}<p class="blocked">Needs a human: {{ r.open_handoffs|join(', ') }}
   &mdash; the scraper stopped and did not try to get past it. <a href="/handoffs">Open handoffs</a></p>{% endif %}
 {% if r.pending_changes %}<p><b>{{ r.pending_changes }}</b> change(s) waiting: <a href="/review">review them</a></p>{% endif %}
 <div class="row">
  <div class="grow"><b>Latest scraped content</b> <span class="muted">({{ r.snapshot_taken_at or 'none yet' }})</span>
   <pre>{{ r.preview }}</pre></div>
  {% if r.screenshot %}<div class="grow"><b>Screenshot</b><br>
   <a href="/files/{{ r.screenshot }}" target="_blank"><img class="shot" src="/files/{{ r.screenshot }}"></a></div>{% endif %}
 </div>
 <form method="post" action="/sources/{{ r.id }}/run"><button>Check now</button></form>
</div>{% else %}<p>No sources yet. <a href="/sources">Add one.</a></p>{% endfor %}""")

SOURCES = _tpl("""
<h2>Sources</h2>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<table><tr><th>ID</th><th>Name</th><th>Authority / State</th><th>URL</th><th>Selector</th><th>Every (min)</th><th>Status</th><th>Last checked</th><th></th></tr>
{% for s in sources %}<tr><td>{{s.id}}</td><td>{{s.name}}</td><td>{{s.authority}} / {{s.state}}</td>
<td>{{s.url}}</td><td><code>{{s.selector}}</code></td><td>{{s.check_every_minutes}}</td>
<td class="{{s.status}}">{{s.status}}{% if s.last_error %}<br><small>{{s.last_error}}</small>{% endif %}</td>
<td>{{s.last_checked_at or '-'}}</td>
<td><form method="post" action="/sources/{{s.id}}/run"><button>Check now</button></form></td></tr>
{% else %}<tr><td colspan="9">No sources yet.</td></tr>{% endfor %}</table>
<h3>Add source</h3>
<form method="post" action="/sources">
 <input name="name" placeholder="Name" required> <input name="authority" placeholder="Authority (e.g. CBIC)">
 <input name="state" placeholder="State" value="CENTRAL"> <input name="url" placeholder="https://..." size="40" required>
 <input name="selector" placeholder="CSS selector (optional)"> <input name="check_every_minutes" type="number" value="360" min="1" style="width:80px">
 <label><input type="checkbox" name="respect_robots" checked> respect robots.txt</label> <button>Add</button>
</form>""")

REVIEW = _tpl("""
<h2>Review queue</h2>
{% for c in changes %}<div class="card">
<b>#{{c.id}} {{c.source_name}}</b> ({{c.state}}) &middot; {{c.detected_at}} &middot; <i>{{c.status}}</i>
<p>{{c.summary}}</p><pre>{{c.diff}}</pre>
{% if c.status == 'pending' %}<form method="post" action="/changes/{{c.id}}/decide">
<input name="reviewer" placeholder="Your name" required>
<button name="decision" value="approved">Approve</button> <button name="decision" value="rejected">Reject</button></form>
{% else %}<small>by {{c.reviewer}} at {{c.decided_at}}</small>{% endif %}</div>
{% else %}<p>Nothing to review.</p>{% endfor %}""")

HANDOFFS = _tpl("""
<h2>Handoffs (a human is needed)</h2>
<p>The worker stopped on these pages because they showed a CAPTCHA, OTP, login wall or block.
Open the page yourself, then record how it was resolved (for example, switch the source to an official feed or API).</p>
{% for h in handoffs %}<div class="card">
<b>#{{h.id}} {{h.source_name}}</b> &middot; <span class="blocked">{{h.kind}}</span> &middot; {{h.created_at}} &middot; <i>{{h.status}}</i>
<p>{{h.detail}}<br><a href="{{h.url}}" target="_blank" rel="noopener noreferrer">{{h.url}}</a></p>
{% if h.screenshot_path %}<a href="/files/{{h.screenshot_path}}" target="_blank"><img class="shot" src="/files/{{h.screenshot_path}}"></a>{% endif %}
{% if h.status == 'open' %}<form method="post" action="/handoffs/{{h.id}}/resolve" style="margin-top:8px">
<input name="resolved_by" placeholder="Your name" required> <input name="resolution" placeholder="What did you do?" size="40">
<button>Mark resolved</button></form>{% else %}<small>{{h.resolved_by}}: {{h.resolution}}</small>{% endif %}</div>
{% else %}<p>No handoffs.</p>{% endfor %}""")

PUBLIC_PREFIXES = ("/healthz", "/demo/")


def create_app(db_path, snap_dir, headless=True, password=None, username="admin", allow_private_urls=None):
    app = Flask(__name__)
    app.register_blueprint(demo_bp)
    snap_dir = Path(snap_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)
    if allow_private_urls is None:
        allow_private_urls = os.environ.get("ALLOW_PRIVATE_URLS") == "1"

    local = threading.local()

    def get_conn():
        # One SQLite connection per thread: request threads never share one with the scheduler.
        if not hasattr(local, "conn"):
            local.conn = db.connect(db_path)
        return local.conn

    def counts(conn):
        return {
            "n_pending": conn.execute("SELECT COUNT(*) FROM changes WHERE status='pending'").fetchone()[0],
            "n_open": conn.execute("SELECT COUNT(*) FROM handoffs WHERE status='open'").fetchone()[0],
        }

    @app.before_request
    def guard():
        if request.path == "/healthz" or request.path.startswith("/demo/"):
            return None
        if password:
            a = request.authorization
            ok = (a is not None and a.type == "basic"
                  and hmac.compare_digest((a.username or "").encode(), username.encode())
                  and hmac.compare_digest((a.password or "").encode(), password.encode()))
            if not ok:
                return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Compliance Monitor"'})
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.host:
                abort(403, "cross-site request blocked")
        return None

    @app.get("/healthz")
    def healthz():
        return "ok"

    # ------------------------------------------------------------------ results
    def build_results(conn):
        out = []
        for s in conn.execute("SELECT * FROM sources ORDER BY id").fetchall():
            snap = conn.execute("SELECT * FROM snapshots WHERE source_id=? ORDER BY id DESC LIMIT 1",
                                (s["id"],)).fetchone()
            lines = []
            if snap:
                try:
                    lines = (snap_dir / snap["text_path"]).read_text(encoding="utf-8").splitlines()
                except OSError:
                    lines = []
            out.append({
                "id": s["id"], "name": s["name"], "authority": s["authority"], "state": s["state"],
                "url": s["url"], "status": s["status"], "last_checked_at": s["last_checked_at"],
                "last_error": s["last_error"],
                "snapshot_taken_at": snap["taken_at"] if snap else None,
                "screenshot": snap["screenshot_path"] if snap else None,
                "lines": lines[:50],
                "preview": "\n".join(lines[:12]) + ("\n..." if len(lines) > 12 else "") if lines else "(nothing scraped yet)",
                "pending_changes": conn.execute(
                    "SELECT COUNT(*) FROM changes WHERE source_id=? AND status='pending'", (s["id"],)).fetchone()[0],
                "open_handoffs": [h["kind"] for h in conn.execute(
                    "SELECT kind FROM handoffs WHERE source_id=? AND status='open'", (s["id"],)).fetchall()],
            })
        return out

    @app.get("/")
    def results():
        conn = get_conn()
        return render_template_string(RESULTS, results=build_results(conn), **counts(conn))

    @app.get("/api/results")
    def api_results():
        conn = get_conn()
        data = build_results(conn)
        for r in data:
            r.pop("preview", None)
            r["screenshot_url"] = f"/files/{r.pop('screenshot')}" if r.get("screenshot") else None
        return jsonify({"sources": data, **counts(conn)})

    # ------------------------------------------------------------------ sources
    @app.get("/sources")
    def sources_page(error=None):
        conn = get_conn()
        rows = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
        return render_template_string(SOURCES, sources=rows, error=error, **counts(conn))

    @app.post("/sources")
    def add_source():
        f, conn = request.form, get_conn()
        url = f.get("url", "").strip()
        try:
            if not allow_private_urls:
                validate_public_url(url)
            db.add_source(conn, f["name"].strip(), url, f.get("authority", "").strip(),
                          f.get("state", "CENTRAL").strip() or "CENTRAL", f.get("selector", "").strip(),
                          f.get("check_every_minutes", 360), bool(f.get("respect_robots")))
        except ValueError as e:
            abort(400, str(e))
        return redirect(url_for("sources_page"))

    @app.post("/sources/<int:sid>/run")
    def run_source(sid):
        conn = get_conn()
        if conn.execute("SELECT 1 FROM sources WHERE id=?", (sid,)).fetchone() is None:
            abort(404)
        with CHECK_LOCK, Checker(conn, snap_dir, headless) as c:
            c.check(sid)
        back = urlparse(request.referrer).path if request.referrer else "/"
        if not back.startswith("/") or back.startswith("//"):   # same-site paths only, never an open redirect
            back = "/"
        return redirect(back)

    # ------------------------------------------------------------------- review
    @app.get("/review")
    def review():
        conn = get_conn()
        rows = conn.execute(
            "SELECT c.*, s.name AS source_name, s.state FROM changes c JOIN sources s ON s.id=c.source_id"
            " ORDER BY (c.status='pending') DESC, c.id DESC LIMIT 200").fetchall()
        return render_template_string(REVIEW, changes=rows, **counts(conn))

    @app.post("/changes/<int:cid>/decide")
    def decide(cid):
        conn = get_conn()
        decision = request.form.get("decision")
        if decision not in ("approved", "rejected"):
            abort(400, "decision must be approved or rejected")
        cur = conn.execute(
            "UPDATE changes SET status=?, reviewer=?, decided_at=? WHERE id=? AND status='pending'",
            (decision, request.form.get("reviewer", "").strip(), db.now(), cid))
        conn.commit()
        if cur.rowcount == 0:
            abort(409, "change not found or already decided")
        return redirect(url_for("review"))

    # ----------------------------------------------------------------- handoffs
    @app.get("/handoffs")
    def handoffs():
        conn = get_conn()
        rows = conn.execute(
            "SELECT h.*, s.name AS source_name, s.url FROM handoffs h JOIN sources s ON s.id=h.source_id"
            " ORDER BY (h.status='open') DESC, h.id DESC LIMIT 200").fetchall()
        return render_template_string(HANDOFFS, handoffs=rows, **counts(conn))

    @app.post("/handoffs/<int:hid>/resolve")
    def resolve(hid):
        conn = get_conn()
        cur = conn.execute(
            "UPDATE handoffs SET status='resolved', resolved_at=?, resolved_by=?, resolution=?"
            " WHERE id=? AND status='open'",
            (db.now(), request.form.get("resolved_by", "").strip(), request.form.get("resolution", "").strip(), hid))
        conn.commit()
        if cur.rowcount == 0:
            abort(409, "handoff not found or already resolved")
        return redirect(url_for("handoffs"))

    @app.get("/files/<path:name>")
    def files(name):
        return send_from_directory(snap_dir.resolve(), name)

    return app
