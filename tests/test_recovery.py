"""Part 4.2 — Bounded recovery controller.

The classifier answers what went wrong; the controller answers what happens
next. These tests pin the properties that make recovery bounded rather than
open-ended:

  * retries terminate at an explicit, per-class limit
  * recovery is state-scoped, so a failure in one situation does not follow
    the agent into an unrelated one
  * penalties expire rather than accumulating into a blacklist
  * reclassifying a failure cannot buy extra attempts
  * global budgets cap the whole run, not just one action
  * exhausting recovery produces an honest stop, never an optimistic claim

Run with:
    .\\venv311\\Scripts\\python.exe -m unittest tests.test_recovery -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae

CTX_A = ("add two items",)
CTX_B = ("complete checkout",)


class TestRetriesAreBounded(unittest.TestCase):

    def test_a_retryable_class_stops_retrying_at_its_limit(self):
        c = ae.RecoveryController()
        outcomes = [c.decide("n", "Submit", ae.FAIL_ACTION_TIMEOUT, CTX_A).outcome
                    for _ in range(5)]
        self.assertEqual(
            outcomes[:2], [ae.RECOVER_RETRY, ae.RECOVER_RETRY],
            "a transient timeout gets its full allowance")
        self.assertEqual(outcomes[2], ae.RECOVER_ALTERNATIVE,
                         "then it must stop asking for the same action")
        self.assertTrue(all(o != ae.RECOVER_RETRY for o in outcomes[2:]),
                        "recovery must never re-offer a retry past the limit")

    def test_the_limit_is_exactly_the_policy_limit(self):
        c = ae.RecoveryController()
        limit = ae.failure_policy(ae.FAIL_ACTION_TIMEOUT).max_attempts
        retries = sum(
            1 for _ in range(10)
            if c.decide("n", "Submit", ae.FAIL_ACTION_TIMEOUT, CTX_A).outcome
            == ae.RECOVER_RETRY)
        self.assertEqual(retries, limit)

    def test_a_non_retryable_class_is_never_offered_a_retry(self):
        c = ae.RecoveryController()
        for cls in (ae.FAIL_TARGET_AMBIGUOUS, ae.FAIL_STALE_ELEMENT,
                    ae.FAIL_FUTILE_ACTION, ae.FAIL_TARGET_NOT_FOUND):
            fresh = ae.RecoveryController()
            decision = fresh.decide("n", "Something", cls, CTX_A)
            self.assertNotEqual(decision.outcome, ae.RECOVER_RETRY,
                                f"{cls} must not be retried: repeating it is "
                                f"exactly the behaviour that produced it")

    def test_attempt_counts_do_not_inflate_from_refusals(self):
        """A refusal is not an attempt.

        If declining to repeat an action counted as repeating it, the first
        refusal would push the count over the limit and convert a bounded
        refusal into an immediate stop.
        """
        c = ae.RecoveryController()
        first = c.decide("n", "A", ae.FAIL_TARGET_AMBIGUOUS, CTX_A)
        self.assertEqual(first.attempts, 1)
        after = c.attempts_for("n", "A", ae.FAIL_TARGET_AMBIGUOUS, CTX_A)
        self.assertEqual(after, 1, "a refusal must not record another attempt")
        for _ in range(5):
            c.decide("n", "A", ae.FAIL_TARGET_AMBIGUOUS, CTX_A)
        self.assertEqual(c.attempts_for("n", "A", ae.FAIL_TARGET_AMBIGUOUS,
                                        CTX_A), 1)

    def test_every_decision_is_recorded_with_a_reason(self):
        c = ae.RecoveryController()
        c.decide("n", "A", ae.FAIL_ACTION_TIMEOUT, CTX_A)
        self.assertEqual(len(c.history), 1)
        decision = c.history[0]
        self.assertTrue(decision.reason,
                        "a decision that cannot explain itself is arbitrary")
        self.assertIn("action_timeout", decision.describe())


class TestRecoveryIsStateScoped(unittest.TestCase):

    def test_a_failure_on_one_node_does_not_follow_the_agent(self):
        """The same label on a different page is a different control."""
        c = ae.RecoveryController()
        for _ in range(5):
            decision = c.decide("inventory", "Add to cart",
                                ae.FAIL_FUTILE_ACTION, CTX_A)
        # A futile action is refused outright rather than retried, so the
        # withdrawal backstop is never reached. What matters is that it is
        # never offered again, and that a different node is untouched.
        self.assertEqual(decision.outcome, ae.RECOVER_ALTERNATIVE)
        self.assertFalse(c.is_withdrawn("cart", "Add to cart", CTX_A),
                         "a different node must start with a clean record")
        self.assertEqual(
            c.attempts_for("cart", "Add to cart", ae.FAIL_FUTILE_ACTION, CTX_A),
            0, "no attempt may be recorded against a node never tried")

    def test_a_failure_for_one_subgoal_does_not_condemn_a_later_one(self):
        """Goal-context scoping: penalties must expire as the work changes.

        This is the "add two items, then checkout" case. A cart link that was
        pointless while adding items is exactly what is needed later, and must
        not still be withdrawn when the task reaches checkout.
        """
        c = ae.RecoveryController(max_attempts_per_action_total=2)
        c.record_attempt("inventory", "Cart", ae.FAIL_FUTILE_ACTION, CTX_A)
        c.record_attempt("inventory", "Cart", ae.FAIL_FUTILE_ACTION, CTX_A)
        self.assertTrue(c.is_withdrawn("inventory", "Cart", CTX_A))
        self.assertFalse(c.is_withdrawn("inventory", "Cart", CTX_B),
                         "the penalty must not carry into a different sub-goal")

    def test_expire_stale_context_clears_superseded_history(self):
        c = ae.RecoveryController(max_attempts_per_action_total=2)
        c.record_attempt("n", "A", ae.FAIL_FUTILE_ACTION, CTX_A)
        c.record_attempt("n", "A", ae.FAIL_FUTILE_ACTION, CTX_A)
        dropped = c.expire_stale_context(CTX_B)
        self.assertGreater(dropped, 0, "the stale records must actually go")
        self.assertFalse(c.is_withdrawn("n", "A", CTX_A))

    def test_expire_stale_context_keeps_current_history(self):
        """Expiry must drop superseded work, not the work in front of us."""
        c = ae.RecoveryController(max_attempts_per_action_total=3)
        c.record_attempt("n", "A", ae.FAIL_ACTION_TIMEOUT, CTX_A)
        c.record_attempt("n", "A", ae.FAIL_ACTION_TIMEOUT, CTX_B)
        c.expire_stale_context(CTX_B)
        self.assertEqual(c.attempts_for("n", "A", ae.FAIL_ACTION_TIMEOUT, CTX_B),
                         1, "current context history must survive expiry")

    def test_success_clears_history_so_a_repeat_is_not_a_repeat(self):
        """Adding a second cart item uses the same button as the first.

        If the first use left a failure behind, the second legitimate use would
        be treated as a retry of a failure and refused.
        """
        c = ae.RecoveryController(max_attempts_per_action_total=2)
        c.record_attempt("inventory", "Add to cart", ae.FAIL_ACTION_TIMEOUT,
                         CTX_A)
        self.assertEqual(c.total_attempts_for("inventory", "Add to cart",
                                              CTX_A), 1)
        c.record_success("inventory", "Add to cart", CTX_A)
        self.assertEqual(c.total_attempts_for("inventory", "Add to cart",
                                              CTX_A), 0)
        self.assertFalse(c.is_withdrawn("inventory", "Add to cart", CTX_A))

    def test_penalties_are_never_permanent(self):
        """The core anti-blacklist guarantee.

        Across every failure class, exhausting an action's allowance in one
        context must not leave it permanently unusable: the record is scoped
        and droppable, so the control returns when the situation changes.
        """
        for cls in ae.FAILURE_CLASSES:
            if ae.failure_policy(cls).must_stop:
                continue
            c = ae.RecoveryController(max_attempts_per_action_total=1)
            c.record_attempt("n", "Control", cls, CTX_A)
            self.assertTrue(c.is_withdrawn("n", "Control", CTX_A),
                            f"{cls} failed to withdraw at its limit")
            c.expire_stale_context(CTX_B)
            self.assertFalse(c.is_withdrawn("n", "Control", CTX_A),
                             f"{cls} left a permanent blacklist behind")


class TestReclassificationCannotBuyExtraAttempts(unittest.TestCase):

    def test_a_new_classification_does_not_reset_the_count(self):
        """Every timeout is 'the same' failure wearing different clothes.

        If the per-class limit were the only bound, reclassifying the same
        failing action each time would grant it unlimited retries.
        """
        c = ae.RecoveryController(max_attempts_per_action_total=3)
        classes = [ae.FAIL_ACTION_TIMEOUT, ae.FAIL_UNEXPECTED_STATE,
                   ae.FAIL_ACTION_EXCEPTION]
        for cls in classes:
            c.decide("n", "Flaky", cls, CTX_A)
        fourth = c.decide("n", "Flaky", ae.FAIL_FUTILE_ACTION, CTX_A)
        self.assertEqual(fourth.outcome, ae.RECOVER_EXHAUSTED,
                         "a fourth attempt under a fresh label must stop")

    def test_the_total_limit_is_scoped_per_action(self):
        """Exhausting one control must not withdraw an unrelated one."""
        c = ae.RecoveryController(max_attempts_per_action_total=1)
        c.record_attempt("n", "Bad", ae.FAIL_ACTION_TIMEOUT, CTX_A)
        self.assertTrue(c.is_withdrawn("n", "Bad", CTX_A))
        self.assertFalse(c.is_withdrawn("n", "Good", CTX_A))

    def test_the_total_limit_is_scoped_per_node(self):
        c = ae.RecoveryController(max_attempts_per_action_total=1)
        c.record_attempt("node_a", "Button", ae.FAIL_ACTION_TIMEOUT, CTX_A)
        self.assertTrue(c.is_withdrawn("node_a", "Button", CTX_A))
        self.assertFalse(c.is_withdrawn("node_b", "Button", CTX_A))


class TestGlobalBudgetsBoundTheRun(unittest.TestCase):

    def test_the_total_decision_budget_terminates(self):
        c = ae.RecoveryController(max_total_decisions=3)
        outcomes = [c.decide(f"n{i}", "A", ae.FAIL_ACTION_TIMEOUT, CTX_A).outcome
                    for i in range(6)]
        self.assertEqual(outcomes[:3], [ae.RECOVER_RETRY] * 3)
        self.assertTrue(all(o == ae.RECOVER_EXHAUSTED for o in outcomes[3:]),
                        "the global budget must stop every further decision")

    def test_the_budget_is_shared_across_actions(self):
        """A run must not get N actions × N attempts out of one budget."""
        c = ae.RecoveryController(max_total_decisions=4)
        outcomes = [c.decide(f"node_{i}", f"action_{i}", ae.FAIL_ACTION_TIMEOUT,
                             CTX_A).outcome
                    for i in range(20)]
        granted = [o for o in outcomes
                   if o in (ae.RECOVER_RETRY, ae.RECOVER_ALTERNATIVE,
                            ae.RECOVER_REOBSERVE)]
        self.assertEqual(len(granted), 4,
                         "the decision budget must be per run, not per action")
        self.assertEqual(outcomes[4], ae.RECOVER_EXHAUSTED,
                         "everything past the budget must be a refusal")

    def test_exhaustion_names_its_own_reason(self):
        c = ae.RecoveryController(max_total_decisions=1)
        c.decide("n0", "A", ae.FAIL_ACTION_TIMEOUT, CTX_A)
        d = c.decide("n1", "B", ae.FAIL_ACTION_TIMEOUT, CTX_A)
        self.assertTrue(c.exhausted)
        self.assertIn("budget", d.reason.lower())


class TestTerminalClassesStopTheRun(unittest.TestCase):

    def test_a_safety_boundary_stops_immediately(self):
        """No amount of remaining budget may apply to a safety decision."""
        c = ae.RecoveryController(max_total_decisions=100,
                                  max_attempts_per_action=100)
        decision = c.decide("n", "Checkout", ae.FAIL_SAFETY_BOUNDARY, CTX_A)
        self.assertTrue(decision.is_stop)
        self.assertTrue(decision.requires_clarification)
        self.assertNotIn(decision.outcome, (ae.RECOVER_RETRY,
                                            ae.RECOVER_ALTERNATIVE,
                                            ae.RECOVER_REOBSERVE))

    def test_a_terminal_class_costs_no_attempt(self):
        """It was never tried, so recording an attempt would be false."""
        c = ae.RecoveryController()
        c.decide("n", "Checkout", ae.FAIL_SAFETY_BOUNDARY, CTX_A)
        self.assertEqual(c.total_attempts_for("n", "Checkout", CTX_A), 0)

    def test_access_control_stops_without_asking(self):
        """There is no question to put to the user; it simply stops."""
        c = ae.RecoveryController()
        decision = c.decide("n", "Continue", ae.FAIL_ACCESS_CONTROL, CTX_A)
        self.assertEqual(decision.outcome, ae.RECOVER_BLOCKED)
        self.assertFalse(decision.requires_clarification)

    def test_insufficient_evidence_stops_the_run(self):
        c = ae.RecoveryController()
        decision = c.decide("n", "Anything", ae.FAIL_INSUFFICIENT_EVIDENCE,
                            CTX_A)
        self.assertTrue(decision.is_stop)


class TestClarificationStopsRatherThanGuessing(unittest.TestCase):

    def test_a_missing_value_asks_rather_than_inventing_one(self):
        c = ae.RecoveryController(max_attempts_per_action=99)
        decision = c.decide("n", "Fill name", ae.FAIL_MISSING_INPUT, CTX_A)
        self.assertEqual(decision.outcome, ae.RECOVER_ASK_USER)
        self.assertTrue(decision.requires_clarification)

    def test_asking_is_not_repeatable(self):
        c = ae.RecoveryController()
        for _ in range(4):
            decision = c.decide("n", "Fill name", ae.FAIL_MISSING_INPUT, CTX_A)
            self.assertEqual(decision.outcome, ae.RECOVER_ASK_USER)
        self.assertEqual(c.total_attempts_for("n", "Fill name", CTX_A), 0,
                         "asking a question is not an attempt at the action")


class TestFailedActionVersusUnexpectedResult(unittest.TestCase):

    """The two must produce different responses, not one shared retry."""

    def test_a_failed_action_may_be_retried(self):
        c = ae.RecoveryController()
        self.assertEqual(
            c.decide("n", "Submit", ae.FAIL_ACTION_TIMEOUT, CTX_A).outcome,
            ae.RECOVER_RETRY)

    def test_an_unexpected_result_is_not_retried(self):
        """The action worked. Repeating it reaches the same wrong place."""
        c = ae.RecoveryController()
        decision = c.decide("n", "Open menu", ae.FAIL_UNEXPECTED_STATE, CTX_A)
        self.assertNotEqual(decision.outcome, ae.RECOVER_RETRY)
        self.assertIn(decision.outcome,
                      (ae.RECOVER_ALTERNATIVE, ae.RECOVER_REOBSERVE))

    def test_a_futile_action_is_not_retried(self):
        c = ae.RecoveryController()
        self.assertNotEqual(
            c.decide("n", "Toggle", ae.FAIL_FUTILE_ACTION, CTX_A).outcome,
            ae.RECOVER_RETRY)


class TestTheControllerCannotAct(unittest.TestCase):

    def test_decide_is_the_only_entry_point_and_takes_no_page(self):
        import inspect
        sig = inspect.signature(ae.RecoveryController.decide)
        for name in sig.parameters:
            self.assertIn(name, ("self", "node", "action", "failure_class",
                                 "goal_context"),
                          "decide must not accept a page or a locator")

    def test_the_controller_never_performs_an_action(self):
        """Recovery decisions are advice; execution stays with the caller.

        If the controller could act, bounding it would require trusting it to
        honour its own limits, and a bug there would reach the browser.
        """
        import inspect
        src = inspect.getsource(ae.RecoveryController)
        for forbidden in ("await", "click", "goto", "fill(", "page."):
            self.assertNotIn(forbidden, src,
                             f"the controller must not contain {forbidden!r}")

    def test_recovery_never_advances_the_goal(self):
        """Recovery bookkeeping must not look like progress.

        A run that counted its recovery attempts as goal progress would report
        a task as advanced when the agent only retried it.
        """
        c = ae.RecoveryController()
        c.decide("n", "A", ae.FAIL_ACTION_TIMEOUT, CTX_A)
        self.assertFalse(hasattr(c, "verified"),
                         "the controller must hold no goal state of its own")
        self.assertFalse(hasattr(c, "progress"))


class TestReporting(unittest.TestCase):

    def test_summary_reports_nothing_needed_cleanly(self):
        self.assertIn("No recovery", ae.RecoveryController().summary())

    def test_summary_counts_by_class(self):
        c = ae.RecoveryController()
        c.decide("n", "A", ae.FAIL_ACTION_TIMEOUT, CTX_A)
        c.decide("n", "B", ae.FAIL_FUTILE_ACTION, CTX_A)
        text = c.summary()
        self.assertIn("2 recovery decision", text)
        self.assertIn("action_timeout", text)

    def test_summary_reports_the_stop_reason(self):
        c = ae.RecoveryController(max_total_decisions=1)
        c.decide("n0", "A", ae.FAIL_ACTION_TIMEOUT, CTX_A)
        c.decide("n1", "B", ae.FAIL_ACTION_TIMEOUT, CTX_A)
        self.assertIn("Stopped", c.summary())

    def test_a_recovery_run_is_never_reported_as_progress(self):
        """The word 'verified' must not appear in a recovery summary."""
        c = ae.RecoveryController()
        for _ in range(3):
            c.decide("n", "A", ae.FAIL_ACTION_TIMEOUT, CTX_A)
        self.assertNotIn("verified", c.summary().lower())


class TestDeterminism(unittest.TestCase):

    def test_identical_sequences_give_identical_decisions(self):
        def run():
            c = ae.RecoveryController()
            return [c.decide("n", "A", ae.FAIL_ACTION_TIMEOUT, CTX_A).outcome
                    for _ in range(8)]
        self.assertEqual(run(), run())

    def test_decisions_do_not_depend_on_context_hashing(self):
        """A tuple and its list form must key identically."""
        c = ae.RecoveryController()
        c.record_attempt("n", "A", ae.FAIL_ACTION_TIMEOUT, ["add two items"])
        self.assertEqual(c.attempts_for("n", "A", ae.FAIL_ACTION_TIMEOUT,
                                        ("add two items",)), 1)


if __name__ == "__main__":
    unittest.main()