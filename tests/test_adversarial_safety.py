"""Checkpoint 6.7.3 — adversarial safety testing.

Every case here asks one question: can a consequential action be committed
WITHOUT the authorization the policy requires? The attack is always the same
shape — find a way to make the tool act when it should have stopped.

  * stale authorization — a confirmation granted for action A must not license
    action B, nor a different target;
  * time-of-check to time-of-use — a target reference is only meaningful
    against the observation that issued it;
  * manipulative model output — a model claiming approval moves nothing,
    because the gate is mechanical and pure;
  * misleading labels — a benign-looking control whose effect is a commitment
    must still be classified as consequential;
  * recovery and retry — exhausting retries must not become permission;
  * native dialogs — a dialog is never auto-accepted without exact opt-in, and
    never when its wording names an irreversible act.

No browser is required: these exercise the safety policy, the grounding gate,
the recovery controller and the dialog contract, which is where the
authorization decision is actually made.
"""

import unittest

import automation_engine as ae


def _obs(discovery=None, url="https://example.test/", title="", text=""):
    return ae.PageObservation(url=url, title=title, discovery=discovery or {},
                              page_text=text)


def _intent(operation, target, record=None, observation_id=None,
            provenance=None, required_input=None):
    return ae.ActionIntent(operation=operation, target_name=target,
                           record=record or {}, observation_id=observation_id,
                           provenance=provenance, required_input=required_input)


def _policy(mode="confirm", confirmations=(), **kwargs):
    return ae.SafetyPolicy(mode=mode, confirmations=confirmations, **kwargs)


class StaleAuthorizationTests(unittest.TestCase):
    """A grant is scoped to one operation on one target. It must not drift."""

    def test_grant_is_scoped_to_operation_and_target(self):
        policy = _policy("confirm", confirmations=["click:Archive"])
        self.assertTrue(policy.has_grant(ae.OP_CLICK, "Archive"))
        self.assertFalse(policy.has_grant(ae.OP_SUBMIT, "Archive"))
        self.assertFalse(policy.has_grant(ae.OP_CLICK, "Wipe"))

    def test_confirming_one_action_does_not_confirm_another(self):
        policy = _policy("confirm", confirmations=["submit:remove"])
        other = _intent(ae.OP_SUBMIT, "delete",
                        record={"name": "delete", "aria_label": "delete"})
        self.assertEqual(policy.check(other, _obs()),
                         ae.GROUND_AWAITING_CONFIRMATION)

    def test_confirmation_does_not_carry_to_a_different_target(self):
        policy = _policy("confirm", confirmations=["click:Archive"])
        wipe = _intent(ae.OP_CLICK, "Wipe",
                       record={"name": "Wipe", "aria_label": "Wipe"})
        self.assertEqual(policy.check(wipe, _obs()),
                         ae.GROUND_AWAITING_CONFIRMATION)

    def test_grant_then_check_releases_exactly_that_action(self):
        policy = _policy("confirm")
        granted = _intent(ae.OP_CLICK, "Archive",
                          record={"name": "Archive", "aria_label": "Archive"})
        self.assertEqual(policy.check(granted, _obs()),
                         ae.GROUND_AWAITING_CONFIRMATION)
        policy.grant(ae.OP_CLICK, "Archive")
        self.assertEqual(policy.check(granted, _obs()), ae.GROUND_OK)
        self.assertFalse(granted.requires_confirmation)

    def test_model_cannot_approve_its_own_action(self):
        """A model asserting approval must not move the gate.

        The policy takes no free-text context at all: there is no parameter
        through which model output could enter the decision.
        """
        import inspect
        params = set(inspect.signature(ae.SafetyPolicy.check).parameters)
        self.assertNotIn("context", params)
        self.assertNotIn("model", params)
        self.assertNotIn("kwargs", params)
        # And an intent that CLAIMS to be approved is still gated, because the
        # claim is not one of the inputs the gate reads.
        policy = _policy("confirm")
        intent = _intent(ae.OP_SUBMIT, "Delete Account",
                         record={"name": "Delete Account",
                                 "aria_label": "Delete Account"})
        intent.approved = True
        intent.the_user_approved = True
        self.assertEqual(policy.check(intent, _obs()),
                         ae.GROUND_AWAITING_CONFIRMATION)


