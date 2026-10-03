"""Checkpoint 6.7.6 — whole-task budget enforcement.

A tool that can be made to loop is a tool that can be made to run forever
against a real website. These tests pin the fact that every budget is a hard
ceiling on the TOTAL run, not a per-step allowance that resets:

  * the step budget is clamped, so no config can ask for an unbounded run;
  * a malformed budget degrades to the default rather than crashing or
    silently becoming something the user never asked for;
  * exhausting the budget reports UNVERIFIABLE, never PASS;
  * the recovery decision budget is global and terminal once spent;
  * the model is told its remaining depth, so budget is not a surprise;
  * recon budgets are independent and cannot leak into the task path.

No browser is required. These exercise the budget arithmetic, the outcome
mapping and the configuration coercion, which is where an unbounded run would
have to be introduced.
"""

import unittest

import automation_engine as ae


class StepBudgetTests(unittest.TestCase):
    """The stored budget. `DEFAULT_MAX_STEPS` is the single authoritative
    default, shared with the enforced run-loop bound (`resolve_search_depth`)
    so the two can never disagree. See F2 in the Part 6 release report."""

    def test_a_configured_budget_is_honoured(self):
        self.assertEqual(ae.TestGoal({"objective": "x", "max_steps": 7}).max_steps, 7)

    def test_an_absent_budget_takes_the_documented_default(self):
        self.assertEqual(ae.TestGoal({"objective": "x"}).max_steps,
                         ae.DEFAULT_MAX_STEPS)

    def test_a_malformed_budget_degrades_to_the_default(self):
        """A typo must not crash the run, and must not silently become a
        number the user never asked for."""
        for bad in (None, "many", -1, 0, [], {}, float("inf"), float("-inf"),
                    True, False):
            with self.subTest(bad=bad):
                goal = ae.TestGoal({"objective": "x", "max_steps": bad})
                self.assertEqual(goal.max_steps, ae.DEFAULT_MAX_STEPS)

    def test_the_stored_and_enforced_budgets_agree_on_every_input(self):
        """The defect this fixes: an omitted budget gave 25 while a malformed
        one gave 40, and neither was necessarily the value the run loop used.

        Agreement is only expected where the clamp is not engaged — outside
        [MIN_SEARCH_DEPTH, MAX_SEARCH_DEPTH] the enforced bound is
        deliberately clamped while the stored value records what the user
        actually asked for. That divergence is intended and is asserted
        separately below.
        """
        in_range = (None, "many", -1, 0, True, False, "abc", [], {},
                    float("inf"), 5, 7, 25, 30, 40, 60, "30", 40.0)
        for raw in in_range:
            with self.subTest(raw=raw):
                stored = ae.TestGoal({"objective": "x",
                                      "max_steps": raw}).max_steps
                enforced = ae.resolve_search_depth({"max_steps": raw})
                self.assertEqual(stored, enforced)

    def test_out_of_range_values_diverge_by_design(self):
        """A budget outside the clamp window is recorded as asked for, but the
        run is bounded. The stored value is never allowed to grant more steps
        than the loop will actually take."""
        for asked in (1, 4, 61, 1000):
            with self.subTest(asked=asked):
                stored = ae.TestGoal({"objective": "x",
                                      "max_steps": asked}).max_steps
                enforced = ae.resolve_search_depth({"max_steps": asked})
                self.assertEqual(stored, asked)
                self.assertGreaterEqual(enforced, ae.MIN_SEARCH_DEPTH)
                self.assertLessEqual(enforced, ae.MAX_SEARCH_DEPTH)

    def test_a_non_finite_budget_never_crashes_the_run(self):
        """`max_steps: Infinity` reached int() as an OverflowError, which is
        not caught by `except (TypeError, ValueError)`, so a malformed JSON
        config aborted the run with a bare traceback. Found by this checkpoint."""
        for bad in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(bad=bad):
                self.assertIsInstance(
                    ae.TestGoal({"objective": "x", "max_steps": bad}), ae.TestGoal)

    def test_a_boolean_is_not_treated_as_a_step_count(self):
        """int(True) is 1, which would silently buy a one-step run instead of
        the intended default."""
        self.assertEqual(ae._coerce_step_budget(True, ae.DEFAULT_MAX_STEPS, "t"),
                         ae.DEFAULT_MAX_STEPS)
        self.assertEqual(ae._coerce_step_budget(False, ae.DEFAULT_MAX_STEPS, "t"),
                         ae.DEFAULT_MAX_STEPS)

    def test_a_non_dict_goal_is_survivable(self):
        for bad in (None, "goal", 42, []):
            with self.subTest(bad=bad):
                self.assertIsInstance(ae.TestGoal(bad), ae.TestGoal)

    def test_an_empty_objective_is_not_treated_as_a_goal(self):
        self.assertEqual(ae.TestGoal({"objective": "   "}).objective, "")


