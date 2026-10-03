"""Part 4.4 — Shadow replanning evaluation.

Shadow mode exists to answer "would replanning help?" without the one unsafe
way of finding out — turning it on against a real site. These tests pin the
isolation that makes it safe to gather that evidence at all:

  * evaluation cannot mutate the plan, its verification, or its replan budget
  * a shadow result cannot reach the browser, the goal, or the final status
  * the log is bounded
  * page data is reduced before it is recorded, so no secret is copied
  * shadow mode is gated separately from production replanning, so measuring
    replanning does not enable it

Run with:
    .\\venv311\\Scripts\\python.exe -m unittest tests.test_shadow_replan -v
"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae

OBJECTIVE = "Add two items to the cart, then complete checkout."
ELEMENTS = ["Open Menu", "Add to cart", "Add to cart #2", "Cart, empty"]
URL = "https://shop.test/inventory"


def _plan():
    plan = ae.RuntimePlan(OBJECTIVE)
    plan.seed_from_objective(ELEMENTS, URL, "page")
    return plan


def _fingerprint(plan):
    """Everything about a plan that must not change during evaluation."""
    return (
        [dict(i) for i in plan.items],
        dict(plan._verified),
        dict(plan._verified_at),
        set(plan._retired),
        plan.replans,
        plan.objective,
        list(plan.proposal_log),
    )


class TestShadowEvaluationCannotMutate(unittest.TestCase):

    def setUp(self):
        self.plan = _plan()
        self.before = _fingerprint(self.plan)

    def test_would_adopt_leaves_the_plan_untouched(self):
        for proposal in (["open the menu"], ["open the payment page"],
                         ["bypass the login"], [], ["x" * 400], None):
            self.plan.would_adopt(proposal, ELEMENTS, URL, "page",
                                  replace_keys=["complete checkout"])
        self.assertEqual(_fingerprint(self.plan), self.before,
                         "shadow evaluation must not change the plan in any way")

    def test_would_adopt_does_not_consume_the_replan_budget(self):
        for _ in range(20):
            self.plan.would_adopt(["open the payment page"], ELEMENTS, URL,
                                  "page", replace_keys=["complete checkout"])
        self.assertEqual(self.plan.replans, 0,
                         "shadow evaluation must not spend replan budget")

    def test_would_adopt_records_no_proposal_log_entry(self):
        self.plan.would_adopt(["open the payment page"], ELEMENTS, URL, "page",
                              replace_keys=["complete checkout"])
        self.assertEqual(self.plan.proposal_log, [],
                         "shadow evaluation must not write to the audit log")

    def test_evaluator_leaves_the_plan_untouched(self):
        ev = ae.ShadowEvaluator(enabled=True)
        for _ in range(10):
            ev.evaluate(self.plan, ["open the payment page"], step=1,
                        elements=ELEMENTS, url=URL, page_text="page",
                        replace_keys=["complete checkout"])
        self.assertEqual(_fingerprint(self.plan), self.before)

    def test_the_returned_plan_cannot_be_used_to_mutate_the_plan(self):
        """A caller holding the result must not be able to reach back in."""
        ev = ae.ShadowEvaluator(enabled=True)
        record = ev.evaluate(self.plan, ["open the payment page"], step=1,
                             elements=ELEMENTS, url=URL, page_text="page",
                             replace_keys=["complete checkout"])
        record["plan_after"].append("injected requirement")
        record["proposed"].append("also injected")
        self.assertEqual(_fingerprint(self.plan), self.before,
                         "results must be plain copies, not live references")

    def test_repeated_evaluation_is_deterministic(self):
        ev = ae.ShadowEvaluator(enabled=True)
        first = copy.deepcopy(ev.evaluate(
            self.plan, ["open the payment page"], step=1, elements=ELEMENTS,
            url=URL, page_text="page", replace_keys=["complete checkout"]))
        for _ in range(5):
            again = ev.evaluate(self.plan, ["open the payment page"], step=1,
                                elements=ELEMENTS, url=URL, page_text="page",
                                replace_keys=["complete checkout"])
            self.assertEqual(again, first)


class TestShadowResultsCannotAffectExecution(unittest.TestCase):

    def test_the_evaluator_holds_no_page_and_no_state_manager(self):
        ev = ae.ShadowEvaluator(enabled=True)
        state = vars(ev)
        for forbidden in ("page", "locator", "state_mgr", "edge_weights",
                          "test_goal"):
            self.assertNotIn(forbidden, state,
                             f"shadow mode must not hold {forbidden!r}")

    def test_the_evaluator_source_contains_no_browser_calls(self):
        import inspect
        for fn in (ae.ShadowEvaluator.evaluate, ae.shadow_page_facts):
            src = inspect.getsource(fn)
            for forbidden in ("await ", "click", "goto", "page.url",
                              "fill(", "snapshot"):
                self.assertNotIn(forbidden, src,
                                 f"{fn.__name__} must not contain {forbidden!r}")

    def test_evaluation_cannot_change_goal_status(self):
        """Verification is decided by configured evidence alone.

        Shadow evaluation produces no requirement rows, so there is nothing for
        the goal gate to consume — asserted structurally by checking the record
        carries no verified/evidence fields that could be mistaken for one.
        """
        ev = ae.ShadowEvaluator(enabled=True)
        record = ev.evaluate(_plan(), ["open the payment page"], step=1,
                             elements=ELEMENTS, url=URL, page_text="page",
                             replace_keys=["complete checkout"])
        self.assertNotIn("goal_status", record)
        self.assertNotIn("goal_evidence", record)
        self.assertNotIn("final_status", record)

    def test_shadow_is_gated_separately_from_adoption(self):
        """Measuring replanning must never be a back door to enabling it.

        Both flags being False is the correct current state. What matters is
        that they are two independent switches: nothing in the shadow path sets,
        reads, or defaults from the production one.
        """
        self.assertFalse(ae.ENABLE_RUNTIME_PLAN_REPLANNING)
        self.assertFalse(ae.ENABLE_SHADOW_REPLANNING)
        import inspect
        shadow_src = (inspect.getsource(ae.ShadowEvaluator)
                      + inspect.getsource(ae.shadow_page_facts)
                      + inspect.getsource(ae.RuntimePlan.would_adopt))
        self.assertNotIn("ENABLE_RUNTIME_PLAN_REPLANNING", shadow_src,
                         "the shadow path must not read the production flag")

    def test_the_shadow_flag_is_its_own_module_constant(self):
        import inspect
        src = inspect.getsource(ae)
        self.assertIn("ENABLE_SHADOW_REPLANNING = False", src)
        self.assertIn("ENABLE_RUNTIME_PLAN_REPLANNING = False", src)

    def test_the_adoption_branch_still_requires_the_production_flag(self):
        import inspect
        src = inspect.getsource(ae.run_pathfinder_agent)
        adopt_at = src.index("_plan.adopt_model_plan(")
        guard = src.rindex("if ENABLE_RUNTIME_PLAN_REPLANNING", 0, adopt_at)
        self.assertGreater(guard, 0,
                           "adoption must still sit behind its own flag")

    def test_shadow_evaluation_runs_even_when_adoption_is_impossible(self):
        """The whole point: observe without being able to act."""
        plan = _plan()
        plan.replans = ae.RUNTIME_PLAN_MAX_REPLANS  # budget spent
        self.assertFalse(plan.can_replan())
        ev = ae.ShadowEvaluator(enabled=True)
        record = ev.evaluate(plan, ["open the payment page"], step=1,
                             elements=ELEMENTS, url=URL, page_text="page",
                             replace_keys=["complete checkout"])
        self.assertIn("would_adopt", record)
        self.assertEqual(plan.replans, ae.RUNTIME_PLAN_MAX_REPLANS)


class TestShadowLogIsBounded(unittest.TestCase):

    def test_the_log_cannot_grow_without_limit(self):
        ev = ae.ShadowEvaluator(enabled=True, log_limit=5)
        for i in range(50):
            ev.evaluate(_plan(), [f"open page {i}"], step=i,
                        elements=ELEMENTS, url=URL, page_text="page")
        self.assertLessEqual(len(ev.records), 5)

    def test_the_limit_default_is_a_finite_constant(self):
        self.assertIsInstance(ae.SHADOW_LOG_LIMIT, int)
        self.assertGreater(ae.SHADOW_LOG_LIMIT, 0)
        self.assertLess(ae.SHADOW_LOG_LIMIT, 200,
                        "a shadow log is a diagnostic, not an archive")

    def test_a_disabled_evaluator_stores_nothing(self):
        ev = ae.ShadowEvaluator(enabled=False)
        for i in range(10):
            record = ev.evaluate(_plan(), [f"open page {i}"], step=i,
                                 elements=ELEMENTS, url=URL, page_text="page")
            self.assertIn("would_adopt", record,
                          "the verdict is still computed for callers and tests")
        self.assertEqual(ev.records, [],
                         "a disabled evaluator must not accumulate state")
        self.assertEqual(ev.proposals_seen, 0)


class TestShadowLogHoldsNoSensitivePageData(unittest.TestCase):
    """A diagnostic log must not become a second copy of the page."""

    SECRET = "hunter2-SUPER-SECRET"

    def _record(self, **kwargs):
        ev = ae.ShadowEvaluator(enabled=True)
        plan = _plan()
        return ev.evaluate(plan, ["open the payment page"], step=1,
                           elements=kwargs.get("elements", ELEMENTS),
                           url=kwargs.get("url", URL),
                           page_text=kwargs.get("page_text", "page"),
                           replace_keys=["complete checkout"])

    def test_a_password_in_page_text_is_not_recorded(self):
        record = self._record(page_text=f"Welcome back. Password: {self.SECRET}")
        self.assertNotIn(self.SECRET, json.dumps(record))

    def test_a_secret_query_parameter_is_not_recorded(self):
        record = self._record(url=f"https://shop.test/cart?token={self.SECRET}")
        self.assertNotIn(self.SECRET, json.dumps(record))

    def test_a_secret_in_an_element_label_is_not_recorded_beyond_the_cap(self):
        """Labels are sampled, not copied wholesale."""
        leaky = ELEMENTS + [f"password {self.SECRET}"] * 50
        record = self._record(elements=leaky)
        blob = json.dumps(record)
        self.assertNotIn(self.SECRET, blob)
        self.assertEqual(record["page"]["element_count"], len(leaky),
                         "the count must survive even though content does not")
        self.assertGreater(record["page"]["elements_elided"], 0,
                           "the elision must be reported, not silent")

    def test_the_url_is_reduced_to_shape(self):
        record = self._record(url="https://shop.test/a/b/c?session=abc123")
        self.assertEqual(record["page"]["url_origin"], "https://shop.test")
        self.assertEqual(record["page"]["url_path_depth"], 3)
        self.assertNotIn("session", json.dumps(record))
        self.assertNotIn("abc123", json.dumps(record))

    def test_page_text_is_recorded_only_as_a_length(self):
        record = self._record(page_text="y" * 4321)
        self.assertEqual(record["page"]["page_text_chars"], 4321)
        self.assertNotIn("yyy", json.dumps(record))

    def test_sensitive_markers_are_refused_by_the_observation_layer(self):
        """Belt and braces: the engine already withholds these before here."""
        marker = ae.hash_form_value(self.SECRET)
        self.assertNotIn(self.SECRET, marker)
        self.assertTrue(ae.is_sensitive_field("password"))

    def test_a_malformed_url_does_not_crash_the_summariser(self):
        record = self._record(url="not a url at all :::")
        self.assertIn("url_origin", record["page"])


class TestShadowRecordsWhyItWouldBeRejected(unittest.TestCase):

    def test_a_filler_proposal_is_recorded_as_refused_with_a_reason(self):
        ev = ae.ShadowEvaluator(enabled=True)
        record = ev.evaluate(_plan(), ["open the menu"], step=1,
                             elements=ELEMENTS, url=URL, page_text="page",
                             replace_keys=["complete checkout"])
        self.assertFalse(record["would_adopt"])
        self.assertTrue(record["reason"])
        self.assertTrue(record["dropped"])

    def test_filler_is_flagged_even_when_the_proposal_is_refused(self):
        """A proposal dropped FOR BEING filler is the clearest evidence of it.

        Reporting it as filler-free would erase precisely the signal shadow
        mode exists to collect.
        """
        ev = ae.ShadowEvaluator(enabled=True)
        record = ev.evaluate(_plan(), ["open the menu"], step=1,
                             elements=ELEMENTS, url=URL, page_text="page",
                             replace_keys=["complete checkout"])
        self.assertTrue(record["introduces_filler"])

    def test_a_lost_requirement_is_reported_rather_than_hidden(self):
        """The historical failure mode, made measurable without adopting."""
        ev = ae.ShadowEvaluator(enabled=True)
        record = ev.evaluate(_plan(), ["open the payment page"], step=1,
                             elements=ELEMENTS, url=URL, page_text="page",
                             replace_keys=["complete checkout"])
        self.assertTrue(record["would_adopt"])
        self.assertFalse(record["preserves_outstanding"])
        self.assertTrue(record["lost_outstanding"],
                        "work the proposal would have dropped must be named")

    def test_a_proposal_replacing_nothing_loses_nothing(self):
        """A proposal that replaces nothing must preserve everything."""
        ev = ae.ShadowEvaluator(enabled=True)
        record = ev.evaluate(_plan(), ["open the payment page"], step=1,
                             elements=ELEMENTS, url=URL, page_text="page",
                             replace_keys=[])
        self.assertTrue(record["would_adopt"])
        self.assertEqual(record["lost_outstanding"], [])
        self.assertEqual(record["lost_verified"], [])
        self.assertTrue(record["preserves_outstanding"])

    def test_a_verified_requirement_is_reported_as_lost_when_displaced(self):
        """Shadow mode must make the known gap measurable, not hide it.

        `replace_keys` is trusted by the caller, so displacing a verified
        requirement is reported here rather than silently absorbed.
        """
        plan = _plan()
        key = ae.normalise_requirement(plan.items[0]["requirement"])
        plan._verified[key] = "text=\"Cart, 2 items\""
        ev = ae.ShadowEvaluator(enabled=True)
        record = ev.evaluate(plan, ["open the payment page"], step=1,
                             elements=ELEMENTS, url=URL, page_text="page",
                             replace_keys=[plan.items[0]["requirement"]])
        self.assertIn(key, record["lost_verified"],
                      "displaced verification must be reported")
        self.assertFalse(record["preserves_verified"])

    def test_extra_model_calls_are_counted_for_honest_cost_accounting(self):
        ev = ae.ShadowEvaluator(enabled=True)
        for _ in range(3):
            ev.evaluate(_plan(), ["open the payment page"], step=1,
                        elements=ELEMENTS, url=URL, page_text="page")
        self.assertEqual(ev.extra_model_calls, 3)
        self.assertIn("3 extra model call", ev.summary())

    def test_the_summary_states_nothing_was_adopted(self):
        ev = ae.ShadowEvaluator(enabled=True)
        ev.evaluate(_plan(), ["open the payment page"], step=1,
                    elements=ELEMENTS, url=URL, page_text="page",
                    replace_keys=["complete checkout"])
        self.assertIn("without adopting", ev.summary())

    def test_the_summary_reports_a_disabled_evaluator_honestly(self):
        self.assertIn("disabled", ae.ShadowEvaluator(enabled=False).summary())


class TestShadowHandlesAbsentInput(unittest.TestCase):

    def test_no_proposal_is_handled(self):
        ev = ae.ShadowEvaluator(enabled=True)
        record = ev.evaluate(_plan(), None, step=1, elements=ELEMENTS,
                             url=URL, page_text="page")
        self.assertFalse(record["would_adopt"])
        self.assertTrue(record["reason"])

    def test_no_plan_is_handled(self):
        ev = ae.ShadowEvaluator(enabled=True)
        record = ev.evaluate(None, ["open the payment page"], step=1,
                             elements=ELEMENTS, url=URL, page_text="page")
        self.assertFalse(record["would_adopt"])
        self.assertIn("no runtime plan", record["reason"])

    def test_an_empty_plan_is_handled(self):
        ev = ae.ShadowEvaluator(enabled=True)
        record = ev.evaluate(ae.RuntimePlan(OBJECTIVE),
                             ["open the payment page"], step=1,
                             elements=ELEMENTS, url=URL, page_text="page")
        self.assertIn("would_adopt", record)


if __name__ == "__main__":
    unittest.main()