"""Seed demo sources into an empty database."""
from . import db


def seed_demo(conn, base_url):
    if conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]:
        return False
    db.add_source(conn, "Demo: GST circulars (changes every 10 min)", f"{base_url}/demo/circulars",
                  authority="DEMO", state="CENTRAL", selector="#list", check_every_minutes=5)
    db.add_source(conn, "Demo: portal behind a CAPTCHA", f"{base_url}/demo/gate-captcha",
                  authority="DEMO", state="CENTRAL", check_every_minutes=30)
    return True