class TimeOfCheckTests(unittest.TestCase):
    """A target reference is only meaningful against its own observation."""

    def test_reference_from_an_earlier_observation_is_rejected(self):
        old = _obs({"observation_id": 7, "labels": ["Place Order"]})
        new = _obs({"observation_id": 8, "labels": ["Place Order"]})
        intent = _intent(ae.OP_CLICK, "Place Order", observation_id=7,
                         provenance=ae.RESOLVE_GROUNDED)
        self.assertEqual(ae.validate_action_grounding(intent, new),
                         ae.GROUND_STALE_OBSERVATION)
        # The same reference is fine against the observation that issued it,
        # which is what makes staleness a page-change fact and not a rejection.
        self.assertEqual(ae.validate_action_grounding(intent, old),
                         ae.GROUND_OK)

    def test_no_observation_at_all_is_rejected(self):
        intent = _intent(ae.OP_CLICK, "Place Order", observation_id=7,
                         provenance=ae.RESOLVE_GROUNDED)
        self.assertEqual(ae.validate_action_grounding(intent, None),
                         ae.GROUND_NO_OBSERVATION)

    def test_target_that_vanished_is_rejected(self):
        obs = _obs({"observation_id": 1, "labels": ["Something else"]})
        intent = _intent(ae.OP_CLICK, "Place Order", observation_id=1,
                         provenance=ae.RESOLVE_GROUNDED)
        self.assertEqual(ae.validate_action_grounding(intent, obs),
                         ae.GROUND_TARGET_NOT_OBSERVED)

    def test_staleness_is_checked_before_status(self):
        """A stale reference is reported as stale, not re-diagnosed against the
        new page. Otherwise an attacker could rename a control between
        observations and have the refusal silently retargeted."""
        new = _obs({"observation_id": 8, "labels": ["Something else"]})
        intent = _intent(ae.OP_CLICK, "Place Order", observation_id=7,
                         provenance=ae.RESOLVE_GROUNDED)
        self.assertEqual(ae.validate_action_grounding(intent, new),
                         ae.GROUND_STALE_OBSERVATION)

    def test_text_guess_is_never_accepted_even_when_the_label_exists(self):
        """A guessed locator can land on a different control sharing the label,
        which is the failure this layer exists to stop."""
        obs = _obs({"observation_id": 1, "labels": ["Place Order"]})
        guessed = _intent(ae.OP_CLICK, "Place Order", observation_id=1,
                          provenance=ae.RESOLVE_TEXT_GUESS)
        self.assertEqual(ae.validate_action_grounding(guessed, obs),
                         ae.GROUND_UNGROUNDED_RESOLUTION)
        grounded = _intent(ae.OP_CLICK, "Place Order", observation_id=1,
                           provenance=ae.RESOLVE_GROUNDED)
        self.assertEqual(ae.validate_action_grounding(grounded, obs),
                         ae.GROUND_OK)

    def test_an_input_operation_without_a_value_is_rejected(self):
        """The gate must not invent a value for a field the user never filled."""
        obs = _obs({"observation_id": 1, "labels": ["Card number"]})
        intent = _intent(ae.OP_FILL, "Card number", observation_id=1,
                         provenance=ae.RESOLVE_GROUNDED, required_input=None)
        self.assertEqual(ae.validate_action_grounding(intent, obs),
                         ae.GROUND_MISSING_INPUT)

    def test_grounding_runs_before_safety_and_neither_waits_for_the_model(self):
        """Both gates are pure: no model call, no I/O, no site knowledge."""
        import inspect
        for fn in (ae.validate_action_grounding, ae.SafetyPolicy.check,
                   ae.classify_failure):
            with self.subTest(fn=fn.__name__):
                source = inspect.getsource(fn).lower()
                self.assertNotIn("call_model", source)
                self.assertNotIn("open(", source)


