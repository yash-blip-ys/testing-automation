"""Checkpoints 3.1-3.4 — evidence, verification, and honest completion.

Grouped as:
  3.1  evidence inventory: what each clause does and does not prove
  3.2  freshness and independence: stale, circular and unrelated evidence
  3.3  objective-to-requirement mapping, including its lexical limits
  3.4  the final status contract and the report contract
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae


def goal(evidence, objective="do the thing"):
    return ae.TestGoal({"objective": objective, "evidence": evidence})


def state(elements=(), ui_only=False, navigation_observed=True, signals=None):
    return {
        "available_elements": list(elements),
        "ui_only_changed": ui_only,
        "navigation_observed": navigation_observed,
        "semantic_signals": signals or {},
    }


# ===========================================================================
# 3.1 Evidence inventory
# ===========================================================================

class TestEvidenceSemantics(unittest.TestCase):
    """Each clause's strength, pinned so the documentation cannot drift."""

    def evaluate(self, evidence, *, text="", url="", elements=(),
                 ui_only=False, navigation_observed=True, signals=None,
                 steps_taken=0, structural_changed=True):
        return goal(evidence).evaluate(
            page_state=state(elements, ui_only, navigation_observed, signals),
            page_text=text, url=url, structural_changed=structural_changed,
            steps_taken=steps_taken)

    # -- element_present ---------------------------------------------------

    def test_element_present_confirms_presence_only(self):
        status, _ = self.evaluate(
            {"element_present": ['text="Checkout"']}, elements=["Checkout"])
        self.assertEqual(status, ae.GOAL_PASS)

    def test_element_present_absent_is_not_a_pass(self):
        status, reason = self.evaluate({"element_present": ['text="Checkout"']},
                                       elements=[])
        self.assertEqual(status, ae.GOAL_BLOCKED)
        self.assertIn("no conclusive evidence", reason)

    def test_element_present_does_not_match_a_different_control(self):
        status, _ = self.evaluate(
            {"element_present": ['text="Checkout"']}, elements=["Cart"])
        self.assertEqual(status, ae.GOAL_BLOCKED)

    # -- url_contains ------------------------------------------------------

    def test_url_contains_confirms_the_fragment(self):
        status, _ = self.evaluate({"url_contains": ["checkout-complete"]},
                                  url="https://x.test/checkout-complete")
        self.assertEqual(status, ae.GOAL_PASS)

    def test_url_contains_is_any_of(self):
        status, _ = self.evaluate({"url_contains": ["checkout", "receipt"]},
                                  url="https://x.test/receipt")
        self.assertEqual(status, ae.GOAL_PASS)

    def test_url_contains_all_requires_every_fragment(self):
        ev = {"url_contains_all": ["checkout", "complete"]}
        status, _ = self.evaluate(ev, url="https://x.test/checkout-complete")
        self.assertEqual(status, ae.GOAL_PASS)

    def test_url_contains_all_partial_match_contradicts(self):
        ev = {"url_contains_all": ["checkout", "complete"]}
        status, reason = self.evaluate(ev, url="https://x.test/checkout")
        self.assertEqual(status, ae.GOAL_FAIL,
                         "a partial match must not read as success")
        self.assertIn("partially matched", reason)

    def test_misleading_url_substring_is_a_known_weakness(self):
        """Documented, not fixed: a short fragment can match incidentally.

        This test exists so the limitation cannot be quietly forgotten, and so
        that if it is ever tightened the test is the thing that changes.
        """
        status, _ = self.evaluate({"url_contains": ["cart"]},
                                  url="https://x.test/cartoon-store")
        self.assertEqual(status, ae.GOAL_PASS,
                         "known weakness: 'cart' matches '/cartoon-store'")

    # -- text_contains -----------------------------------------------------

    def test_text_contains_confirms_the_phrase(self):
        status, _ = self.evaluate({"text_contains": ["Thank you for your order"]},
                                  text="Thank you for your order!")
        self.assertEqual(status, ae.GOAL_PASS)

    def test_text_contains_is_any_of(self):
        status, _ = self.evaluate({"text_contains": ["a", "b"]}, text="b")
        self.assertEqual(status, ae.GOAL_PASS)

    def test_text_contains_all_requires_every_phrase(self):
        ev = {"text_contains_all": ["order total", "thank you"]}
        status, _ = self.evaluate(ev, text="thank you — order total: $10")
        self.assertEqual(status, ae.GOAL_PASS)

    def test_text_contains_all_partial_match_contradicts(self):
        ev = {"text_contains_all": ["order total", "thank you"]}
        status, reason = self.evaluate(ev, text="order total: $10")
        self.assertEqual(status, ae.GOAL_FAIL)
        self.assertIn("partially matched", reason)

    def test_text_evidence_cannot_tell_where_a_phrase_appeared(self):
        """Unrelated content is a real weakness of substring text evidence.

        The phrase in a footer is indistinguishable from the phrase in the
        order summary. `text_contains_all` narrows it but cannot localise it,
        so the limitation is recorded in every report instead of hidden.
        """
        status, _ = self.evaluate(
            {"text_contains": ["Thank you for your order"]},
            text="footer blurb: Thank you for your order")
        self.assertEqual(status, ae.GOAL_PASS,
                         "known weakness: text is not localised")

    # -- form_value --------------------------------------------------------

    def test_form_value_confirms_a_matching_digest(self):
        ev = {"form_value": {"#zip": "90210"}}
        signals = {"form_values": {"#zip": ae.hash_form_value("90210")}}
        status, _ = self.evaluate(ev, signals=signals)
        self.assertEqual(status, ae.GOAL_PASS)

    def test_form_value_mismatch_does_not_pass(self):
        ev = {"form_value": {"#zip": "90210"}}
        signals = {"form_values": {"#zip": ae.hash_form_value("00000")}}
        status, reason = self.evaluate(ev, signals=signals)
        self.assertEqual(status, ae.GOAL_FAIL)
        self.assertIn("mismatch", reason)

    def test_unobserved_field_cannot_pass(self):
        status, reason = self.evaluate({"form_value": {"#zip": "90210"}})
        self.assertEqual(status, ae.GOAL_FAIL)
        self.assertIn("was not observed", reason)

    def test_sensitive_field_confirms_presence_only(self):
        ev = {"form_value": {"#password": "anything"}}
        signals = {"form_values": {"#password": ae.SENSITIVE_PRESENT}}
        status, evidence = self.evaluate(ev, signals=signals)
        self.assertEqual(status, ae.GOAL_PASS)
        self.assertIn("populated", evidence)

    def test_form_value_never_reveals_the_value(self):
        ev = {"form_value": {"#zip": "90210"}}
        signals = {"form_values": {"#zip": ae.hash_form_value("90210")}}
        _status, evidence = self.evaluate(ev, signals=signals)
        self.assertNotIn("90210", evidence)

    def test_populated_field_is_not_a_completed_transaction(self):
        """A filled form is an input, not an outcome."""
        ev = {"form_value": {"#zip": "90210"}}
        signals = {"form_values": {"#zip": ae.hash_form_value("90210")}}
        status, _ = self.evaluate(ev, signals=signals)
        self.assertEqual(status, ae.GOAL_PASS)
        # ...and the goal still cannot claim submission, because nothing
        # observed a submission.
        self.assertNotIn("submitted", str(self.evaluate(ev, signals=signals)))

    # -- navigate ----------------------------------------------------------

    def test_navigate_passes_when_this_run_actually_navigated(self):
        status, _ = self.evaluate({"navigate": ["checkout"]},
                                  url="https://x.test/checkout",
                                  navigation_observed=True)
        self.assertEqual(status, ae.GOAL_PASS)

    def test_navigate_fails_when_the_url_predates_the_run(self):
        """The whole point: arriving is not the same as having travelled."""
        status, reason = self.evaluate({"navigate": ["checkout"]},
                                       url="https://x.test/checkout",
                                       navigation_observed=False)
        self.assertEqual(status, ae.GOAL_FAIL)
        self.assertIn("no navigation", reason)

    def test_navigate_is_stricter_than_url_contains(self):
        """Same URL, different verdict — the difference is provenance."""
        g = goal({"navigate": ["checkout"]})
        page = state(navigation_observed=False)
        nav = g.evaluate(page, "", "https://x.test/checkout", True)
        g2 = goal({"url_contains": ["checkout"]})
        urlc = g2.evaluate(state(navigation_observed=False), "",
                           "https://x.test/checkout", True)
        self.assertNotEqual(urlc[0], ae.GOAL_FAIL)
        self.assertEqual(nav[0], ae.GOAL_FAIL)

    # -- state_changed and steps_min ---------------------------------------

    def test_state_changed_is_satisfied_by_a_real_transition(self):
        status, _ = self.evaluate({"state_changed": True},
                                  structural_changed=True)
        self.assertEqual(status, ae.GOAL_PASS)

    def test_state_changed_refuses_a_ui_only_transition(self):
        status, reason = self.evaluate({"state_changed": True},
                                       structural_changed=True,
                                       ui_only=True)
        self.assertEqual(status, ae.GOAL_FAIL)
        self.assertIn("UI-only", reason)

    def test_steps_min_is_a_gate_and_never_evidence(self):
        status, reason = self.evaluate({"steps_min": 3}, steps_taken=1)
        self.assertEqual(status, ae.GOAL_BLOCKED)
        self.assertNotEqual(status, ae.GOAL_PASS)

    def test_steps_min_cannot_create_a_pass_alone(self):
        status, _ = self.evaluate({"steps_min": 3}, steps_taken=99)
        self.assertEqual(status, ae.GOAL_BLOCKED,
                         "reaching the floor proves nothing by itself")

    def test_steps_min_holds_back_evidence_until_reached(self):
        ev = {"url_contains": ["done"], "steps_min": 5}
        status, reason = self.evaluate(ev, url="https://x.test/done",
                                       steps_taken=2)
        self.assertEqual(status, ae.GOAL_BLOCKED)
        self.assertIn("step floor", reason)

    def test_steps_min_releases_real_evidence_once_reached(self):
        ev = {"url_contains": ["done"], "steps_min": 5}
        status, _ = self.evaluate(ev, url="https://x.test/done", steps_taken=5)
        self.assertEqual(status, ae.GOAL_PASS)

    # -- negative clauses --------------------------------------------------

    def test_text_not_contains_can_fail_on_its_own(self):
        status, reason = self.evaluate({"text_not_contains": ["Error"]},
                                       text="Error: payment declined")
        self.assertEqual(status, ae.GOAL_FAIL)
        self.assertIn("violated", reason)

    def test_absence_of_an_error_is_not_proof_of_success(self):
        status, _ = self.evaluate({"text_not_contains": ["Error"]},
                                  text="Everything is fine")
        self.assertEqual(status, ae.GOAL_BLOCKED,
                         "'no error' must never be reported as success")


