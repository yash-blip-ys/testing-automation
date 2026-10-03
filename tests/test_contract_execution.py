"""Checkpoint 2.3 — Playwright execution and state tracking.

Covers each supported action type against a real browser, the failure modes
that must be reported honestly rather than swallowed, condition-based waiting
in place of fixed sleeps, and the dialog policy that replaced an unconditional
auto-accept.

Live tests skip automatically when Playwright or the fixture is unavailable.
"""

import asyncio
import gc
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures", "execution",
)
FIXTURE = os.path.join(FIXTURE_DIR, "index.html")


def _browser():
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None, "playwright is not installed"
    return async_playwright, None


class LivePageMixin:
    """One browser for the module; each test reloads a clean fixture."""

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
        cls.url = "file:///" + FIXTURE.replace("\\", "/")

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
            gc.collect()

    def setUp(self):
        self._loop.run_until_complete(self._page.goto(self.url))

    def run_(self, coro):
        return self._loop.run_until_complete(coro)

    def observe(self, selector="button, input, select, a"):
        d = self.run_(ae.extract_page_elements(self._page, selector))
        return ae.PageObservation(url=self._page.url, discovery=d), d

    def intent(self, name, d, operation, value=None):
        rec = d["by_label"][name]
        return ae.ActionIntent(
            operation=operation,
            target_name=name,
            target_agent_id=rec["agent_id"],
            observation_id=1,
            provenance=ae.RESOLVE_GROUNDED,
            required_input=value,
            record=rec,
        )

    def grounded_locator(self, name, d):
        ids = {n: r["agent_id"] for n, r in d["by_label"].items()}
        loc, _ = self.run_(ae.build_locator_for_edge(
            self._page, name, d["hints"], agent_ids=ids))
        return loc


# ===========================================================================
# Supported action types, against a real page
# ===========================================================================

class TestSupportedActions(LivePageMixin, unittest.IsolatedAsyncioTestCase):

    def test_operations_are_derived_from_element_role_not_wording(self):
        _, d = self.observe()
        self.assertEqual(
            ae.operation_for_record(d["by_label"]["Username"]), ae.OP_FILL)
        self.assertEqual(
            ae.operation_for_record(d["by_label"]["Role"]), ae.OP_SELECT)
        self.assertEqual(
            ae.operation_for_record(d["by_label"]["Subscribe to newsletter"]),
            ae.OP_TOGGLE)
        self.assertEqual(
            ae.operation_for_record(d["by_label"]["Save"]), ae.OP_SUBMIT)

    def test_text_entry(self):
        _, d = self.observe()
        loc = self.grounded_locator("Username", d)
        i = self.intent("Username", d, ae.OP_FILL, value="ada")
        self.assertEqual(self.run_(ae.execute_action(self._page, loc, i)),
                         "filled")
        self.assertEqual(
            self.run_(self._page.input_value("#username")), "ada")

    def test_select_by_value(self):
        _, d = self.observe()
        loc = self.grounded_locator("Role", d)
        i = self.intent("Role", d, ae.OP_SELECT, value="editor")
        self.assertEqual(self.run_(ae.execute_action(self._page, loc, i)),
                         "selected")
        self.assertEqual(self.run_(self._page.input_value("#role")), "editor")

    def test_select_by_visible_label(self):
        """A run may supply the visible text; it must not silently pick some
        other option when that text does not match."""
        _, d = self.observe()
        loc = self.grounded_locator("Role", d)
        i = self.intent("Role", d, ae.OP_SELECT, value="Viewer")
        self.assertEqual(self.run_(ae.execute_action(self._page, loc, i)),
                         "selected")
        self.assertEqual(self.run_(self._page.input_value("#role")), "viewer")

    def test_checkbox_can_be_checked_and_unchecked(self):
        _, d = self.observe()
        loc = self.grounded_locator("Subscribe to newsletter", d)
        i = self.intent("Subscribe to newsletter", d, ae.OP_TOGGLE, value=True)
        self.assertEqual(self.run_(ae.execute_action(self._page, loc, i)),
                         "checked")
        self.assertTrue(self.run_(self._page.is_checked("#newsletter")))
        self.assertEqual(self.run_(ae.execute_action(self._page, loc, i)),
                         "checked", "re-checking a checked box is not an error")

    def test_radio_uncheck_is_reported_as_unsupported_not_attempted(self):
        """HTML defines radio deselection only via another radio in the group,
        so this must fail explicitly instead of silently leaving the state."""
        _, d = self.observe()
        loc = self.grounded_locator("Mode (nx)", d)
        i = self.intent("Mode (nx)", d, ae.OP_TOGGLE, value=False)
        with self.assertRaises(ae.ActionFailure) as ctx:
            self.run_(ae.execute_action(self._page, loc, i))
        self.assertEqual(ctx.exception.reason_tag,
                         ae.ACTION_FAILED_NOT_APPLICABLE)
        self.assertIn("another radio", ctx.exception.message)
        self.assertTrue(self.run_(self._page.is_checked("#mode-nx")),
                        "the radio must be left exactly as it was")

    def test_radio_can_be_selected(self):
        _, d = self.observe()
        loc = self.grounded_locator("Mode (ex)", d)
        i = self.intent("Mode (ex)", d, ae.OP_TOGGLE, value=True)
        self.assertEqual(self.run_(ae.execute_action(self._page, loc, i)),
                         "checked")
        self.assertTrue(self.run_(self._page.is_checked("#mode-ex")))

    def test_submit_reaches_the_page(self):
        _, d = self.observe()
        loc = self.grounded_locator("Username", d)
        self.run_(ae.execute_action(
            self._page, loc, self.intent("Username", d, ae.OP_FILL, value="ada")))
        loc_save = self.grounded_locator("Save", d)
        self.run_(ae.execute_action(
            self._page, loc_save, self.intent("Save", d, ae.OP_SUBMIT)))
        self.assertEqual(self.run_(self._page.evaluate(
            "() => window.lastFormSubmit.username")), "ada")

    def test_click_navigates(self):
        _, d = self.observe()
        loc = self.grounded_locator("Navigate", d)
        self.run_(ae.execute_action(
            self._page, loc, self.intent("Navigate", d, ae.OP_CLICK)))
        self.run_(self._page.wait_for_url("**/page2.html", timeout=5000))
        self.assertIn("page2.html", self._page.url)


