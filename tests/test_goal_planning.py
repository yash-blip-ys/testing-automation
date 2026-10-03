"""Offline tests for goal sub-step derivation and navigator prompt grounding.

No browser, no Ollama. Run with:

    .\\venv311\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae


GOAL = {
    "objective": "Buy the thing.",
    "steps": [
        {"describe": "Open the cart", "evidence": {"url_contains": ["cart"]}},
        {"describe": "Start checkout", "evidence": {"url_contains": ["checkout-step-one"]}},
        {"describe": "Submit info", "evidence": {"url_contains": ["checkout-step-two"]}},
        {"describe": "Finish", "evidence": {"url_contains": ["checkout-complete"]}},
    ],
    "evidence": {"text_contains": ["Thank you"]},
}


def _goal():
    return ae.TestGoal(GOAL)


def _remaining(url, elements=None, text=""):
    return _goal().remaining_work(
        page_state={"available_elements": elements or []},
        page_text=text, url=url, structural_changed=False,
    )


class TestRemainingWorkDerivation(unittest.TestCase):
    def test_all_steps_outstanding_on_a_fresh_page(self):
        rows = _remaining("https://x.test/inventory.html")
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(not r["done"] for r in rows))
        self.assertEqual([r["describe"] for r in rows], [
            "Open the cart", "Start checkout", "Submit info", "Finish",
        ])
        self.assertEqual([r["index"] for r in rows], [1, 2, 3, 4])

    def test_verified_progress_removes_only_completed_steps(self):
        rows = _remaining("https://x.test/cart.html")
        done = [r["describe"] for r in rows if r["done"]]
        todo = [r["describe"] for r in rows if not r["done"]]
        self.assertEqual(done, ["Open the cart"])
        self.assertEqual(todo, ["Start checkout", "Submit info", "Finish"])

    def test_step_completion_requires_that_steps_own_evidence(self):
        """Landing on checkout-step-two confirms only the matching sub-step.

        URL containment is per-step, so 'Start checkout' (checkout-step-one)
        must NOT be reported done merely because the URL also contains the
        substring 'checkout-step'.
        """
        rows = _remaining("https://x.test/checkout-step-two.html")
        done = {r["describe"] for r in rows if r["done"]}
        self.assertEqual(done, {"Submit info"},
                         "only the step whose own evidence matched may be done")

    def test_near_miss_substring_does_not_complete_a_step(self):
        """'cart' must not match 'checkout-step-two' or vice versa."""
        for url in ("https://x.test/checkout-step-one.html",
                    "https://x.test/checkout-step-two.html",
                    "https://x.test/inventory.html"):
            rows = _remaining(url)
            cart_step = [r for r in rows if r["describe"] == "Open the cart"][0]
            self.assertFalse(cart_step["done"],
                             f"cart step wrongly completed at {url}")

    def test_progress_is_monotonic_through_the_flow(self):
        """Verified progress accumulates and never un-verifies.

        Reuses one goal instance across the walk, as the real run does.
        """
        goal = _goal()
        seen = []
        for url in ("inventory.html", "cart.html", "checkout-step-one.html",
                    "checkout-step-two.html", "checkout-complete.html"):
            rows = goal.remaining_work(
                page_state={"available_elements": []}, page_text="", url=url,
                structural_changed=False,
            )
            seen.append(len([r for r in rows if r["done"]]))
        self.assertEqual(seen, [0, 1, 2, 3, 4],
                         "each stage must confirm exactly one more step")

    def test_latched_progress_survives_navigating_backwards(self):
        """Confirmed work must not reappear as outstanding after leaving it."""
        goal = _goal()
        goal.remaining_work(page_state={"available_elements": []}, page_text="",
                            url="https://x.test/cart.html",
                            structural_changed=False)
        rows = goal.remaining_work(page_state={"available_elements": []},
                                  page_text="", url="https://x.test/inventory.html",
                                  structural_changed=False)
        done = [r["describe"] for r in rows if r["done"]]
        self.assertEqual(done, ["Open the cart"],
                         "verified step must stay verified after navigation")

    def test_reset_progress_clears_latched_verification(self):
        goal = _goal()
        goal.remaining_work(page_state={"available_elements": []}, page_text="",
                            url="https://x.test/checkout-complete.html",
                            structural_changed=False)
        goal.reset_progress()
        rows = goal.remaining_work(page_state={"available_elements": []},
                                  page_text="", url="https://x.test/inventory.html",
                                  structural_changed=False)
        self.assertTrue(all(not r["done"] for r in rows))

    def test_attempted_but_unverified_action_does_not_complete_a_step(self):
        """Clicking something is not evidence.

        This models the regression: the agent fires 'Open Menu' repeatedly. The
        URL never changes to the cart, so no step may be marked done, however
        many actions were attempted.
        """
        goal = ae.TestGoal(GOAL)
        for _ in range(12):
            rows = goal.remaining_work(
                page_state={"available_elements": ["Open Menu", "All Items"]},
                page_text="inventory",
                url="https://www.saucedemo.com/inventory.html",
                structural_changed=False,
            )
        self.assertTrue(all(not r["done"] for r in rows),
                        "attempting an action must never complete a step")

    def test_structural_change_alone_completes_nothing(self):
        """A node-hash change is not goal evidence unless a step asks for it."""
        rows = ae.TestGoal(GOAL).remaining_work(
            page_state={"available_elements": []},
            page_text="", url="https://x.test/inventory.html",
            structural_changed=True,
        )
        self.assertTrue(all(not r["done"] for r in rows))

    def test_state_changed_step_can_be_satisfied_by_real_change(self):
        goal = ae.TestGoal({
            "objective": "x",
            "steps": [{"describe": "cause a change", "evidence": {"state_changed": True}}],
        })
        rows = goal.remaining_work(
            page_state={"available_elements": []}, page_text="", url="u",
            structural_changed=True,
        )
        self.assertTrue(rows[0]["done"])

    def test_contradicted_step_is_not_done(self):
        """Negative evidence must keep a step outstanding, not mark it done."""
        goal = ae.TestGoal({
            "objective": "x",
            "steps": [{
                "describe": "reach confirmation",
                "evidence": {"url_contains": ["checkout-complete"],
                             "text_not_contains": ["error"]},
            }],
        })
        rows = goal.remaining_work(
            page_state={"available_elements": []},
            page_text="there was an error",
            url="https://x.test/checkout-complete.html",
            structural_changed=True,
        )
        self.assertFalse(rows[0]["done"],
                         "a contradicting error message must block completion")


class TestRemainingWorkBackCompat(unittest.TestCase):
    def test_no_steps_key_now_produces_a_runtime_plan(self):
        """Behaviour change: steps are optional.

        Previously an absent "steps" key yielded an empty remaining-work list.
        It now derives a short runtime plan from the objective alone, so a user
        can state a task in one sentence. See tests/test_runtime_plan.py.
        """
        goal = ae.TestGoal({"objective": "just do it", "evidence": {}})
        self.assertEqual(goal.steps, [])
        self.assertFalse(goal.uses_configured_steps())
        rows = goal.remaining_work(
            page_state={"available_elements": []},
            page_text="t", url="u", structural_changed=False,
        )
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["done"])

    def test_no_objective_yields_empty_remaining_work(self):
        """The genuinely legacy shape is preserved exactly."""
        goal = ae.TestGoal({"evidence": {}})
        self.assertFalse(goal.is_configured())
        self.assertEqual(goal.remaining_work(
            page_state={"available_elements": []},
            page_text="t", url="u", structural_changed=False,
        ), [])

    def test_bare_string_steps_are_accepted_but_never_auto_complete(self):
        goal = ae.TestGoal({"objective": "x", "steps": ["do the thing"]})
        self.assertEqual(len(goal.steps), 1)
        rows = goal.remaining_work(
            page_state={"available_elements": []},
            page_text="anything", url="anywhere", structural_changed=True,
        )
        self.assertFalse(rows[0]["done"],
                         "a step with no evidence can never auto-complete")

    def test_malformed_steps_are_ignored_not_fatal(self):
        for bad in (None, [], "nope", 5, [{"nope": 1}]):
            goal = ae.TestGoal({"objective": "x", "steps": bad})
            self.assertEqual(goal.steps, [], f"steps={bad!r} must not raise")

    def test_legacy_config_still_evaluates(self):
        goal = ae.TestGoal({"objective": "x", "evidence": {"text_contains": ["ok"]}})
        status, _ = goal.evaluate(
            page_state={"available_elements": []}, page_text="ok",
            url="u", structural_changed=False, steps_taken=5,
        )
        self.assertEqual(status, ae.GOAL_PASS)


class TestNavigatorPrompt(unittest.TestCase):
    """Capture the prompt the navigator would send, without calling Ollama."""

    def _prompt(self, remaining_work, elements=("Cart, 2 items", "Open Menu", "Checkout")):
        captured = {}

        def fake_chat(model, messages):
            captured["prompt"] = messages[0]["content"]
            return {"message": {"content":
                                '{"best_choice": "Cart, 2 items", '
                                '"ranked_backup": [], "reasoning": "x"}'}}

        with mock.patch.object(ae.ollama, "chat", fake_chat):
            ae.ask_ai_navigator(
                list(elements), "buy it", [],
                remaining_work=remaining_work,
                mechanical_safety_tags={},
            )
        return captured["prompt"]

    def test_prompt_contains_remaining_work_and_observed_elements(self):
        prompt = self._prompt(_remaining("https://x.test/inventory.html"))
        self.assertIn("REMAINING WORK", prompt)
        self.assertIn("Open the cart", prompt)
        self.assertIn("Start checkout", prompt)
        self.assertIn("Cart, 2 items", prompt)
        self.assertIn("Checkout", prompt)

    def test_completed_steps_are_marked_do_not_redo(self):
        prompt = self._prompt(_remaining("https://x.test/cart.html"))
        self.assertIn("ALREADY COMPLETED", prompt)
        self.assertIn("do NOT redo", prompt)
        self.assertIn("Open the cart", prompt)

    def test_first_outstanding_step_is_presented_as_the_priority(self):
        rows = _remaining("https://x.test/cart.html")
        prompt = self._prompt(rows)
        # "Start checkout" is step 2 and the first outstanding entry.
        self.assertIn("2. Start checkout", prompt)
        self.assertIn("advancing RIGHT NOW", prompt)

    def test_prompt_forbids_selecting_unobserved_elements(self):
        prompt = self._prompt(_remaining("https://x.test/inventory.html"))
        self.assertIn("you MUST pick one of these exact strings", prompt)

    def test_no_remaining_work_keeps_previous_prompt_shape(self):
        prompt = self._prompt([])
        self.assertNotIn("REMAINING WORK", prompt)
        self.assertIn("USER GOAL", prompt)
        self.assertIn("AVAILABLE CLICKABLE OPTIONS ON SCREEN", prompt)

    def test_all_steps_done_prompts_for_goal_evidence(self):
        goal = _goal()
        rows = []
        for url in ("inventory.html", "cart.html", "checkout-step-one.html",
                    "checkout-step-two.html", "checkout-complete.html"):
            rows = goal.remaining_work(
                page_state={"available_elements": []}, page_text="",
                url=f"https://x.test/{url}", structural_changed=False,
            )
        self.assertTrue(all(r["done"] for r in rows))
        prompt = self._prompt(rows)
        self.assertIn("already has confirming evidence", prompt)


class TestNavigatorGrounding(unittest.TestCase):
    """The navigator may only return elements it was actually shown."""

    def _respond(self, payload, elements=()):
        tags = {e: 0 for e in elements}
        return mock.patch.object(
            ae.ollama, "chat",
            lambda model, messages: {"message": {"content": payload}},
        ), tags

    def test_unobserved_element_is_rejected(self):
        observed = ["Cart, 2 items", "Open Menu"]
        payload = ('{"best_choice": "Secret Checkout", "ranked_backup": [], '
                   '"reasoning": "x", "safety_tags": '
                   '{"Cart, 2 items": 0, "Open Menu": 0}}')
        patcher, _ = self._respond(payload, observed)
        with patcher:
            decision = ae.ask_ai_navigator(
                observed, "goal", [], remaining_work=[],
                mechanical_safety_tags={},
            )
        self.assertIsNone(decision,
                          "a choice outside the observed list must never be accepted")

    def test_observed_element_is_accepted(self):
        observed = ["Cart, 2 items", "Open Menu"]
        payload = ('{"best_choice": "Cart, 2 items", "ranked_backup": ["Open Menu"], '
                   '"reasoning": "x", "safety_tags": '
                   '{"Cart, 2 items": 0, "Open Menu": 0}}')
        patcher, _ = self._respond(payload, observed)
        with patcher:
            decision = ae.ask_ai_navigator(
                observed, "goal", [], remaining_work=[],
                mechanical_safety_tags={},
            )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["best_choice"], "Cart, 2 items")

    def test_backup_outside_observed_list_is_dropped_by_caller(self):
        """Validator allows the shape; the caller filters unranked entries."""
        observed = ["Cart, 2 items"]
        payload = ('{"best_choice": "Cart, 2 items", "ranked_backup": ["Ghost"], '
                   '"reasoning": "x", "safety_tags": {"Cart, 2 items": 0}}')
        patcher, _ = self._respond(payload, observed)
        with patcher:
            decision = ae.ask_ai_navigator(
                observed, "goal", [], remaining_work=[],
                mechanical_safety_tags={},
            )
        self.assertIsNotNone(decision)
        applied = [e for e in decision.get("ranked_backup", []) if e in observed]
        self.assertEqual(applied, [],
                         "an unobserved backup must not be actionable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
