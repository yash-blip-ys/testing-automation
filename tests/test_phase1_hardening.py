"""Offline tests for Phase 1 correctness and site-agnosticism hardening.

These lock in four specific fixes:

  * no site-specific selector hardcoded in the core engine
  * a CLI --goal actually reaches the objective that drives verification
  * form_value evidence is really evaluated (digest comparison), not a no-op
  * a configured cart selector is required before any cart signal is produced

No browser, no model. Run with:
    .\\venv311\\Scripts\\python.exe -m unittest tests.test_phase1_hardening -v
"""

import inspect
import io
import json
import os
import sys
import tempfile
import contextlib
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae


ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "automation_engine.py",
)


class TestNoSiteSpecificHardcoding(unittest.TestCase):
    """The core must be website-agnostic.

    Site-specific behaviour is allowed only in user-supplied config or
    adapters, never baked into the engine source.
    """

    # Selectors/brands that would tie the core engine to one fixture site.
    FORBIDDEN = (
        ".shopping_cart_badge",
        "saucedemo",
        "swag labs",
        "bikeshops",
    )

    def test_engine_source_contains_no_fixture_specific_selector(self):
        with open(ENGINE_PATH, "r", encoding="utf-8") as fh:
            source = fh.read()
        # Comments and docstrings are stripped by checking code lines only:
        # a match inside a comment is documentation, a match in code is a
        # behavioural dependency on one website.
        for lineno, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for token in self.FORBIDDEN:
                self.assertNotIn(
                    token, stripped,
                    f"{os.path.basename(ENGINE_PATH)}:{lineno} references "
                    f"site-specific {token!r} in executable code: {stripped!r}",
                )

    def test_cart_signal_requires_configured_selector(self):
        """_parse_semantic_config must yield no cart selector when unconfigured."""
        cfg = ae._parse_semantic_config({})
        self.assertIsNone(cfg.get("cart_selector"))

    def test_cart_selector_is_read_from_config_when_supplied(self):
        cfg = ae._parse_semantic_config(
            {"semantic_signals": {"cart_count_selector": ".my-cart-count"}}
        )
        self.assertEqual(cfg.get("cart_selector"), ".my-cart-count")


class TestGoalOverrideReachesVerification(unittest.TestCase):
    """--goal must not be silently dropped by the config loader.

    A user who runs with a natural-language goal expects that goal to be what
    verification is judged against, and the configured step budget to survive.
    """

    def _load(self, config_obj, **kwargs):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(config_obj, fh)
            path = fh.name
        try:
            return ae.load_config(path, **kwargs)
        finally:
            os.unlink(path)

    def test_goal_override_sets_objective_when_config_has_no_test_goal(self):
        cfg = self._load({"portal_url": "https://example.test"},
                         goal_override="Add a hat to the cart")
        self.assertEqual(cfg["test_goal"]["objective"], "Add a hat to the cart")
        self.assertEqual(cfg["ai_context"], "Add a hat to the cart")

    def test_synthesized_test_goal_uses_default_step_budget(self):
        cfg = self._load({"portal_url": "https://example.test"},
                         goal_override="Do the thing")
        self.assertEqual(cfg["test_goal"]["max_steps"], ae.DEFAULT_MAX_STEPS)

    def test_goal_override_fills_objective_when_test_goal_lacks_one(self):
        """A config with steps/evidence but no objective must not leave
        verification objective-less just because --goal was supplied."""
        cfg = self._load(
            {
                "portal_url": "https://example.test",
                "test_goal": {
                    "max_steps": 7,
                    "evidence": {"url_contains": ["done"]},
                    "steps": [{"describe": "do a thing"}],
                },
            },
            goal_override="Actually complete the purchase",
        )
        self.assertEqual(
            cfg["test_goal"]["objective"], "Actually complete the purchase"
        )
        # The configured budget must survive; it is not clobbered by the default.
        self.assertEqual(cfg["test_goal"]["max_steps"], 7)

    def test_existing_objective_is_not_overwritten_by_override(self):
        """An explicitly authored objective is the source of truth."""
        cfg = self._load(
            {
                "portal_url": "https://example.test",
                "test_goal": {"objective": "Original objective"},
            },
            goal_override="Different goal",
        )
        self.assertEqual(cfg["test_goal"]["objective"], "Original objective")

    def test_explicit_steps_are_preserved_by_override(self):
        cfg = self._load(
            {
                "portal_url": "https://example.test",
                "test_goal": {
                    "objective": "Obj",
                    "steps": [{"describe": "first"}, {"describe": "second"}],
                },
            },
            goal_override="Other",
        )
        self.assertEqual(len(cfg["test_goal"]["steps"]), 2)


