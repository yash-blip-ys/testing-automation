"""Live-browser verification of generic occlusion + UI-only detection.

Uses a local fixture that behaves like the reported problem: a sidebar
controller that opens a panel over the product list with no URL change, and no
product reachable while it is open.

Run with:
    .\\venv311\\Scripts\\python.exe tests\\test_occlusion_live.py -v

Skipped automatically when the browser or fixture is unavailable.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae

FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures", "occlusion", "index.html",
)


def _browser():
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None, "playwright is not installed"
    return async_playwright, None


class TestLiveOcclusion(unittest.IsolatedAsyncioTestCase):
    """Requirement 1 + 5: how the extractor represents the sidebar and overlap."""

    @classmethod
    def setUpClass(cls):
        pw, err = _browser()
        if pw is None or not os.path.exists(FIXTURE):
            raise unittest.SkipTest(err or f"fixture missing: {FIXTURE}")
        cls._pw = pw
        cls._loop = asyncio.new_event_loop()
        cls._pw_ctx = cls._loop.run_until_complete(pw().start())
        cls._browser_obj = cls._loop.run_until_complete(
            cls._pw_ctx.chromium.launch()
        )
        cls._page = cls._loop.run_until_complete(cls._browser_obj.new_page())
        cls._loop.run_until_complete(
            cls._page.goto("file:///" + FIXTURE.replace("\\", "/"))
        )

    @classmethod
    def tearDownClass(cls):
        loop = getattr(cls, "_loop", None)
        if loop is None:
            return
        try:
            loop.run_until_complete(cls._browser_obj.close())
            loop.run_until_complete(cls._pw_ctx.stop())
            loop.run_until_complete(asyncio.sleep(0))
        except Exception:
            pass
        finally:
            try:
                loop.close()
            except Exception:
                pass
        # Give the transport a moment to release its pipes before the loop is
        # collected, otherwise Windows complains about a closed pipe at GC time.
        import gc
        gc.collect()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def setUp(self):
        # Tests share one page, so reload to guarantee the drawer starts closed
        # and no test can inherit another's obstruction state.
        self._run(self._page.goto("file:///" + FIXTURE.replace("\\", "/")))
        self._run(self._page.wait_for_timeout(250))

    async def _extract(self):
        return await ae.extract_page_elements(self._page, "button, a", True)

    def test_sidebar_control_and_products_are_discovered(self):
        out = self._run(self._extract())
        self.assertIn("Open Sidebar", out["labels"])
        self.assertIn("Buy Backpack", out["labels"])

    def test_no_occlusion_when_the_sidebar_is_closed(self):
        out = self._run(self._extract())
        self.assertEqual(out["occluded"], {},
                         "nothing covers the page before the sidebar opens")

    def test_opening_the_sidebar_occludes_the_controls_beneath_it(self):
        self._run(self._page.click("#open-sidebar"))
        self._run(self._page.wait_for_timeout(250))
        out = self._run(self._extract())
        self.assertIn("Buy Backpack", out["occluded"],
                      "a covered product must be reported as occluded")
        self.assertIn("Buy Backpack", out["labels"],
                      "but it stays discovered — occlusion is not removal")

    def test_the_dismiss_control_itself_is_not_occluded(self):
        self._run(self._page.click("#open-sidebar"))
        self._run(self._page.wait_for_timeout(250))
        out = self._run(self._extract())
        self.assertIn("Close Sidebar", out["labels"])
        self.assertNotIn("Close Sidebar", out["occluded"],
                         "the way out must stay clickable")

    def test_drawer_controls_are_absent_until_the_drawer_opens(self):
        """Regression: transform-hidden panels kept a non-zero box."""
        out = self._run(self._extract())
        self.assertNotIn("Close Sidebar", out["labels"],
                         "a closed drawer must not advertise its controls")
        self.assertNotIn("All Items", out["labels"])

    def test_drawer_controls_appear_when_it_opens(self):
        self._run(self._page.click("#open-sidebar"))
        self._run(self._page.wait_for_timeout(250))
        out = self._run(self._extract())
        self.assertIn("Close Sidebar", out["labels"])
        self.assertIn("All Items", out["labels"])

    def test_close_control_is_offered_as_a_dismiss_candidate(self):
        self._run(self._page.click("#open-sidebar"))
        self._run(self._page.wait_for_timeout(250))
        out = self._run(self._extract())
        self.assertIn("Close Sidebar", out["dismiss_candidates"])

    def test_ui_only_transition_is_detected_on_a_real_drawer(self):
        mgr = ae.StateManager(max_search_depth=10)
        before = self._run(self._extract())
        closed_node = ae.compute_node_hash(self._page.url, before["labels"])
        url_at_start = self._page.url

        self._run(self._page.click("#open-sidebar"))
        self._run(self._page.wait_for_timeout(250))
        after = self._run(self._extract())
        open_node = ae.compute_node_hash(self._page.url, after["labels"])

        self.assertEqual(self._page.url, url_at_start,
                         "the fixture must not change the URL")
        self.assertNotEqual(closed_node, open_node,
                            "the drawer state is a distinct node")

        mgr.snapshot_initial(node_hash=closed_node, url=url_at_start,
                             available_elements=before["labels"])
        mgr.previous_state = mgr.current_state
        changed, ui_only = mgr.classify_transition(
            mgr.previous_state, open_node, url_at_start,
        )
        self.assertTrue(changed, "opening the drawer is an observable change")
        self.assertTrue(ui_only, "and it is UI-only: URL unchanged, no signals")

    def test_withheld_controls_leave_a_reachable_dismiss_option(self):
        self._run(self._page.click("#open-sidebar"))
        self._run(self._page.wait_for_timeout(250))
        out = self._run(self._extract())
        withheld = set(out["occluded"]) - set(out["dismiss_candidates"])
        self.assertTrue(withheld, "the fixture must occlude something")
        remaining = [e for e in out["labels"] if e not in withheld]
        self.assertTrue(remaining, "some control must remain actionable")
        self.assertTrue(
            any(e in out["dismiss_candidates"] for e in remaining),
            "and the way out must be among them",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)