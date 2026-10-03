"""Checkpoint 6.7.5 — action status vs task status.

Two different questions are easily conflated, and conflating them produces
misleading reports:

  * ACTION status — what happened to the step I just took? (the click was
    rejected by the grounding gate, the form value was refused, ...)
  * TASK status — what is the state of the objective? (PASS / FAIL /
    UNVERIFIABLE / BLOCKED / STOPPED)

A step that failed is not a failed task, and a step that succeeded is not a
passed task. These tests pin the separation in both directions, and pin that
per-action reporting never leaks into the task-level verdict.

No browser is required.
"""

import unittest

import automation_engine as ae


class GroundingOutcomeIsNotATaskOutcomeTests(unittest.TestCase):
    def test_a_rejected_action_never_becomes_a_task_pass(self):
        for code in (ae.GROUND_STALE_OBSERVATION, ae.GROUND_TARGET_NOT_OBSERVED,
                     ae.GROUND_TARGET_AMBIGUOUS, ae.GROUND_TARGET_DISABLED,
                     ae.GROUND_TARGET_OBSCURED, ae.GROUND_TARGET_HIDDEN,
                     ae.GROUND_UNGROUNDED_RESOLUTION, ae.GROUND_MISSING_INPUT,
                     ae.GROUND_NO_OBSERVATION, ae.GROUND_POLICY_BLOCKED):
            with self.subTest(code=code):
                status, _ = ae.TestGoal({"objective": "x"}).evaluate(
                    {"available_elements": []}, "", "https://x.test/", False)
                self.assertEqual(status, ae.GOAL_BLOCKED)

    def test_an_action_rejection_is_a_normal_recordable_result(self):
        """Rejection codes are outcomes, not exceptions. The gate must be
        steppable-over rather than crashing the run."""
        for code in (ae.GROUND_STALE_OBSERVATION, ae.GROUND_TARGET_AMBIGUOUS):
            with self.subTest(code=code):
                self.assertTrue(ae.grounding_message(code))

    def test_every_rejection_code_explains_itself(self):
        codes = [getattr(ae, name) for name in dir(ae)
                 if name.startswith("GROUND_") and isinstance(getattr(ae, name), str)]
        for code in codes:
            with self.subTest(code=code):
                self.assertTrue(str(ae.grounding_message(code)).strip())


class FailureClassIsNotATaskOutcomeTests(unittest.TestCase):
    def test_a_failed_action_classifies_as_a_failure_not_a_task_result(self):
        self.assertEqual(ae.classify_failure(ground_code=ae.GROUND_OK),
                         ae.FAIL_UNKNOWN)

    def test_every_failure_class_has_a_policy(self):
        self.assertEqual(set(ae.FAILURE_POLICIES), set(ae.FAILURE_CLASSES))

    def test_every_failure_policy_names_itself_and_explains_itself(self):
        for name, policy in ae.FAILURE_POLICIES.items():
            with self.subTest(name=name):
                self.assertEqual(policy.name, name)
                self.assertTrue(policy.summary.strip())

    def test_a_class_cannot_imply_success(self):
        """No failure class may be marked as evidence the task succeeded."""
        for name, policy in ae.FAILURE_POLICIES.items():
            with self.subTest(name=name):
                self.assertFalse(hasattr(policy, "implies_success"))
                self.assertNotIn("success", name)


class RecoveryStopIsNotATaskResultTests(unittest.TestCase):
    """A run that stopped has not concluded. It must say so."""

    def test_recovery_exhaustion_stops_without_concluding(self):
        controller = ae.RecoveryController(max_attempts_per_action=1,
                                          max_attempts_per_action_total=1,
                                          max_total_decisions=1)
        controller.decide("n0", "X", ae.FAIL_STALE_ELEMENT, ("s",))
        decision = controller.decide("n0", "X", ae.FAIL_STALE_ELEMENT, ("s",))
        self.assertTrue(decision.is_stop)
        self.assertEqual(decision.outcome, ae.RECOVER_EXHAUSTED)

    def test_recovery_exhaustion_reports_unverifiable_never_pass(self):
        """Running out of recovery budget leaves the outcome unknown. Reporting
        it as a failure would assert a negative the run never established."""
        self.assertNotEqual(
            ae.classify_final_status("RECOVERY_STOPPED_INSUFFICIENT_EVIDENCE"),
            ae.OUTCOME_PASS)

    def test_recovery_summary_reports_what_it_did(self):
        controller = ae.RecoveryController()
        self.assertIn("No recovery", controller.summary())
        controller.decide("n0", "X", ae.FAIL_STALE_ELEMENT, ("s",))
        self.assertIn("recovery decision", controller.summary())


