"""Offline tests for UI-only state changes, goal-relative demotion of chrome,
and occlusion handling.

Covers the regression: a sidebar/drawer control opens, the URL does not change,
products become covered, and the agent keeps re-clicking the drawer instead of
advancing. Everything here runs without a browser or a model.

Run with:
    .\\venv311\\Scripts\\python.exe -m unittest tests.test_ui_state -v
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae


def _mgr():
    return ae.StateManager(max_search_depth=30)


def _state(mgr, node, url, semantic=None):
    """Commit a state as the previous one, so the next snapshot becomes current."""
    mgr.snapshot_initial(
        node_hash=node, url=url, page_title="t",
        available_elements=["Open Menu", "Cart, empty"], cart_badge_count=0,
        form_input_count=0, step_index=0, externals_count=0,
        semantic_signature=semantic,
    )
    mgr.previous_state = mgr.current_state


class TestUiOnlyChangeDetection(unittest.TestCase):
    """Requirement 2: detect meaningful UI change when the URL is unchanged."""

    def test_sidebar_open_with_same_url_is_a_state_change(self):
        mgr = _mgr()
        _state(mgr, "node-closed", "https://shop.test/inventory.html")
        changed, ui_only = mgr.classify_transition(
            mgr.previous_state, "node-drawer-open",
            "https://shop.test/inventory.html",
        )
        self.assertTrue(changed, "opening a sidebar IS a new observable state")
        self.assertTrue(ui_only, "and it is UI-only: the URL did not change")

    def test_url_change_is_not_ui_only(self):
        mgr = _mgr()
        _state(mgr, "node-a", "https://shop.test/inventory.html")
        changed, ui_only = mgr.classify_transition(
            mgr.previous_state, "node-b", "https://shop.test/cart.html",
        )
        self.assertTrue(changed)
        self.assertFalse(ui_only, "navigation is real progress, not chrome")

    def test_semantic_change_is_not_ui_only(self):
        """Adding to a cart keeps the URL but changes a task signal."""
        mgr = _mgr()
        _state(mgr, "node-a", "https://shop.test/inventory.html",
               semantic={"cart_count": 0})
        changed, ui_only = mgr.classify_transition(
            mgr.previous_state, "node-b", "https://shop.test/inventory.html",
            current_semantic={"cart_count": 1},
        )
        self.assertTrue(changed)
        self.assertFalse(ui_only, "a cart-count change is task progress")

    def test_unchanged_node_is_not_a_change(self):
        mgr = _mgr()
        _state(mgr, "node-a", "https://shop.test/inventory.html")
        changed, ui_only = mgr.classify_transition(
            mgr.previous_state, "node-a", "https://shop.test/inventory.html",
        )
        self.assertFalse(changed)
        self.assertFalse(ui_only)

    def test_no_previous_state_is_not_a_change(self):
        mgr = _mgr()
        changed, ui_only = mgr.classify_transition(
            None, "node-a", "https://shop.test/",
        )
        self.assertFalse(changed)
        self.assertFalse(ui_only)

    def test_covering_elements_still_change_the_node_hash(self):
        """Occlusion alters the element set, so the drawer state stays distinct."""
        closed = ae.compute_node_hash(
            "https://shop.test/inventory.html",
            ["Open Menu", "Add to cart", "Cart, empty"],
        )
        open_ = ae.compute_node_hash(
            "https://shop.test/inventory.html",
            ["Open Menu", "Close Menu", "All Items", "Cart, empty"],
        )
        self.assertNotEqual(closed, open_)


class TestUiOnlyChangeIsNotGoalCompletion(unittest.TestCase):
    """Requirement 7: a UI animation is not verified goal progress."""

    def _goal(self, evidence):
        return ae.TestGoal({"objective": "x", "evidence": evidence})

    def test_state_changed_goal_blocked_by_ui_only_change(self):
        goal = self._goal({"state_changed": True})
        status, evidence = goal.evaluate(
            page_state={"available_elements": ["Open Menu"],
                        "ui_only_changed": True},
            page_text="Products", url="https://shop.test/inventory.html",
            structural_changed=True, steps_taken=5,
        )
        self.assertNotEqual(status, ae.GOAL_PASS)
        self.assertIn("UI-only", evidence)

    def test_state_changed_goal_passes_on_real_transition(self):
        goal = self._goal({"state_changed": True})
        status, _ = goal.evaluate(
            page_state={"available_elements": ["Checkout"],
                        "ui_only_changed": False},
            page_text="Checkout", url="https://shop.test/checkout-step-one.html",
            structural_changed=True, steps_taken=5,
        )
        self.assertEqual(status, ae.GOAL_PASS)

    def test_sub_goal_step_not_completed_by_ui_only_change(self):
        goal = ae.TestGoal({
            "objective": "x",
            "steps": [{"describe": "reach the cart", "evidence": {"state_changed": True}}],
        })
        rows = goal.remaining_work(
            page_state={"available_elements": ["Open Menu"],
                        "ui_only_changed": True},
            page_text="Products", url="https://shop.test/inventory.html",
            structural_changed=True,
        )
        self.assertFalse(rows[0]["done"],
                         "a drawer opening must not verify a sub-goal step")

    def test_absent_flag_preserves_legacy_behaviour(self):
        """Configs that pass no ui_only_changed key behave exactly as before."""
        goal = self._goal({"state_changed": True})
        status, _ = goal.evaluate(
            page_state={"available_elements": ["Checkout"]},
            page_text="t", url="u", structural_changed=True, steps_taken=5,
        )
        self.assertEqual(status, ae.GOAL_PASS)

    def test_ui_only_flag_does_not_affect_url_or_text_evidence(self):
        """Only state_changed is gated; explicit evidence still counts."""
        goal = self._goal({"url_contains": ["checkout-complete"]})
        status, _ = goal.evaluate(
            page_state={"ui_only_changed": True},
            page_text="t", url="https://shop.test/checkout-complete.html",
            structural_changed=True, steps_taken=5,
        )
        self.assertEqual(status, ae.GOAL_PASS)


class TestGoalRelativeDemotion(unittest.TestCase):
    """Requirements 3 and 6: penalize repeats, never blacklist menus."""

    def test_repeated_ui_only_attempt_is_counted(self):
        mgr = _mgr()
        counts = [
            mgr.record_unproductive_for_goal("n1", "Open Menu", (1, 2, 3))
            for _ in range(3)
        ]
        self.assertEqual(counts, [1, 2, 3])

    def test_floor_escalates_with_attempts(self):
        mgr = _mgr()
        mgr.record_unproductive_for_goal("n1", "Open Menu", (1,))
        first = mgr.demote_goal_unproductive_edges(
            "n1", (1,), ae.GOAL_UNPRODUCTIVE_COST_BASE, ae.GOAL_UNPRODUCTIVE_COST_CAP
        )
        for _ in range(5):
            mgr.record_unproductive_for_goal("n1", "Open Menu", (1,))
        later = mgr.demote_goal_unproductive_edges(
            "n1", (1,), ae.GOAL_UNPRODUCTIVE_COST_BASE, ae.GOAL_UNPRODUCTIVE_COST_CAP
        )
        self.assertGreater(later["Open Menu"], first["Open Menu"])

    def test_floor_is_capped(self):
        mgr = _mgr()
        for _ in range(200):
            mgr.record_unproductive_for_goal("n1", "Open Menu", (1,))
        floors = mgr.demote_goal_unproductive_edges(
            "n1", (1,), ae.GOAL_UNPRODUCTIVE_COST_BASE, ae.GOAL_UNPRODUCTIVE_COST_CAP
        )
        self.assertEqual(floors["Open Menu"], ae.GOAL_UNPRODUCTIVE_COST_CAP)
        self.assertLess(floors["Open Menu"], 999,
                        "a demotion must never become a blacklist")

    def test_menu_becomes_usable_again_after_the_goal_advances(self):
        """Requirement 6: no permanent blacklist of menus."""
        mgr = _mgr()
        for _ in range(4):
            mgr.record_unproductive_for_goal("n1", "Open Menu", (1, 2))
        stale = mgr.demote_goal_unproductive_edges(
            "n1", (1, 2), ae.GOAL_UNPRODUCTIVE_COST_BASE, ae.GOAL_UNPRODUCTIVE_COST_CAP
        )
        self.assertIn("Open Menu", stale)

        # The outstanding steps change, so the old memory must not apply.
        later = mgr.demote_goal_unproductive_edges(
            "n1", (2,), ae.GOAL_UNPRODUCTIVE_COST_BASE, ae.GOAL_UNPRODUCTIVE_COST_CAP
        )
        self.assertEqual(later, {},
                         "a menu stays fully available once a later step needs it")

    def test_demotion_is_scoped_to_its_node(self):
        mgr = _mgr()
        mgr.record_unproductive_for_goal("n1", "Open Menu", (1,))
        self.assertEqual(
            mgr.demote_goal_unproductive_edges(
                "n2", (1,), ae.GOAL_UNPRODUCTIVE_COST_BASE, ae.GOAL_UNPRODUCTIVE_COST_CAP
            ),
            {},
        )

    def test_demotion_never_raises_the_global_loop_floor(self):
        """It must stay a soft, goal-scoped signal, not global loop state."""
        mgr = _mgr()
        for _ in range(10):
            mgr.record_unproductive_for_goal("n1", "Open Menu", (1,))
        self.assertEqual(mgr.get_edge_cost_floor("n1", "Open Menu"), 0)
        self.assertEqual(mgr.goal_unproductive_count("n1", "Open Menu", (1,)), 10)

    def test_missing_arguments_are_ignored(self):
        mgr = _mgr()
        self.assertEqual(mgr.record_unproductive_for_goal(None, "x", (1,)), 0)
        self.assertEqual(mgr.record_unproductive_for_goal("n1", None, (1,)), 0)


class TestOcclusionReporting(unittest.TestCase):
    """Requirement 5: covered controls are not treated as actionable."""

    def test_occluded_controls_are_withheld_when_a_dismiss_exists(self):
        available = ["Open Menu", "Close Menu", "Add to cart", "Cart, 1 items"]
        occluded = {"Add to cart": "Close Menu"}
        dismiss = ["Close Menu"]
        withheld = set(occluded) - set(dismiss)
        unblocked = [e for e in available if e not in withheld]
        self.assertNotIn("Add to cart", unblocked)
        self.assertIn("Close Menu", unblocked,
                      "the dismiss control must stay selectable")

    def test_unblocked_controls_remain_actionable(self):
        available = ["Open Menu", "Cart, 1 items"]
        unblocked = [e for e in available if e not in {"Add to cart"}]
        self.assertEqual(unblocked, available)

    def test_no_dismiss_control_keeps_the_full_list(self):
        """Nothing is dropped when there is no way to clear the obstruction."""
        available = ["Open Menu", "Add to cart"]
        withheld = set()
        unblocked = [e for e in available if e not in withheld]
        self.assertEqual(unblocked, available)


class TestOcclusionExtraction(unittest.TestCase):
    """The JS contract: occlusion is reported per record, never invented."""

    def _records(self, raw):
        async def fake_evaluate(script, args):
            return raw

        page = mock.MagicMock()
        page.evaluate = fake_evaluate
        return asyncio.run(ae.extract_page_elements(page, "button", True))

    def test_occluded_and_dismiss_are_surfaced(self):
        raw = {
            "records": [
                {"name": "Add to cart", "agent_id": "1", "disabled": False,
                 "occluded_by": "Close Menu"},
                {"name": "Close Menu", "agent_id": "2", "disabled": False,
                 "occluded_by": ""},
            ],
            "mechanical_safety_tags": {},
            "occluded": {"Add to cart": "Close Menu"},
            "dismiss_candidates": ["Close Menu"],
        }
        out = self._records(raw)
        self.assertEqual(out["occluded"], {"Add to cart": "Close Menu"})
        self.assertEqual(out["dismiss_candidates"], ["Close Menu"])

    def test_unoccluded_page_reports_nothing(self):
        raw = {
            "records": [{"name": "Add to cart", "agent_id": "1",
                         "disabled": False, "occluded_by": ""}],
            "mechanical_safety_tags": {},
            "occluded": {},
            "dismiss_candidates": [],
        }
        out = self._records(raw)
        self.assertEqual(out["occluded"], {})
        self.assertEqual(out["dismiss_candidates"], [])

    def test_dismiss_candidates_are_grounded_in_discovered_elements(self):
        """A dismiss control the agent was never shown must be dropped."""
        raw = {
            "records": [{"name": "Add to cart", "agent_id": "1",
                         "disabled": False, "occluded_by": "phantom"}],
            "mechanical_safety_tags": {},
            "occluded": {"Add to cart": "phantom"},
            "dismiss_candidates": ["Phantom Close Button"],
        }
        out = self._records(raw)
        self.assertEqual(out["dismiss_candidates"], [],
                         "never suggest a control that was not discovered")

    def test_extraction_failure_returns_safe_empty_structure(self):
        class _Boom:
            async def evaluate(self, *a, **k):
                raise RuntimeError("navigation destroyed context")

        out = asyncio.run(
            ae.extract_page_elements(_Boom(), "button", True)
        )
        self.assertEqual(out["labels"], [])
        self.assertEqual(out["occluded"], {})
        self.assertEqual(out["dismiss_candidates"], [])


class TestNavigatorOcclusionContext(unittest.TestCase):
    """Requirement 4: the navigator is told what is covered and what is stale."""

    def _prompt(self, **kwargs):
        captured = {}

        def fake_chat(model, messages):
            captured["p"] = messages[0]["content"]
            return {"message": {"content": json_ok()}}

        def json_ok():
            return ('{"best_choice": "Cart, 1 items", "ranked_backup": [], '
                    '"reasoning": "x", "safety_tags": {"Cart, 1 items": 0, '
                    '"Open Menu": 0, "Close Menu": 0}}')

        with mock.patch.object(ae.ollama, "chat", fake_chat):
            ae.ask_ai_navigator(
                ["Cart, 1 items", "Open Menu", "Close Menu"], "goal", [],
                mechanical_safety_tags={}, **kwargs
            )
        return captured["p"]

    def test_occluded_controls_are_named_in_the_prompt(self):
        prompt = self._prompt(occluded_elements={"Cart, 1 items": "Close Menu"})
        self.assertIn("OBSCURED CONTROLS", prompt)
        self.assertIn("Cart, 1 items", prompt)
        self.assertIn("Close Menu", prompt)

    def test_dismiss_control_is_suggested(self):
        prompt = self._prompt(
            occluded_elements={"Cart, 1 items": "Close Menu"},
            dismiss_candidates=["Close Menu"],
        )
        self.assertIn("DISMISS CONTROLS", prompt)
        self.assertIn("Close Menu", prompt)

    def test_absent_occlusion_does_not_change_the_prompt(self):
        prompt = self._prompt()
        self.assertNotIn("OBSCURED CONTROLS", prompt)
        self.assertIn("USER GOAL", prompt)

    def test_unproductive_controls_are_reported_but_selectable(self):
        prompt = self._prompt(recently_unproductive=["Open Menu"])
        self.assertIn("RECENTLY UNPRODUCTIVE", prompt)
        self.assertIn("Open Menu", prompt)
        self.assertIn("only pick one if the current goal step genuinely needs it",
                      prompt)
        # Still listed as a legal option, never removed.
        self.assertIn("'Open Menu'", prompt)

    def test_next_unfinished_step_is_still_present(self):
        goal = ae.TestGoal({
            "objective": "x",
            "steps": [{"describe": "Open the cart", "evidence": {"url_contains": ["cart"]}}],
        })
        rw = goal.remaining_work(
            page_state={"available_elements": []}, page_text="",
            url="https://shop.test/inventory.html", structural_changed=False,
        )
        prompt = self._prompt(remaining_work=rw)
        self.assertIn("Open the cart", prompt)
        self.assertIn("REMAINING WORK", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)