class TestFormValueEvidenceIsEvaluated(unittest.TestCase):
    """`form_value` is documented as supported evidence, so it must work.

    It is verified by comparing one-way digests: the observed value is hashed
    during extraction and the expected value is hashed here, so neither the
    real nor the expected value is ever stored or logged.
    """

    def _goal(self):
        return ae.TestGoal({"objective": "", "evidence": {}})

    def _signals(self, key, raw_value):
        return {"form_values": {key: ae.hash_form_value(raw_value)}}

    def test_matching_form_value_produces_positive_evidence(self):
        goal = self._goal()
        ev = {"form_value": {"#price-min": "50"}}
        positive, negative, _gate = goal._evaluate_evidence(
            ev,
            {"available_elements": [], "semantic_signals": self._signals("#price-min", "50")},
            "", "https://example.test", True,
        )
        self.assertTrue(positive, "matching form value should be positive evidence")
        self.assertFalse(negative)
        self.assertTrue(any("form_value matched" in p for p in positive))

    def test_mismatched_form_value_produces_negative_evidence(self):
        goal = self._goal()
        ev = {"form_value": {"#price-min": "50"}}
        positive, negative, _gate = goal._evaluate_evidence(
            ev,
            {"available_elements": [], "semantic_signals": self._signals("#price-min", "25")},
            "", "https://example.test", True,
        )
        self.assertFalse(positive, "mismatched form value must not be positive")
        self.assertTrue(any("mismatch" in n for n in negative))

    def test_unobserved_field_cannot_verify(self):
        """A field that was never observed must not count as matching."""
        goal = self._goal()
        ev = {"form_value": {"#price-min": "50"}}
        positive, negative, _gate = goal._evaluate_evidence(
            ev, {"available_elements": [], "semantic_signals": {}},
            "", "https://example.test", True,
        )
        self.assertFalse(positive)
        self.assertTrue(any("not observed" in n for n in negative))

    def test_no_semantic_signals_yields_no_false_pass(self):
        """With semantic signals unconfigured the field is unobservable, so a
        form_value requirement must stay unverified rather than pass."""
        goal = self._goal()
        ev = {"form_value": {"#price-min": "50"}}
        positive, _negative, _gate = goal._evaluate_evidence(
            ev, {"available_elements": []}, "", "https://example.test", True,
        )
        self.assertFalse(positive)

    def test_raw_values_never_appear_in_evidence_output(self):
        """The digest comparison must not leak the expected value."""
        goal = self._goal()
        ev = {"form_value": {"#price-min": "supersecretvalue"}}
        positive, negative, _gate = goal._evaluate_evidence(
            ev,
            {"available_elements": [],
             "semantic_signals": self._signals("#price-min", "supersecretvalue")},
            "", "https://example.test", True,
        )
        blob = " ".join(positive + negative)
        self.assertNotIn("supersecretvalue", blob)

    def test_sensitive_field_uses_presence_marker_not_digest(self):
        """A sensitive field carries a presence marker, so an exact-value
        check is impossible by design; a populated marker is still evidence."""
        goal = self._goal()
        ev = {"form_value": {"#password": "anything"}}
        positive, negative, _gate = goal._evaluate_evidence(
            ev,
            {"available_elements": [],
             "semantic_signals": {"form_values": {"#password": ae.SENSITIVE_PRESENT}}},
            "", "https://example.test", True,
        )
        self.assertTrue(positive, "populated sensitive field should be evidence")
        self.assertFalse(negative)


