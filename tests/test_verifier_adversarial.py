"""Checkpoint 6.7.4 — adversarial evidence verification.

A verifier that can be fooled by a banner is worse than no verifier, because
its output is what the user acts on. Every case here attacks the evidence
layer with a page that is designed to look like success:

  * decoy text — a footer that claims the order was placed on EVERY page;
  * URL-only evidence — the success page happens to be the landing page;
  * any-of vs all-of — one incidental fragment out of three;
  * circular evidence — the "Checkout" button proving the checkout was done;
  * presence vs outcome — a control existing before and after the action;
  * a populated field vs a committed transaction;
  * UI-only change — a drawer opening;
  * latching — evidence confirmed once must not resurrect, and an attempt
    must never latch;
  * no evidence at all must be BLOCKED, never PASS and never FAIL.

No browser is required: `TestGoal.evaluate` takes plain page state, which is
exactly the contract a fixture could otherwise bypass.
"""

import unittest

import automation_engine as ae


def _state(elements=None, form_values=None, **extra):
    state = {"available_elements": list(elements or [])}
    if form_values is not None:
        state["semantic_signals"] = {"form_values": form_values}
    state.update(extra)
    return state


def _goal(evidence, **extra):
    goal = {"objective": "x", "evidence": evidence}
    goal.update(extra)
    return ae.TestGoal(goal)


class DecoyTextTests(unittest.TestCase):
    """A page that always claims success must not be able to prove success."""

    def test_a_permanent_banner_does_not_prove_anything(self):
        """The transactional fixture prints an order reference in the footer of
        every page, including the order form. Any-of text evidence would be
        satisfied before the order is ever placed."""
        banner = "Order confirmed. Order reference: ORD-9931"
        goal = _goal({"text_contains": ["Order reference: ORD-9931"]})
        status, _summary = goal.evaluate(_state(), banner,
                                         "https://shop.test/order", True)
        # Any-of DOES match here — which is exactly why all-of and
        # application-state evidence exist. The point of this test is to pin
        # the weakness so it can never be mistaken for a strength.
        self.assertEqual(status, ae.GOAL_PASS)

    def test_all_of_rejects_a_page_that_merely_mentions_the_thing(self):
        banner = "Order confirmed. Order reference: ORD-9931"
        goal = _goal({"text_contains_all": [
            "Order reference: ORD-9931",
            "Delivery scheduled for Thursday",
        ]})
        status, summary = goal.evaluate(_state(), banner,
                                        "https://shop.test/order", True)
        self.assertEqual(status, ae.GOAL_FAIL)
        self.assertIn("partially matched", summary)

    def test_all_of_rejects_an_empty_page(self):
        goal = _goal({"text_contains_all": ["Order reference: ORD-9931"]})
        status, _ = goal.evaluate(_state(), "", "https://shop.test/", True)
        self.assertNotEqual(status, ae.GOAL_PASS)

    def test_a_negative_clause_overrides_a_positive_one(self):
        """A page showing both a success banner and an error is not a success.
        Contradiction must win, or an error page with boilerplate passes."""
        goal = _goal({
            "text_contains_all": ["Order confirmed"],
            "text_not_contains": ["error"],
        })
        status, _ = goal.evaluate(
            _state(), "Order confirmed. Payment error: card declined.",
            "https://shop.test/order", True)
        self.assertEqual(status, ae.GOAL_FAIL)


class UrlOnlyTests(unittest.TestCase):
    """Being on a URL is not having done the thing that leads to it."""

    def test_a_navigation_claim_needs_an_observed_navigation(self):
        goal = _goal({"navigate": ["/confirmation"]})
        # Already on the URL, but the run never navigated there.
        status, summary = goal.evaluate(
            _state(navigation_observed=False), "",
            "https://shop.test/confirmation", True)
        self.assertNotEqual(status, ae.GOAL_PASS)
        self.assertIn("no navigation", summary)

    def test_a_navigation_claim_is_satisfied_by_an_observed_navigation(self):
        goal = _goal({"navigate": ["/confirmation"]})
        status, _ = goal.evaluate(
            _state(navigation_observed=True), "",
            "https://shop.test/confirmation", True)
        self.assertEqual(status, ae.GOAL_PASS)

    def test_url_contains_all_rejects_one_incidental_fragment(self):
        goal = _goal({"url_contains_all": ["shop.test", "checkout", "payment"]})
        status, summary = goal.evaluate(
            _state(), "", "https://shop.test/checkout", True)
        self.assertEqual(status, ae.GOAL_FAIL)
        self.assertIn("partially matched", summary)

    def test_url_contains_any_of_is_available_but_is_the_weak_test(self):
        """Pinned so the weaker clause is a documented choice, not an accident.
        The benchmark specs deliberately use the _all variants."""
        goal = _goal({"url_contains_all": ["shop.test", "checkout", "payment"]})
        with_any = _goal({"url_contains": ["shop.test", "checkout", "payment"]})
        state, url = _state(), "https://shop.test/checkout"
        self.assertEqual(ae.GOAL_FAIL, goal.evaluate(state, "", url, True)[0])
        self.assertEqual(ae.GOAL_PASS, with_any.evaluate(state, "", url, True)[0])