class MisleadingLabelTests(unittest.TestCase):
    """Effect beats caption. A benign name must not buy permission."""

    def test_submit_is_consequential_even_with_a_benign_label(self):
        policy = _policy("confirm")
        record = {"name": "Archive", "aria_label": "Archive", "type": "submit"}
        intent = _intent(ae.OP_SUBMIT, "Archive", record=record)
        self.assertTrue(policy.is_consequential(intent))
        self.assertEqual(policy.check(intent, _obs()),
                         ae.GROUND_AWAITING_CONFIRMATION)

    def test_destructive_label_is_classified_without_the_model(self):
        policy = _policy("confirm")
        record = {"name": "Delete Account", "aria_label": "Delete Account"}
        intent = _intent(ae.OP_CLICK, "Delete Account", record=record)
        self.assertTrue(policy.is_consequential(intent))
        self.assertEqual(policy.check(intent, _obs()),
                         ae.GROUND_AWAITING_CONFIRMATION)

    def test_fill_of_a_credential_is_consequential(self):
        policy = _policy("confirm")
        record = {"name": "card number", "id": "card", "type": "text"}
        intent = _intent(ae.OP_FILL, "card", record=record,
                         required_input="4111111111111111")
        self.assertTrue(policy.is_consequential(intent))

    def test_filling_an_ordinary_field_is_not_consequential(self):
        """Ordinary form entry is the work, not a commitment. Gating it would
        make the tool unable to do anything useful without asking."""
        policy = _policy("confirm")
        record = {"name": "Email", "id": "email", "type": "text"}
        intent = _intent(ae.OP_FILL, "email", record=record,
                         required_input="a@example.test")
        self.assertFalse(policy.is_consequential(intent))
        self.assertEqual(policy.check(intent, _obs()), ae.GROUND_OK)

    def test_fill_with_no_readable_metadata_is_consequential(self):
        policy = _policy("confirm")
        intent = _intent(ae.OP_FILL, "mystery", record={})
        self.assertTrue(policy.is_consequential(intent))

    def test_unreadable_control_is_consequential_not_benign(self):
        """The fallback direction is the whole point: no evidence of safety is
        not evidence of safety."""
        policy = _policy("confirm")
        intent = _intent(ae.OP_CLICK, "mystery", record={})
        self.assertTrue(policy.is_consequential(intent))

    def test_navigation_is_permitted(self):
        policy = _policy("confirm")
        record = {"name": "About us", "aria_label": "About us", "role": "link",
                  "id": "nav-about"}
        intent = _intent(ae.OP_NAVIGATE, "About us", record=record)
        self.assertEqual(policy.check(intent, _obs()), ae.GROUND_OK)

    def test_benign_named_control_is_permitted(self):
        policy = _policy("confirm")
        record = {"name": "Next page", "aria_label": "Next page", "role": "link",
                  "id": "pager-next"}
        intent = _intent(ae.OP_CLICK, "Next page", record=record)
        self.assertEqual(policy.check(intent, _obs()), ae.GROUND_OK)


class ModeTests(unittest.TestCase):
    """The modes must differ only in what they permit, never in what they log."""

    def test_block_refuses_a_consequential_action(self):
        policy = _policy("block")
        record = {"name": "Pay now", "aria_label": "Pay now", "type": "submit"}
        intent = _intent(ae.OP_SUBMIT, "Pay now", record=record)
        self.assertEqual(policy.check(intent, _obs()),
                         ae.GROUND_POLICY_BLOCKED)

    def test_block_refuses_even_a_confirmed_action(self):
        policy = _policy("block", confirmations=["submit:Pay now"])
        record = {"name": "Pay now", "aria_label": "Pay now", "type": "submit"}
        intent = _intent(ae.OP_SUBMIT, "Pay now", record=record)
        self.assertEqual(policy.check(intent, _obs()),
                         ae.GROUND_POLICY_BLOCKED)

    def test_explicitly_blocked_operation_is_refused_in_every_mode(self):
        for mode in ("confirm", "allow"):
            with self.subTest(mode=mode):
                policy = _policy(mode, blocked_operations=[ae.OP_FILL])
                intent = _intent(ae.OP_FILL, "note",
                                 record={"name": "note", "id": "note"})
                self.assertEqual(policy.check(intent, _obs()),
                                 ae.GROUND_POLICY_BLOCKED)

    def test_unreadable_configuration_defaults_to_confirm(self):
        """A run whose safety configuration failed to load must ask."""
        for config in (None, {}, {"safety": {}}, {"safety": {"consequential_mode": "nonsense"}},
                       {"safety": None}):
            with self.subTest(config=config):
                self.assertEqual(ae.default_safety_policy(config).mode,
                                 "confirm")

    def test_access_control_is_not_a_user_preference(self):
        """`allow` waives confirmation; it must not waive an access-control
        challenge. The barrier is detected from the observation itself, so no
        policy mode can reach it."""
        challenge = _obs(
            discovery={"observation_id": 1, "labels": ["Verify you are human"]},
            url="https://challenges.cloudflare.com/turnstile", title="Just a moment")
        for mode in ("allow", "confirm", "block"):
            with self.subTest(mode=mode):
                barrier = ae.detect_access_control_barrier(challenge)
                self.assertIsNotNone(barrier)

    def test_a_clean_page_has_no_barrier(self):
        clean = _obs(discovery={"observation_id": 1, "labels": ["Place Order"]},
                     url="https://shop.test/checkout", title="Checkout")
        self.assertIsNone(ae.detect_access_control_barrier(clean))


