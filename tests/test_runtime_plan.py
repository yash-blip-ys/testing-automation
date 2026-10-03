"""Offline tests for runtime planning from a single natural-language objective.

Covers the refactor from hand-written `test_goal.steps` to one objective:

  * a one-sentence objective works with no steps array
  * the runtime plan is generated and reaches the navigator context
  * verified sub-goals survive replanning
  * an attempted but unverified action does not complete a sub-goal
  * missing or ambiguous evidence cannot produce a false PASS
  * explicit configured steps behave exactly as before

No browser, no model. Run with:
    .\\venv311\\Scripts\\python.exe -m unittest tests.test_runtime_plan -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae


OBJECTIVE = "Add the backpack to the cart, then complete checkout"


class TestObjectiveSplitting(unittest.TestCase):
    def test_connectives_split_an_objective_into_requirements(self):
        parts = ae.split_objective(OBJECTIVE)
        self.assertEqual(len(parts), 2)
        self.assertIn("Add the backpack to the cart", parts[0])
        self.assertIn("complete checkout", parts[1])

    def test_single_clause_yields_one_requirement(self):
        self.assertEqual(len(ae.split_objective("Log in as a standard user")), 1)

    def test_semicolon_split(self):
        parts = ae.split_objective("Open the cart; pay for the order")
        self.assertEqual(len(parts), 2)

    def test_empty_objective_yields_nothing(self):
        self.assertEqual(ae.split_objective(""), [])
        self.assertEqual(ae.split_objective(None), [])

    def test_split_is_capped(self):
        long_objective = ", ".join(f"do thing number {i}" for i in range(30))
        self.assertLessEqual(
            len(ae.split_objective(long_objective)), ae.RUNTIME_PLAN_MAX_SUBGOALS
        )

    def test_normalisation_is_stable(self):
        self.assertEqual(
            ae.normalise_requirement("Open the Cart!"),
            ae.normalise_requirement("open   the cart"),
        )


class TestEvidenceDerivation(unittest.TestCase):
    def test_element_observation_becomes_evidence(self):
        """A control whose label reports real state IS evidence."""
        ev = ae._derive_evidence_from_observation(
            "add the backpack to the cart",
            ["Add to cart", "Cart, 1 items", "Open Menu"],
            "https://shop.test/inventory.html", "Products",
        )
        self.assertIn("element_present", ev)

    def test_action_affordance_is_not_evidence_of_completion(self):
        """The circular case.

        A "Checkout" control on screen says the checkout can be started, not
        that it was completed. Deriving element_present from a label named
        after the action let a live run mark "complete checkout" verified while
        still sitting on the inventory page.
        """
        ev = ae._derive_evidence_from_observation(
            "complete checkout",
            ["Cart, 2 items", "Checkout", "Continue Shopping"],
            "https://shop.test/inventory.html", "Products",
        )
        self.assertEqual(ev, {},
                         "a bare action label must not verify a completion claim")

    def test_state_carrying_label_still_verifies_an_add(self):
        """The non-circular counterpart must keep working."""
        ev = ae._derive_evidence_from_observation(
            "add the backpack to the cart",
            ["Add to cart", "Cart, 2 items"],
            "https://shop.test/inventory.html", "Products",
        )
        self.assertEqual(ev.get("element_present"), ['text="Cart, 2 items"'])

    def test_navigate_still_accepts_control_presence(self):
        """Arriving at a target page is a real state change, so navigate
        requirements keep using presence."""
        ev = ae._derive_evidence_from_observation(
            "open the cart",
            ["Cart", "Open Menu"],
            "https://shop.test/cart", "Your cart",
        )
        self.assertIn("element_present", ev)

    def test_url_literal_becomes_evidence(self):
        ev = ae._derive_evidence_from_observation(
            "open the receipt",
            ["Back", "Help"], "https://shop.test/receipt.html", "Thanks",
        )
        self.assertEqual(ev.get("url_contains"), ["receipt"])

    def test_completion_claim_is_not_verified_by_its_own_noun_in_the_url(self):
        """Being on a checkout URL is not having completed the checkout.

        The literal fallback used to return url_contains=["checkout"], which
        matched /checkout-step-one.html as readily as /checkout-complete.html.
        A live run therefore marked "complete checkout" verified the moment the
        agent entered the flow. There is no generic positive signal for "the
        action finished", so the requirement must be reported unverifiable.
        """
        for url in ("https://shop.test/checkout-step-one.html",
                    "https://shop.test/checkout-complete.html"):
            ev = ae._derive_evidence_from_observation(
                "complete checkout", ["Continue", "Finish"], url, "Checkout",
            )
            self.assertEqual(ev, {},
                             f"naming the flow in the URL must not verify {url}")

    def test_navigate_literal_still_becomes_evidence(self):
        """Arriving at a target page is a real state change, so navigate keeps
        its literal fallback."""
        ev = ae._derive_evidence_from_observation(
            "open the receipt",
            ["Back", "Help"], "https://shop.test/receipt.html", "Thanks",
        )
        self.assertEqual(ev.get("url_contains"), ["receipt"])
        for literal in ev["url_contains"]:
            self.assertIn(literal, "https://shop.test/receipt.html")

    def test_abstract_requirement_yields_no_evidence(self):
        ev = ae._derive_evidence_from_observation(
            "make the experience delightful",
            ["Back", "Help"], "https://shop.test/", "Welcome",
        )
        self.assertEqual(ev, {},
                         "an unobservable requirement must derive no evidence")

    def test_no_verb_yields_no_evidence(self):
        ev = ae._derive_evidence_from_observation(
            "the sky",
            ["Back"], "https://shop.test/", "text",
        )
        self.assertEqual(ev, {})

    def test_dismissive_ui_only_requirement(self):
        ev = ae._derive_evidence_from_observation(
            "discover the catalog",
            ["All Items", "Add to cart"], "https://shop.test/", "Products",
        )
        self.assertEqual(ev, {})


class TestPlanSeeding(unittest.TestCase):
    def _goal(self, objective=OBJECTIVE):
        return ae.TestGoal({"objective": objective, "evidence": {}})

    def test_objective_without_steps_creates_a_plan(self):
        goal = self._goal()
        self.assertFalse(goal.uses_configured_steps())
        plan = goal.ensure_runtime_plan(["Add to cart", "Cart, empty"], "u", "t")
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.items), 2)

    def test_plan_is_seeded_only_once(self):
        goal = self._goal()
        goal.ensure_runtime_plan(["Add to cart"], "u", "t")
        seeded = [i["requirement"] for i in goal.runtime_plan.items]
        self.assertEqual(len(seeded), 2)
        # A later call on a different page must not re-seed.
        goal.ensure_runtime_plan(["Buy Bike Light", "Cart, empty"], "u2", "t2")
        self.assertEqual(
            [i["requirement"] for i in goal.runtime_plan.items], seeded,
            "re-seeding would silently discard verified requirements",
        )

    def test_reset_allows_a_fresh_plan(self):
        goal = self._goal()
        goal.ensure_runtime_plan(["Add to cart"], "u", "t")
        goal.reset_progress()
        self.assertEqual(goal.runtime_plan.items, [])
        goal.ensure_runtime_plan(["Add to cart"], "u", "t")
        self.assertEqual(len(goal.runtime_plan.items), 2)

    def test_configured_steps_suppress_the_runtime_plan(self):
        goal = ae.TestGoal({
            "objective": OBJECTIVE,
            "steps": [{"describe": "Open the cart", "evidence": {"url_contains": ["cart"]}}],
        })
        self.assertTrue(goal.uses_configured_steps())
        self.assertIsNone(goal.ensure_runtime_plan(["Cart"], "u", "t"))


class TestPlanVerification(unittest.TestCase):
    """Requirement 5 and 10: only observations complete a sub-goal."""

    def _goal_with_plan(self, objective=OBJECTIVE):
        goal = ae.TestGoal({"objective": objective, "evidence": {}})
        goal.ensure_runtime_plan(["Add to cart", "Complete checkout"], "u", "t")
        return goal

    def _remaining(self, goal, url="u", text="t", elements=None, changed=False):
        return goal.remaining_work(
            page_state={"available_elements": elements or []},
            page_text=text, url=url, structural_changed=changed,
        )

    def test_nothing_is_done_on_a_fresh_page(self):
        goal = self._goal_with_plan()
        rows = self._remaining(goal)
        self.assertTrue(all(not r["done"] for r in rows))

    def test_observed_state_completes_its_subgoal(self):
        """A control reporting real state confirms the requirement."""
        goal = ae.TestGoal({"objective": "Add the backpack to the cart",
                            "evidence": {}})
        rows = goal.remaining_work(
            page_state={"available_elements": ["Add to cart", "Cart, 1 items"]},
            page_text="Products", url="https://shop.test/inventory.html",
            structural_changed=True,
        )
        self.assertTrue(rows[0]["done"],
                        "an observed cart quantity confirms the requirement")

    def test_unstarted_action_control_does_not_complete_subgoal(self):
        """A bare affordance is not progress.

        This is the guard against the live regression where "complete checkout"
        was satisfied by the Checkout button merely existing.
        """
        goal = ae.TestGoal({"objective": "Complete checkout",
                            "evidence": {}})
        rows = goal.remaining_work(
            page_state={"available_elements": ["Cart, 2 items", "Checkout"]},
            page_text="Products", url="https://shop.test/cart",
            structural_changed=True,
        )
        self.assertFalse(rows[0]["done"],
                         "the Checkout control alone must not complete checkout")
        self.assertTrue(rows[0]["unverifiable"])

    def test_attempted_but_unverified_action_completes_nothing(self):
        """The regression guard: acting is not observing."""
        goal = ae.TestGoal({"objective": "Open the receipt page",
                            "evidence": {}})
        for _ in range(10):
            rows = goal.remaining_work(
                page_state={"available_elements": ["Back", "Help"]},
                page_text="Home", url="https://shop.test/",
                structural_changed=True,
            )
        self.assertTrue(all(not r["done"] for r in rows),
                        "attempting must never complete a sub-goal")

    def test_absent_evidence_is_reported_unverifiable(self):
        goal = ae.TestGoal({"objective": "make it delightful",
                            "evidence": {}})
        rows = goal.remaining_work(
            page_state={"available_elements": ["Back"]},
            page_text="Hi", url="https://shop.test/", structural_changed=False,
        )
        self.assertFalse(rows[0]["done"])
        self.assertTrue(rows[0]["unverifiable"])
        self.assertFalse(rows[0]["verifiable"])

    def test_unverifiable_subgoal_cannot_make_the_goal_pass(self):
        goal = ae.TestGoal({"objective": "make it delightful",
                            "evidence": {}})
        status, evidence = goal.evaluate(
            page_state={"available_elements": ["Back"]},
            page_text="Hi", url="https://shop.test/", structural_changed=True,
            steps_taken=9,
        )
        self.assertNotEqual(status, ae.GOAL_PASS,
                            "an unverifiable requirement must never pass the goal")
        self.assertEqual(status, ae.GOAL_BLOCKED)

    def test_no_configured_evidence_cannot_pass_from_a_click_alone(self):
        """Requirement 10: the model cannot invent success evidence."""
        goal = ae.TestGoal({"objective": "reach the thank-you page",
                            "evidence": {}})
        status, _ = goal.evaluate(
            page_state={"available_elements": ["Continue"]},
            page_text="Checkout", url="https://shop.test/checkout",
            structural_changed=True, steps_taken=5,
        )
        self.assertEqual(status, ae.GOAL_BLOCKED)
        self.assertIn("no conclusive evidence", _)

    def test_verified_evidence_latches_across_navigation(self):
        goal = ae.TestGoal({"objective": "Open the receipt page",
                            "evidence": {}})
        goal.remaining_work(
            page_state={"available_elements": ["Open Receipt"]},
            page_text="t", url="https://shop.test/receipt.html",
            structural_changed=True,
        )
        rows = goal.remaining_work(
            page_state={"available_elements": ["Home"]},
            page_text="Home", url="https://shop.test/", structural_changed=False,
        )
        self.assertTrue(rows[0]["done"],
                        "verified work must not be undone by navigating away")


class TestReplanning(unittest.TestCase):
    """Requirement 6: replan without losing verified requirements."""

    def _verified_plan(self):
        goal = ae.TestGoal({"objective": OBJECTIVE, "evidence": {}})
        goal.ensure_runtime_plan(["Add to cart", "Complete checkout"],
                                 "https://shop.test/", "t")
        # The first requirement is confirmed by an OBSERVED STATE change: the
        # cart label now reports a quantity. A bare "Add to cart" control is
        # not evidence that anything was added.
        goal.remaining_work(
            page_state={"available_elements": ["Add to cart", "Cart, 1 items"]},
            page_text="t", url="https://shop.test/", structural_changed=True,
        )
        return goal

    def test_verified_subgoal_survives_a_different_plan(self):
        goal = self._verified_plan()
        done_before = [
            r["requirement"] for r in goal.remaining_work(
                page_state={"available_elements": ["Add to cart", "Complete checkout"]},
                page_text="t", url="https://shop.test/", structural_changed=True,
            ) if r["done"]
        ]
        self.assertTrue(done_before, "precondition: something is verified")

        # Replan to a completely different route.
        goal.runtime_plan.adopt_model_plan(
            ["reach the receipt", "confirm the total"],
            elements=["Receipt", "Confirm"], url="https://shop.test/",
            page_text="t",
        )
        rows = goal.remaining_work(
            page_state={"available_elements": ["Receipt", "Confirm"]},
            page_text="t", url="https://shop.test/", structural_changed=True,
        )
        self.assertTrue(any(r["done"] for r in rows),
                        "verified requirements must survive replanning")
        self.assertEqual(goal.runtime_plan.replans, 1)

    def test_replanning_does_not_reverify_anything(self):
        """A replan must not turn unverified work into verified work."""
        goal = ae.TestGoal({"objective": OBJECTIVE, "evidence": {}})
        goal.ensure_runtime_plan(["Add to cart", "Complete checkout"],
                                 "https://shop.test/", "t")
        goal.runtime_plan.adopt_model_plan(
            ["reach the receipt"], elements=["Back"], url="https://shop.test/",
            page_text="t",
        )
        rows = goal.remaining_work(
            page_state={"available_elements": ["Back"]},
            page_text="t", url="https://shop.test/", structural_changed=True,
        )
        self.assertTrue(all(not r["done"] for r in rows),
                        "adopting a plan verifies nothing by itself")

    def test_retired_requirement_is_not_proposed_again(self):
        goal = ae.TestGoal({"objective": OBJECTIVE, "evidence": {}})
        plan = goal.ensure_runtime_plan(["Add to cart"], "https://shop.test/", "t")
        plan.retire(plan.items[0]["requirement"])
        plan.adopt_model_plan(
            [plan.items[0]["requirement"], "complete checkout"],
            elements=["Complete checkout"], url="https://shop.test/", page_text="t",
        )
        proposed = [ae.normalise_requirement(i["requirement"])
                    for i in plan.items]
        self.assertNotIn(
            ae.normalise_requirement("Add the backpack to the cart"), proposed
        )

    def test_adopting_an_empty_plan_is_refused(self):
        goal = ae.TestGoal({"objective": OBJECTIVE, "evidence": {}})
        plan = goal.ensure_runtime_plan(["Add to cart"], "u", "t")
        before = [i["requirement"] for i in plan.items]
        self.assertFalse(plan.adopt_model_plan([], elements=["Add to cart"],
                                               url="u", page_text="t"))
        self.assertEqual([i["requirement"] for i in plan.items], before,
                         "an empty proposal must not wipe the plan")

    def test_reset_clears_verified_and_proposal(self):
        goal = self._verified_plan()
        goal.reset_progress()
        self.assertEqual(goal.runtime_plan._verified, {},
                         "reset must forget every verified requirement")
        # A plan is re-seeded from the objective on the next observation.
        rows = goal.remaining_work(
            page_state={"available_elements": ["Back", "Help"]},
            page_text="t", url="https://shop.test/", structural_changed=False,
        )
        self.assertTrue(all(not r["done"] for r in rows))


class TestNavigatorPlanContext(unittest.TestCase):
    """Requirement 3: the plan reaches the navigator."""

    def _prompt(self, remaining_work, proposed):
        captured = {}

        def fake_chat(model, messages):
            captured["p"] = messages[0]["content"]
            return {"message": {"content":
                                '{"best_choice": "Add to cart", '
                                '"ranked_backup": [], "reasoning": "x", '
                                '"safety_tags": {"Add to cart": 0}}'}}

        with mock.patch.object(ae.ollama, "chat", fake_chat):
            ae.ask_ai_navigator(
                ["Add to cart"], "objective", [],
                mechanical_safety_tags={}, remaining_work=remaining_work,
                runtime_plan_proposed=proposed,
            )
        return captured["p"]

    def test_plan_appears_in_the_prompt(self):
        goal = ae.TestGoal({"objective": OBJECTIVE, "evidence": {}})
        rows = goal.remaining_work(
            page_state={"available_elements": ["Add to cart"]},
            page_text="t", url="u", structural_changed=False,
        )
        prompt = self._prompt(rows, proposed=True)
        self.assertIn("RUNTIME PLAN", prompt)
        self.assertIn("Add the backpack to the cart", prompt)
        self.assertIn("complete checkout", prompt)

    def test_configured_steps_keep_the_original_heading(self):
        goal = ae.TestGoal({
            "objective": OBJECTIVE,
            "steps": [{"describe": "Open the cart", "evidence": {"url_contains": ["cart"]}}],
        })
        rows = goal.remaining_work(
            page_state={"available_elements": ["Cart"]}, page_text="t",
            url="https://shop.test/cart.html", structural_changed=False,
        )
        prompt = self._prompt(rows, proposed=False)
        self.assertIn("REMAINING WORK", prompt)
        self.assertNotIn("RUNTIME PLAN", prompt)

    def test_unverifiable_subgoal_is_flagged_in_the_prompt(self):
        goal = ae.TestGoal({"objective": "make it delightful",
                            "evidence": {}})
        rows = goal.remaining_work(
            page_state={"available_elements": ["Back"]}, page_text="t",
            url="u", structural_changed=False,
        )
        prompt = self._prompt(rows, proposed=True)
        self.assertIn("UNVERIFIABLE", prompt)

    def test_plan_is_marked_revisable(self):
        goal = ae.TestGoal({"objective": OBJECTIVE, "evidence": {}})
        rows = goal.remaining_work(
            page_state={"available_elements": ["Add to cart"]},
            page_text="t", url="u", structural_changed=False,
        )
        prompt = self._prompt(rows, proposed=True)
        self.assertIn("it may", prompt)


class TestBackwardCompatibility(unittest.TestCase):
    """Requirement 8: explicit steps behave exactly as before."""

    STEPS_GOAL = {
        "objective": "Buy the thing.",
        "steps": [
            {"describe": "Open the cart", "evidence": {"url_contains": ["cart"]}},
            {"describe": "Start checkout", "evidence": {"url_contains": ["checkout-step-one"]}},
        ],
        "evidence": {"text_contains": ["Thank you"]},
    }

    def _remaining(self, url):
        return ae.TestGoal(self.STEPS_GOAL).remaining_work(
            page_state={"available_elements": ["Cart"]}, page_text="t",
            url=url, structural_changed=False,
        )

    def test_configured_steps_still_track_progress(self):
        rows = self._remaining("https://x.test/cart.html")
        done = [r["describe"] for r in rows if r["done"]]
        self.assertEqual(done, ["Open the cart"])

    def test_configured_steps_index_starts_at_one(self):
        rows = self._remaining("https://x.test/inventory.html")
        self.assertEqual([r["index"] for r in rows], [1, 2])

    def test_configured_steps_expose_no_unverifiable_flag(self):
        rows = self._remaining("https://x.test/inventory.html")
        self.assertFalse(any(r.get("unverifiable") for r in rows))

    def test_configured_steps_keep_final_evidence_gate(self):
        goal = ae.TestGoal(self.STEPS_GOAL)
        status, _ = goal.evaluate(
            page_state={"available_elements": ["Cart"]}, page_text="Thank you",
            url="https://x.test/checkout-complete.html",
            structural_changed=True, steps_taken=5,
        )
        self.assertEqual(status, ae.GOAL_PASS)

    def test_legacy_config_without_steps_or_evidence(self):
        goal = ae.TestGoal({"objective": "do the thing"})
        rows = goal.remaining_work(
            page_state={"available_elements": []}, page_text="t", url="u",
            structural_changed=False,
        )
        self.assertIsInstance(rows, list)
        self.assertTrue(all("describe" in r for r in rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)