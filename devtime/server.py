"""Local dashboard server: http://127.0.0.1:8765 (stdlib only, binds to localhost by default).

A background thread rescans the transcripts every poll_s seconds; the page polls /api/version and refetches its data
when the version moves.
"""

import gzip
import json
import mimetypes
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def make_handler(store):
    encoded = {}  # (version, project) -> gzipped JSON
    lock = threading.Lock()
    indexes = {}  # (version, project) -> overview.Index
    overviews = {}  # (version, project, range) -> (time, result)

    def overview_for(project, range_key):
        from . import overview
        # Both slices and every index must come from one build: a rebuild between reads would pair one build's
        # index with the next one's data.
        for _ in range(5):
            data, full = store.slice(project), store.slice("*")
            if data.get("version") == full.get("version"):
                break
        version = full.get("version")
        key = (version, project, range_key)
        with lock:
            hit = overviews.get(key)
        if hit and time.time() - hit[0] < 15:
            return hit[1]

        def index(p):
            with lock:
                found = indexes.get((version, p))
            if found is None:
                found = overview.Index(full if p == "*" else data)
                with lock:
                    for old in [k for k in indexes if k[0] != version]:
                        del indexes[old]
                    indexes[(version, p)] = found
            return found
        now = time.time()
        week = overview.period(full, index("*"), now - 7 * 86400, now)["cost"]
        plan = overview.limits(full, index("*"), store.config.settings, now)
        result = overview.overview(data, index(project), range_key, store.config.settings, week, now, plan)
        with lock:
            for old in [k for k in overviews if k[0] != version]:
                del overviews[old]
            overviews[key] = (time.time(), result)
        return result

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def send(self, status, body, ctype, gz=False, cache=False):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(body)

        def json(self, obj):
            body = json.dumps(obj, separators=(",", ":")).encode()
            if "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 2048:
                self.send(200, gzip.compress(body, 5), "application/json", gz=True)
            else:
                self.send(200, body, "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/api/version":
                data = store.data or {}
                return self.json({"version": store.version, "generated": data.get("generated"),
                                  "lastScan": store.last_scan})
            if url.path == "/api/projects":
                data = store.data or {}
                return self.json({"version": store.version, "projects": data.get("projects", []),
                                  "defaultProject": data.get("defaultProject")})
            if url.path == "/api/overview":
                return self.json(overview_for((query.get("project") or ["*"])[0], (query.get("range") or ["24h"])[0]))
            if url.path == "/api/alerts":
                return self.json({"alerts": store.alerts.list()})
            if url.path == "/api/watch":
                from . import overview
                data = store.slice("*")
                hours = float((query.get("hours") or ["3"])[0])
                return self.json(overview.watch(data, overview.Index(data), store.alerts.list(), time.time(), hours))
            if url.path == "/api/tick":
                from . import overview
                return self.json(overview.tick_summary(store.slice("*"), store.config.settings,
                                                       store.alerts.list(), time.time()))
            if url.path == "/api/findings":
                from . import findings
                runs = list(store.harvester.runs.values())
                since = float((query.get("since") or ["0"])[0]) or None
                return self.json(findings.findings(runs, (query.get("project") or [None])[0],
                                                   (query.get("script") or [None])[0], since, 50))
            if url.path == "/api/data":
                project = (query.get("project") or ["*"])[0]
                key = (store.version, project)
                with lock:
                    body = encoded.get(key)
                if body is None:
                    data = store.slice(project)
                    body = gzip.compress(json.dumps(data, separators=(",", ":")).encode(), 5)
                    with lock:
                        for old in [k for k in encoded if k[0] != store.version]:
                            del encoded[old]
                        encoded[key] = body
                if "gzip" in self.headers.get("Accept-Encoding", ""):
                    return self.send(200, body, "application/json", gz=True)
                return self.send(200, gzip.decompress(body), "application/json")
            path = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC, path))
            if not full.startswith(STATIC) or not os.path.isfile(full):
                return self.send(404, b"not found", "text/plain")
            with open(full, "rb") as handle:
                body = handle.read()
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self.send(200, body, ctype)

    return Handler


def serve(store, host=None, port=None, open_browser=True):
    s = store.config.settings
    host = host or s.get("host", "127.0.0.1")
    port = port or int(s.get("port", 8765))
    poll = float(s.get("poll_s", 10))
    store.log("first scan of", s.get("projects_dir"))
    store.refresh()

    def loop():
        while True:
            time.sleep(poll)
            try:
                store.refresh()
            except Exception as error:  # keep serving the last good dataset
                store.log("refresh failed:", repr(error))

    threading.Thread(target=loop, daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(store))
    url = f"http://{host}:{port}/"
    store.log("serving", url)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.cache.save()
        store._save_blocks()