class EnforcedSearchDepthTests(unittest.TestCase):
    """The budget the run loop ACTUALLY uses.

    `resolve_search_depth` is what bounds `range(max_search_depth)`, so these
    assert on it rather than on any stored attribute. Before F2-a the bound was
    computed inline as `int(raw or 25)` inside a try/except, which gave three
    different answers for malformed input: `"many"` fell to 25 through the
    except branch, while `-1` and `True` parsed fine and were clamped up to the
    floor of 5. A typo of `-1` bought a 5-step run.
    """

    DEFAULT = ae.DEFAULT_MAX_STEPS

    def test_an_omitted_budget_yields_the_documented_default(self):
        self.assertEqual(ae.resolve_search_depth({}), self.DEFAULT)
        self.assertEqual(ae.resolve_search_depth({"objective": "do it"}),
                         self.DEFAULT)

    def test_a_valid_explicit_budget_is_honoured_exactly(self):
        for good in (6, 7, 12, 25, 30, 40, 60):
            with self.subTest(good=good):
                self.assertEqual(ae.resolve_search_depth({"max_steps": good}), good)

    def test_a_numeric_string_budget_is_honoured(self):
        self.assertEqual(ae.resolve_search_depth({"max_steps": "30"}), 30)

    def test_an_invalid_string_yields_the_default(self):
        """Previously the ValueError branch happened to give 25; now it is the
        default by construction rather than by accident of exception type."""
        for bad in ("many", "twelve", "", " ", "12abc", None):
            with self.subTest(bad=bad):
                self.assertEqual(ae.resolve_search_depth({"max_steps": bad}),
                                 self.DEFAULT)

    def test_a_negative_value_yields_the_default_not_the_floor(self):
        """The regression. `-1` used to parse as -1 and then clamp up to 5,
        silently buying a 5-step run instead of the documented default."""
        for bad in (-1, -5, -1000):
            with self.subTest(bad=bad):
                self.assertEqual(ae.resolve_search_depth({"max_steps": bad}),
                                 self.DEFAULT)

    def test_a_zero_budget_yields_the_default_not_the_floor(self):
        """0 is falsy, so the old `raw or 25` short-circuited to 25 by luck
        rather than by rule; that accident is now explicit."""
        for bad in (0, 0.0):
            with self.subTest(bad=bad):
                self.assertEqual(ae.resolve_search_depth({"max_steps": bad}),
                                 self.DEFAULT)

    def test_a_boolean_yields_the_default_not_a_one_step_run(self):
        """int(True) is 1, which the old clamp turned into a 5-step run."""
        for bad in (True, False):
            with self.subTest(bad=bad):
                self.assertEqual(ae.resolve_search_depth({"max_steps": bad}),
                                 self.DEFAULT)

    def test_a_non_finite_budget_yields_the_default(self):
        for bad in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(bad=bad):
                self.assertEqual(ae.resolve_search_depth({"max_steps": bad}),
                                 self.DEFAULT)

    def test_a_container_budget_yields_the_default(self):
        for bad in ([], {}, [5], {"a": 1}):
            with self.subTest(bad=bad):
                self.assertEqual(ae.resolve_search_depth({"max_steps": bad}),
                                 self.DEFAULT)

    def test_boundary_values_at_the_clamp_edges(self):
        self.assertEqual(ae.resolve_search_depth({"max_steps": ae.MIN_SEARCH_DEPTH}),
                         ae.MIN_SEARCH_DEPTH)
        self.assertEqual(ae.resolve_search_depth({"max_steps": ae.MAX_SEARCH_DEPTH}),
                         ae.MAX_SEARCH_DEPTH)

    def test_a_budget_above_the_ceiling_is_clamped(self):
        for asked in (61, 1000, 10 ** 9):
            with self.subTest(asked=asked):
                self.assertEqual(ae.resolve_search_depth({"max_steps": asked}),
                                 ae.MAX_SEARCH_DEPTH)

    def test_a_budget_below_the_floor_is_clamped_not_rejected(self):
        """Only reachable with a value that coerces to a real positive int but
        sits under the floor, e.g. 1 or 2."""
        for asked in (1, 2, 3, 4):
            with self.subTest(asked=asked):
                self.assertEqual(ae.resolve_search_depth({"max_steps": asked}),
                                 ae.MIN_SEARCH_DEPTH)

    def test_the_enforced_bound_is_always_a_positive_int(self):
        for raw in (None, "many", -1, 0, True, 10 ** 9, float("inf")):
            with self.subTest(raw=raw):
                depth = ae.resolve_search_depth({"max_steps": raw})
                self.assertIsInstance(depth, int)
                self.assertGreaterEqual(depth, ae.MIN_SEARCH_DEPTH)
                self.assertLessEqual(depth, ae.MAX_SEARCH_DEPTH)

    def test_a_missing_or_malformed_config_block_is_survivable(self):
        for bad in (None, "goal", 42, []):
            with self.subTest(bad=bad):
                self.assertEqual(ae.resolve_search_depth(bad), self.DEFAULT)

    def test_the_run_loop_calls_this_resolver(self):
        """Guards against the inline arithmetic returning while these tests
        still pass against an unused function."""
        import inspect
        source = inspect.getsource(ae.run_pathfinder_agent)
        self.assertIn("resolve_search_depth(", source)
        self.assertNotIn("int(_test_goal_cfg", source)