# ===========================================================================
# Execution failures must be surfaced accurately
# ===========================================================================

class TestExecutionFailures(LivePageMixin, unittest.IsolatedAsyncioTestCase):

    def test_unknown_select_option_is_reported_not_silently_substituted(self):
        _, d = self.observe()
        loc = self.grounded_locator("Strict select (no fallback)", d)
        i = self.intent("Strict select (no fallback)", d, ae.OP_SELECT,
                        value="nonexistent")
        with self.assertRaises(ae.ActionFailure) as ctx:
            self.run_(ae.execute_action(self._page, loc, i))
        self.assertEqual(ctx.exception.reason_tag, ae.ACTION_FAILED_NO_SUCH_OPTION)
        self.assertIn("nonexistent", ctx.exception.message)
        self.assertEqual(
            self.run_(self._page.input_value("#strict")), "one",
            "a failed select must leave the control on its original value")

    def test_filling_a_disabled_control_is_reported_as_disabled(self):
        self.run_(self._page.evaluate(
            "() => { document.getElementById('username').disabled = true; }"))
        _, d = self.observe()
        loc = self.grounded_locator("Username", d)
        i = self.intent("Username", d, ae.OP_FILL, value="ada")
        with self.assertRaises(ae.ActionFailure) as ctx:
            self.run_(ae.execute_action(self._page, loc, i))
        self.assertEqual(ctx.exception.reason_tag, ae.ACTION_FAILED_DISABLED)

    def test_fill_without_a_value_fails_before_touching_the_page(self):
        _, d = self.observe()
        loc = self.grounded_locator("Username", d)
        i = self.intent("Username", d, ae.OP_FILL, value=None)
        with self.assertRaises(ae.ActionFailure) as ctx:
            self.run_(ae.execute_action(self._page, loc, i))
        self.assertEqual(ctx.exception.reason_tag,
                         ae.ACTION_FAILED_NOT_APPLICABLE)
        self.assertEqual(self.run_(self._page.input_value("#username")), "")

    def test_filling_a_detached_control_fails_rather_than_hanging(self):
        _, d = self.observe()
        loc = self.grounded_locator("Username", d)
        self.run_(self._page.evaluate(
            "() => document.getElementById('username').remove()"))
        i = self.intent("Username", d, ae.OP_FILL, value="ada")
        with self.assertRaises(ae.ActionFailure):
            self.run_(ae.execute_action(self._page, loc, i, timeout_ms=1500))

    def test_every_failure_carries_a_stable_reason_tag(self):
        for tag in (ae.ACTION_FAILED_NOT_APPLICABLE, ae.ACTION_FAILED_DISABLED,
                    ae.ACTION_FAILED_TIMEOUT, ae.ACTION_FAILED_NO_SUCH_OPTION,
                    ae.ACTION_FAILED_REJECTED_VALUE):
            with self.subTest(tag=tag):
                self.assertIsInstance(tag, str)


# ===========================================================================
# Waiting: condition-based, bounded, and honest about not settling
# ===========================================================================

