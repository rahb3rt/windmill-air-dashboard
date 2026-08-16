#!/usr/bin/env python3
"""Serve the Windmill AC dashboard and log power samples in the background.

    python3 serve.py            # http://localhost:8787

One process does both jobs: a daemon thread appends a sample for every unit
to samples.jsonl on a fixed interval, and the HTTP server renders whatever
has accumulated. Leave it running and the history deepens.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import blynk
import config
import energy

PORT = 8787
SAMPLE_INTERVAL = 30


def sampler():
    while True:
        try:
            row = {"ts": int(time.time()), "units": blynk.read_all()}
            with config.SAMPLES.open("a") as f:
                f.write(json.dumps(row) + "\n")
        except Exception as exc:            # never let the logger die
            print(f"sample failed: {exc}")
        time.sleep(SAMPLE_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/summary"):
            try:
                body = json.dumps(energy.snapshot()).encode()
            except Exception as exc:
                body = json.dumps({"error": str(exc)}).encode()
            self._send(body, "application/json")
        else:
            self._send((config.ROOT / "index.html").read_bytes(),
                       "text/html; charset=utf-8")


if __name__ == "__main__":
    threading.Thread(target=sampler, daemon=True).start()
    print(f"{len(config.DEVICES)} units | sampling every {SAMPLE_INTERVAL}s")
    print(f"dashboard -> http://localhost:{PORT}")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
