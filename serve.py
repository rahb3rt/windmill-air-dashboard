#!/usr/bin/env python3
"""Serve the Windmill AC dashboard and keep the local history growing.

    python3 serve.py            # http://localhost:8787

One process does three jobs: samples every unit on an interval into SQLite,
keeps the hourly rollup current, and serves the page. Leave it running --
Windmill only retains 30 days, so anything longer only exists here.
"""
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import analysis
import backfill
import blynk
import config
import energy
import store

PORT = 8787
SAMPLE_INTERVAL = 30

_lock = threading.Lock()


def migrate_jsonl(con):
    """One-time import of the original samples.jsonl into SQLite."""
    legacy = config.ROOT / "samples.jsonl"
    if not legacy.exists() or store.get_meta(con, "jsonl_imported"):
        return 0
    n = 0
    for line in legacy.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        n += store.write_sample(con, row["ts"], row.get("units", {}))
    store.set_meta(con, "jsonl_imported", int(time.time()))
    return n


def sampler():
    con = store.connect()
    while True:
        try:
            ts = int(time.time())
            units = blynk.read_all()
            with _lock:
                store.write_sample(con, ts, units)
                store.rebuild_hours(con, [store.hour_of(ts)], config.POWER_PIN,
                                    energy.COMPRESSOR_W, energy.MAX_GAP)
        except Exception as exc:
            print(f"sample failed: {exc}")
        time.sleep(SAMPLE_INTERVAL)


def maintainer():
    """Refresh device metadata, and keep retrying backfill for units with gaps.

    A unit can be missing history simply because its export quota was exhausted
    when we asked. That clears on its own, so retry rather than leaving a hole
    (or, worse, inventing numbers to fill it).
    """
    while True:
        try:
            con = store.connect()
            for device in config.DEVICES:
                info = blynk.device_info(device)
                if info and info["activated_at"]:
                    store.save_device(con, device["name"], info["device_id"],
                                      info["activated_at"])

            t0, t1, _ = energy.resolve_range("30d")
            activated = store.activations(con)
            for device in config.DEVICES:
                name = device["name"]
                start = max(t0, activated.get(name, t0))
                expected = max(0.0, (min(t1, time.time()) - start) / 3600)
                if expected < 1:
                    continue
                if store.covered_hours(con, name, start, t1) / expected >= 0.5:
                    continue
                with _lock:
                    r = backfill.fetch_power(con, device, force=True)
                print(f"backfill retry {name}: {r['status']} "
                      f"{r.get('detail') or str(r['rows']) + ' hours'}")
            con.close()
        except Exception as exc:
            print(f"maintainer failed: {exc}")
        time.sleep(3600)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, ctype="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj).encode(), "application/json", code)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        one = lambda k, d=None: (q.get(k) or [d])[0]

        if url.path == "/api/summary":
            con = store.connect()
            try:
                if one("from") and one("to"):
                    t0, t1 = float(one("from")), float(one("to"))
                    label = one("label", "Custom range")
                else:
                    t0, t1, label = energy.resolve_range(one("range", "today"))
                self._json(energy.snapshot(con, t0, t1, label))
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        elif url.path == "/api/backfill":
            con = store.connect()
            try:
                with _lock:
                    results = backfill.run(con, force=one("force") == "1")
                self._json({"results": results})
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            finally:
                con.close()

        elif url.path == "/api/comparison":
            con = store.connect()
            try:
                self._json(analysis.comparison(
                    con, change=one("change"), new_units=(
                        one("new").split(",") if one("new") else None)))
            except Exception as exc:
                self._json({"available": False, "reason": str(exc)})
            finally:
                con.close()

        elif url.path == "/api/rate":
            con = store.connect()
            try:
                if one("set"):
                    store.set_meta(con, "rate_per_kwh", float(one("set")))
                self._json({"rate": energy.rate(con)})
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            finally:
                con.close()

        else:
            self._send((config.ROOT / "index.html").read_bytes(),
                       "text/html; charset=utf-8")


if __name__ == "__main__":
    con = store.connect()
    if store.get_meta(con, "rate_per_kwh") is None:
        store.set_meta(con, "rate_per_kwh", config.RATE_PER_KWH)
    imported = migrate_jsonl(con)
    if imported:
        print(f"imported {imported} readings from samples.jsonl")
    lo, hi = store.bounds(con)
    if lo:
        print(f"history: {time.strftime('%Y-%m-%d', time.localtime(lo))} -> "
              f"{time.strftime('%Y-%m-%d', time.localtime(hi))}")
    con.close()

    threading.Thread(target=sampler, daemon=True).start()
    print(f"{len(config.DEVICES)} units | sampling every {SAMPLE_INTERVAL}s")
    print(f"dashboard -> http://localhost:{PORT}")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