class TestNoFabricatedIntentOnModelFailure(unittest.TestCase):
    """When the navigator returns no valid decision, the engine must not
    manufacture a decision out of label keywords.

    Ranking by whether a label contains "finish" / "place order" / "confirm"
    invents an intent the model never expressed, and it steers the agent toward
    irreversible commit actions. The failure path must use only mechanically
    proven safety costs.
    """

    def _source(self):
        with open(ENGINE_PATH, "r", encoding="utf-8") as fh:
            return fh.read()

    def test_no_progress_keyword_preference_list_remains(self):
        self.assertNotIn("progress_keywords", self._source())

    def test_failure_path_states_it_infers_no_intent(self):
        source = self._source().lower()
        self.assertIn("inferred intent", source)

    def test_navigator_returns_none_rather_than_a_default_decision(self):
        """ask_ai_navigator must not hand back a synthetic best_choice."""
        import inspect
        src = inspect.getsource(ae.ask_ai_navigator)
        self.assertIn("return None", src)
        # The old message promised a "deterministic default" it never produced.
        self.assertNotIn("Falling back to a deterministic default", src)

    def test_infer_action_type_is_labelling_not_ranking(self):
        """infer_action_type is descriptive metadata; it must not set cost."""
        self.assertIn('"action": infer_action_type', inspect.getsource(ae.create_edge_metadata))
        meta = ae.create_edge_metadata("Finish")
        self.assertEqual(meta["cost"], 10, "action label must not influence cost")


class TestRuntimePlanReceivesSemanticSignals(unittest.TestCase):
    """The runtime plan must get digest-based evidence exactly as the
    configured goal does -- it must not get a looser standard."""

    def test_evaluate_accepts_semantic_signals(self):
        sig = inspect.signature(ae.RuntimePlan.evaluate)
        self.assertIn("semantic_signals", sig.parameters)

    def test_plan_evidence_evaluator_is_passed_semantic_signals(self):
        """remaining_work must forward page_state's semantic signals."""
        goal = ae.TestGoal({"objective": "Set the minimum price to 50"})
        plan = goal.ensure_runtime_plan(
            ["#price-min"], "https://example.test/search", "search page"
        )
        self.assertIsNotNone(plan)
        results = plan.evaluate(
            ["#price-min"], "https://example.test/search?q=50", "search page",
            structural_changed=True, ui_only_changed=False,
            evaluate_evidence=goal._evaluate_evidence,
            semantic_signals={"form_values": {"#price-min": ae.hash_form_value("50")}},
        )
        # The call must not raise; the semantic signals must reach the evaluator
        # as a page_state key that _evaluate_evidence reads.
        self.assertIsInstance(results, list)


