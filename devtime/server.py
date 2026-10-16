"""Local dashboard server of one org's instance: http://127.0.0.1:8765 (stdlib only, binds to localhost by default).

Each org on the PC has its own server (its own port, paths.py / ports.py) serving only that org's transcripts. A
background thread rescans the transcripts every poll_s seconds; the page polls /api/version and refetches its data
when the version moves.

A caller that says who it is is checked: `org=<uuid>` must be this instance's org and `session=<cli session id>` must
be one of its sessions (its transcript's org tag or the desktop index; an unknown session is refused). The hooks
module's panel passes both, so a session of another org gets nothing. /api/version answers everyone (it carries the
instance's `home` and `org`, which is how a session finds its own org's server).
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

from . import paths, ports

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
            self.route()

        def refused(self, query):
            """The reason this caller may not see the instance's data, or None."""
            if not paths.ORG:
                return paths.NO_ORG_TEXT
            claimed = (query.get("org") or [""])[0]
            session = (query.get("session") or [""])[0]
            if claimed and claimed != paths.ORG:
                return "this devtime server is another organisation's"
            if session and not store.is_ours(session):
                return "this devtime server is another organisation's (or the session's organisation is not known yet)"
            return None

        def do_POST(self):
            url = urlparse(self.path)
            if url.path != "/api/reading":
                return self.send(404, b"not found", "text/plain")
            # Sessions on this machine only: loopback peer, a loopback Host (no DNS rebinding) and a JSON body (a
            # browser page cannot send one cross-origin without a preflight, which this server never answers).
            host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]")
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if self.client_address[0] not in ("127.0.0.1", "::1") or host not in ("127.0.0.1", "localhost", "::1"):
                return self.send(403, b'{"error":"local requests only"}', "application/json")
            if ctype != "application/json":
                return self.send(415, b'{"error":"send application/json"}', "application/json")
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size)) if 0 < size <= 4096 else None
            except ValueError:
                body = None
            outcome = store.record_reading(body)
            if outcome is None:
                return self.send(400, b'{"error":"not a reading"}', "application/json")
            self.json({"result": outcome})

        def route(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/api/version":
                data = store.data or {}
                return self.json({"version": store.version, "generated": data.get("generated"),
                                  "lastScan": store.last_scan, "home": paths.home_id(), "org": paths.ORG,
                                  "orgName": paths.org_name(paths.ORG) if paths.ORG else None})
            if url.path.startswith("/api/"):
                why = self.refused(query)
                if why:
                    return self.send(403, json.dumps({"error": why}).encode(), "application/json")
            if url.path == "/api/projects":
                data = store.slice("*") if store.data else {}
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
                out = overview.tick_summary(store.slice("*"), store.config.settings, store.alerts.list(), time.time())
                out["org"] = paths.ORG
                return self.json(out)
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
    if not paths.ORG:
        raise SystemExit("devtime: " + paths.NO_ORG_TEXT)
    s = store.config.settings
    host = host or s.get("host", "127.0.0.1")
    port = port or ports.choose(store.config.root)  # the configured port, or the next free one if it is not ours
    poll = float(s.get("poll_s", 10))
    store.log("org %s (%s): first scan of" % (paths.org_name(paths.ORG), paths.ORG), s.get("projects_dir"))
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
    ports.record(port)
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
