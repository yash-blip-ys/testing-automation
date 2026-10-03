"""Part 4.3 — Replanning proposal validation.

A replan proposal is model output, so it is untrusted input. These tests pin
the properties that must hold before anything a planner proposes is adopted:

  * the objective is never rewritten, only read
  * outstanding requirements the planner was not asked about survive
  * verified requirements stay verified
  * filler already satisfied by trivial presence is refused
  * a proposal cannot introduce a route the safety boundary forbids
  * a proposal cannot quietly change the user's stated quantity
  * proposals are bounded in size
  * adoption MERGES; it never replaces outstanding work wholesale

The historical failure mode is pinned explicitly in
TestHistoricalFailureMode: a planner that replaced "complete checkout" with
controls already visible on the page, so the plan looked finished while the
checkout was never reached.

Run with:
    .\\venv311\\Scripts\\python.exe -m unittest tests.test_replan_validation -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae

OBJECTIVE = "Add two items to the cart, then complete checkout."
INVENTORY = ["Add to cart", "Add to cart #2", "Cart, empty", "Open Menu",
             "View details for Sauce Labs Backpack"]


def _plan(objective=OBJECTIVE, **kwargs):
    return ae.RuntimePlan(objective, **kwargs)


def _keys(plan):
    return [ae.normalise_requirement(i["requirement"]) for i in plan.items]


class TestProposalValidationIsDeterministic(unittest.TestCase):

    def test_the_validator_is_pure(self):
        import inspect
        src = inspect.getsource(ae.validate_replan_proposal)
        for forbidden in ("await", "ollama", "page.", "print("):
            self.assertNotIn(forbidden, src)

    def test_the_same_proposal_always_gives_the_same_verdict(self):
        proposal = ["open the cart", "bypass the login", "add three items"]
        first = ae.validate_replan_proposal(proposal, OBJECTIVE)
        for _ in range(10):
            self.assertEqual(ae.validate_replan_proposal(proposal, OBJECTIVE),
                             first)

    def test_every_rejection_names_a_reason(self):
        """A refusal that cannot explain itself is indistinguishable from a bug."""
        _, dropped = ae.validate_replan_proposal(
            ["bypass the login", "add three items", "  ", "x" * 300],
            OBJECTIVE)
        self.assertTrue(dropped)
        for reason in dropped:
            self.assertIn("(", reason)
            self.assertTrue(reason.rstrip().endswith(")"))


class TestSizeIsBounded(unittest.TestCase):

    def test_too_many_entries_are_refused(self):
        proposal = [f"step {i}" for i in range(
            ae.RUNTIME_PLAN_MAX_PROPOSAL_ENTRIES + 5)]
        accepted, dropped = ae.validate_replan_proposal(proposal, OBJECTIVE)
        self.assertLessEqual(len(accepted), ae.RUNTIME_PLAN_MAX_PROPOSAL_ENTRIES)
        self.assertTrue(dropped, "the overflow must be reported, not silent")

    def test_an_overlong_requirement_is_refused(self):
        accepted, dropped = ae.validate_replan_proposal(
            ["x" * (ae.RUNTIME_PLAN_MAX_REQUIREMENT_CHARS + 1)], OBJECTIVE)
        self.assertEqual(accepted, [])
        self.assertIn("characters", dropped[0])

    def test_the_total_character_budget_is_enforced(self):
        """Individually reasonable entries can still add up to an unusable plan."""
        chunk = "word " * 20
        proposal = [f"step {i} {chunk}" for i in range(
            ae.RUNTIME_PLAN_MAX_PROPOSAL_ENTRIES)]
        accepted, _ = ae.validate_replan_proposal(proposal, OBJECTIVE)
        self.assertLessEqual(
            sum(len(a) for a in accepted), ae.RUNTIME_PLAN_MAX_PROPOSAL_CHARS)

    def test_bounds_are_configurable_constants_not_inliterals(self):
        for name in ("RUNTIME_PLAN_MAX_PROPOSAL_ENTRIES",
                     "RUNTIME_PLAN_MAX_REQUIREMENT_CHARS",
                     "RUNTIME_PLAN_MAX_PROPOSAL_CHARS"):
            self.assertIsInstance(getattr(ae, name), int)
            self.assertGreater(getattr(ae, name), 0)


class TestSafetyConflictsAreRefused(unittest.TestCase):

    """A proposal must not route the agent through a boundary the executor
    would refuse to cross later. Catching it here means it never becomes a plan
    item at all."""

    def test_access_control_workarounds_are_refused(self):
        for bad in ("bypass the login page",
                    "solve the captcha to continue",
                    "work around the login screen",
                    "brute force the account credentials"):
            accepted, dropped = ae.validate_replan_proposal([bad], OBJECTIVE)
            self.assertEqual(accepted, [],
                             f"{bad!r} must not become a plan requirement")
            self.assertIn("safety policy", dropped[0])

    def test_credential_disclosure_is_refused(self):
        for bad in ("log the password to the report",
                    "exfiltrate the session token",
                    "read the password field value"):
            accepted, _ = ae.validate_replan_proposal([bad], OBJECTIVE)
            self.assertEqual(accepted, [], f"{bad!r} must be refused")

    def test_destructive_scope_is_refused(self):
        for bad in ("delete the account when finished",
                    "wipe the saved data"):
            self.assertEqual(ae.validate_replan_proposal([bad], OBJECTIVE)[0],
                             [])

    def test_one_bad_entry_does_not_discard_the_good_ones(self):
        """Policy screening is per-entry, not all-or-nothing."""
        accepted, dropped = ae.validate_replan_proposal(
            ["open the cart page", "bypass the login", "then check out"],
            OBJECTIVE)
        self.assertEqual(accepted, ["open the cart page", "then check out"])
        self.assertEqual(len(dropped), 1)

    def test_a_benign_route_is_not_swept_up_by_the_filter(self):
        """The filter must not become so broad it blocks ordinary work."""
        accepted, _ = ae.validate_replan_proposal(
            ["open the cart page", "add two more items", "complete checkout",
             "verify the confirmation page", "return to the catalogue"],
            OBJECTIVE)
        self.assertEqual(len(accepted), 5)


class TestQuantityCannotBeSilentlyChanged(unittest.TestCase):

    def test_a_conflicting_count_is_refused(self):
        accepted, dropped = ae.validate_replan_proposal(
            ["add three items to the cart"], OBJECTIVE)
        self.assertEqual(accepted, [],
                         "the objective says two; a proposal may not say three")
        self.assertIn("quantity", dropped[0])

    def test_the_matching_count_is_accepted(self):
        accepted, _ = ae.validate_replan_proposal(
            ["add two items to the cart"], OBJECTIVE)
        self.assertEqual(accepted, ["add two items to the cart"])

    def test_digits_and_number_words_agree(self):
        """'2 items' and 'two items' must be the same claim, not two claims."""
        for text in ("add 2 items", "add two items"):
            accepted, _ = ae.validate_replan_proposal([text], OBJECTIVE)
            self.assertEqual(len(accepted), 1, f"{text!r} must be accepted")

    def test_a_requirement_with_no_count_is_never_policed(self):
        """Most requirements state no quantity, and must not be treated as if
        they did — rejecting them would gut the planner."""
        accepted, _ = ae.validate_replan_proposal(
            ["open the cart page", "complete checkout"], OBJECTIVE)
        self.assertEqual(len(accepted), 2)

    def test_quantity_rules_do_not_apply_when_the_objective_states_none(self):
        self.assertEqual(
            ae.validate_replan_proposal(["add three items"], "buy something")[0],
            ["add three items"],
            "with no quantity in the objective there is nothing to contradict")

    def test_a_rejected_quantity_cannot_reach_the_plan(self):
        plan = _plan()
        plan.seed_from_objective(INVENTORY, "https://x.test/inventory", "page")
        before = list(_keys(plan))
        plan.adopt_model_plan(["add three items to the cart", "checkout"],
                              INVENTORY, "https://x.test/inventory", "page",
                              replace_keys=[before[0]])
        self.assertNotIn("add three items to the cart",
                         " ".join(i["requirement"] for i in plan.items))


class TestTheObjectiveIsPreserved(unittest.TestCase):

    def test_adoption_never_writes_the_objective(self):
        plan = _plan()
        plan.seed_from_objective(INVENTORY, "https://x.test/inventory", "page")
        plan.adopt_model_plan(["open the receipt"], ["Receipt"],
                              "https://x.test/r", "Receipt",
                              replace_keys=[plan.items[0]["requirement"]])
        self.assertEqual(plan.objective, OBJECTIVE)

    def test_a_rejected_proposal_changes_nothing_at_all(self):
        plan = _plan()
        plan.seed_from_objective(INVENTORY, "https://x.test/inventory", "page")
        before_items = list(_keys(plan))
        before_replans = plan.replans
        self.assertFalse(plan.adopt_model_plan(
            ["bypass the login to continue"], INVENTORY,
            "https://x.test/inventory", "page",
            replace_keys=[before_items[0]]))
        self.assertEqual(_keys(plan), before_items)
        self.assertEqual(plan.replans, before_replans)

    def test_the_rejection_is_recorded_with_its_reason(self):
        plan = _plan()
        plan.seed_from_objective(INVENTORY, "https://x.test/inventory", "page")
        plan.adopt_model_plan(["bypass the login"], INVENTORY,
                              "https://x.test/inventory", "page",
                              replace_keys=[plan.items[0]["requirement"]])
        self.assertTrue(plan.proposal_log)
        entry = plan.proposal_log[-1]
        self.assertIn("safety policy", " ".join(entry["dropped"]))


def _adoptable(proposal, plan, replace_key=None):
    """Adopt a proposal that describes a page which does not exist yet.

    Such a route is a real future step, so it survives the filler check — which
    is what makes it a usable counter-example to the rejection tests.
    """
    return plan.adopt_model_plan(
        proposal, INVENTORY, "https://x.test/inventory", "page",
        replace_keys=[replace_key] if replace_key is not None else None)


class TestOutstandingRequirementsSurvive(unittest.TestCase):

    def test_requirements_the_planner_was_not_asked_about_are_preserved(self):
        plan = _plan()
        plan.seed_from_objective(INVENTORY, "https://x.test/inventory", "page")
        checkout = ae.normalise_requirement("complete checkout")
        self.assertTrue(_adoptable(["open the receipt page"], plan,
                                   plan.items[0]["requirement"]))
        self.assertIn(checkout, _keys(plan),
                      "outstanding work must survive a replan it was not "
                      "asked about")

    def test_adoption_merges_rather_than_replacing(self):
        plan = _plan()
        plan.seed_from_objective(INVENTORY, "https://x.test/inventory", "page")
        before = list(_keys(plan))
        target = before[0]
        self.assertTrue(_adoptable(["open the payment page"], plan, target))
        after = _keys(plan)
        self.assertNotIn(target, after, "the replaced requirement should go")
        for key in before[1:]:
            self.assertIn(key, after, f"{key!r} must have been preserved")

    def test_a_requirement_never_may_reappear_after_retirement(self):
        plan = _plan()
        plan.seed_from_objective(INVENTORY, "https://x.test/inventory", "page")
        target = plan.items[0]["requirement"]
        plan.retire(target)
        self.assertTrue(_adoptable([target, "open the payment page"], plan,
                                   target))
        self.assertNotIn(ae.normalise_requirement(target), _keys(plan))

    def test_replan_budget_is_enforced(self):
        plan = _plan(max_subgoals=6)
        plan.seed_from_objective(INVENTORY, "https://x.test/inventory", "page")
        accepted_any = False
        for _ in range(ae.RUNTIME_PLAN_MAX_REPLANS + 4):
            if _adoptable(["open the payment page"], plan,
                          plan.items[0]["requirement"]):
                accepted_any = True
        self.assertTrue(accepted_any)
        self.assertLessEqual(plan.replans, ae.RUNTIME_PLAN_MAX_REPLANS,
                             "the replan budget must bound the run")


class TestVerifiedRequirementsSurvive(unittest.TestCase):

    def _verified_plan(self):
        plan = _plan()
        plan.seed_from_objective(INVENTORY, "https://x.test/inventory", "page")
        key = ae.normalise_requirement(plan.items[0]["requirement"])
        plan._verified[key] = "text=\"Cart, 2 items\""
        plan._verified_at[key] = {"url": "https://x.test/inventory",
                                  "evidence": "Cart, 2 items"}
        return plan, key

    def test_verified_work_is_not_discarded_by_a_replan(self):
        plan, key = self._verified_plan()
        plan.adopt_model_plan(["open the receipt page"], ["Receipt"],
                              "https://x.test/r", "Receipt",
                              replace_keys=[plan.items[0]["requirement"]])
        self.assertIn(key, plan._verified,
                      "a confirmed sub-goal must stay confirmed")

    def test_verified_work_survives_a_full_replacement(self):
        plan, key = self._verified_plan()
        plan.adopt_model_plan(["open the cart page"], ["Cart, empty"],
                              "https://x.test/cart", "Cart")
        self.assertIn(key, plan._verified)

    def test_verified_work_still_appears_in_the_plan(self):
        """The navigator must still see it, or it may redo completed work."""
        plan, key = self._verified_plan()
        plan.adopt_model_plan(["open the receipt page"], ["Receipt"],
                              "https://x.test/r", "Receipt",
                              replace_keys=[plan.items[0]["requirement"]])
        self.assertIn(key, _keys(plan))

    def test_a_replan_cannot_make_verified_work_unverified(self):
        plan, key = self._verified_plan()
        plan.adopt_model_plan(["open the receipt page"], ["Receipt"],
                              "https://x.test/r", "Receipt",
                              replace_keys=[plan.items[0]["requirement"]])
        rows = plan.evaluate(["Receipt"], "https://x.test/r", "Receipt",
                             evaluate_evidence=lambda e, p: None)
        done = [r for r in rows if ae.normalise_requirement(r["requirement"]) == key]
        self.assertTrue(done)
        self.assertTrue(done[0]["verified"])


class TestFillerIsRefused(unittest.TestCase):

    """The filler test: a requirement the CURRENT page already satisfies
    describes the present, not a next step."""

    def test_a_requirement_satisfied_by_visible_controls_is_refused(self):
        plan = _plan()
        plan.seed_from_objective(INVENTORY, "https://x.test/inventory", "page")
        before = list(_keys(plan))
        self.assertFalse(plan.adopt_model_plan(
            ["open the menu"], ["Open Menu"], "https://x.test/inventory",
            "Open Menu Add to cart",
            replace_keys=[before[0]]))
        self.assertEqual(_keys(plan), before,
                         "a rejected proposal must leave the plan untouched")

    def test_the_reason_names_the_filler(self):
        plan = _plan()
        plan.seed_from_objective(INVENTORY, "https://x.test/inventory", "page")
        plan.adopt_model_plan(["open the menu"], ["Open Menu"],
                              "https://x.test/inventory", "Open Menu",
                              replace_keys=[plan.items[0]["requirement"]])
        joined = " ".join(plan.proposal_log[-1]["dropped"])
        self.assertTrue("satisfied by the current page" in joined
                        or "future step" in plan.proposal_log[-1]["reason"])

    def test_an_empty_proposal_is_refused(self):
        plan = _plan()
        plan.seed_from_objective(INVENTORY, "https://x.test/inventory", "page")
        before = list(_keys(plan))
        for bad in ([], [""], ["   "], None):
            self.assertFalse(plan.adopt_model_plan(
                bad, INVENTORY, "https://x.test/inventory", "page",
                replace_keys=[before[0]]))
        self.assertEqual(_keys(plan), before)

    def test_a_proposal_may_not_hide_a_quantity_claim_in_filler(self):
        """Screening happens before the filler test, so a bad entry cannot be
        smuggled through by also being filler."""
        plan = _plan()
        plan.seed_from_objective(INVENTORY, "https://x.test/inventory", "page")
        before = list(_keys(plan))
        plan.adopt_model_plan(["add three items", "open the menu"], INVENTORY,
                              "https://x.test/inventory", "Open Menu",
                              replace_keys=[before[0]])
        joined = " ".join(plan.proposal_log[-1]["dropped"])
        self.assertIn("quantity", joined)


class TestHistoricalFailureMode(unittest.TestCase):
    """The regression this whole validation layer exists for.

    A planner restated the controls that were already on screen and called it a
    route. The plan then looked complete while the checkout was never reached,
    and the run reported progress it had not made.
    """

    def _stalled_plan(self):
        plan = _plan()
        plan.seed_from_objective(INVENTORY, "https://x.test/inventory", "page")
        self.assertIn("complete checkout",
                      " ".join(i["requirement"] for i in plan.items),
                      "the seeded plan must contain the real remaining work")
        return plan

    def test_filler_entries_are_dropped_from_the_historical_proposal(self):
        """The historical proposal's usable-looking entries are refused.

        Two of the three restate controls already on screen. Those must be
        dropped and recorded; the third is inadmissible for a different reason
        (see the limitation test below), so this asserts the screening rather
        than an all-or-nothing refusal.
        """
        plan = self._stalled_plan()
        checkout = ae.normalise_requirement("complete checkout")
        plan.adopt_model_plan(
            ["open the menu", "add to cart", "view details for backpack"],
            INVENTORY, "https://x.test/inventory",
            "Open Menu Add to cart View details", replace_keys=[checkout])
        entry = plan.proposal_log[-1]
        joined = " ".join(entry["dropped"])
        self.assertIn("open the menu", joined,
                      "the restated control must be dropped and recorded")
        self.assertIn("already satisfied by the current page", joined,
                      "the reason must name why it described the present")
        self.assertNotIn("open the menu",
                         " ".join(i["requirement"] for i in plan.items),
                         "a dropped requirement must not reach the plan")

    def test_a_fully_filler_proposal_is_refused_outright(self):
        """When NOTHING survives, the plan must be left exactly as it was."""
        plan = self._stalled_plan()
        checkout = ae.normalise_requirement("complete checkout")
        before = list(_keys(plan))
        self.assertFalse(plan.adopt_model_plan(
            ["open the menu", "view details for backpack"], INVENTORY,
            "https://x.test/inventory", "Open Menu Add to cart View details",
            replace_keys=[checkout]))
        self.assertEqual(_keys(plan), before)
        self.assertIn(checkout, _keys(plan))

    def test_known_limitation_replace_keys_trusts_the_caller(self):
        """Documented gap, pinned deliberately rather than hidden.

        `replace_keys` is chosen by the caller, and nothing mechanically checks
        that the replacement is actually about the requirement being replaced.
        A proposal that names unrelated but non-filler work can therefore
        displace an outstanding requirement.

        This is NOT fixed here because any check that would catch it — requiring
        the replacement to share a content word with what it replaces — also
        rejects legitimate reroutes ("complete checkout" -> "open the receipt
        page" shares nothing). Guessing intent from wording would trade a
        real gap for a real regression. Recorded in the Part 4.5 decision
        report as an unsupported case, and pinned by this test so that closing
        it is a deliberate change rather than an accident.
        """
        plan = self._stalled_plan()
        checkout = ae.normalise_requirement("complete checkout")
        plan.adopt_model_plan(["add to cart"], INVENTORY,
                              "https://x.test/inventory", "page",
                              replace_keys=[checkout])
        self.assertNotIn(checkout, _keys(plan),
                         "documents the current, known behaviour")
        self.assertEqual(plan.proposal_log[-1]["outcome"], "merged")

    def test_the_failed_replan_is_visible_in_the_audit_log(self):
        plan = self._stalled_plan()
        checkout = ae.normalise_requirement("complete checkout")
        plan.adopt_model_plan(["open the menu"], INVENTORY,
                              "https://x.test/inventory", "Open Menu",
                              replace_keys=[checkout])
        outcomes = plan.proposal_outcomes()
        self.assertTrue(outcomes)
        self.assertEqual(outcomes[-1], "rejected")

    def test_a_plausible_reroute_that_keeps_the_work_is_accepted(self):
        """The refusal must be discriminating, not blanket.

        A route toward a page that does not exist yet describes a real future
        step, so it must be adoptable — otherwise the guard would make progress
        impossible rather than merely safe.
        """
        plan = self._stalled_plan()
        checkout = ae.normalise_requirement("complete checkout")
        self.assertTrue(plan.adopt_model_plan(
            ["open the payment confirmation page"], INVENTORY,
            "https://x.test/inventory", "page", replace_keys=[checkout]))
        joined = " ".join(i["requirement"] for i in plan.items)
        self.assertIn("payment confirmation", joined,
                      "the reroute must be adopted")
        self.assertTrue(any(
            ae.normalise_requirement(i["requirement"]) == checkout
            or "payment confirmation" in i["requirement"]
            for i in plan.items),
            "the replaced requirement must be covered, not silently dropped")


class TestProductionReplanningStaysOff(unittest.TestCase):

    def test_the_flag_is_still_disabled(self):
        """Part 4's gate: tests passing is not a reason to switch it on."""
        self.assertFalse(ae.ENABLE_RUNTIME_PLAN_REPLANNING)

    def test_the_trigger_is_still_guarded_by_the_flag(self):
        import inspect
        src = inspect.getsource(ae.run_pathfinder_agent)
        self.assertLess(src.index("ENABLE_RUNTIME_PLAN_REPLANNING"),
                        src.index("ask_ai_planner"),
                        "the planner call must remain behind the flag")


if __name__ == "__main__":
    unittest.main()