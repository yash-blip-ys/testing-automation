"""Part 4.1 — Failure classification.

The claim under test is that "what do we do now" is answered by a pure function
of observed facts, never by the model improvising. These tests pin:

  * every failure class the run can produce has a documented policy
  * classification is deterministic and does not depend on argument order
  * precedence is fixed, and terminal boundaries cannot be overridden
  * the required taxonomy is fully covered
  * no class, under any circumstance, implies the goal advanced

Run with:
    .\\venv311\\Scripts\\python.exe -m unittest tests.test_failure_class -v
"""

import inspect
import itertools
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae


class TestTaxonomyIsComplete(unittest.TestCase):

    def test_every_required_failure_class_exists(self):
        """The eleven classes the contract names must all be present."""
        required = {
            "target_not_found": ae.FAIL_TARGET_NOT_FOUND,
            "target_ambiguous": ae.FAIL_TARGET_AMBIGUOUS,
            "stale_element": ae.FAIL_STALE_ELEMENT,
            "action_timeout": ae.FAIL_ACTION_TIMEOUT,
            "navigation_timeout": ae.FAIL_NAVIGATION_TIMEOUT,
            "unexpected_page_state": ae.FAIL_UNEXPECTED_STATE,
            "form_validation_failure": ae.FAIL_FORM_VALIDATION,
            "repeated_no_progress": ae.FAIL_REPEATED_NO_PROGRESS,
            "model_output_invalid": ae.FAIL_MODEL_OUTPUT_INVALID,
            "insufficient_evidence": ae.FAIL_INSUFFICIENT_EVIDENCE,
            "safety_boundary_reached": ae.FAIL_SAFETY_BOUNDARY,
        }
        for label, value in required.items():
            self.assertIn(value, ae.FAILURE_CLASSES,
                          f"{label} must be a declared failure class")

    def test_every_class_has_a_policy(self):
        self.assertEqual(ae.FAILURE_CLASSES, set(ae.FAILURE_POLICIES),
                         "a class with no policy has no defined response")

    def test_no_policy_exists_for_an_undeclared_class(self):
        """A policy for a class that cannot occur is dead or misleading."""
        self.assertEqual(set(ae.FAILURE_POLICIES), ae.FAILURE_CLASSES)

    def test_every_policy_documents_all_four_questions(self):
        """Retry / re-observe / alternative / clarify must all be stated.

        Documented as a real per-class decision, not left to default: a policy
        is only useful if someone can read why it says what it says.
        """
        for name, policy in ae.FAILURE_POLICIES.items():
            self.assertTrue(policy.summary, f"{name} has no documented summary")
            self.assertIsInstance(policy.retry_safe, bool)
            self.assertIsInstance(policy.reobserve_required, bool)
            self.assertIsInstance(policy.alternative_allowed, bool)
            self.assertIsInstance(policy.needs_user_clarification, bool)
            self.assertIsInstance(policy.must_stop, bool)

    def test_every_policy_states_when_the_run_must_stop(self):
        """The fifth question: when must the system stop?

        Checked structurally — a terminal class must say so, and a non-terminal
        class must be allowed to keep going within budget.
        """
        for name, policy in ae.FAILURE_POLICIES.items():
            self.assertGreaterEqual(policy.max_attempts, 1,
                                    f"{name} must allow at least one attempt")
            if policy.must_stop:
                self.assertEqual(policy.max_attempts, 1,
                                 f"{name} is terminal, so attempts are moot")
                self.assertFalse(policy.alternative_allowed,
                                 f"{name} is terminal, so no alternative may "
                                 f"be offered")


class TestTerminalBoundaries(unittest.TestCase):

    def test_access_control_is_terminal_and_forbids_alternatives(self):
        p = ae.failure_policy(ae.FAIL_ACCESS_CONTROL)
        self.assertTrue(p.must_stop)
        self.assertFalse(p.alternative_allowed)
        self.assertFalse(p.retry_safe)

    def test_safety_boundary_is_terminal(self):
        p = ae.failure_policy(ae.FAIL_SAFETY_BOUNDARY)
        self.assertTrue(p.must_stop)
        self.assertFalse(p.retry_safe)
        self.assertTrue(p.needs_user_clarification,
                        "the decision belongs to the user")

    def test_safety_boundary_reached_via_grounding_code(self):
        for code in (ae.GROUND_AWAITING_CONFIRMATION, ae.GROUND_POLICY_BLOCKED):
            self.assertEqual(ae.classify_failure(ground_code=code),
                             ae.FAIL_SAFETY_BOUNDARY)

    def test_nothing_overrides_a_terminal_boundary(self):
        """Terminal wins even when every other fact points at recoverability."""
        self.assertEqual(
            ae.classify_failure(access_control=True, node_changed=True,
                                goal_advanced=True, model_invalid=False),
            ae.FAIL_ACCESS_CONTROL)
        self.assertEqual(
            ae.classify_failure(ground_code=ae.GROUND_POLICY_BLOCKED,
                                node_changed=False, url_changed=False),
            ae.FAIL_SAFETY_BOUNDARY)

    def test_insufficient_evidence_stops_the_run(self):
        p = ae.failure_policy(ae.FAIL_INSUFFICIENT_EVIDENCE)
        self.assertTrue(p.must_stop,
                        "unverifiable work must not be pursued indefinitely")