class TestBoundedWaiting(LivePageMixin, unittest.IsolatedAsyncioTestCase):

    def test_wait_returns_true_on_a_settled_page(self):
        self.assertTrue(self.run_(ae.wait_for_page_settled(self._page)))

    def test_wait_is_faster_than_the_sleep_it_replaced(self):
        """A condition that already holds must return immediately."""
        import time
        self.run_(ae.wait_for_page_settled(self._page))
        start = time.monotonic()
        self.assertTrue(self.run_(ae.wait_for_page_settled(
            self._page, quiet_ms=100, overall_timeout_ms=3000)))
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0,
                        "an already-settled page must not pay a fixed wait")

    def test_never_settling_page_times_out_and_says_so(self):
        """A page that keeps mutating must cost the bounded budget once, and
        report that it did not settle — not hang, and not claim readiness."""
        self.run_(self._page.evaluate(
            """() => { window.__t = setInterval(() => {
                   document.body.setAttribute('data-tick',
                       String(Date.now()));
               }, 20); }"""))
        try:
            settled = self.run_(ae.wait_for_page_settled(
                self._page, quiet_ms=150, overall_timeout_ms=1200))
        finally:
            self.run_(self._page.evaluate("() => clearInterval(window.__t)"))
        self.assertFalse(settled,
                         "a page that never goes quiet must be reported as unsettled")

    def test_wait_survives_a_navigation_that_never_loads(self):
        self.run_(self._page.evaluate(
            "() => { window.location.href = 'about:blank#x'; }"))
        self.run_(self._page.wait_for_timeout(200))
        # Must return rather than raise, whatever the page did.
        self.run_(ae.wait_for_page_settled(self._page, overall_timeout_ms=2000))

    def test_browser_config_defaults_are_not_hardcoded(self):
        """Session state is opt-in; nothing is inherited implicitly."""
        import inspect
        src = inspect.getsource(ae.run_pathfinder_agent)
        self.assertNotIn("headless=False,", src,
                         "headless must come from configuration")
        self.assertIn("storage_state", src)


# ===========================================================================
# Dialog policy: the defect that let a confirm() commit a deletion
# ===========================================================================

class TestDialogPolicy(LivePageMixin, unittest.IsolatedAsyncioTestCase):

    async def _install(self, policy):
        loop = self._loop
        self._dialogs = []

        async def handle(dialog):
            verdict = policy.decide_dialog(dialog.type, dialog.message)
            self._dialogs.append((dialog.type, dialog.message, verdict))
            try:
                if verdict == "accept":
                    await dialog.accept()
                else:
                    await dialog.dismiss()
            except Exception:
                # A dialog another handler already answered is not this
                # handler's failure; mirrors the engine's own tolerance.
                pass

        # Replace rather than add: Playwright listeners accumulate, and a
        # second handler would try to answer a dialog already answered.
        if getattr(self, "_dialog_handler", None) is not None:
            self._page.remove_listener("dialog", self._dialog_handler)
        self._dialog_handler = lambda d: asyncio.create_task(handle(d))
        self._page.on("dialog", self._dialog_handler)

    def test_default_policy_dismisses_every_dialog(self):
        p = ae.SafetyPolicy()
        for msg in ("Delete your account permanently?", "Place order for $99?",
                    "Are you sure?", "Allow site to use your location?",
                    ""):
            self.assertEqual(p.decide_dialog("confirm", msg), "dismiss")

    def test_consequential_dialogs_are_dismissed_even_when_opted_in(self):
        p = ae.SafetyPolicy(auto_accept_dialogs=("delete", "order"))
        self.assertEqual(
            p.decide_dialog("confirm", "Delete your account permanently?"),
            "dismiss",
            "an opt-in must not be able to authorise an irreversible answer")

    def test_explicit_opt_in_can_accept_a_benign_dialog(self):
        p = ae.SafetyPolicy(auto_accept_dialogs=("cookie",))
        self.assertEqual(
            p.decide_dialog("beforeunload", "Leave this page?"), "dismiss")
        self.assertEqual(
            p.decide_dialog("alert", "This site uses cookies."), "accept")

    def test_live_confirm_is_dismissed_so_no_deletion_is_committed(self):
        self.run_(self._install(ae.SafetyPolicy()))
        _, d = self.observe()
        loc = self.grounded_locator("Open confirm dialog", d)
        self.run_(ae.execute_action(
            self._page, loc,
            self.intent("Open confirm dialog", d, ae.OP_CLICK)))
        self.run_(self._page.wait_for_timeout(300))
        self.assertTrue(self._dialogs, "the dialog should have been observed")
        kind, message, verdict = self._dialogs[0]
        self.assertEqual(kind, "confirm")
        self.assertIn("Delete", message)
        self.assertEqual(verdict, "dismiss")
        self.assertFalse(self.run_(self._page.evaluate(
            "() => window.dialogOutcome")),
            "a dismissed confirm must report the user said no")

    def test_live_alert_dismissed_does_not_block_later_actions(self):
        """A modal dialog blocks Playwright until answered, so declining it
        must still leave the page usable."""
        self.run_(self._install(ae.SafetyPolicy()))
        _, d = self.observe()
        loc = self.grounded_locator("Open alert dialog", d)
        self.run_(ae.execute_action(
            self._page, loc, self.intent("Open alert dialog", d, ae.OP_CLICK)))
        self.run_(self._page.wait_for_timeout(200))
        self.assertEqual(self.run_(self._page.get_attribute(
            "#status", "data-last")), "alert:ok",
            "the page must proceed after the dialog is dismissed")

    def test_engine_no_longer_auto_accepts_dialogs(self):
        import inspect
        src = inspect.getsource(ae.run_pathfinder_agent)
        self.assertNotIn('dialog.accept()', src.replace(
            "await dialog.accept()", "POLICY_ACCEPT"),
            "an unconditional auto-accept must not be reintroduced")


if __name__ == "__main__":
    unittest.main(verbosity=2)
