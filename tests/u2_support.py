"""Shared support for the U2 action-selection tests.

Two things live here, and nothing else:

  * `SiteFixture` — a local HTTP site that RECORDS what the browser actually
    did to it. Tests assert against that record, never against the agent's own
    account of what it did. A page reports its own control state to the server
    on change, so the server holds an independent observation of the DOM rather
    than a copy of the agent's reasoning.

  * `run_agent` — drives the real `run_pathfinder_agent` against a config
    written to a temporary directory. Nothing here changes how the agent
    behaves; it only stands up a site and reads back the evidence.

No fixture is exempted from a check, and no check is written to pass. Sites are
built from the markup a test supplies, so no page in the suite is special.
"""

import asyncio
import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import automation_engine as ae


class SiteFixture:
    """A local site that reports its own state to the server.

    `pages` maps a path to HTML. `reporter` is the JavaScript body a page uses
    to announce a change; it is called with the page's declared payload. The
    server appends every report to `self.reports`, which is the oracle.
    """

    def __init__(self, pages, reporter=None, on_submit=None):
        self.pages = dict(pages)
        self.reporter = reporter
        self.on_submit = on_submit
        self.requests = []
        self.reports = []
        self.submissions = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, code, body, ctype="text/html; charset=utf-8"):
                payload = body.encode("utf-8") if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                fixture.requests.append(("GET", path))
                if path == "/__state":
                    return self._send(200, json.dumps({
                        "reports": fixture.reports,
                        "submissions": fixture.submissions,
                    }), "application/json")
                body = fixture.pages.get(path)
                if body is None:
                    return self._send(404, "<html><body>no such page</body></html>")
                if fixture.reporter:
                    body = body.replace("<!--REPORTER-->",
                                        f"<script>{fixture.reporter}</script>")
                return self._send(200, body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", "replace")
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = {"_raw": raw}
                fixture.requests.append(("POST", self.path))
                if self.path == "/__report":
                    fixture.reports.append(payload)
                    return self._send(200, "ok", "text/plain")
                fixture.submissions.append(payload)
                body = b"<html><body>received</body></html>"
                if self.on_submit is not None:
                    body = self.on_submit(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False

    @property
    def base(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def reported(self, key=None):
        """Every reported value for `key`, in order."""
        return [r.get(key) for r in self.reports if key in r]

    def last(self, key=None):
        values = self.reported(key)
        return values[-1] if values else None


# A page-side reporter. It reads the controls the TEST declared in
# <script id="declare" type="application/json"> and posts their current state
# whenever one changes. The engine never sees or influences this.
REPORTER_JS = """
(function () {
  var decl = document.getElementById('declare');
  if (!decl) return;
  var spec = JSON.parse(decl.textContent);
  var clicks = {};
  function snapshot() {
    var out = {};
    for (var key in spec.controls) {
      var el = document.querySelector(spec.controls[key]);
      if (!el) { out[key] = null; continue; }
      if (el.tagName === 'SELECT') {
        out[key] = el.value ? el.options[el.selectedIndex].text : '';
      } else if (el.type === 'checkbox' || el.type === 'radio') {
        out[key] = el.checked ? 'checked' : 'unchecked';
      } else {
        out[key] = el.value || '';
      }
    }
    out.page = location.pathname;
    out.clicks = clicks;
    return out;
  }
  window.__u2state = snapshot;
  function announce(tag) {
    var payload = snapshot();
    payload.tag = tag || '';
    try { fetch('/__report', {method: 'POST', body: JSON.stringify(payload)}); }
    catch (e) {}
  }
  window.__u2announce = announce;
  document.addEventListener('click', function (ev) {
    var el = ev.target;
    while (el && el !== document) {
      var key = el.getAttribute && el.getAttribute('data-u2-id');
      if (key) { clicks[key] = (clicks[key] || 0) + 1; break; }
      el = el.parentElement;
    }
    announce('click');
  }, true);
  document.addEventListener('change', function () { announce('change'); });
  document.addEventListener('input', function () { announce('input'); });
  announce('load');
})();
"""


def declare(controls):
    """The JSON the reporter reads, naming controls for the test.

    `controls` maps an oracle key to a CSS selector for the test's own fixture.
    Pass a control id string to have the reporter also stamp
    data-u2-id="<key>" onto it, which is how click attribution works.
    """
    return ('<script id="declare" type="application/json">'
            + json.dumps({"controls": controls})
            + "</script>")


def stamp(ids):
    """Inline script adding data-u2-id markers so clicks can be attributed."""
    pairs = ", ".join(f'"{el}": "{key}"' for el, key in ids.items())
    return ("<script>(function(){var m={" + pairs + "};"
            "for (var s in m){var e=document.querySelector(s);"
            "if (e) e.setAttribute('data-u2-id', m[s]);}})();</script>")


class RunResult:
    """Everything a test may assert on, and nothing that is not evidence."""

    def __init__(self, log, requests, reports, submissions, reports_dir,
                 navigator_calls):
        self.log = log
        self.requests = requests
        self.reports = reports
        self.submissions = submissions
        self.reports_dir = reports_dir
        self.navigator_calls = navigator_calls

    def reported(self, key=None):
        """Every value the PAGE reported for `key`, in order.

        This is the independent oracle: it is produced by the page observing
        its own controls and posting the result to the server, not by the
        agent describing itself.
        """
        return [r.get(key) for r in self.reports if key in r]

    def last(self, key=None):
        values = self.reported(key)
        return values[-1] if values else None

    def report_text(self):
        names = sorted(n for n in os.listdir(self.reports_dir)
                       if n.startswith("scan_report_"))
        if not names:
            return ""
        with open(os.path.join(self.reports_dir, names[-1]),
                  encoding="utf-8") as handle:
            return handle.read()

    def chose(self, step=0):
        if step < len(self.navigator_calls):
            return self.navigator_calls[step].get("choice")
        return None

    def choices(self):
        return [c.get("choice") for c in self.navigator_calls]

    def prompted_goals(self):
        return [c.get("goal") or "" for c in self.navigator_calls]

    def line(self, needle):
        for row in self.log.splitlines():
            if needle in row:
                return row
        return None

    def lines(self, needle):
        return [row for row in self.log.splitlines() if needle in row]


async def run_agent(goal, pages, *, start_path="/", test_goal=None,
                    evidence=None, max_steps=5, safety=None, reporter=None,
                    on_submit=None, use_real_model=True, workdir=None,
                    extra_config=None, navigator=None,
                    goal_replacement=None, replace_after_call=1):
    """Run the real agent against a local site and return the evidence.

    `goal_replacement` is (instruction, kind). When given, the replacement is
    appended to the update file after `replace_after_call` navigator calls —
    which is the real U1 mechanism, the same external channel a user process
    would write to, not a private back door into the agent.
    """
    tmp = tempfile.mkdtemp(prefix="u2run_")
    reports_dir = workdir or os.path.join(tmp, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    updates_path = os.path.join(tmp, "goal_updates.jsonl")
    open(updates_path, "w", encoding="utf-8").close()

    config = {
        "site_name": "u2-fixture",
        "portal_url": "PLACEHOLDER" + start_path,
        "ai_context": goal,
        "test_goal": dict(test_goal or {"objective": goal,
                                        "evidence": evidence or {}}),
        "goal_updates_file": updates_path,
        "target_elements_query": (
            "button, a, input, select, textarea, [role='button'], "
            "[role='link'], [role='checkbox']"),
    }
    config["test_goal"]["max_steps"] = max_steps
    if safety is not None:
        config["safety"] = safety
    if extra_config:
        config.update(extra_config)

    calls = []
    real_navigator = ae.ask_ai_navigator

    def spy_navigator(available_elements, goal_text, recent_actions, **kw):
        decision = real_navigator(available_elements, goal_text,
                                  recent_actions, **kw)
        calls.append({
            "step": kw.get("step_index"),
            "url": kw.get("page_url"),
            "options": list(available_elements or []),
            "choice": (decision or {}).get("best_choice"),
            "inputs": (decision or {}).get("action_inputs"),
            "goal": goal_text,
        })
        if goal_replacement and len(calls) == replace_after_call:
            _write_update(updates_path, goal_replacement[0],
                          goal_replacement[1])
        return decision

    with SiteFixture(pages, reporter=reporter or REPORTER_JS,
                     on_submit=on_submit) as site:
        config["portal_url"] = site.base + start_path
        cfg_path = os.path.join(tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)

        patches = [_patch(ae, "ask_ai_navigator",
                          spy_navigator if use_real_model else navigator),
                   _patch(ae, "ask_ai_planner", ae.ask_ai_planner)]
        buffer = io.StringIO()
        cwd = os.getcwd()
        try:
            os.chdir(reports_dir)
            for patch in patches:
                patch.__enter__()
            try:
                with contextlib.redirect_stdout(buffer):
                    await ae.run_pathfinder_agent(cfg_path)
            finally:
                for patch in reversed(patches):
                    patch.__exit__(None, None, None)
        finally:
            os.chdir(cwd)
        result = RunResult(buffer.getvalue(), list(site.requests),
                           list(site.reports), list(site.submissions),
                           reports_dir, calls)
    shutil.rmtree(tmp, ignore_errors=True)
    return result


def _write_update(path, instruction, kind):
    """Append one update exactly as a separate user process would."""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"instruction": instruction, "kind": kind,
                                 "client_seq": 1}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class _patch:
    """Minimal attribute patcher so this module needs no extra dependency."""

    def __init__(self, target, name, value):
        self.target, self.name, self.value = target, name, value
        self.old = getattr(target, name)

    def __enter__(self):
        setattr(self.target, self.name, self.value)

    def __exit__(self, *exc):
        setattr(self.target, self.name, self.old)


def run_sync(coro):
    return asyncio.run(coro)


def ollama_available():
    try:
        import ollama
        models = ollama.list()
    except Exception:
        return False
    try:
        names = [m.get("model", "") for m in (models.get("models") or [])]
    except AttributeError:
        names = []
    return bool(names)


def playwright_available():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as pw:
            pw.chromium.launch().close()
    except Exception:
        return False
    return True