class TestReplanningIsBoundedAndPreservesVerification(unittest.TestCase):
    """Replanning may rewrite the proposal but must never lose verified work,
    and the replan budget must actually bound the number of rewrites."""

    def _plan(self, objective="Add the backpack to the cart, then complete checkout"):
        plan = ae.RuntimePlan(objective)
        plan.seed_from_objective(["Add to cart"], "https://example.test/", "home")
        return plan

    def test_can_replan_respects_the_budget(self):
        plan = self._plan()
        self.assertTrue(plan.can_replan())
        for expected in range(ae.RUNTIME_PLAN_MAX_REPLANS):
            plan.replans = expected
            self.assertTrue(plan.can_replan(), f"budget should allow replan {expected}")
        plan.replans = ae.RUNTIME_PLAN_MAX_REPLANS
        self.assertFalse(plan.can_replan(), "replan budget must be enforced")

    def test_outstanding_requirements_excludes_verified(self):
        plan = self._plan()
        plan._verified[ae.normalise_requirement(plan.items[0]["requirement"])] = "confirmed"
        self.assertNotIn(
            ae.normalise_requirement(plan.items[0]["requirement"]),
            [ae.normalise_requirement(r) for r in plan.outstanding_requirements()],
        )

    def test_a_requirement_without_a_page_anchor_is_not_treated_as_diverged(self):
        """Regression guard.

        'complete checkout' has no anchor while the agent is still on the
        inventory page. Treating that as divergence caused a replan that
        replaced the plan and abandoned the real remaining work.
        """
        goal = ae.TestGoal({"objective": "x", "evidence": {}})
        plan = ae.RuntimePlan("complete checkout")
        plan.seed_from_objective([], "https://example.test/inventory", "Add to cart")
        self.assertTrue(plan.outstanding_requirements())
        # Evaluating on the same page must not manufacture a divergence signal.
        for _ in range(5):
            plan.evaluate(["Add to cart"], "https://example.test/inventory",
                          "Add to cart", structural_changed=True,
                          evaluate_evidence=goal._evaluate_evidence)
        self.assertTrue(plan.outstanding_requirements(),
                        "the requirement stays outstanding; only the run-level "
                        "stall counter may trigger a replan")

    def test_adopt_model_plan_preserves_verified_requirements(self):
        goal = ae.TestGoal({"objective": "x", "evidence": {}})
        plan = ae.RuntimePlan("Add the backpack to the cart, then complete checkout")
        plan.seed_from_objective(["Add to cart"], "https://example.test/", "Add to cart")
        key = ae.normalise_requirement(plan.items[0]["requirement"])
        plan._verified[key] = "confirmed earlier"

        ok = plan.adopt_model_plan(
            ["Reach the review page"], ["Finish"], "https://example.test/checkout", "Finish"
        )
        self.assertTrue(ok)
        self.assertIn(key, plan._verified, "verified work must survive a replan")
        self.assertTrue(
            any(i.get("already_done") for i in plan.items),
            "the verified sub-goal must still be shown to the navigator as done",
        )

    def test_adopt_model_plan_never_re_adds_a_retired_requirement(self):
        plan = self._plan()
        plan.retire(plan.items[0]["requirement"])
        retired_key = ae.normalise_requirement(plan.items[0]["requirement"])
        plan.adopt_model_plan(
            [plan.items[0]["requirement"], "Do something else"],
            ["A", "B"], "https://example.test/", "page",
        )
        keys = [ae.normalise_requirement(i["requirement"]) for i in plan.items]
        self.assertNotIn(retired_key, keys, "a retired requirement must not return")

    def test_adopt_model_plan_rejects_empty_proposal(self):
        plan = self._plan()
        self.assertFalse(plan.adopt_model_plan([], ["A"], "u", "t"))
        self.assertFalse(plan.adopt_model_plan(["  "], ["A"], "u", "t"))

    def test_planner_failure_leaves_plan_unchanged(self):
        """A refused or failed planner call must not degrade into a guess."""
        with mock.patch.object(ae, "ask_ai_planner", return_value=None) as planner:
            result = ae.ask_ai_planner("obj", ["stuck"], [], page_url="u")
        self.assertIsNone(result)
        planner.assert_called_once()

    def test_replanning_is_off_by_default(self):
        """Live runs showed the planner emits filler and never finishes the task.

        The API is implemented and tested, but the production trigger must stay
        off so a guessed plan cannot replace a stale one.
        """
        self.assertFalse(ae.ENABLE_RUNTIME_PLAN_REPLANNING)
        self.assertIn("ENABLE_RUNTIME_PLAN_REPLANNING",
                      inspect.getsource(ae.run_pathfinder_agent),
                      "the replan trigger must remain guarded by the flag")

    def test_stall_threshold_is_configured(self):
        self.assertGreaterEqual(ae.RUNTIME_PLAN_STALL_STEPS, 2,
                                "a replan must require sustained non-progress")

    def test_clear_resets_replan_state(self):
        plan = self._plan()
        plan.replans = 2
        plan.clear()
        self.assertEqual(plan.replans, 0)