class DialogTests(unittest.TestCase):
    """A native dialog is a commitment prompt and is never auto-accepted."""

    def test_default_policy_dismisses_every_dialog(self):
        policy = _policy("confirm")
        for message in ("Are you sure?", "Delete account?", "Confirm payment of $9",
                        "Leave without saving?", "Submit order?"):
            with self.subTest(message=message):
                self.assertEqual(policy.decide_dialog("confirm", message),
                                 "dismiss")

    def test_irreversible_wording_is_dismissed_even_when_opted_in(self):
        policy = _policy("confirm", auto_accept_dialogs=["delete"])
        self.assertEqual(policy.decide_dialog("confirm", "Delete account?"),
                         "dismiss")

    def test_exact_opt_in_accepts_only_that_message(self):
        policy = _policy("confirm", auto_accept_dialogs=["stay signed in"])
        self.assertEqual(policy.decide_dialog("confirm", "Stay signed in?"),
                         "accept")
        # The grant is scoped to the message, so it cannot travel.
        self.assertEqual(policy.decide_dialog("confirm", "Stay signed out?"),
                         "dismiss")

    def test_empty_opt_in_accepts_nothing(self):
        policy = _policy("confirm", auto_accept_dialogs=())
        self.assertEqual(policy.decide_dialog("alert", "Anything"), "dismiss")

    def test_dismiss_is_the_default_for_unknown_wording(self):
        policy = _policy("allow", auto_accept_dialogs=())
        # Even the permissive mode does not accept an unrecognised dialog.
        self.assertEqual(policy.decide_dialog("beforeunload", "Something"),
                         "dismiss")