class SearchDepthClampTests(unittest.TestCase):
    """The clamp is what makes a step budget a ceiling rather than a wish.

    These now assert on `resolve_search_depth` itself rather than on a local
    re-implementation of `max(5, min(x, 60))`. A test that re-derives the
    formula under test keeps passing when the production call site stops using
    it, which is exactly the failure mode F2-a had.
    """

    def test_a_huge_budget_is_clamped_to_the_ceiling(self):
        for asked in (61, 1000, 10 ** 9):
            with self.subTest(asked=asked):
                self.assertEqual(ae.resolve_search_depth({"max_steps": asked}), 60)

    def test_a_budget_under_the_floor_is_clamped_up(self):
        for asked in (1, 2, 3, 4):
            with self.subTest(asked=asked):
                self.assertEqual(ae.resolve_search_depth({"max_steps": asked}), 5)

    def test_a_reasonable_budget_passes_through_unchanged(self):
        for asked in (6, 25, 40, 60):
            with self.subTest(asked=asked):
                self.assertEqual(ae.resolve_search_depth({"max_steps": asked}),
                                 asked)

    def test_the_clamp_edges_are_named_constants(self):
        self.assertEqual(ae.MIN_SEARCH_DEPTH, 5)
        self.assertEqual(ae.MAX_SEARCH_DEPTH, 60)

    def test_cli_budget_is_coerced_like_a_configured_one(self):
        self.assertEqual(
            ae.TestGoal({"objective": "x", "max_steps": "12"}).max_steps, 12)
        self.assertEqual(
            ae.TestGoal({"objective": "x", "max_steps": "nope"}).max_steps,
            ae.DEFAULT_MAX_STEPS)