class TestEvidenceInventoryCompleteness(unittest.TestCase):

    def test_every_documented_clause_is_reachable(self):
        """Each clause named in the module docstring is actually evaluated."""
        import inspect
        src = inspect.getsource(ae.TestGoal._evaluate_evidence)
        for clause in ("url_contains", "text_contains", "element_present",
                       "form_value", "navigate", "state_changed", "steps_min",
                       "text_not_contains", "element_absent",
                       "url_contains_all", "text_contains_all"):
            with self.subTest(clause=clause):
                self.assertIn(f'"{clause}"', src)

    def test_no_final_evidence_means_no_pass(self):
        g = ae.TestGoal({"objective": "x"})
        self.assertEqual(g.evaluate(state(), "", "", True)[0], ae.GOAL_BLOCKED)


# ===========================================================================
# 3.2 Freshness and independence
# ===========================================================================

class TestEvidenceFreshness(unittest.TestCase):

    def test_evidence_is_re_evaluated_per_page(self):
        """The same clause matches on one page and fails on the next."""
        g = goal({"url_contains": ["done"]})
        first = g.evaluate(state(), "", "https://x.test/done", True)
        second = g.evaluate(state(), "", "https://x.test/other", True)
        self.assertEqual(first[0], ae.GOAL_PASS)
        self.assertEqual(second[0], ae.GOAL_BLOCKED,
                         "a clause satisfied earlier must not carry over")

    def test_stale_page_text_cannot_verify_a_new_page(self):
        g = goal({"text_contains": ["Order complete"]})
        status, _ = g.evaluate(state(), "Totally different page", "", True)
        self.assertEqual(status, ae.GOAL_BLOCKED)

    def test_verification_records_where_it_was_observed(self):
        plan = ae.RuntimePlan("add an item")
        plan._verified["req"] = "text_contains matched"
        plan._verified_at["req"] = {
            "url": "https://x.test/cart", "evidence": "text_contains matched"}
        rows = plan.verification_provenance("https://x.test/checkout")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["observed_on_url"], "https://x.test/cart")
        self.assertFalse(rows[0]["still_current"],
                         "a latch must not masquerade as present-tense evidence")

    def test_provenance_marks_evidence_still_on_the_current_page(self):
        plan = ae.RuntimePlan("add an item")
        plan._verified_at["req"] = {"url": "https://x.test/cart", "evidence": "e"}
        rows = plan.verification_provenance("https://x.test/cart")
        self.assertTrue(rows[0]["still_current"])

    def test_clearing_the_plan_clears_provenance(self):
        plan = ae.RuntimePlan("x")
        plan._verified["req"] = "e"
        plan._verified_at["req"] = {"url": "u", "evidence": "e"}
        plan.clear()
        self.assertEqual(plan._verified_at, {})


