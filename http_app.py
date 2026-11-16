# http_app.py
# ==============================================================
# HTTP server เล็กๆ ด้วย stdlib (แทน Flask + flask-cors)
# เหตุผล: python-for-android ติดตั้งได้เฉพาะแพ็กเกจที่มี wheel สำเร็จรูป แต่ markupsafe
# (ที่ Flask -> Jinja2 ต้องใช้) ไม่มี wheel สำหรับ Android — จึงตัด Flask ออกทั้งหมด
# พฤติกรรมฝั่ง client เหมือนเดิม: JSON เดิม + CORS แบบ Access-Control-Allow-Origin: *
# ==============================================================

import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qsl


class Request:
    def __init__(self, method, path, args, headers, body):
        self.method = method
        self.path = path
        self.args = args
        self.headers = headers
        self.body = body

    @property
    def json(self):
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:
            return None


class App:
    """ใช้งานคล้าย Flask: @app.route(path, methods=[...]) ฟังก์ชันรับ req คืน (obj, status)"""

    def __init__(self):
        self.routes = {}

    def route(self, path, methods=("GET",)):
        def decorator(fn):
            for m in methods:
                self.routes[(m.upper(), path)] = fn
            return fn
        return decorator

    def dispatch(self, req):
        handler = self.routes.get((req.method, req.path))
        if handler is None:
            if any(p == req.path for _, p in self.routes):
                return {"error": "Method Not Allowed"}, 405
            return {"error": "Not Found"}, 404
        try:
            result = handler(req)
        except Exception as e:
            traceback.print_exc()
            return {"error": str(e)}, 500
        if isinstance(result, tuple):
            return result
        return result, 200

    def make_server(self, host, port):
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass  # ไม่ต้อง log ทุก request (เว็บ poll /ping บ่อย)

            def _cors_headers(self):
                self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
                self.send_header("Vary", "Origin")

            def _send_json(self, obj, status):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self._cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length > 0 else b""
                req = Request(method, parts.path, dict(parse_qsl(parts.query)), self.headers, body)
                obj, status = app.dispatch(req)
                self._send_json(obj, status)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_OPTIONS(self):
                # ✅ CORS preflight (+ Private Network Access ของ Chrome)
                self.send_response(204)
                self._cors_headers()
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                req_headers = self.headers.get("Access-Control-Request-Headers")
                self.send_header("Access-Control-Allow-Headers", req_headers or "Content-Type")
                if self.headers.get("Access-Control-Request-Private-Network") == "true":
                    self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer((host, port), Handler)
        server.daemon_threads = True
        return server

    def run(self, host="0.0.0.0", port=9333):
        server = self.make_server(host, port)
        print(f" * Running on http://{host}:{port}")
        server.serve_forever()