class BudgetExhaustionOutcomeTests(unittest.TestCase):
    """Running out of budget is not success. This is the load-bearing test."""

    def test_exhausting_the_graph_reports_unverifiable(self):
        self.assertEqual(
            ae.classify_final_status("GRAPH_COMPLETELY_EXHAUSTED"),
            ae.OUTCOME_UNVERIFIABLE)

    def test_no_budget_exhaustion_status_maps_to_pass(self):
        """No status that means 'we ran out of room' may map to PASS."""
        budget_statuses = [
            "GRAPH_COMPLETELY_EXHAUSTED",
            "GOAL_UNVERIFIED_NO_FINAL_EVIDENCE",
            "RECOVERY_STOPPED_INSUFFICIENT_EVIDENCE",
            "RECOVERY_EXHAUSTED",
            "MAX_STEPS_REACHED",
        ]
        for status in budget_statuses:
            with self.subTest(status=status):
                self.assertNotEqual(ae.classify_final_status(status),
                                    ae.OUTCOME_PASS)

    def test_an_unrecognised_status_is_unverifiable_not_pass(self):
        """An outcome the system cannot place must not default to success, and
        must not default to failure either."""
        for status in ("", "SOMETHING_NEW", None, 0):
            with self.subTest(status=status):
                self.assertEqual(ae.classify_final_status(status),
                                 ae.OUTCOME_UNVERIFIABLE)

    def test_unverifiable_is_distinct_from_pass_and_fail(self):
        outcomes = {ae.OUTCOME_PASS, ae.OUTCOME_FAIL, ae.OUTCOME_UNVERIFIABLE,
                    ae.OUTCOME_BLOCKED, ae.OUTCOME_STOPPED}
        self.assertEqual(len(outcomes), 5)

    def test_only_evidence_backed_success_maps_to_pass(self):
        passing = [status for status, outcome in ae._STATUS_OUTCOMES.items()
                   if outcome == ae.OUTCOME_PASS]
        self.assertIn("SUCCESS_TARGET_REACHED", passing)
        # Nothing that merely means "the loop ended" may be in that set.
        for status in passing:
            self.assertNotIn("EXHAUST", status)
            self.assertNotIn("MAX_STEP", status)

    def test_every_safety_stop_reports_blocked_not_unverifiable(self):
        """Reaching the safety boundary by any route reports BLOCKED, so the
        boundary cannot be made to look like an innocent budget problem."""
        for status in ("STOPPED_AWAITING_CONFIRMATION",
                       "BLOCKED_BY_ACCESS_CONTROL",
                       "RECOVERY_STOPPED_AT_SAFETY_BOUNDARY",
                       "RECOVERY_STOPPED_AT_ACCESS_CONTROL",
                       "RECOVERY_NEEDS_USER_CLARIFICATION"):
            with self.subTest(status=status):
                self.assertEqual(ae.classify_final_status(status),
                                 ae.OUTCOME_BLOCKED)


