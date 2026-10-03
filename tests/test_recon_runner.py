"""End-to-end reconnaissance behaviour, driven by a fake page.

Exercises the runner's real control flow — budget enforcement, link
classification, barrier stopping, and the read-only posture — without launching
a browser, so the guarantees are pinned as fast deterministic tests.
"""

import unittest

import reconnaissance as r


class FakePage:
    """Minimal stand-in for a Playwright page.

    Records every `goto` so a test can assert that reconnaissance only ever
    navigated same-origin links and never issued a fill, click, or submit.
    """

    def __init__(self, pages):
        # {url: {"title", "options", "links", "forms", "records"}}
        self.pages = pages
        self.visited = []
        self.actions = []
        self.url = ""

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        self.actions.append(("goto", url))
        self.url = url

    async def wait_for_load_state(self, *a, **k):
        return None

    async def wait_for_function(self, *a, **k):
        return None

    async def evaluate(self, script, arg=None):
        # The engine's element extractor and the recon link extractor are both
        # intercepted; neither is what is under test here.
        if "form" in script and "resolved" in script:
            data = self.pages.get(self.url, {})
            return {
                "url": self.url,
                "title": data.get("title", ""),
                "links": data.get("links", []),
                "link_overflow": 0,
                "forms": data.get("forms", []),
                "has_password_field": False,
            }
        data = self.pages.get(self.url, {})
        return {
            "records": data.get("records", []),
            "options": data.get("options", []),
            "labels": data.get("options", []),
            "hidden": [],
            "duplicates": {},
            "observation_id": 1,
        }

    # Any richer interaction is a contract violation for reconnaissance.
    def __getattr__(self, name):
        async def _forbidden(*a, **k):
            self.actions.append((name, a))
            raise AssertionError(
                f"reconnaissance attempted a forbidden page call: {name}")

        return _forbidden


async def _noop_settle(page, **kwargs):
    return True


async def _run(pages, budget=None, start="https://shop.test/"):
    budget = budget or r.ReconBudget()
    runner = r.ReconRunner(budget)
    page = FakePage(pages)
    # The settle helper awaits real Playwright state; swap it for a no-op so
    # the runner's own control flow is what is under test.
    original = r.wait_for_page_settled
    r.wait_for_page_settled = _noop_settle
    try:
        graph = await runner.run(page, start)
    finally:
        r.wait_for_page_settled = original
    return graph, runner, page