class TestClassificationIsDeterministic(unittest.TestCase):

    ARGUMENTS = ("ground_code", "action_tag", "outcome", "failure_reason",
                 "node_changed", "url_changed", "semantic_changed",
                 "goal_advanced", "consecutive_no_progress", "model_invalid",
                 "access_control", "missing_evidence_for")

    def _sample_inputs(self):
        """A spread of realistic fact combinations, including all-None."""
        return [
            {},
            {"ground_code": ae.GROUND_TARGET_AMBIGUOUS},
            {"ground_code": ae.GROUND_STALE_OBSERVATION},
            {"ground_code": ae.GROUND_TARGET_OBSCURED},
            {"action_tag": ae.ACTION_FAILED_TIMEOUT},
            {"action_tag": ae.ACTION_FAILED_REJECTED_VALUE},
            {"action_tag": ae.ACTION_FAILED_NOT_APPLICABLE},
            {"outcome": "failed_not_visible"},
            {"outcome": "failed_scroll"},
            {"outcome": "failed_click"},
            {"node_changed": True, "goal_advanced": False},
            {"node_changed": False, "url_changed": False,
             "semantic_changed": False},
            {"node_changed": True, "failure_reason": "form validation failed"},
            {"consecutive_no_progress": 2},
            {"model_invalid": True},
            {"missing_evidence_for": "complete checkout"},
            {"access_control": True},
            {"failure_reason": "Timeout 4000ms exceeded"},
        ]

    def test_same_inputs_always_give_the_same_class(self):
        for kwargs in self._sample_inputs():
            results = {ae.classify_failure(**kwargs) for _ in range(20)}
            self.assertEqual(len(results), 1,
                             f"{kwargs} produced inconsistent classes")

    def test_classification_does_not_mutate_its_arguments(self):
        """A classifier that mutates its inputs is not a classifier."""
        kwargs = {"ground_code": ae.GROUND_TARGET_AMBIGUOUS, "outcome": "failed_click"}
        before = dict(kwargs)
        ae.classify_failure(**kwargs)
        self.assertEqual(kwargs, before)

    def test_every_classification_is_a_declared_class(self):
        for kwargs in self._sample_inputs():
            self.assertIn(ae.classify_failure(**kwargs), ae.FAILURE_CLASSES)

    def test_keyword_arguments_are_independent_of_each_other(self):
        """Supplying an irrelevant fact must not change the answer.

        This is what makes the precedence rules meaningful: if a redundant
        argument could flip the result, the rules are order-sensitive and a
        future edit could silently change recovery behaviour.
        """
        for kwargs in self._sample_inputs():
            baseline = ae.classify_failure(**kwargs)
            for name in self.ARGUMENTS:
                if name in kwargs:
                    continue
                probe = dict(kwargs)
                # A fact that cannot apply: None for scalars, empty for the
                # two that take truthy signals.
                probe[name] = None
                self.assertEqual(
                    ae.classify_failure(**probe), baseline,
                    f"adding an unused {name!r} changed "
                    f"{kwargs!r} from {baseline!r}")


