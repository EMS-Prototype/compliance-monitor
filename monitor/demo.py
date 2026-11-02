"""Built-in demo pages so a fresh deployment has something to scrape with no outside network.

/demo/circulars publishes a "new notification" every DEMO_BUCKET_SECONDS (default 10 minutes),
so the scraper keeps detecting real changes. /demo/gate-captcha always shows a CAPTCHA wall,
so the handoff flow is visible. These pages are public and harmless (no auth needed).
"""
import os
import time

from flask import Blueprint, Response

bp = Blueprint("demo", __name__, url_prefix="/demo")


def _bucket():
    return int(time.time() // int(os.environ.get("DEMO_BUCKET_SECONDS", "600")))


@bp.get("/circulars")
def circulars():
    n = _bucket()
    items = "".join(
        f"<li>Notification {(n - i) % 1000:03d}/2026: GSTR-3B advisory, issue {n - i} (demo data)"
        f' <a href="/demo/files/n{n - i}.pdf">PDF</a></li>'
        for i in range(3))
    return (f"<html><body><header>Demo Dept of Revenue</header>"
            f"<div id='list'><ul>{items}</ul></div>"
            f"<footer>Rendered at {time.strftime('%H:%M:%S')}</footer></body></html>")


@bp.get("/gate-captcha")
def gate_captcha():
    return ("<html><body><h1>Demo GST portal</h1><div class='g-recaptcha' data-sitekey='demo'></div>"
            "<p>Please verify you are human.</p></body></html>")


@bp.get("/robots.txt")
def robots():  # served at /demo/robots.txt; real robots.txt lookups hit /robots.txt (404 = allowed)
    return Response("User-agent: *\nAllow: /\n", mimetype="text/plain")
