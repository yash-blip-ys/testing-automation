"""Fixture application server for the Part 6 benchmark.

WHY A SERVER AND NOT `file://`
------------------------------
The benchmark needs three things a `file://` page cannot provide:

  1. An INDEPENDENT ORACLE (6.7.1). The oracle must check the application's
     actual state, not re-read the DOM through the same extraction the agent
     used. A server can answer `/__state` from its own authoritative state,
     which is genuinely a different source of truth.
  2. RESETTABLE STATE (6.7.2). Five repetitions each start from a clean initial
     state; `POST /__reset` makes that exact and cheap.
  3. SERVER-SIDE EFFECTS. "Did this order actually get placed?" is a question
     about the application, not about what a page displays afterwards.

The agent only ever sees the HTML over HTTP, exactly as it would see a real
site. It has no access to `/__state` unless it guesses the endpoint, and the
benchmark does not ask it to.

Stdlib only: `http.server`, no framework, no dependency added to the project.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FixtureApp:
    """Base class for a benchmark fixture application.

    A fixture owns:
      * `pages`   — path -> HTML (all control labels, ids and copy live here,
                    never in engine code);
      * `state`   — the authoritative application state the oracle reads;
      * `handle`  — a pure-ish request handler that may mutate `state`.

    Subclasses implement `handle()`. The base class provides routing, state
    reset, and the introspection endpoints.
    """

    name = "fixture"

    def __init__(self):
        self.state = {}
        self.reset()

    # -- lifecycle ----------------------------------------------------------

    def reset(self):
        """Return to the documented INITIAL state. Must be deterministic."""
        raise NotImplementedError

    # -- request handling ---------------------------------------------------

    def html(self, path, query=None, body=None):
        """Return the HTML for a GET, or None if this fixture has no such page."""
        raise NotImplementedError

    def handle(self, method, path, query, body):
        """Handle a request.

        Returns `(html, status_code)`. Implementations may mutate `self.state`;
        every mutation is what the oracle later inspects, so anything an
        oracle must be able to detect has to be recorded here rather than
        inferred from rendered markup.
        """
        raise NotImplementedError

    # -- helpers for fixtures ----------------------------------------------

    @staticmethod
    def field(body, name, default=""):
        """Read one form field from a urlencoded body."""
        if not body:
            return default
        values = parse_qs(body, keep_blank_values=True)
        found = values.get(name)
        return found[0] if found else default

    @staticmethod
    def one(query, name, default=""):
        if not query:
            return default
        found = query.get(name)
        return found[0] if found else default


class _Handler(BaseHTTPRequestHandler):
    app = None

    def log_message(self, *args):
        """Silence the default stderr access log during benchmark runs."""

    def _send(self, body, status=200, content_type="text/html; charset=utf-8"):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)

        if path == "/__state":
            self._send(json.dumps(self.app.state, default=str),
                       content_type="application/json")
            return
        if path == "/__reset":
            self.app.reset()
            self._send(json.dumps({"reset": True}))
            return
        try:
            body, status = self.app.handle("GET", path, query, None)
        except Exception as exc:
            body, status = f"<h1>Fixture error</h1><p>{exc}</p>", 500
        self._send(body or "<h1>Not found</h1>", status)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            out, status = self.app.handle("POST", path, query, body)
        except Exception as exc:
            out, status = f"<h1>Fixture error</h1><p>{exc}</p>", 500
        self._send(out or "<h1>Not found</h1>", status)


class FixtureServer:
    """Runs one `FixtureApp` on a background thread on an ephemeral port."""

    def __init__(self, app):
        self.app = app
        handler = type("_BoundHandler", (_Handler,), {"app": app})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._server.server_address[1]
        self._thread = None

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- oracle access ------------------------------------------------------

    def snapshot(self):
        """Read the authoritative state DIRECTLY from the application.

        This is the oracle's channel. It deliberately shares no code with the
        agent's observation or evidence extraction: it asks the application what
        happened, rather than looking at what the application displays.
        """
        import urllib.request
        with urllib.request.urlopen(
                f"{self.base_url}/__state", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def reset(self):
        self.app.reset()
        return self.snapshot()