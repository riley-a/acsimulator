"""Tiny stdlib HTTP server: static UI, JSON API, and Server-Sent Events.

    GET  /                          UI
    GET  /api/layout                points/doors/boards (static)
    GET  /api/state                 current snapshot
    GET  /api/events                SSE stream of snapshots
    POST /api/outputs/<id>          {"action": "on|off|toggle|pulse"}
    POST /api/doors/<n>/open        DPI relay -> door open
    POST /api/doors/<n>/close       DPI relay -> door closed
    POST /api/doors/<n>/rex/<1|2>   pulse REX
    POST /api/sim/inputs/<id>       {"active": bool}  (mock input boards only)
    POST /api/fai/trigger | clear   fire alarm on / off
    POST /api/fai/mode              {"mode": "no|nc"}  (only while not triggered)
    POST /api/fai/latch             {"open": bool}
    POST /api/all-off               release every output, clear the fire alarm
"""

import json
import logging
import mimetypes
import queue
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .controller import HardwareError, PointError

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def make_handler(ctl):
    doors = {str(d["id"]): d for d in ctl.doors}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "AC43Sim/1.0"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _json(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                return {}

        # --- GET ---------------------------------------------------------

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/layout":
                return self._json(200, ctl.layout())
            if path == "/api/state":
                return self._json(200, ctl.snapshot())
            if path == "/api/events":
                return self._sse()
            return self._static(path)

        def _static(self, path):
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            f = (STATIC / rel).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self._json(404, {"error": "not found"})
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = ctl.subscribe()
            try:
                while True:
                    try:
                        snap = q.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(snap)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                ctl.unsubscribe(q)
                self.close_connection = True

        # --- POST --------------------------------------------------------

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            body = self._body()
            try:
                if m := re.fullmatch(r"/api/outputs/([\w.\-]+)", path):
                    ctl.action(m[1], body.get("action", "toggle"))
                elif m := re.fullmatch(r"/api/doors/(\w+)/(open|close)", path):
                    d = doors.get(m[1])
                    if not d or not d["dpi"]:
                        raise PointError(f"door {m[1]} has no DPI output")
                    ctl.set_output(d["dpi"], m[2] == "open")
                elif m := re.fullmatch(r"/api/doors/(\w+)/rex/([12])", path):
                    d = doors.get(m[1])
                    oid = d and d[f"rex{m[2]}"]
                    if not oid:
                        raise PointError(f"door {m[1]} has no REX{m[2]} output")
                    ctl.pulse_output(oid)
                elif m := re.fullmatch(r"/api/sim/inputs/([\w.\-]+)", path):
                    ctl.sim_input(m[1], bool(body.get("active")))
                elif path == "/api/fai/trigger":
                    ctl.fai_set_triggered(True)
                elif path == "/api/fai/clear":
                    ctl.fai_set_triggered(False)
                elif path == "/api/fai/mode":
                    ctl.fai_set_mode(body.get("mode"))
                elif path == "/api/fai/latch":
                    ctl.fai_set_latch(bool(body.get("open")))
                elif path == "/api/all-off":
                    ctl.all_off()
                else:
                    return self._json(404, {"error": "not found"})
            except PointError as e:
                return self._json(400, {"error": str(e)})
            except HardwareError as e:
                return self._json(502, {"error": str(e)})
            return self._json(200, ctl.snapshot())

    return Handler


def serve(ctl, host, port):
    httpd = ThreadingHTTPServer((host, port), make_handler(ctl))
    httpd.daemon_threads = True
    log.info("AC43 simulator listening on http://%s:%s", host, port)
    return httpd
