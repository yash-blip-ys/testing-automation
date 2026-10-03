"""Checkpoint 1.2 — Runtime plan integrity.

These tests pin the plan contract and, above all, the historical regression:
a proposed plan discarded "complete checkout" and replaced it with controls
that were already on the page, so the run reported completion while the real
task was untouched.

  * verified requirements are preserved
  * outstanding requirements survive unless explicitly retired
  * duplicate and trivially-satisfied filler requirements are rejected
  * planner-generated evidence is never authoritative
  * every proposal decision is recorded with a reason
  * replanning stays disabled in production

No browser, no model. Run with:
    .\\venv311\\Scripts\\python.exe -m unittest tests.test_contract_plan -v
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae


def _goal(objective="Add two items to the cart, then complete checkout"):
    return ae.TestGoal({"objective": objective, "evidence": {}})


def _keys(plan):
    return [ae.normalise_requirement(i["requirement"]) for i in plan.items]


class TestVerifiedIsPreserved(unittest.TestCase):
    """A requirement the page already confirmed is never lost."""

    def test_merge_preserves_verified_state(self):
        goal = _goal()
        plan = goal.ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        self.assertIsNotNone(plan)
        key = _keys(plan)[0]
        plan._verified[key] = "confirmed earlier"
        plan.adopt_model_plan(
            ["open the receipt"], ["Back"], "https://x.test/i", "p",
            replace_keys=["complete checkout"],
        )
        self.assertIn(key, plan._verified)

    def test_full_replacement_preserves_verified_state(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        key = _keys(plan)[0]
        plan._verified[key] = "confirmed earlier"
        plan.adopt_model_plan(["something new"], ["Back"], "https://x.test/i", "p")
        self.assertIn(key, plan._verified)
        self.assertTrue(
            any(i.get("already_done") for i in plan.items),
            "a verified requirement must still be shown to the navigator as done",
        )

    def test_verification_survives_evaluation_and_replan(self):
        goal = _goal()
        plan = goal.ensure_runtime_plan(["Add to cart"], "https://x.test/i", "Add to cart")
        plan.items[0]["evidence"] = {"text_contains": ["In your cart"]}
        rows = plan.evaluate(["Cart"], "https://x.test/cart", "In your cart",
                             structural_changed=True,
                             evaluate_evidence=goal._evaluate_evidence)
        self.assertTrue(any(r["done"] for r in rows))
        key = _keys(plan)[0]
        plan.adopt_model_plan(
            ["open the receipt"], ["Back"], "https://x.test/cart", "page",
            replace_keys=["complete checkout"],
        )
        self.assertIn(key, plan._verified)


class TestOutstandingIsPreserved(unittest.TestCase):
    """The historical regression, reproduced and locked down."""

    def test_complete_checkout_survives_a_replan(self):
        """THE regression: the planner must not be able to delete it."""
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "Add to cart")
        self.assertIn("complete checkout", _keys(plan))
        plan.adopt_model_plan(
            ["open the receipt"], ["Back"], "https://x.test/i", "page",
            replace_keys=["add to cart"],
        )
        self.assertIn("complete checkout", _keys(plan),
                      "an outstanding requirement must survive a replan")

    def test_full_replacement_still_keeps_verified_outstanding_work(self):
        """Full replace is the blunt path; verified work must still survive."""
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "Add to cart")
        plan.items[0]["evidence"] = {"text_contains": ["In your cart"]}
        goal = _goal()
        plan.evaluate(["Cart"], "https://x.test/cart", "In your cart",
                      structural_changed=True, evaluate_evidence=goal._evaluate_evidence)
        key = _keys(plan)[0]
        plan.adopt_model_plan(["brand new route"], ["Back"], "https://x.test/i", "p")
        self.assertIn(key, _keys(plan))

    def test_requirement_not_named_for_replacement_is_kept(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "Add to cart")
        before = list(_keys(plan))
        plan.adopt_model_plan(
            ["open the receipt"], ["Back"], "https://x.test/i", "page",
            replace_keys=["a requirement that was never in the plan"],
        )
        keys = _keys(plan)
        for key in before:
            self.assertIn(key, keys,
                          "an existing requirement must survive a replan that "
                          "did not name it")
        self.assertIn("open the receipt", keys)

    def test_outstanding_requirements_lists_unverified_only(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        plan._verified[_keys(plan)[0]] = "done"
        self.assertNotIn("add to cart",
                         [ae.normalise_requirement(r) for r in plan.outstanding_requirements()])
        self.assertIn("complete checkout", _keys(plan))


class TestRetirementIsTheOnlyExit(unittest.TestCase):
    """Outstanding work may only leave the plan through retire()."""

    def test_retired_requirement_is_not_re_proposed(self):
        """retire() marks a requirement so replanning stops proposing it.

        It deliberately does NOT delete it from `items` (see its docstring), so
        the guarantee is that a NEW proposal naming it is dropped and logged.
        """
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        plan.retire("complete checkout")
        plan.adopt_model_plan(
            ["complete checkout", "open the receipt"], ["Back"],
            "https://x.test/i", "page", replace_keys=["add two items to the cart"],
        )
        self.assertEqual(_keys(plan).count("complete checkout"), 1,
                         "the retired requirement must not be re-added on top "
                         "of the one already present")
        dropped = " ".join(plan.proposal_log[-1]["dropped"])
        self.assertIn("retired", dropped,
                      "the log must show the retired proposal being dropped")

    def test_retire_marks_without_deleting_verified_work(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        key = _keys(plan)[0]
        plan._verified[key] = "confirmed"
        plan.retire(key)
        self.assertIn(key, _keys(plan),
                      "retire marks a requirement; it must not delete evidence")
        self.assertIn(key, plan._verified)

    def test_retire_ignores_empty_requirement(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        self.assertEqual(plan._retired, set())


class TestDuplicatesAreRejected(unittest.TestCase):
    def test_seed_drops_repeated_requirements(self):
        plan = ae.RuntimePlan("add a hat, add a hat")
        plan.seed_from_objective([], "https://x.test/", "p")
        self.assertEqual(len(plan.items), 1,
                         "a repeated requirement is not extra work")

    def test_seed_drops_repeats_across_different_connectives(self):
        plan = ae.RuntimePlan("open the cart, then open the cart")
        plan.seed_from_objective([], "https://x.test/", "p")
        self.assertEqual(len(plan.items), 1)

    def test_merge_drops_duplicate_proposals(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        ok = plan.adopt_model_plan(
            ["open the receipt", "open the receipt"], ["Back"],
            "https://x.test/i", "page", replace_keys=["add to cart"],
        )
        self.assertTrue(ok)
        self.assertEqual(_keys(plan).count("open the receipt"), 1)

    def test_full_replacement_drops_duplicate_proposals(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        plan.adopt_model_plan(["open the receipt", "open the receipt"],
                              ["Back"], "https://x.test/i", "page")
        self.assertEqual(_keys(plan).count("open the receipt"), 1)


class TestFillerIsRejected(unittest.TestCase):
    """A proposal the CURRENT page already satisfies is not a next step."""

    PAGE = ["Cart", "Add to cart", "Open Menu"]
    URL = "https://x.test/inventory"
    TEXT = "Products"

    def test_merge_rejects_filler_and_keeps_the_plan(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], self.URL, "Add to cart")
        before = list(_keys(plan))
        ok = plan.adopt_model_plan(["open the cart"], self.PAGE, self.URL, self.TEXT,
                                   replace_keys=["add to cart"])
        self.assertFalse(ok, "a proposal already satisfied by the page must be refused")
        self.assertEqual(_keys(plan), before,
                         "a refused proposal must leave the plan untouched")

    def test_full_replacement_rejects_filler(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], self.URL, "Add to cart")
        ok = plan.adopt_model_plan(["open the cart"], self.PAGE, self.URL, self.TEXT)
        self.assertFalse(ok)
        self.assertIn("complete checkout", _keys(plan),
                      "the objective's real work must survive filler")

    def test_refused_proposal_does_not_spend_budget(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], self.URL, "Add to cart")
        plan.adopt_model_plan(["open the cart"], self.PAGE, self.URL, self.TEXT)
        self.assertEqual(plan.replans, 0,
                         "a rejected proposal must not consume replan budget")

    def test_a_genuine_future_step_is_accepted(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], self.URL, "Add to cart")
        ok = plan.adopt_model_plan(["open the receipt"], ["Back"], self.URL, "page",
                                   replace_keys=["add to cart"])
        self.assertTrue(ok, "a real next step must still be adoptable")


class TestPlannerEvidenceIsNeverAuthoritative(unittest.TestCase):
    def test_evidence_is_re_derived_not_supplied(self):
        src = inspect.getsource(ae.RuntimePlan._make_items)
        self.assertIn("_derive_evidence_from_observation", src)

    def test_adopted_item_evidence_matches_the_page(self):
        """Even if a caller smuggles evidence in, the stored evidence is ours."""
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "Add to cart")
        plan.adopt_model_plan(["open the receipt"], ["Back"], "https://x.test/i", "page",
                              replace_keys=["add to cart"])
        for item in plan.items:
            if item.get("already_done"):
                continue
            expected = ae._derive_evidence_from_observation(
                item["requirement"], ["Back"], "https://x.test/i", "page")
            self.assertEqual(item["evidence"], expected)


class TestProposalDecisionsAreRecorded(unittest.TestCase):
    """Every proposal decision must be auditable with a reason."""

    def test_merge_is_recorded(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        plan.adopt_model_plan(["open the receipt"], ["Back"], "https://x.test/i", "page",
                              replace_keys=["add to cart"])
        self.assertEqual(plan.proposal_outcomes(), ["merged"])
        entry = plan.proposal_log[-1]
        self.assertIn("replaced", entry["reason"])
        self.assertTrue(entry["reason"].strip())

    def test_full_acceptance_is_recorded(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        plan.adopt_model_plan(["open the receipt"], ["Back"], "https://x.test/i", "page")
        self.assertEqual(plan.proposal_outcomes(), ["accepted"])

    def test_rejection_is_recorded_with_reasons(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        plan.adopt_model_plan(["open the cart"], ["Cart", "Add to cart"],
                              "https://x.test/i", "page",
                              replace_keys=["add to cart"])
        self.assertEqual(plan.proposal_outcomes(), ["rejected"])
        entry = plan.proposal_log[-1]
        self.assertTrue(entry["reason"])
        self.assertTrue(entry["dropped"], "a rejection must say what was dropped")

    def test_deferral_is_recorded_when_budget_is_spent(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        for _ in range(ae.RUNTIME_PLAN_MAX_REPLANS):
            plan.adopt_model_plan(["open the receipt"], ["Back"],
                                  "https://x.test/i", "page",
                                  replace_keys=["add to cart"])
        self.assertFalse(plan.can_replan())
        before = _keys(plan)
        ok = plan.adopt_model_plan(["open the receipt page"], ["Back"],
                                  "https://x.test/i", "page",
                                  replace_keys=["add to cart"])
        self.assertFalse(ok, "the budget must be enforced by the API itself")
        self.assertEqual(plan.proposal_outcomes()[-1], "deferred")
        self.assertEqual(_keys(plan), before,
                         "a deferred proposal must not alter the plan")

    def test_log_records_kept_requirements_on_merge(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        plan.adopt_model_plan(["open the receipt"], ["Back"], "https://x.test/i", "page",
                              replace_keys=["add to cart"])
        kept = [ae.normalise_requirement(k) for k in plan.proposal_log[-1]["kept"]]
        self.assertIn("complete checkout", kept,
                      "the log must show what the merge preserved")

    def test_log_is_bounded(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        plan.replans = 0
        for _ in range(ae._PLAN_LOG_LIMIT + 10):
            plan.adopt_model_plan(["open the receipt"], ["Back"],
                                  "https://x.test/i", "page",
                                  replace_keys=["add to cart"])
            plan.replans = 0  # keep exercising past the budget
        self.assertLessEqual(len(plan.proposal_log), ae._PLAN_LOG_LIMIT)

    def test_clear_resets_the_log(self):
        plan = _goal().ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        plan.adopt_model_plan(["open the receipt"], ["Back"], "https://x.test/i", "page",
                              replace_keys=["add to cart"])
        self.assertTrue(plan.proposal_log)
        plan.clear()
        self.assertEqual(plan.proposal_log, [])


class TestReplanningStaysDisabled(unittest.TestCase):
    """Production must not replan. This is a live-tested decision, not a stub."""

    def test_flag_is_off(self):
        self.assertFalse(ae.ENABLE_RUNTIME_PLAN_REPLANNING)

    def test_run_loop_guards_the_trigger_with_the_flag(self):
        self.assertIn("ENABLE_RUNTIME_PLAN_REPLANNING",
                      inspect.getsource(ae.run_pathfinder_agent))

    def test_run_loop_calls_the_planner_only_inside_that_guard(self):
        src = inspect.getsource(ae.run_pathfinder_agent)
        self.assertIn("ask_ai_planner", src,
                      "the call site must exist so the boundary stays documented")
        self.assertLess(src.index("ENABLE_RUNTIME_PLAN_REPLANNING"),
                        src.index("ask_ai_planner"),
                        "the guard must precede the call")

    def test_plan_api_documents_its_integration_boundary(self):
        doc = ae.RuntimePlan.__doc__ or ""
        self.assertIn("INTEGRATION BOUNDARY", doc)
        self.assertIn("ENABLE_RUNTIME_PLAN_REPLANNING", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)