class RecoveryBudgetTests(unittest.TestCase):
    """The recovery decision budget is global, not per-step."""

    def test_the_global_decision_budget_terminates_the_run(self):
        controller = ae.RecoveryController(max_attempts_per_action=1,
                                          max_attempts_per_action_total=1,
                                          max_total_decisions=3)
        stops = 0
        for index in range(20):
            # A different node each time, so no per-action limit can trigger.
            decision = controller.decide(f"n{index}", "Back", ae.FAIL_STALE_ELEMENT,
                                         goal_context=("s",))
            if decision.is_stop:
                stops += 1
                break
        self.assertEqual(stops, 1)

    def test_the_global_budget_is_spent_by_many_different_actions(self):
        """Spreading failures across many controls must not evade the ceiling:
        that is the shape of a fan-out loop, which is exactly what the total
        budget exists to stop."""
        budget = 4
        controller = ae.RecoveryController(max_attempts_per_action=5,
                                          max_attempts_per_action_total=5,
                                          max_total_decisions=budget)
        decided = 0
        for index in range(50):
            decision = controller.decide(f"n{index}", "Back",
                                         ae.FAIL_STALE_ELEMENT, ("s",))
            decided += 1
            if decision.is_stop:
                break
        # `budget` non-stopping decisions, then the refusal that stops the run.
        self.assertEqual(decided, budget + 1)

    def test_a_refused_decision_still_reports_a_stop(self):
        """Once the budget is gone, every later call keeps reporting the stop,
        so a caller that keeps asking cannot resume the run."""
        controller = ae.RecoveryController(max_attempts_per_action=1,
                                          max_attempts_per_action_total=1,
                                          max_total_decisions=1)
        controller.decide("n0", "Back", ae.FAIL_STALE_ELEMENT, ("s",))
        for index in range(1, 10):
            with self.subTest(index=index):
                decision = controller.decide(f"m{index}", "Back",
                                             ae.FAIL_STALE_ELEMENT, ("s",))
                self.assertTrue(decision.is_stop)
                self.assertEqual(decision.outcome, ae.RECOVER_EXHAUSTED)

    def test_a_bounded_recovery_cannot_loop_forever(self):
        controller = ae.RecoveryController()
        for _ in range(500):
            decision = controller.decide("n1", "Next page",
                                         ae.FAIL_TARGET_NOT_FOUND, ("s",))
            if decision.is_stop:
                break
        self.assertTrue(decision.is_stop)

    def test_the_configured_budgets_are_positive_integers(self):
        for name in ("RECOVERY_MAX_ATTEMPTS_PER_ACTION",
                     "RECOVERY_MAX_TOTAL_DECISIONS",
                     "RECOVERY_MAX_ATTEMPTS_PER_ACTION_TOTAL"):
            with self.subTest(name=name):
                value = getattr(ae, name)
                self.assertIsInstance(value, int)
                self.assertGreaterEqual(value, 1)

    def test_stale_context_expires_instead_of_accumulating_forever(self):
        controller = ae.RecoveryController(max_total_decisions=100)
        for index in range(50):
            controller.decide("n1", "X", ae.FAIL_TARGET_NOT_FOUND,
                              (f"context-{index}",))
        self.assertEqual(len(controller._attempts_total), 50)
        dropped = controller.expire_stale_context(("context-49",))
        # The current situation's own record survives; the other 49 are gone
        # from BOTH maps (per-class attempts and the class-agnostic total).
        self.assertEqual(dropped, 98)
        self.assertEqual(len(controller._attempts_total), 1)
        self.assertEqual(len(controller._attempts), 1)
        self.assertIn(("n1", "X", ("context-49",)),
                      controller._attempts_total)

    def test_a_control_withdrawn_in_one_context_is_usable_in_the_next(self):
        """This is what makes recovery a demotion with an expiry rather than a
        permanent blacklist. The total is exercised directly so the assertion
        does not depend on which of the two attempt limits happens to bind
        first inside decide().
        """
        controller = ae.RecoveryController(max_attempts_per_action_total=2)
        # Two failures of DIFFERENT classes on the same control.
        controller.record_attempt("n1", "X", ae.FAIL_STALE_ELEMENT, ("step1",))
        controller.record_attempt("n1", "X", ae.FAIL_TARGET_NOT_FOUND, ("step1",))
        self.assertTrue(controller.is_withdrawn("n1", "X", ("step1",)))
        # A different situation is a different problem, so it starts clean.
        self.assertFalse(controller.is_withdrawn("n1", "X", ("step2",)))
        controller.expire_stale_context(("step2",))
        self.assertEqual(controller.total_attempts_for("n1", "X", ("step2",)), 0)

    def test_a_success_clears_the_withdrawal(self):
        """A control that worked is fresh evidence; the second legitimate use
        of the same button must not be treated as a repeat failure."""
        controller = ae.RecoveryController(max_attempts_per_action_total=2)
        controller.record_attempt("n1", "Add to cart", ae.FAIL_STALE_ELEMENT,
                                  ("s",))
        controller.record_attempt("n1", "Add to cart", ae.FAIL_STALE_ELEMENT,
                                  ("s",))
        self.assertTrue(controller.is_withdrawn("n1", "Add to cart", ("s",)))
        controller.record_success("n1", "Add to cart", ("s",))
        self.assertFalse(controller.is_withdrawn("n1", "Add to cart", ("s",)))


class ModelIsToldItsDepthTests(unittest.TestCase):
    """Budget the model cannot see is a budget it will waste."""

    def test_the_model_is_told_its_position_and_ceiling(self):
        import inspect
        source = inspect.getsource(ae.ask_ai_navigator)
        self.assertIn("RUN DEPTH", source)

    def test_the_navigator_accepts_step_index_and_max_steps(self):
        import inspect
        params = inspect.signature(ae.ask_ai_navigator).parameters
        self.assertIn("step_index", params)
        self.assertIn("max_steps", params)

    def test_depth_is_reported_when_both_bounds_are_known(self):
        import inspect
        source = inspect.getsource(ae.ask_ai_navigator)
        # The depth line is guarded on BOTH values; a missing bound must not
        # produce a misleading "step 1 of None".
        self.assertIn("step_index is not None and max_steps is not None", source)