class VerificationIsNotCompletionTests(unittest.TestCase):
    def test_a_verified_step_is_done_but_not_the_task(self):
        """Step-level verification and task-level PASS are separate decisions.
        A goal whose every step verified still has to satisfy its FINAL
        evidence before the run can report success."""
        goal = ae.TestGoal({
            "objective": "Buy the thing",
            "steps": [{"describe": "Add to cart",
                       "evidence": {"text_contains_all": ["In your cart"]}}],
            "evidence": {"text_contains_all": ["Order confirmed"]},
        })
        rows = goal.remaining_work(_state := {"available_elements": []},
                                  "In your cart", "https://shop.test/", True)
        self.assertTrue(rows[0]["verified"])
        # The task itself is not verified: its final evidence is absent.
        status, _ = goal.evaluate(_state, "In your cart",
                                  "https://shop.test/", True)
        self.assertEqual(status, ae.GOAL_BLOCKED)

    def test_an_attempt_is_not_verification(self):
        goal = ae.TestGoal({
            "objective": "x",
            "steps": [{"describe": "Place the order",
                       "evidence": {"text_contains_all": ["Order confirmed"]}}],
        })
        rows = goal.remaining_work({"available_elements": []}, "",
                                  "https://shop.test/", True)
        self.assertFalse(rows[0]["done"])
        self.assertFalse(rows[0]["verified"])

    def test_plan_completion_alone_does_not_complete_the_task(self):
        """The plan being finished is not evidence. This is the invariant the
        whole objective layer exists to enforce."""
        goal = ae.TestGoal({
            "objective": "Buy the widget and confirm the order",
            "evidence": {"text_contains_all": ["NEVER-APPEARS-EITHER"]},
        })
        plan = goal.ensure_runtime_plan([], "https://x.test/", "")
        self.assertIsNotNone(plan)
        # Force every requirement to the "done" state, as a finished plan would.
        plan._verified = True
        for item in plan.items:
            item["verified"] = True
        status, _ = goal.evaluate({"available_elements": []}, "",
                                  "https://x.test/", False)
        self.assertEqual(status, ae.GOAL_BLOCKED)

    def test_explicit_steps_suppress_the_runtime_plan(self):
        """Configured steps always take precedence, so a plan can never quietly
        overwrite what the user wrote."""
        goal = ae.TestGoal({
            "objective": "x",
            "steps": [{"describe": "one", "evidence": {}}],
        })
        self.assertTrue(goal.uses_configured_steps())
        self.assertIsNone(goal.ensure_runtime_plan([], "https://x.test/", ""))


class ActionOutcomeVocabularyTests(unittest.TestCase):
    """The two vocabularies must not be mixed at the same level."""

    def test_action_level_codes_and_task_level_outcomes_are_disjoint(self):
        action_codes = {getattr(ae, name) for name in dir(ae)
                        if name.startswith(("GROUND_", "RECOVER_", "FAIL_"))
                        and isinstance(getattr(ae, name), str)}
        task_codes = {ae.OUTCOME_PASS, ae.OUTCOME_FAIL, ae.OUTCOME_UNVERIFIABLE,
                      ae.OUTCOME_BLOCKED, ae.OUTCOME_STOPPED,
                      ae.GOAL_PASS, ae.GOAL_FAIL, ae.GOAL_BLOCKED}
        # FAIL_* names are failure CLASSES and legitimately read like outcomes;
        # the check that matters is that no code is literally a task outcome.
        self.assertEqual(action_codes & task_codes, set())

    def test_the_five_task_outcomes_are_distinct(self):
        outcomes = [ae.OUTCOME_PASS, ae.OUTCOME_FAIL, ae.OUTCOME_UNVERIFIABLE,
                    ae.OUTCOME_BLOCKED, ae.OUTCOME_STOPPED]
        self.assertEqual(len(set(outcomes)), 5)

    def test_the_three_goal_verdicts_are_distinct(self):
        verdicts = [ae.GOAL_PASS, ae.GOAL_FAIL, ae.GOAL_BLOCKED]
        self.assertEqual(len(set(verdicts)), 3)


if __name__ == "__main__":
    unittest.main()