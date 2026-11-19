import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _WakeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        pass


def start_wake_server(port: int = 8080) -> None:
    port = int(os.getenv("WAKE_PORT", str(port)))
    server = ThreadingHTTPServer(("0.0.0.0", port), _WakeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