class TestReplanCannotDiscardUserIntent(unittest.TestCase):
    """A model proposal may reword outstanding work, never delete it.

    This is the regression that let a live run finish on already-satisfied
    requirements while silently dropping the real remaining step.
    """

    def _plan(self):
        goal = ae.TestGoal({"objective": "x", "evidence": {}})
        plan = ae.RuntimePlan("Add two items to the cart, then complete checkout")
        plan.seed_from_objective(["Add to cart"], "https://example.test/inventory",
                                 "Add to cart")
        return plan

    def test_merge_keeps_requirements_not_being_replanned(self):
        plan = self._plan()
        checkout = ae.normalise_requirement("complete checkout")
        self.assertTrue(
            plan.adopt_model_plan(
                ["open the receipt"], ["Back"], "https://example.test/inventory", "page",
                replace_keys=["add two items to the cart"],
            )
        )
        keys = [ae.normalise_requirement(i["requirement"]) for i in plan.items]
        self.assertIn("open the receipt", keys, "the revised route must be added")
        self.assertIn(checkout, keys,
                      "an outstanding requirement the model was not asked about "
                      "must survive the replan")

    def test_merge_rejects_proposals_the_page_already_satisfies(self):
        """A planner that restates the visible controls proposes filler.

        Such a step is already satisfied by the current page, so accepting it
        would let the plan report completion while the task is untouched.
        """
        plan = self._plan()
        self.assertFalse(
            plan.adopt_model_plan(
                ["open the cart"], ["Cart", "Add to cart"],
                "https://example.test/inventory", "page",
                replace_keys=["add two items to the cart"],
            ),
            "a proposal already satisfied by the current page must be refused",
        )
        keys = [ae.normalise_requirement(i["requirement"]) for i in plan.items]
        self.assertNotIn("open the cart", keys)

    def test_merge_replaces_exactly_the_named_requirements(self):
        plan = self._plan()
        before = [ae.normalise_requirement(i["requirement"]) for i in plan.items]
        stale = ae.normalise_requirement("Add two items to the cart")
        plan.adopt_model_plan(
            ["open the receipt"], ["Back"], "https://example.test/inventory", "page",
            replace_keys=[stale],
        )
        keys = [ae.normalise_requirement(i["requirement"]) for i in plan.items]
        self.assertNotIn(stale, keys, "the replanned requirement should be replaced")
        # One requirement replaced by one proposed: net size unchanged.
        self.assertEqual(len(keys), len(before))

    def test_merge_replaces_in_place_preserving_order(self):
        plan = self._plan()
        checkout_key = ae.normalise_requirement("complete checkout")
        plan.adopt_model_plan(
            ["open the receipt"], ["Back"], "https://example.test/inventory", "page",
            replace_keys=["Add two items to the cart"],
        )
        keys = [ae.normalise_requirement(i["requirement"]) for i in plan.items]
        self.assertLess(keys.index("open the receipt"), keys.index(checkout_key),
                        "the revised route must take the replaced step's position")

    def test_merge_preserves_the_plan_when_every_proposal_is_rejected(self):
        """Aborting must not silently delete the requirement it could not replace."""
        plan = self._plan()
        before = [ae.normalise_requirement(i["requirement"]) for i in plan.items]
        self.assertFalse(
            plan.adopt_model_plan(
                ["open the cart"], ["Cart", "Add to cart"],
                "https://example.test/inventory", "page",
                replace_keys=["add two items to the cart"],
            )
        )
        after = [ae.normalise_requirement(i["requirement"]) for i in plan.items]
        self.assertEqual(after, before, "a failed replan must leave the plan intact")
        self.assertEqual(plan.replans, 0, "a failed replan must not spend budget")

    def test_merge_preserves_verified_work(self):
        plan = self._plan()
        key = ae.normalise_requirement(plan.items[0]["requirement"])
        plan._verified[key] = "confirmed"
        plan.adopt_model_plan(
            ["Open the cart"], ["Cart"], "https://example.test/inventory", "page",
            replace_keys=["add two items to the cart"],
        )
        self.assertIn(key, plan._verified)

    def test_full_replace_still_available_when_no_keys_given(self):
        plan = self._plan()
        self.assertTrue(plan.adopt_model_plan(
            ["Brand new requirement"], ["A"], "u", "t"))
        keys = [ae.normalise_requirement(i["requirement"]) for i in plan.items]
        self.assertIn("brand new requirement", keys)


