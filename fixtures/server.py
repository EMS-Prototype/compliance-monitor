"""Local fake 'government' site used to test the monitor without touching real portals."""
import threading

from flask import Flask, Response
from werkzeug.serving import make_server

STATE = {"version": 1}


def create_fixture_app():
    app = Flask(__name__)

    @app.get("/circulars")
    def circulars():
        items = ["<li>Notification 01/2026: GSTR-3B due date is 20th of the month</li>",
                 "<li>Notification 02/2026: Annual return due 31 December</li>"]
        if STATE["version"] >= 2:
            items.insert(0, '<li>Notification 09/2026: GSTR-3B for Nov 2026 extended to 25th '
                            '<a href="/files/n09.pdf">PDF</a></li>')
        return (f"<html><body><header>Dept of Revenue</header><div id='list'><ul>{''.join(items)}</ul></div>"
                f"<footer>Visitors: {STATE.get('visitors', 0)}</footer></body></html>")

    @app.get("/gate-captcha")
    def gate_captcha():
        return ("<html><body><h1>Portal</h1><div class='g-recaptcha' data-sitekey='x'></div>"
                "<p>Please verify you are human.</p></body></html>")

    @app.get("/gate-captcha-iframe")
    def gate_captcha_iframe():
        return "<html><body><h1>Portal</h1><iframe src='/fake-captcha-frame'></iframe></body></html>"

    @app.get("/fake-captcha-frame")
    def fake_frame():
        return "<html><body>frame</body></html>"

    @app.get("/gate-otp")
    def gate_otp():
        return "<html><body><h1>Login</h1><input name='otp_code'><p>Enter the OTP sent to your phone</p></body></html>"

    @app.get("/gate-login")
    def gate_login():
        return "<html><body><h1>Sign in</h1><input type='text' name='u'><input type='password' name='p'></body></html>"

    @app.get("/forbidden")
    def forbidden():
        return Response("<html><body>Access denied</body></html>", status=403)

    @app.get("/private/notices")
    def private():
        return "<html><body><div id='n'>Private notices that robots.txt disallows</div></body></html>"

    @app.get("/robots.txt")
    def robots():
        return Response("User-agent: *\nDisallow: /private/\n", mimetype="text/plain")

    @app.get("/empty")
    def empty():
        return "<html><body><div id='other'>content</div></body></html>"

    return app


class FixtureServer:
    def __init__(self, port=0):
        self._srv = make_server("127.0.0.1", port, create_fixture_app(), threaded=True)
        self.port = self._srv.server_port
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self._srv.shutdown()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"