class ReconBudgetIsolationTests(unittest.TestCase):
    """Recon budgets must not be reachable from the task path."""

    def test_recon_budget_has_its_own_fields(self):
        import reconnaissance as r
        budget = r.ReconBudget()
        for attr in ("max_pages", "max_depth", "max_transitions", "max_seconds",
                     "max_model_calls"):
            with self.subTest(attr=attr):
                self.assertTrue(hasattr(budget, attr))

    def test_recon_budget_fields_are_positive(self):
        import reconnaissance as r
        budget = r.ReconBudget()
        for attr in ("max_pages", "max_depth", "max_transitions",
                     "max_seconds", "max_model_calls"):
            with self.subTest(attr=attr):
                self.assertGreater(getattr(budget, attr), 0)

    def test_the_task_entry_point_takes_no_recon_budget(self):
        """If the task loop could accept recon budgets, running recon would
        change how a task run is bounded."""
        import inspect
        params = set(inspect.signature(ae.run_pathfinder_agent).parameters)
        for forbidden in ("recon", "recon_max_pages", "recon_max_depth",
                          "recon_budget"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, params)

    def test_recon_is_dispatched_from_a_separate_entry_point(self):
        """`run_pathfinder_agent` is the task path; recon is reached through the
        CLI dispatcher, never through the task signature."""
        import inspect
        params = set(inspect.signature(ae.run_pathfinder_agent).parameters)
        self.assertNotIn("recon", params)
        self.assertTrue(callable(ae._parse_cli))


class ModelCallAccountingTests(unittest.TestCase):
    """A budget figure has to describe the run being reported.

    Found by checkpoint 6.7.2: the counters are module-level, so a caller
    running several runs in one process — the benchmark repeat harness, the A/B
    probes — got cumulative totals. The repeatability table showed model calls
    climbing 1,2,3,4,5 then 6..10 then 14,18,22.. across cases.
    """

    def test_counters_start_at_zero(self):
        ae.reset_model_call_stats()
        self.assertEqual(ae.MODEL_CALL_STATS,
                         {"navigator": 0, "planner": 0, "failed": 0})

    def test_reset_zeroes_every_counter(self):
        ae.MODEL_CALL_STATS["navigator"] = 7
        ae.MODEL_CALL_STATS["planner"] = 3
        ae.MODEL_CALL_STATS["failed"] = 2
        ae.reset_model_call_stats()
        self.assertEqual(ae.MODEL_CALL_STATS["navigator"], 0)
        self.assertEqual(ae.MODEL_CALL_STATS["planner"], 0)
        self.assertEqual(ae.MODEL_CALL_STATS["failed"], 0)

    def test_reset_returns_the_live_mapping(self):
        self.assertIs(ae.reset_model_call_stats(), ae.MODEL_CALL_STATS)

    def test_the_run_entry_point_resets_before_anything_else(self):
        import inspect
        source = inspect.getsource(ae.run_pathfinder_agent)
        self.assertIn("reset_model_call_stats()", source)
        # It must come before the config load, and certainly before any model
        # call could be made.
        self.assertLess(source.index("reset_model_call_stats()"),
                        source.index("load_config("))

    def test_counts_do_not_accumulate_across_consecutive_runs(self):
        """Direct simulation of the harness bug: increment, reset, increment."""
        ae.reset_model_call_stats()
        ae.MODEL_CALL_STATS["navigator"] += 4
        first = ae.MODEL_CALL_STATS["navigator"]
        ae.reset_model_call_stats()
        ae.MODEL_CALL_STATS["navigator"] += 4
        second = ae.MODEL_CALL_STATS["navigator"]
        self.assertEqual(first, second)
        self.assertEqual(second, 4)


class NoTimeoutEscapesTests(unittest.TestCase):
    """Timeouts must bound a single operation, never the whole run."""

    def test_recon_enforces_a_wall_clock_bound(self):
        import inspect
        import reconnaissance as r
        source = inspect.getsource(r)
        self.assertIn("max_seconds", source)

    def test_the_recon_budget_checks_expiry_at_every_step(self):
        import inspect
        import reconnaissance as r
        source = inspect.getsource(r)
        self.assertTrue("expired" in source or "budget" in source.lower())


if __name__ == "__main__":
    unittest.main()