class TestEvidenceIndependence(unittest.TestCase):

    def test_plan_completion_alone_cannot_produce_a_pass(self):
        """The runtime plan is a proposal; only configured evidence decides."""
        g = ae.TestGoal({"objective": "do it", "evidence": {}})
        plan = ae.RuntimePlan("do it")
        plan.items = [{"requirement": "do it", "evidence": {}}]
        plan._verified["do it"] = "text_contains matched"
        rows = plan.evaluate(["Do it"], "https://x.test/", "do it", True,
                             evaluate_evidence=g._evaluate_evidence)
        self.assertTrue(all(r["done"] for r in rows))
        self.assertEqual(g.evaluate(state(), "do it", "https://x.test/", True)[0],
                         ae.GOAL_BLOCKED,
                         "a fully-satisfied plan with no final evidence is "
                         "not a completion claim")

    def test_a_requirement_cannot_be_its_own_evidence(self):
        """An action control is an affordance, not an outcome."""
        ev = ae._derive_evidence_from_observation(
            "complete checkout", ["Checkout", "Finish"], "https://x.test/cart",
            "Checkout Finish")
        self.assertEqual(ev, {},
                         "'the Checkout button exists' must not verify checkout")

    def test_option_index_is_not_a_quantity(self):
        """Regression: 'Add to cart #2' once satisfied 'add two items'."""
        self.assertFalse(ae._label_carries_state("Add to cart #2"))
        self.assertIsNone(ae._label_count("Add to cart #2"))

    def test_count_evidence_requires_the_stated_quantity(self):
        ev = ae._derive_evidence_from_observation(
            "add two items to the cart", ["Cart, 1 items"], "u", "Cart, 1 items")
        self.assertEqual(ev, {},
                         "one item in the cart must not satisfy 'two items'")

    def test_count_evidence_accepts_the_right_quantity(self):
        ev = ae._derive_evidence_from_observation(
            "add two items to the cart", ["Cart, 2 items"], "u", "Cart, 2 items")
        self.assertEqual(ev, {"element_present": ['text="Cart, 2 items"']})

    def test_real_page_numbers_are_still_read(self):
        self.assertEqual(ae._label_count("Cart, 3 items"), 3)
        self.assertTrue(ae._label_carries_state("12 results"))

    def test_navigator_cannot_declare_success(self):
        """The navigator's schema has no field for a completion claim."""
        import inspect
        src = inspect.getsource(ae._validate_navigator_response)
        for banned in ("\"done\"", "\"success\"", "\"completed\"", "\"passed\""):
            with self.subTest(field=banned):
                self.assertNotIn(banned, src)

    def test_planner_cannot_propose_evidence_for_the_whole_goal(self):
        """Planner proposals are rejected when the current page already
        satisfies them, which is what a self-serving proposal looks like."""
        self.assertTrue(ae._already_satisfied_by_page(
            {"text_contains": ["Checkout"]}, ["Checkout"], "u", "Checkout"))

    def test_rejected_proposal_keeps_requirements_unverified(self):
        plan = ae.RuntimePlan("open the cart")
        before = len(plan._verified)
        plan.adopt_model_plan(["open the cart"], ["Open the cart"],
                              "https://x.test/", "Open the cart")
        self.assertEqual(len(plan._verified), before,
                         "a proposal must not latch verification")

    def test_final_evidence_is_independent_of_plan_state(self):
        """Configuring evidence and never using the plan still evaluates."""
        g = goal({"url_contains": ["done"]})
        self.assertEqual(
            g.evaluate(state(), "", "https://x.test/done", True)[0],
            ae.GOAL_PASS)

    def test_unrelated_success_message_is_not_filtered_but_is_documented(self):
        """A page saying 'Thank you' verifies the phrase, not the task.

        This is a genuine limitation of lexical evidence. It is asserted here
        so the boundary is explicit: verifying an outcome still depends on the
        user choosing a distinctive phrase in their evidence block.
        """
        status, _ = goal({"text_contains": ["Order complete"]}).evaluate(
            state(), "Order complete", "https://x.test/help", True)
        self.assertEqual(status, ae.GOAL_PASS)