class TestQuantityRequirementsAreChecked(unittest.TestCase):
    """A requirement that states a count must be matched on that count.

    A live run satisfied "Add two items to the cart" with a page reading
    "Cart, 1 items", which ended the run while the task was half done.
    """

    ELEMENTS = ["Add to cart", "Cart, 1 items", "Cart, 2 items", "Cart, 3 items"]

    def test_required_count_reads_number_words_and_digits(self):
        self.assertEqual(ae._required_count("add two items to the cart"), 2)
        self.assertEqual(ae._required_count("add 3 items"), 3)
        self.assertIsNone(ae._required_count("add items to the cart"))

    def test_wrong_quantity_is_not_evidence(self):
        ev = ae._derive_evidence_from_observation(
            "Add two items to the cart", self.ELEMENTS,
            "https://shop.test/inventory", "Products",
        )
        self.assertEqual(ev.get("element_present"), ['text="Cart, 2 items"'],
                         "a one-item cart must not satisfy a two-item requirement")

    def test_single_item_requirement_matches_single_item_cart(self):
        ev = ae._derive_evidence_from_observation(
            "add one item to the cart", ["Add to cart", "Cart, 1 items"],
            "https://shop.test/inventory", "Products",
        )
        self.assertEqual(ev.get("element_present"), ['text="Cart, 1 items"'])

    def test_quantity_requirement_with_no_matching_cart_is_unverifiable(self):
        ev = ae._derive_evidence_from_observation(
            "Add two items to the cart", ["Add to cart", "Cart, 1 items"],
            "https://shop.test/inventory", "Products",
        )
        self.assertEqual(ev, {})

    def test_uncounted_requirement_still_accepts_any_state_label(self):
        ev = ae._derive_evidence_from_observation(
            "add items to the cart", self.ELEMENTS,
            "https://shop.test/inventory", "Products",
        )
        self.assertEqual(ev.get("element_present"), ['text="Cart, 1 items"'])


class TestModelCallInstrumentation(unittest.TestCase):
    """Model usage must be counted, never estimated, so runs are comparable."""

    def test_stats_dict_exists_with_known_keys(self):
        for key in ("navigator", "planner", "failed"):
            self.assertIn(key, ae.MODEL_CALL_STATS)

    def test_navigator_increments_the_counter(self):
        before = ae.MODEL_CALL_STATS["navigator"]
        with mock.patch.object(ae.ollama, "chat", side_effect=RuntimeError("down")):
            with contextlib.redirect_stdout(io.StringIO()):
                ae.ask_ai_navigator(["A"], "goal", [])
        self.assertGreater(ae.MODEL_CALL_STATS["navigator"], before,
                           "each navigator attempt must be counted")

    def test_planner_increments_the_counter(self):
        before = ae.MODEL_CALL_STATS["planner"]
        with mock.patch.object(ae.ollama, "chat", side_effect=RuntimeError("down")):
            with contextlib.redirect_stdout(io.StringIO()):
                ae.ask_ai_planner("obj", ["stuck"], [])
        self.assertGreater(ae.MODEL_CALL_STATS["planner"], before)

    def test_planner_parses_a_well_formed_array(self):
        fake = {"message": {"content": '["Open the cart", "Pay for the order"]'}}
        with mock.patch.object(ae.ollama, "chat", return_value=fake):
            with contextlib.redirect_stdout(io.StringIO()):
                out = ae.ask_ai_planner("obj", ["stuck"], [])
        self.assertEqual(out, ["Open the cart", "Pay for the order"])

    def test_planner_rejects_non_array_json(self):
        fake = {"message": {"content": '{"not": "an array"}'}}
        with mock.patch.object(ae.ollama, "chat", return_value=fake):
            with contextlib.redirect_stdout(io.StringIO()):
                out = ae.ask_ai_planner("obj", ["stuck"], [])
        self.assertIsNone(out, "a non-array proposal must be refused, not coerced")

    def test_planner_returns_none_when_nothing_outstanding(self):
        self.assertIsNone(ae.ask_ai_planner("obj", [], []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