class PresenceIsNotOutcomeTests(unittest.TestCase):
    """The distinction the whole evidence layer exists to keep."""

    def test_a_present_control_proves_an_affordance_only(self):
        elements = ['button:has-text("Checkout")']
        goal = _goal({"element_present": ['button:has-text("Checkout")']})
        status, _ = goal.evaluate(_state(elements), "", "https://shop.test/",
                                  True)
        # It matches — but only as PRESENCE. That is precisely why the benchmark
        # oracle refuses to accept element presence as proof of an outcome.
        self.assertEqual(status, ae.GOAL_PASS)
        goal._last_observed_elements = list(elements)
        # Every clause already met => no unmet-evidence text, so the navigator
        # is not sent chasing it again.
        self.assertEqual(goal.unmet_final_evidence_text("", "https://shop.test/"),
                         "")
        # With the control gone, the same clause is reported as NOT MET.
        goal._last_observed_elements = []
        self.assertIn("NOT MET",
                      goal.unmet_final_evidence_text("", "https://shop.test/"))

    def test_a_missing_control_produces_no_evidence_at_all(self):
        goal = _goal({"element_present": ['button:has-text("Checkout")']})
        status, summary = goal.evaluate(_state([]), "", "https://shop.test/",
                                         True)
        self.assertEqual(status, ae.GOAL_BLOCKED)
        self.assertIn("no conclusive evidence", summary)

    def test_an_element_selector_cannot_match_by_incidental_substring(self):
        elements = ['div:has-text("Checkout policy")']
        goal = _goal({"element_present": ['button:has-text("Checkout")']})
        status, _ = goal.evaluate(_state(elements), "", "https://shop.test/",
                                  True)
        self.assertNotEqual(status, ae.GOAL_PASS)


class FormValueIsNotATransactionTests(unittest.TestCase):
    def test_a_populated_field_does_not_commit_anything(self):
        goal = _goal({"form_value": {"#email": "a@example.test"}})
        values = {"#email": ae.hash_form_value("a@example.test")}
        status, _ = goal.evaluate(_state([], form_values=values), "",
                                  "https://shop.test/order", True)
        self.assertEqual(status, ae.GOAL_PASS)
        # ...but the value is only ever compared as a digest.
        self.assertNotEqual(values["#email"], "a@example.test")

    def test_an_unobserved_field_cannot_be_confirmed(self):
        goal = _goal({"form_value": {"#email": "a@example.test"}})
        status, summary = goal.evaluate(_state([], form_values={}), "",
                                        "https://shop.test/order", True)
        self.assertNotEqual(status, ae.GOAL_PASS)
        self.assertIn("not observed", summary)

    def test_a_wrong_value_is_a_contradiction(self):
        goal = _goal({"form_value": {"#email": "a@example.test"}})
        values = {"#email": ae.hash_form_value("someone.else@example.test")}
        status, summary = goal.evaluate(_state([], form_values=values), "",
                                        "https://shop.test/order", True)
        self.assertEqual(status, ae.GOAL_FAIL)
        self.assertIn("mismatch", summary)

    def test_a_sensitive_field_is_checked_by_presence_not_by_value(self):
        """A sensitive field carries a presence marker, never a digest, so an
        exact-value check is impossible by design."""
        goal = _goal({"form_value": {"card": "4111111111111111"}})
        values = {"card": ae.SENSITIVE_PRESENT}
        status, _ = goal.evaluate(_state([], form_values=values), "",
                                  "https://shop.test/pay", True)
        self.assertEqual(status, ae.GOAL_PASS)
        # An absent sensitive field is a contradiction, not a match.
        values = {"card": ae.SENSITIVE_ABSENT}
        status, _ = goal.evaluate(_state([], form_values=values), "",
                                  "https://shop.test/pay", True)
        self.assertEqual(status, ae.GOAL_FAIL)


class UiOnlyChangeTests(unittest.TestCase):
    def test_an_opening_drawer_is_not_task_progress(self):
        goal = _goal({"state_changed": True})
        status, summary = goal.evaluate(
            _state(ui_only_changed=True), "", "https://shop.test/", True)
        self.assertEqual(status, ae.GOAL_FAIL)
        self.assertIn("UI-only", summary)

    def test_a_real_transition_is_progress(self):
        goal = _goal({"state_changed": True})
        status, _ = goal.evaluate(
            _state(ui_only_changed=False), "", "https://shop.test/next", True)
        self.assertEqual(status, ae.GOAL_PASS)

    def test_no_change_at_all_is_no_evidence(self):
        goal = _goal({"state_changed": True})
        status, _ = goal.evaluate(_state(), "", "https://shop.test/", False)
        self.assertEqual(status, ae.GOAL_BLOCKED)