# ===========================================================================
# 3.3 Objective-to-requirement mapping
# ===========================================================================

class TestObjectiveMapping(unittest.TestCase):

    def test_single_clause_is_one_requirement(self):
        self.assertEqual(ae.split_objective("Buy a book"),
                         ["Buy a book"])

    def test_comma_joined_tasks_split(self):
        parts = ae.split_objective(
            "Log in, search for socks, filter by size, and check out")
        self.assertIn("Log in", parts)
        self.assertIn("search for socks", parts)

    def test_continuation_fragments_are_merged_not_invented(self):
        """'author' is not a requirement the user ever stated."""
        parts = ae.split_objective(
            "Search for a book, author, or title")
        self.assertEqual(len(parts), 1)
        self.assertIn("author", parts[0],
                      "the phrase must be preserved, not discarded")

    def test_no_clause_is_silently_dropped(self):
        # Every clause must name a task in its own right, or the splitter
        # correctly treats it as a continuation of the previous one.
        objective = "; ".join([
            "search alpha", "add bravo", "checkout charlie", "open delta",
            "submit echo", "filter foxtrot", "buy golf", "select hotel"])
        parts = ae.split_objective(objective)
        self.assertEqual(len(parts), ae.RUNTIME_PLAN_MAX_SUBGOALS,
                         "the plan tracks at most the configured maximum")
        # ...and the two clauses it did not take must be recorded, not dropped.
        self.assertTrue(ae.RUNTIME_PLAN_DROPPED_CLAUSES,
                        "overflow must be recorded, not discarded silently")
        self.assertEqual(len(ae.RUNTIME_PLAN_DROPPED_CLAUSES), 2)
        joined = " ".join(ae.RUNTIME_PLAN_DROPPED_CLAUSES)
        self.assertIn("filter foxtrot", parts,
                      "the last clause that fits is still tracked")
        self.assertIn("buy golf", joined)
        self.assertIn("select hotel", joined)

    def test_empty_objective_yields_nothing(self):
        self.assertEqual(ae.split_objective(""), [])

    def test_quantities_are_read_but_not_invented(self):
        self.assertEqual(ae._required_count("add two items"), 2)
        self.assertEqual(ae._required_count("add 3 items"), 3)
        self.assertIsNone(ae._required_count("add an item"),
                          "no quantity stated means no quantity required")

    def test_a_count_is_not_assumed_for_a_bare_requirement(self):
        self.assertEqual(ae._required_count("add items"), None)