class TestPrecedence(unittest.TestCase):

    def test_ground_code_wins_over_outcome(self):
        """The gate's precise code is more informative than a ledger outcome."""
        self.assertEqual(
            ae.classify_failure(ground_code=ae.GROUND_TARGET_AMBIGUOUS,
                                outcome="failed_click"),
            ae.FAIL_TARGET_AMBIGUOUS)

    def test_action_tag_wins_over_outcome(self):
        self.assertEqual(
            ae.classify_failure(action_tag=ae.ACTION_FAILED_NO_SUCH_OPTION,
                                outcome="failed_click"),
            ae.FAIL_NO_SUCH_OPTION)

    def test_ground_code_wins_over_action_tag(self):
        """The gate rejects before the executor runs, so its code is nearer
        the cause."""
        self.assertEqual(
            ae.classify_failure(ground_code=ae.GROUND_TARGET_DISABLED,
                                action_tag=ae.ACTION_FAILED_TIMEOUT),
            ae.FAIL_TARGET_DISABLED)

    def test_model_invalid_wins_over_state_facts(self):
        """Nothing was executed, so there is no post-action state to trust."""
        self.assertEqual(
            ae.classify_failure(model_invalid=True, node_changed=False,
                                url_changed=False, semantic_changed=False,
                                outcome="failed_click"),
            ae.FAIL_MODEL_OUTPUT_INVALID)

    def test_insufficient_evidence_wins_over_state(self):
        self.assertEqual(
            ae.classify_failure(missing_evidence_for="complete checkout",
                                node_changed=True),
            ae.FAIL_INSUFFICIENT_EVIDENCE)

    def test_stall_wins_over_timeout_text(self):
        """A sustained stall must not be classified as a retryable timeout.

        If it were, the controller would hand back the very action that has
        already failed several times in a row.
        """
        self.assertEqual(
            ae.classify_failure(consecutive_no_progress=3,
                                failure_reason="timeout"),
            ae.FAIL_REPEATED_NO_PROGRESS)

    def test_stall_policy_forbids_retrying_the_same_action(self):
        p = ae.failure_policy(ae.FAIL_REPEATED_NO_PROGRESS)
        self.assertFalse(p.retry_safe)
        self.assertTrue(p.alternative_allowed)

    def test_unknown_facts_yield_unknown_not_a_guess(self):
        """Silence must not be resolved into a specific invented cause."""
        self.assertEqual(ae.classify_failure(), ae.FAIL_UNKNOWN)
        self.assertEqual(ae.classify_failure(outcome="failed_click"),
                         ae.FAIL_UNEXPECTED_STATE)

    def test_unknown_class_gets_a_conservative_policy_not_a_crash(self):
        p = ae.failure_policy("a_class_that_does_not_exist")
        self.assertIs(p, ae.FAILURE_POLICIES[ae.FAIL_UNKNOWN])
        self.assertFalse(p.must_stop,
                         "an unrecognised class must not silently stop the run")


class TestFailedActionVersusUnexpectedResult(unittest.TestCase):

    """The distinction the contract calls out explicitly.

    A failed action did not do what was asked. An action that succeeded but
    landed somewhere unexpected DID do what was asked. They call for different
    responses, so they must not collapse into one class.
    """

    def test_nothing_happened_is_futile(self):
        self.assertEqual(
            ae.classify_failure(node_changed=False, url_changed=False,
                                semantic_changed=False),
            ae.FAIL_FUTILE_ACTION)

    def test_changed_without_progress_is_futile(self):
        self.assertEqual(
            ae.classify_failure(node_changed=True, goal_advanced=False),
            ae.FAIL_FUTILE_ACTION)

    def test_futile_policy_disables_repeating_the_same_action(self):
        self.assertFalse(ae.failure_policy(ae.FAIL_FUTILE_ACTION).retry_safe)

    def test_the_two_are_distinct_classes(self):
        self.assertNotEqual(ae.FAIL_FUTILE_ACTION, ae.FAIL_UNEXPECTED_STATE)


class TestTimeoutPolicyRequiresReobservation(unittest.TestCase):

    def test_timeouts_require_reobservation_before_a_retry(self):
        """A timed-out action may have taken effect.

        Retrying without re-reading could apply it twice, which for a
        consequential action is not a small mistake.
        """
        for cls in (ae.FAIL_ACTION_TIMEOUT, ae.FAIL_NAVIGATION_TIMEOUT):
            self.assertTrue(ae.failure_policy(cls).reobserve_required)
            self.assertTrue(ae.failure_policy(cls).retry_safe)
            self.assertGreaterEqual(ae.failure_policy(cls).max_attempts, 2,
                                    "a slow page deserves more than one try")

    def test_navigation_timeout_is_distinguished_from_action_timeout(self):
        self.assertEqual(
            ae.classify_failure(node_changed=True,
                                failure_reason="Navigation timeout exceeded"),
            ae.FAIL_NAVIGATION_TIMEOUT)
        self.assertEqual(
            ae.classify_failure(node_changed=True, failure_reason="click timeout"),
            ae.FAIL_ACTION_TIMEOUT)


