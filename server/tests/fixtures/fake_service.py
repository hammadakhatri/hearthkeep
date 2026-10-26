"""A stand-in for llama-server / ComfyUI in supervisor tests. Stdlib only.

Serves 200 on /health and /system_stats at 127.0.0.1:<port>. Unknown arguments (such as the
`--host` and `--api-key-file` that the supervisor always adds) are accepted and ignored.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HEALTH_PATHS = {"/health", "/system_stats"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--fail", action="store_true", help="exit 1 immediately")
    parser.add_argument("--slow", type=float, default=0.0, help="seconds before health is 200")
    parser.add_argument("--ignore-term", action="store_true", help="ignore SIGTERM/CTRL_BREAK")
    args, _unknown = parser.parse_known_args()

    print(f"fake service starting on port {args.port}", flush=True)
    if args.fail:
        print("fake service failing on purpose", file=sys.stderr, flush=True)
        sys.exit(1)
    if args.ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)

    ready_at = time.monotonic() + args.slow

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            if self.path not in HEALTH_PATHS:
                self.send_error(404)
                return
            ready = time.monotonic() >= ready_at
            body = b'{"status":"ok"}' if ready else b'{"status":"loading"}'
            self.send_response(200 if ready else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *log_args: object) -> None:  # noqa: A002
            print(format % log_args, file=sys.stderr, flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("fake service ready", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