class TestObjectiveAmbiguity(unittest.TestCase):

    def test_clear_objective_has_no_flags(self):
        self.assertEqual(
            ae.objective_ambiguities("Add two Backpacks and check out"), [])

    def test_indefinite_reference_is_flagged(self):
        flags = ae.objective_ambiguities("Add an item to the cart")
        self.assertTrue(flags)
        self.assertIn("indefinite", flags[0])

    def test_conditional_objective_is_flagged(self):
        flags = ae.objective_ambiguities("Check out if possible")
        self.assertTrue(flags)
        self.assertIn("conditional", flags[0])

    def test_conflicting_quantities_are_flagged(self):
        flags = ae.objective_ambiguities("add two items then remove 3 items")
        self.assertTrue(flags)
        self.assertIn("more than one quantity", flags[0])

    def test_empty_requirement_has_no_flags(self):
        self.assertEqual(ae.objective_ambiguities(""), [])

    def test_flags_are_reported_on_rows_and_never_resolved(self):
        plan = ae.RuntimePlan("Add an item to the cart")
        plan.items = [{"requirement": "Add an item to the cart",
                       "evidence": {}}]
        g = ae.TestGoal({"objective": "x", "evidence": {}})
        rows = plan.evaluate(["Add to cart"], "u", "Add to cart", True,
                             evaluate_evidence=g._evaluate_evidence)
        self.assertTrue(rows[0]["ambiguity"],
                        "ambiguity must reach the caller for reporting")
        self.assertFalse(rows[0]["verified"],
                         "an ambiguous requirement is not thereby satisfied")

    def test_both_paths_report_ambiguity_identically(self):
        g = ae.TestGoal({
            "objective": "Add an item to the cart",
            "steps": [{"describe": "Add an item to the cart",
                       "evidence": {"url_contains": ["never"]}}],
        })
        rows = g.remaining_work(state(), "", "", True)
        self.assertIn("ambiguity", rows[0])
        self.assertTrue(rows[0]["ambiguity"])