class TestStateDependentFailuresRequireReobservation(unittest.TestCase):

    def test_state_dependent_classes_require_a_fresh_observation(self):
        for cls in (ae.FAIL_TARGET_NOT_FOUND, ae.FAIL_STALE_ELEMENT,
                    ae.FAIL_TARGET_AMBIGUOUS, ae.FAIL_TARGET_DISABLED,
                    ae.FAIL_TARGET_HIDDEN, ae.FAIL_UNEXPECTED_STATE,
                    ae.FAIL_FORM_VALIDATION):
            self.assertTrue(ae.failure_policy(cls).reobserve_required,
                            f"{cls} depends on current page state")

    def test_repeating_them_verbatim_is_never_marked_safe(self):
        """Re-reading is the response; repeating is not.

        Every state-dependent class must forbid blind retry, because the fact
        that made it fail was a property of the state, not of the request.
        """
        for cls in (ae.FAIL_TARGET_NOT_FOUND, ae.FAIL_STALE_ELEMENT,
                    ae.FAIL_TARGET_AMBIGUOUS, ae.FAIL_TARGET_DISABLED,
                    ae.FAIL_TARGET_HIDDEN, ae.FAIL_UNEXPECTED_STATE,
                    ae.FAIL_FORM_VALIDATION, ae.FAIL_REPEATED_NO_PROGRESS,
                    ae.FAIL_FUTILE_ACTION):
            self.assertFalse(ae.failure_policy(cls).retry_safe,
                             f"{cls} must not invite a verbatim retry")
            self.assertEqual(ae.failure_policy(cls).max_attempts, 1,
                             f"{cls} must not be retried at all")

    def test_obscured_is_the_one_state_class_that_may_retry(self):
        """A covering overlay is transient by nature — dismiss and retry."""
        p = ae.failure_policy(ae.FAIL_TARGET_OBSCURED)
        self.assertTrue(p.retry_safe)
        self.assertTrue(p.reobserve_required)


class TestClarificationIsReservedForUserIntent(unittest.TestCase):

    def test_only_user_intent_classes_ask_the_user(self):
        """Asking must mean 'only the user knows', never 'the agent is stuck'.

        If ordinary failures requested clarification the tool would become
        unusable: every timeout would become a prompt.
        """
        asking = {c for c, p in ae.FAILURE_POLICIES.items()
                  if p.needs_user_clarification}
        self.assertEqual(asking, {
            ae.FAIL_MISSING_INPUT,
            ae.FAIL_VALUE_REJECTED,
            ae.FAIL_INSUFFICIENT_EVIDENCE,
            ae.FAIL_SAFETY_BOUNDARY,
        })

    def test_a_missing_value_is_not_invented(self):
        p = ae.failure_policy(ae.FAIL_MISSING_INPUT)
        self.assertTrue(p.needs_user_clarification)
        self.assertFalse(p.retry_safe)

    def test_a_rejected_value_is_not_silently_replaced(self):
        p = ae.failure_policy(ae.FAIL_VALUE_REJECTED)
        self.assertTrue(p.needs_user_clarification)


class TestClassificationCannotImplySuccess(unittest.TestCase):

    def test_classify_failure_returns_only_failures(self):
        """No input may make the classifier return something that is not a
        failure class — in particular never a success or pass marker."""
        for kwargs in ({"ground_code": ae.GROUND_TARGET_AMBIGUOUS},
                       {"node_changed": True, "goal_advanced": True},
                       {"outcome": "victory"},
                       {}):
            result = ae.classify_failure(**kwargs)
            self.assertIn(result, ae.FAILURE_CLASSES)

    def test_no_failure_class_name_claims_success(self):
        for name in ae.FAILURE_CLASSES:
            self.assertNotIn("success", name.lower())
            self.assertNotIn("pass", name.lower())

    def test_classifier_takes_no_model_call_and_no_io(self):
        """Determinism depends on there being nothing else in here.

        The docstring is stripped first: it legitimately mentions the page and
        the model while explaining what the function does not do.
        """
        import ast
        tree = ast.parse(inspect.getsource(ae.classify_failure).lstrip())
        body = ast.Module(body=tree.body, type_ignores=[])
        for node in ast.walk(body):
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                node.value.value = ""
        code = ast.unparse(body)
        for forbidden in ("ollama", "ask_ai", "await", "print(", "open(",
                          "requests", "subprocess"):
            self.assertNotIn(forbidden, code,
                             f"classify_failure must not contain {forbidden!r}")

    def test_classifier_signature_is_keyword_only_and_closed(self):
        """A caller must not be able to pass facts in the wrong order."""
        params = inspect.signature(ae.classify_failure).parameters
        for name, param in params.items():
            self.assertEqual(param.kind, inspect.Parameter.KEYWORD_ONLY,
                             f"{name} must be keyword-only so a caller cannot "
                             f"pass facts in the wrong order")

    def test_optional_facts_default_to_absent_not_to_false(self):
        """An unobserved fact must not default to a value that reads as data.

        `node_changed=False` would assert the node did not change. Omitting the
        argument must instead mean "not known", which the classifier treats as
        unknown rather than as evidence.
        """
        params = inspect.signature(ae.classify_failure).parameters
        for name in ("ground_code", "action_tag", "outcome", "failure_reason",
                     "node_changed", "url_changed", "semantic_changed",
                     "goal_advanced", "consecutive_no_progress",
                     "missing_evidence_for"):
            self.assertIsNone(params[name].default,
                              f"{name} must default to None (absent), not to a "
                              f"value that would read as an observation")
        # The two boolean flags may default to False: absent genuinely does
        # mean "this did not happen", and the distinction would be meaningless.
        for name in ("model_invalid", "access_control"):
            self.assertIs(params[name].default, False)

    def test_absent_and_explicitly_false_are_distinguished(self):
        """Omitting a fact must be weaker than observing that it did not happen."""
        omitted = ae.classify_failure()
        explicit = ae.classify_failure(node_changed=False, url_changed=False,
                                       semantic_changed=False)
        self.assertEqual(omitted, ae.FAIL_UNKNOWN)
        self.assertEqual(explicit, ae.FAIL_FUTILE_ACTION,
                         "an observed non-change is evidence; an absent fact "
                         "is not")


