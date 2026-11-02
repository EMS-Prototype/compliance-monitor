"""End-to-end tests: real Playwright/Chromium against a local fixture site."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "fixtures"))

from fixtures.server import FixtureServer, STATE  # noqa: E402
from monitor import db  # noqa: E402
from monitor.worker import Checker, is_due  # noqa: E402
from monitor.admin import create_app  # noqa: E402


@pytest.fixture(scope="module")
def site():
    s = FixtureServer().start()
    yield s
    s.stop()


@pytest.fixture()
def env(tmp_path, site):
    STATE["version"] = 1
    conn = db.connect(str(tmp_path / "t.db"))
    snaps = tmp_path / "snaps"
    with Checker(conn, snaps) as checker:
        yield conn, checker, site, snaps, tmp_path


def _src(conn, site, path, **kw):
    return db.add_source(conn, kw.pop("name", path), site.base + path, **kw)


def test_baseline_unchanged_then_change_goes_to_review_queue(env):
    conn, checker, site, snaps, _ = env
    sid = _src(conn, site, "/circulars", selector="#list", authority="CBIC", state="CENTRAL")

    r1 = checker.check(sid)
    assert r1["result"] == "baseline"

    r2 = checker.check(sid)
    assert r2["result"] == "unchanged"
    assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 0

    STATE["version"] = 2
    r3 = checker.check(sid)
    assert r3["result"] == "changed"
    ch = conn.execute("SELECT * FROM changes WHERE id=?", (r3["change_id"],)).fetchone()
    assert ch["status"] == "pending"
    assert "extended to 25th" in ch["diff"]
    assert "n09.pdf" in ch["diff"]          # new PDF link surfaced as part of the change
    assert "1 line(s) added" in ch["summary"] or "line(s) added" in ch["summary"]
    assert conn.execute("SELECT status FROM sources WHERE id=?", (sid,)).fetchone()[0] == "ok"
    assert len(list(snaps.glob("*.png"))) == 2 and len(list(snaps.glob("*.txt"))) == 2


def test_selector_ignores_noise_outside_watched_region(env):
    conn, checker, site, *_ = env
    sid = _src(conn, site, "/circulars", selector="#list")
    assert checker.check(sid)["result"] == "baseline"
    STATE["visitors"] = 999                     # footer changes, watched region does not
    assert checker.check(sid)["result"] == "unchanged"
    STATE.pop("visitors", None)


@pytest.mark.parametrize("path,kind", [
    ("/gate-captcha", "captcha"),
    ("/gate-captcha-iframe", "captcha"),
    ("/gate-otp", "otp"),
    ("/gate-login", "login"),
    ("/forbidden", "blocked_http"),
])
def test_gated_pages_pause_and_hand_over_to_a_human(env, path, kind):
    conn, checker, site, snaps, _ = env
    sid = _src(conn, site, path)
    r = checker.check(sid)
    assert r["result"] == "blocked" and r["kind"] == kind
    h = conn.execute("SELECT * FROM handoffs WHERE id=?", (r["handoff_id"],)).fetchone()
    assert h["status"] == "open" and h["kind"] == kind
    assert h["screenshot_path"] and (snaps / h["screenshot_path"]).exists()
    assert conn.execute("SELECT status FROM sources WHERE id=?", (sid,)).fetchone()[0] == "blocked"
    # nothing was captured as content, and no change was created
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 0


def test_repeated_blocked_runs_do_not_spam_handoffs(env):
    conn, checker, site, *_ = env
    sid = _src(conn, site, "/gate-captcha")
    a, b = checker.check(sid), checker.check(sid)
    assert a["handoff_id"] == b["handoff_id"]
    assert conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 1


def test_robots_txt_is_respected_and_can_be_overridden_per_source(env):
    conn, checker, site, *_ = env
    sid = _src(conn, site, "/private/notices", selector="#n")
    assert checker.check(sid)["result"] == "skipped_robots"
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
    sid2 = _src(conn, site, "/private/notices", selector="#n", respect_robots=False)
    assert checker.check(sid2)["result"] == "baseline"


def test_broken_selector_is_reported_as_error_not_silently_ignored(env):
    conn, checker, site, *_ = env
    sid = _src(conn, site, "/empty", selector="#does-not-exist")
    r = checker.check(sid)
    assert r["result"] == "error"
    row = conn.execute("SELECT status,last_error FROM sources WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "error" and "matched nothing" in row["last_error"]


def test_unreachable_site_is_an_error_and_does_not_crash_the_run(env):
    conn, checker, site, *_ = env
    bad = db.add_source(conn, "dead", "http://127.0.0.1:9/nothing")
    good = _src(conn, site, "/circulars", selector="#list")
    assert checker.check(bad)["result"] == "error"
    assert checker.check(good)["result"] == "baseline"


def test_due_logic(env):
    conn, checker, site, *_ = env
    sid = _src(conn, site, "/circulars", selector="#list", check_every_minutes=60)
    assert is_due(conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone())
    checker.check(sid)
    assert not is_due(conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone())


def test_add_source_rejects_non_http_urls(env):
    conn, *_ = env
    with pytest.raises(ValueError):
        db.add_source(conn, "bad", "file:///etc/passwd")


def test_admin_ui_review_and_handoff_flow(tmp_path, site):
    # Deliberately does not use the `env` fixture: the admin routes start their own
    # Playwright browser, and the sync API cannot nest inside an already-open one.
    STATE["version"] = 1
    tmp, snaps = tmp_path, tmp_path / "snaps"
    app = create_app(str(tmp / "t.db"), snaps, allow_private_urls=True)  # fixture site is on 127.0.0.1
    client = app.test_client()

    # add a source through the UI and run it twice around a content change
    resp = client.post("/sources", data={"name": "CBIC circulars", "url": site.base + "/circulars",
                                         "selector": "#list", "authority": "CBIC", "state": "CENTRAL",
                                         "check_every_minutes": "60", "respect_robots": "on"})
    assert resp.status_code == 302
    assert client.post("/sources/1/run").status_code == 302
    api = client.get("/api/results").get_json()
    assert api["sources"][0]["status"] == "ok" and any("Notification 01/2026" in l for l in api["sources"][0]["lines"])
    page = client.get("/").get_data(as_text=True)
    assert "Latest scraped content" in page and "Notification 01/2026" in page
    STATE["version"] = 2
    assert client.post("/sources/1/run").status_code == 302

    page = client.get("/review").get_data(as_text=True)
    assert "extended to 25th" in page and "Approve" in page

    assert client.post("/changes/1/decide", data={"decision": "approved", "reviewer": "CA Sharma"}).status_code == 302
    row = db.connect(str(tmp / "t.db")).execute("SELECT status,reviewer FROM changes WHERE id=1").fetchone()
    assert (row["status"], row["reviewer"]) == ("approved", "CA Sharma")
    assert client.post("/changes/1/decide", data={"decision": "rejected", "reviewer": "x"}).status_code == 409
    assert client.post("/changes/1/decide", data={"decision": "maybe"}).status_code == 400

    # a gated source shows up as a handoff and can be resolved
    client.post("/sources", data={"name": "GST portal", "url": site.base + "/gate-captcha"})
    client.post("/sources/2/run")
    hp = client.get("/handoffs").get_data(as_text=True)
    assert "captcha" in hp and "Mark resolved" in hp
    assert client.post("/handoffs/1/resolve", data={"resolved_by": "Lakshit", "resolution": "Switched to RSS"}).status_code == 302
    assert client.post("/handoffs/1/resolve", data={"resolved_by": "x"}).status_code == 409

    # screenshots are served, path traversal is not
    shot = db.connect(str(tmp / "t.db")).execute("SELECT screenshot_path FROM handoffs WHERE id=1").fetchone()[0]
    assert client.get(f"/files/{shot}").status_code == 200
    assert client.get("/files/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)


# ---------------------------------------------------------------- public-deploy safeguards
def test_login_is_required_but_health_and_demo_stay_public(tmp_path):
    import base64
    app = create_app(str(tmp_path / "a.db"), tmp_path / "s", password="s3cret-pass")
    c = app.test_client()
    good = {"Authorization": "Basic " + base64.b64encode(b"admin:s3cret-pass").decode()}
    bad = {"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()}
    for path in ("/", "/sources", "/review", "/handoffs", "/api/results", "/files/x.png"):
        r = c.get(path)
        assert r.status_code == 401 and "Basic" in r.headers["WWW-Authenticate"], path
    assert c.get("/", headers=bad).status_code == 401
    assert c.post("/sources", data={"name": "x", "url": "http://93.184.216.34/"}).status_code == 401
    assert c.get("/", headers=good).status_code == 200
    assert c.get("/api/results", headers=good).status_code == 200
    assert c.get("/healthz").status_code == 200
    assert "Demo Dept of Revenue" in c.get("/demo/circulars").get_data(as_text=True)


def test_cross_site_post_is_blocked(tmp_path):
    app = create_app(str(tmp_path / "a.db"), tmp_path / "s")
    c = app.test_client()
    r = c.post("/sources", data={"name": "x", "url": "http://93.184.216.34/"},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.post("/sources", data={"name": "x", "url": "http://93.184.216.34/"},
               headers={"Origin": "http://localhost"})   # same host as the test client
    assert r.status_code == 302


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/", "http://localhost/", "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/", "http://192.168.1.1/", "http://[::1]/", "ftp://93.184.216.34/", "file:///etc/passwd",
])
def test_private_and_internal_urls_are_rejected_when_adding_a_source(tmp_path, url):
    app = create_app(str(tmp_path / "a.db"), tmp_path / "s")     # default: private URLs NOT allowed
    c = app.test_client()
    assert c.post("/sources", data={"name": "x", "url": url}).status_code == 400
    assert db.connect(str(tmp_path / "a.db")).execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0


def test_public_ip_literal_is_accepted(tmp_path):
    app = create_app(str(tmp_path / "a.db"), tmp_path / "s")
    assert app.test_client().post("/sources", data={"name": "ok", "url": "http://93.184.216.34/"}).status_code == 302


def test_demo_seed_and_demo_pages_scrape_end_to_end(tmp_path, site, monkeypatch):
    """The demo pages served by the real app are scrapable, and the CAPTCHA demo hands over."""
    import threading
    from werkzeug.serving import make_server
    from monitor.seed import seed_demo
    app = create_app(str(tmp_path / "d.db"), tmp_path / "s")
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        conn = db.connect(str(tmp_path / "d.db"))
        assert seed_demo(conn, f"http://127.0.0.1:{srv.server_port}") is True
        assert seed_demo(conn, "http://x") is False            # only seeds an empty database
        with Checker(conn, tmp_path / "s") as ch:
            assert ch.check(1)["result"] == "baseline"
            r = ch.check(2)
            assert r["result"] == "blocked" and r["kind"] == "captcha"
    finally:
        srv.shutdown()