# ===========================================================================
# 3.4 Final status contract
# ===========================================================================

class TestFinalStatusContract(unittest.TestCase):

    def test_success_is_pass(self):
        self.assertEqual(
            ae.classify_final_status("SUCCESS_TARGET_REACHED"), ae.OUTCOME_PASS)

    def test_evidence_failure_is_fail(self):
        self.assertEqual(
            ae.classify_final_status("GOAL_EVIDENCE_FAILED"), ae.OUTCOME_FAIL)

    def test_unverifiable_is_not_stopped_and_not_fail(self):
        for raw in ("GOAL_UNVERIFIED_NO_FINAL_EVIDENCE",
                    "GRAPH_COMPLETELY_EXHAUSTED"):
            with self.subTest(raw=raw):
                self.assertEqual(ae.classify_final_status(raw),
                                 ae.OUTCOME_UNVERIFIABLE)

    def test_confirmation_and_access_control_are_blocked(self):
        for raw in ("STOPPED_AWAITING_CONFIRMATION",
                    "BLOCKED_BY_ACCESS_CONTROL"):
            with self.subTest(raw=raw):
                self.assertEqual(ae.classify_final_status(raw),
                                 ae.OUTCOME_BLOCKED)

    def test_step_limit_is_stopped_not_unverifiable(self):
        self.assertEqual(ae.classify_final_status("MAX_DEPTH_EXHAUSTED"),
                         ae.OUTCOME_STOPPED)

    def test_unverifiable_and_stopped_stay_distinct(self):
        self.assertNotEqual(
            ae.classify_final_status("GOAL_UNVERIFIED_NO_FINAL_EVIDENCE"),
            ae.classify_final_status("MAX_DEPTH_EXHAUSTED"))

    def test_unknown_status_never_becomes_a_pass(self):
        for raw in ("", "SOMETHING_NEW", None):
            with self.subTest(raw=raw):
                self.assertNotEqual(ae.classify_final_status(raw),
                                    ae.OUTCOME_PASS)

    def test_every_outcome_has_a_stated_meaning(self):
        for outcome in (ae.OUTCOME_PASS, ae.OUTCOME_FAIL, ae.OUTCOME_UNVERIFIABLE,
                        ae.OUTCOME_BLOCKED, ae.OUTCOME_STOPPED):
            with self.subTest(outcome=outcome):
                self.assertTrue(ae._OUTCOME_MEANING[outcome])

    def test_unverifiable_meaning_denies_success(self):
        self.assertIn("not", ae._OUTCOME_MEANING[ae.OUTCOME_UNVERIFIABLE].lower())
        self.assertIn("never", ae._OUTCOME_MEANING[ae.OUTCOME_UNVERIFIABLE].lower())