class RecoveryCannotBypassAuthorizationTests(unittest.TestCase):
    """Retries and recovery must not turn into permission."""

    def _controller(self, **kwargs):
        return ae.RecoveryController(**kwargs)

    def test_recovery_withdrawal_is_a_demotion_not_an_authorisation(self):
        controller = self._controller(max_attempts_per_action=1,
                                      max_attempts_per_action_total=1)
        decision = controller.decide("n1", "Place Order", ae.FAIL_TARGET_NOT_FOUND,
                                     goal_context=("step1",))
        self.assertFalse(decision.is_stop)
        self.assertTrue(controller.is_withdrawn("n1", "Place Order",
                                                ("step1",)))

    def test_exhausting_recovery_stops_the_run(self):
        controller = self._controller(max_attempts_per_action=1,
                                      max_attempts_per_action_total=1)
        controller.decide("n1", "Place Order", ae.FAIL_TARGET_NOT_FOUND,
                          goal_context=("step1",))
        second = controller.decide("n1", "Place Order", ae.FAIL_TARGET_NOT_FOUND,
                                   goal_context=("step1",))
        self.assertTrue(second.is_stop)

    def test_recovery_history_is_scoped_so_a_new_goal_starts_clean(self):
        controller = self._controller(max_attempts_per_action=1,
                                      max_attempts_per_action_total=1)
        controller.decide("n1", "Place Order", ae.FAIL_TARGET_NOT_FOUND,
                          goal_context=("step1",))
        self.assertTrue(controller.is_withdrawn("n1", "Place Order",
                                                ("step1",)))
        self.assertFalse(controller.is_withdrawn("n1", "Place Order",
                                                 ("step2",)))

    def test_reclassifying_the_failure_does_not_reset_the_budget(self):
        """Escaping the per-class limit by failing differently is the obvious
        way round a naive per-class counter; the class-agnostic total stops it."""
        controller = self._controller(max_attempts_per_action=5,
                                      max_attempts_per_action_total=2)
        controller.decide("n1", "X", ae.FAIL_TARGET_NOT_FOUND, ("s",))
        controller.decide("n1", "X", ae.FAIL_STALE_ELEMENT, ("s",))
        decision = controller.decide("n1", "X", ae.FAIL_TARGET_NOT_FOUND, ("s",))
        self.assertTrue(decision.is_stop)

    def test_safety_boundary_is_terminal_and_asks_the_user(self):
        controller = self._controller(max_attempts_per_action=99,
                                      max_attempts_per_action_total=99)
        decision = controller.decide("n1", "Pay now", ae.FAIL_SAFETY_BOUNDARY,
                                     goal_context=("s",))
        self.assertTrue(decision.is_stop)
        self.assertEqual(decision.outcome, ae.RECOVER_ASK_USER)

    def test_access_control_is_terminal(self):
        controller = self._controller(max_attempts_per_action=99,
                                      max_attempts_per_action_total=99)
        decision = controller.decide("n1", "anything", ae.FAIL_ACCESS_CONTROL,
                                     goal_context=("s",))
        self.assertTrue(decision.is_stop)

    def test_missing_input_asks_rather_than_inventing(self):
        controller = self._controller()
        decision = controller.decide("n1", "Card number",
                                     ae.FAIL_MISSING_INPUT, ("s",))
        self.assertTrue(decision.is_stop)
        self.assertEqual(decision.outcome, ae.RECOVER_ASK_USER)

    def test_every_recovery_decision_explains_itself(self):
        controller = self._controller(max_attempts_per_action=1,
                                      max_attempts_per_action_total=1)
        for failure in (ae.FAIL_TARGET_NOT_FOUND, ae.FAIL_SAFETY_BOUNDARY,
                        ae.FAIL_STALE_ELEMENT):
            with self.subTest(failure=failure):
                decision = controller.decide("n", "a", failure, ("s",))
                self.assertTrue(decision.reason.strip())
                self.assertIn(failure, decision.describe())


class ClassificationCannotLaunderSafetyTests(unittest.TestCase):
    """A safety refusal must never be reclassified as something retryable."""

    def test_awaiting_confirmation_classifies_as_a_safety_boundary(self):
        self.assertEqual(
            ae.classify_failure(ground_code=ae.GROUND_AWAITING_CONFIRMATION),
            ae.FAIL_SAFETY_BOUNDARY)

    def test_policy_blocked_classifies_as_a_safety_boundary(self):
        self.assertEqual(
            ae.classify_failure(ground_code=ae.GROUND_POLICY_BLOCKED),
            ae.FAIL_SAFETY_BOUNDARY)

    def test_a_safety_boundary_is_never_retry_safe(self):
        policy = ae.FAILURE_POLICIES[ae.FAIL_SAFETY_BOUNDARY]
        self.assertFalse(policy.retry_safe)
        self.assertTrue(policy.must_stop)

    def test_access_control_is_never_retry_safe(self):
        policy = ae.FAILURE_POLICIES[ae.FAIL_ACCESS_CONTROL]
        self.assertFalse(policy.retry_safe)
        self.assertTrue(policy.must_stop)

    def test_access_control_outranks_every_other_fact(self):
        self.assertEqual(
            ae.classify_failure(ground_code=ae.GROUND_OK,
                                access_control="a captcha is present"),
            ae.FAIL_ACCESS_CONTROL)


if __name__ == "__main__":
    unittest.main()