def _link(href, text=""):
    return {"href": href, "resolved": href, "text": text or href,
            "title": "", "rel": ""}


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_visits_only_same_origin_links(self):
        pages = {
            "https://shop.test/": {
                "title": "Shop", "options": ["Home"],
                "links": [_link("https://shop.test/about", "About"),
                          _link("https://other.test/x", "Partner")],
            },
            "https://shop.test/about": {"title": "About", "links": []},
        }
        graph, runner, page = await _run(pages)
        self.assertIn("https://shop.test/about", page.visited)
        # Off-site link recorded but never walked.
        self.assertNotIn("https://other.test/x", page.visited)
        offsite = [e for e in graph.edges if "other.test" in (e.target_url or "")]
        self.assertEqual(len(offsite), 1)

    async def test_consequential_link_is_blocked_not_followed(self):
        pages = {
            "https://shop.test/": {
                "title": "Shop",
                "links": [_link("https://shop.test/checkout", "Checkout"),
                          _link("https://shop.test/delete", "Delete account")],
            },
        }
        graph, runner, page = await _run(pages)
        # Never navigated to a destructive or purchasing flow.
        self.assertEqual(page.visited, ["https://shop.test/"])
        self.assertEqual(len(graph.edges), 2)
        for edge in graph.edges:
            self.assertEqual(edge.kind, r.EDGE_BLOCKED)
        self.assertEqual(len(graph.unexplored), 2)

    async def test_never_fills_clicks_or_submits(self):
        pages = {
            "https://shop.test/": {
                "title": "Shop",
                "links": [_link("https://shop.test/about", "About")],
            },
            "https://shop.test/about": {"title": "About", "links": []},
        }
        _graph, _runner, page = await _run(pages)
        # The fake page raises on any interaction beyond goto.
        for name, _arg in page.actions:
            self.assertEqual(name, "goto")

    async def test_stops_at_auth_barrier(self):
        pages = {
            "https://shop.test/": {
                "title": "Shop",
                "links": [_link("https://shop.test/login", "Sign in")],
            },
            "https://shop.test/login": {
                "title": "Sign in",
                "records": [{"name": "password", "type": "password"}],
                "links": [],
            },
        }
        graph, runner, page = await _run(pages)
        self.assertEqual(runner.stop_reason, r.RECON_DONE_AUTH)
        self.assertTrue(any(b.kind == r.BARRIER_AUTH
                            for b in runner.barriers))

    async def test_stops_at_consent_gate(self):
        pages = {
            "https://shop.test/": {
                "title": "Shop",
                "records": [{"name": "Accept all cookies", "role": "button"}],
                "links": [],
            },
        }
        _graph, runner, _page = await _run(pages)
        self.assertEqual(runner.stop_reason, r.RECON_DONE_CONSENT)

    async def test_page_budget_is_enforced(self):
        pages = {"https://shop.test/": {
            "title": "Shop",
            "links": [_link(f"https://shop.test/p{i}", f"Page {i}")
                      for i in range(10)],
        }}
        budget = r.ReconBudget(max_pages=3)
        graph, runner, page = await _run(pages, budget)
        self.assertLessEqual(len(page.visited), 3)
        self.assertIn(runner.stop_reason,
                      (r.RECON_DONE_PAGES, r.RECON_DONE_EXHAUSTED))

    async def test_depth_budget_is_enforced(self):
        # A chain: / -> /a -> /a/b -> /a/b/c
        pages = {
            "https://shop.test/": {"links": [_link("https://shop.test/a", "A")]},
            "https://shop.test/a": {"links": [_link("https://shop.test/a/b", "B")]},
            "https://shop.test/a/b": {
                "links": [_link("https://shop.test/a/b/c", "C")]},
            "https://shop.test/a/b/c": {"links": []},
        }
        budget = r.ReconBudget(max_depth=1)
        _graph, _runner, page = await _run(pages, budget)
        # Depth 0 and depth 1 only; /a/b and deeper are not visited.
        self.assertIn("https://shop.test/a", page.visited)
        self.assertNotIn("https://shop.test/a/b/c", page.visited)

    async def test_duplicate_links_visit_target_once(self):
        pages = {
            "https://shop.test/": {
                "links": [_link("https://shop.test/about", "About"),
                          _link("https://shop.test/about", "About")],
            },
            "https://shop.test/about": {"links": []},
        }
        _graph, _runner, page = await _run(pages)
        # Two links to one target is one page visit.
        self.assertEqual(page.visited.count("https://shop.test/about"), 1)

    async def test_traversed_edge_is_recorded_as_walked(self):
        pages = {
            "https://shop.test/": {
                "links": [_link("https://shop.test/about", "About")]},
            "https://shop.test/about": {"links": []},
        }
        graph, _runner, _page = await _run(pages)
        walked = [e for e in graph.edges if e.is_traversed]
        self.assertEqual(len(walked), 1)
        self.assertEqual(walked[0].target_id, graph.node_at_url(
            "https://shop.test/about"))

    async def test_start_page_with_no_links_exhausts_cleanly(self):
        pages = {"https://shop.test/": {"title": "Empty", "links": []}}
        _graph, runner, _page = await _run(pages)
        self.assertEqual(runner.stop_reason, r.RECON_DONE_EXHAUSTED)
        # Exhausting the frontier is not an incomplete-coverage stop.
        self.assertNotIn(runner.stop_reason, r.RECON_INCOMPLETE_REASONS)


if __name__ == "__main__":
    unittest.main()