class TestReportContract(unittest.TestCase):

    def setUp(self):
        self._before = set(os.listdir("."))

    def tearDown(self):
        for name in set(os.listdir(".")) - self._before:
            if name.startswith("scan_report_"):
                try:
                    os.remove(name)
                except OSError:
                    pass

    def render(self, **kw):
        kwargs = dict(
            site_name="Fixture", target_goal="Buy a book",
            status="GOAL_UNVERIFIED_NO_FINAL_EVIDENCE", total_steps=3,
            nodes_discovered=2, trajectory_log=[],
            goal_evidence="", run_stats={"Model calls (total)": 4},
            errors=["**Safety:** 1 action withheld."],
            requirements=[{"index": 1, "requirement": "Add two items",
                           "describe": "Add two items", "verified": False,
                           "unverifiable": True, "contradicted": False,
                           "evidence": "", "ambiguity": ["needs detail"]}],
            verification_provenance=[{"requirement": "Add two items",
                                      "evidence": "Cart, 2 items",
                                      "observed_on_url": "https://x/cart",
                                      "still_current": False}],
            transitions="- step 1: reached `https://x/cart`",
        )
        kwargs.update(kw)
        ae.generate_scan_report(**kwargs)
        newest = max(
            (f for f in os.listdir(".") if f.startswith("scan_report_")),
            key=lambda f: os.path.getmtime(f))
        with open(newest, encoding="utf-8") as fh:
            return fh.read()

    def flatten(self, md):
        """Reports are wrapped for readability; assertions are not."""
        return " ".join(md.split())

    def test_report_states_the_outcome_and_its_meaning(self):
        md = self.render()
        self.assertIn(ae.OUTCOME_UNVERIFIABLE, md)
        self.assertIn("Meaning:", md)

    def test_report_includes_the_objective(self):
        self.assertIn("Buy a book", self.render())

    def test_report_shows_requirements_and_their_state(self):
        md = self.render()
        self.assertIn("Add two items", md)
        self.assertIn("unverifiable", md)

    def test_report_distinguishes_reaching_a_state_from_being_verified(self):
        md = self.flatten(self.render())
        self.assertIn("can never establish the final outcome on their own", md)

    def test_report_marks_no_final_evidence_as_no_claim(self):
        md = self.render()
        self.assertIn("no completion claim", md)

    def test_report_names_where_each_confirmation_was_observed(self):
        md = self.render()
        self.assertIn("Where each confirmation was observed", md)
        self.assertIn("confirmed on an earlier page", md)

    def test_report_includes_counts(self):
        self.assertIn("Model calls (total)", self.render())

    def test_report_includes_transitions(self):
        self.assertIn("Observed Transitions", self.render())

    def test_report_includes_safety_stops(self):
        self.assertIn("Safety:", self.render())

    def test_report_states_the_text_evidence_limitation(self):
        """The known weakness is stated on every report, not omitted."""
        md = self.flatten(self.render())
        self.assertIn("substring tests over whole-page content", md)

    def test_report_states_that_absence_is_never_success(self):
        self.assertIn("never reported as success", self.flatten(self.render()))

    def test_report_marks_a_pass_correctly(self):
        md = self.render(status="SUCCESS_TARGET_REACHED",
                         goal_evidence="url_contains matched")
        self.assertIn(ae.OUTCOME_PASS, md)
        self.assertNotIn("no completion claim", md)

    def test_report_cannot_receive_credentials(self):
        """The renderer takes no config, so it cannot read a secret at all."""
        import inspect
        sig = inspect.signature(ae.generate_scan_report)
        self.assertNotIn("config", sig.parameters)
        self.assertNotIn("credentials", sig.parameters)
        self.assertNotIn("auth", sig.parameters)

    def test_report_source_never_reads_config(self):
        import inspect
        src = inspect.getsource(ae.generate_scan_report)
        self.assertNotIn("config[", src)
        self.assertNotIn("load_config", src)

    def test_confirmation_report_explains_how_to_resume(self):
        md = self.render(
            status="STOPPED_AWAITING_CONFIRMATION",
            confirmation_request={"target": "Place Order", "operation": "click",
                                  "reason": "consequential",
                                  "observation_id": 3,
                                  "url": "https://x/cart"})
        self.assertIn("consequential_mode", md)
        self.assertIn("scoped to that one action", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