class StepFloorTests(unittest.TestCase):
    def test_evidence_before_the_step_floor_does_not_pass(self):
        goal = _goal({"text_contains_all": ["Done"], "steps_min": 3})
        status, summary = goal.evaluate(_state(), "Done", "https://x.test/",
                                        True, steps_taken=1)
        self.assertEqual(status, ae.GOAL_BLOCKED)
        self.assertIn("step floor", summary)

    def test_the_step_floor_is_satisfied_at_the_boundary(self):
        goal = _goal({"text_contains_all": ["Done"], "steps_min": 3})
        status, _ = goal.evaluate(_state(), "Done", "https://x.test/", True,
                                  steps_taken=3)
        self.assertEqual(status, ae.GOAL_PASS)

    def test_a_malformed_step_floor_is_ignored_not_fatal(self):
        goal = _goal({"text_contains_all": ["Done"], "steps_min": "many"})
        status, _ = goal.evaluate(_state(), "Done", "https://x.test/", True)
        self.assertEqual(status, ae.GOAL_PASS)


class LatchingTests(unittest.TestCase):
    """Evidence latches; an attempt never can."""

    def test_an_attempt_alone_never_completes_a_step(self):
        goal = ae.TestGoal({
            "objective": "x",
            "steps": [{"describe": "Place the order",
                       "evidence": {"text_contains_all": ["Order confirmed"]}}],
        })
        rows = goal.remaining_work(_state(), "", "https://shop.test/", True)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["done"])
        self.assertFalse(rows[0]["verified"])

    def test_observed_evidence_completes_a_step(self):
        goal = ae.TestGoal({
            "objective": "x",
            "steps": [{"describe": "Place the order",
                       "evidence": {"text_contains_all": ["Order confirmed"]}}],
        })
        rows = goal.remaining_work(_state(), "Order confirmed",
                                  "https://shop.test/", True)
        self.assertTrue(rows[0]["verified"])

    def test_verified_evidence_latches_and_is_not_resurrected(self):
        goal = ae.TestGoal({
            "objective": "x",
            "steps": [{"describe": "Add to cart",
                       "evidence": {"text_contains_all": ["In your cart"]}}],
        })
        first = goal.remaining_work(_state(), "In your cart",
                                   "https://shop.test/", True)
        self.assertTrue(first[0]["verified"])
        # Navigate away: the evidence is gone from the page, but the step was
        # genuinely confirmed and must not become outstanding again.
        second = goal.remaining_work(_state(), "",
                                    "https://shop.test/elsewhere", True)
        self.assertTrue(second[0]["done"])
        self.assertTrue(second[0]["verified"])

    def test_a_contradicted_step_is_not_done(self):
        goal = ae.TestGoal({
            "objective": "x",
            "steps": [{"describe": "Sign in",
                       "evidence": {"text_contains_all": ["Welcome back"],
                                    "text_not_contains": ["error"]}}],
        })
        rows = goal.remaining_work(_state(), "Welcome back. error: bad password",
                                  "https://shop.test/login", True)
        self.assertFalse(rows[0]["done"])
        self.assertTrue(rows[0]["contradicted"])


class NoEvidenceTests(unittest.TestCase):
    """The three-way verdict must be honest when the page says nothing."""

    def test_no_configured_goal_is_blocked_not_passed(self):
        goal = ae.TestGoal({"objective": ""})
        status, summary = goal.evaluate(_state(), "anything", "https://x.test/",
                                        True)
        self.assertEqual(status, ae.GOAL_BLOCKED)
        self.assertIn("no goal", summary)

    def test_an_empty_evidence_block_is_blocked(self):
        goal = _goal({})
        status, summary = goal.evaluate(_state(), "Order confirmed",
                                        "https://x.test/", True)
        self.assertEqual(status, ae.GOAL_BLOCKED)
        self.assertIn("no conclusive evidence", summary)

    def test_evidence_never_implies_the_other_verdicts(self):
        self.assertEqual(len({ae.GOAL_PASS, ae.GOAL_FAIL, ae.GOAL_BLOCKED}), 3)

    def test_only_pass_maps_to_a_pass_outcome(self):
        self.assertEqual(ae.classify_final_status("GOAL_EVIDENCE_FAILED"),
                         ae.OUTCOME_FAIL)
        passing = [s for s, o in ae._STATUS_OUTCOMES.items() if o == ae.OUTCOME_PASS]
        self.assertEqual(passing, ["SUCCESS_TARGET_REACHED"])


class UnverifiableRowsTests(unittest.TestCase):
    """A requirement nothing can observe is reported, not quietly dropped."""

    def test_a_step_with_no_evidence_is_marked_unverifiable(self):
        goal = ae.TestGoal({
            "objective": "x",
            "steps": [{"describe": "Feel satisfied"}],
        })
        rows = goal.remaining_work(_state(), "", "https://x.test/", True)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["verifiable"])
        self.assertFalse(rows[0]["done"])


if __name__ == "__main__":
    unittest.main()