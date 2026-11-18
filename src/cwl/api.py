"""HTTP surface. Every route except /health requires a signed request."""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import security
from .app import App

MAX_BODY = 8192
REF = re.compile(r"^/referrals/([0-9a-f-]{36})$")
DONE = re.compile(r"^/queue/([0-9a-f-]{36})/complete$")


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # never log paths or bodies: they can carry identifiers
            pass

        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return None
            return self.rfile.read(n)

        def _handle(self, method):
            if self.path == "/health":
                return self._send(200, {"ok": True})
            body = self._body()
            if body is None:
                return self._send(413, {"error": "too large"})
            if not security.verify(app.secret, self.headers.get("X-Timestamp"),
                                   self.headers.get("X-Signature"), method, self.path, body):
                return self._send(401, {"error": "bad signature"})
            try:
                if method == "POST" and self.path == "/referrals":
                    rid, created = app.create_referral(json.loads(body or b"{}"), self.headers.get("Idempotency-Key"))
                    return self._send(202 if created else 200, {"id": rid})
                if method == "GET" and (m := REF.match(self.path)):
                    v = app.view(m.group(1))
                    return self._send(200, v) if v else self._send(404, {"error": "not found"})
                if method == "GET" and self.path == "/queue/next":
                    item = app.claim()
                    return self._send(200, item) if item else self._send(204, {})
                if method == "POST" and (m := DONE.match(self.path)):
                    app.complete(m.group(1), json.loads(body or b"{}").get("result"))
                    return self._send(200, {"ok": True})
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return H


def serve(app, port=8080, pump_every=0.2):
    stop = threading.Event()

    def loop():
        while not stop.wait(pump_every):
            app.pump()

    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    return srv, stop


if __name__ == "__main__":
    import os
    srv, _ = serve(App(os.environ.get("CWL_SECRET", "dev-only-secret").encode()))
    print(f"listening on http://127.0.0.1:{srv.server_port}")
    srv.serve_forever()