class TestRejectionCodesArePreserved(unittest.TestCase):

    """The gate already distinguishes these precisely.

    Collapsing them into one bucket would discard exactly what recovery needs,
    so every code must map to a class that reflects its own meaning.
    """

    def test_every_ground_code_maps_to_a_class(self):
        """Every gate rejection must reach a real class, not slip through."""
        for code in (ae.GROUND_NO_OBSERVATION, ae.GROUND_STALE_OBSERVATION,
                     ae.GROUND_TARGET_NOT_OBSERVED, ae.GROUND_TARGET_AMBIGUOUS,
                     ae.GROUND_TARGET_DISABLED, ae.GROUND_TARGET_OBSCURED,
                     ae.GROUND_TARGET_HIDDEN, ae.GROUND_UNGROUNDED_RESOLUTION,
                     ae.GROUND_MISSING_INPUT, ae.GROUND_AWAITING_CONFIRMATION,
                     ae.GROUND_POLICY_BLOCKED):
            self.assertIn(ae.classify_failure(ground_code=code),
                          ae.FAILURE_CLASSES,
                          f"{code} must classify, not fall through")

    def test_codes_with_different_meanings_do_not_collapse(self):
        """Only genuinely equivalent codes may share a class.

        `no_observation` and `stale_observation` do collapse, because both mean
        "this reference is not valid against the current observation" and they
        carry an identical policy. Codes whose meanings differ must stay apart,
        because recovery responds to each differently.
        """
        must_stay_distinct = [
            ae.GROUND_TARGET_NOT_OBSERVED, ae.GROUND_TARGET_AMBIGUOUS,
            ae.GROUND_TARGET_DISABLED, ae.GROUND_TARGET_OBSCURED,
            ae.GROUND_TARGET_HIDDEN, ae.GROUND_UNGROUNDED_RESOLUTION,
            ae.GROUND_MISSING_INPUT,
        ]
        classes = [ae.classify_failure(ground_code=c) for c in must_stay_distinct]
        self.assertEqual(len(set(classes)), len(classes),
                         "distinct rejection codes must not share a class")

    def test_the_two_reference_codes_share_one_class_deliberately(self):
        """Documented collapse, asserted so it stays deliberate."""
        self.assertEqual(ae.classify_failure(ground_code=ae.GROUND_NO_OBSERVATION),
                         ae.classify_failure(
                             ground_code=ae.GROUND_STALE_OBSERVATION))
        shared = ae.failure_policy(ae.FAIL_STALE_ELEMENT)
        self.assertFalse(shared.retry_safe)
        self.assertTrue(shared.reobserve_required)

    def test_every_action_tag_maps_to_a_class(self):
        for tag in (ae.ACTION_FAILED_NOT_APPLICABLE, ae.ACTION_FAILED_DISABLED,
                    ae.ACTION_FAILED_TIMEOUT, ae.ACTION_FAILED_NO_SUCH_OPTION,
                    ae.ACTION_FAILED_REJECTED_VALUE):
            self.assertIn(ae.classify_failure(action_tag=tag),
                          ae.FAILURE_CLASSES)

    def test_an_unrecognised_ground_code_is_unknown_not_a_crash(self):
        self.assertEqual(ae.classify_failure(ground_code="something_new"),
                         ae.FAIL_UNKNOWN)


if __name__ == "__main__":
    unittest.main()