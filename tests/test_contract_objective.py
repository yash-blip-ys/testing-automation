"""Checkpoint 1.1 — Objective and task contract invariants.

These tests pin the six contract requirements so a future change cannot
silently weaken them:

  1. The original objective is immutable during a run.
  2. Explicit steps are an optional override, not a mandatory workflow.
  3. A runtime plan may propose requirements but cannot redefine the objective.
  4. User-provided final evidence is independent of the planner.
  5. Uncertainty and unverifiable requirements are represented explicitly.
  6. Existing configuration and CLI behaviour is preserved.

No browser, no model. Run with:
    .\\venv311\\Scripts\\python.exe -m unittest tests.test_contract_objective -v
"""

import inspect
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae


class TestReq1_ObjectiveIsImmutable(unittest.TestCase):
    """1. The original objective is immutable during a run."""

    def test_objective_is_exposed(self):
        goal = ae.TestGoal({"objective": "  Add a hat to the cart  "})
        self.assertEqual(goal.objective, "Add a hat to the cart",
                         "the objective must still be readable and stripped")

    def test_objective_cannot_be_reassigned(self):
        goal = ae.TestGoal({"objective": "Complete the checkout"})
        with self.assertRaises(AttributeError):
            goal.objective = "Something else entirely"
        self.assertEqual(goal.objective, "Complete the checkout",
                         "a rejected assignment must not change the objective")

    def test_objective_cannot_be_cleared(self):
        goal = ae.TestGoal({"objective": "Complete the checkout"})
        with self.assertRaises(AttributeError):
            goal.objective = ""
        self.assertEqual(goal.objective, "Complete the checkout")

    def test_objective_survives_plan_creation_and_evaluation(self):
        """The whole run must not be able to move the objective."""
        goal = ae.TestGoal({"objective": "Add two items to the cart",
                            "evidence": {}})
        before = goal.objective
        plan = goal.ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        self.assertIsNotNone(plan)
        goal.remaining_work(
            page_state={"available_elements": ["Cart, 2 items"]},
            page_text="p", url="https://x.test/cart", structural_changed=True,
        )
        self.assertEqual(goal.objective, before)

    def test_objective_cannot_be_deleted(self):
        goal = ae.TestGoal({"objective": "Complete the checkout"})
        with self.assertRaises(AttributeError):
            del goal.objective
        self.assertEqual(goal.objective, "Complete the checkout")

    def test_objective_property_is_read_only(self):
        """Structural guarantee, not just convention."""
        prop = ae.TestGoal.objective
        self.assertIsInstance(prop, property)
        self.assertIsNone(prop.fset, "objective must have no setter")


class TestReq2_ExplicitStepsAreOptional(unittest.TestCase):
    """2. Explicit steps are an optional override, not a mandatory workflow."""

    def test_no_steps_and_no_objective_is_unconfigured(self):
        goal = ae.TestGoal({})
        self.assertFalse(goal.is_configured())
        self.assertEqual(goal.steps, [])
        self.assertIsNone(goal.runtime_plan)

    def test_objective_alone_configures_the_goal(self):
        goal = ae.TestGoal({"objective": "Find the return policy"})
        self.assertTrue(goal.is_configured())
        self.assertEqual(goal.steps, [], "steps must stay optional")
        self.assertFalse(goal.uses_configured_steps())

    def test_steps_alone_do_not_configure_the_goal(self):
        """A step list is not a substitute for the objective."""
        goal = ae.TestGoal({"steps": [{"describe": "do a thing"}]})
        self.assertFalse(goal.is_configured())

    def test_steps_take_precedence_over_runtime_plan(self):
        goal = ae.TestGoal({"objective": "Do the thing",
                            "steps": [{"describe": "explicit one"}]})
        self.assertTrue(goal.uses_configured_steps())
        rows = goal.remaining_work(
            page_state={"available_elements": ["A"]},
            page_text="t", url="https://x.test/", structural_changed=True,
        )
        self.assertEqual([r["describe"] for r in rows], ["explicit one"],
                         "explicit steps must be used verbatim, not re-planned")
        self.assertIsNone(goal.runtime_plan,
                          "no runtime plan may be built when steps are explicit")

    def test_step_forms_accepted(self):
        """Dict steps, bare-string steps, and junk entries.

        A dict step carrying evidence but no describe is KEPT with an empty
        description; remaining_work() substitutes a readable "step N" at render
        time, so the stored config stays faithful to what the user wrote.
        """
        goal = ae.TestGoal({
            "objective": "Do the thing",
            "steps": [
                {"describe": "dict step"},
                "bare string step",
                {"evidence": {"url_contains": ["done"]}},  # no describe
                {},                                          # empty -> ignored
                None,                                        # junk -> ignored
                42,                                          # junk -> ignored
            ],
        })
        self.assertEqual(len(goal.steps), 3,
                         "only entries with describe or evidence are kept")
        self.assertEqual([s["describe"] for s in goal.steps],
                         ["dict step", "bare string step", ""])

    def test_undescribed_step_renders_a_readable_label(self):
        goal = ae.TestGoal({
            "objective": "Do the thing",
            "steps": [{"evidence": {"url_contains": ["done"]}}],
        })
        rows = goal.remaining_work(
            page_state={"available_elements": ["A"]},
            page_text="t", url="https://x.test/", structural_changed=True,
        )
        self.assertEqual(rows[0]["describe"], "step 1")
        self.assertEqual(rows[0]["requirement"], "step 1")

    def test_non_dict_goal_dict_is_tolerated(self):
        """Requirement 6: bad input must not crash construction."""
        for bad in (None, "a string", 42, [], object()):
            goal = ae.TestGoal(bad)
            self.assertFalse(goal.is_configured())
            self.assertEqual(goal.objective, "")


class TestReq3_PlanCannotRedefineObjective(unittest.TestCase):
    """3. A runtime plan may propose requirements but cannot redefine the
    objective."""

    def _goal(self, objective="Add two items to the cart, then complete checkout"):
        return ae.TestGoal({"objective": objective, "evidence": {}})

    def test_plan_holds_its_own_copy_of_the_objective(self):
        """The plan's objective is a value copy, not the goal's attribute.

        Strings are immutable so identity proves nothing here; what matters is
        that the goal's objective field is never written by the plan.
        """
        goal = self._goal()
        plan = goal.ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.objective, goal.objective)
        self.assertEqual(goal.objective,
                         "Add two items to the cart, then complete checkout")

    def test_adopting_a_plan_leaves_the_objective_untouched(self):
        goal = self._goal()
        plan = goal.ensure_runtime_plan(["Add to cart"], "https://x.test/i", "p")
        plan.adopt_model_plan(
            ["Reach the receipt page"], ["Receipt"], "https://x.test/r", "Receipt"
        )
        self.assertEqual(goal.objective, "Add two items to the cart, then complete checkout")

    def test_adopting_a_plan_cannot_write_the_objective_field(self):
        """Structural: no adopt path assigns to an objective attribute.

        Checked against the AST rather than the source text. Reading
        `self.objective` is necessary and correct — proposal validation compares
        a candidate requirement's quantities against the objective the user
        actually wrote — so a substring ban would forbid the safe read along
        with the unsafe write. What must not exist is an assignment.
        """
        import ast

        def _assigned_to_objective(fn):
            tree = ast.parse(inspect.getsource(fn).lstrip())
            hits = []
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    targets = [node.target]
                for t in targets:
                    if (isinstance(t, ast.Attribute)
                            and t.attr in ("objective", "_objective")):
                        hits.append(t.attr)
            return hits

        for fn in (ae.RuntimePlan.adopt_model_plan, ae.RuntimePlan._adopt_all,
                   ae.RuntimePlan._make_items, ae.RuntimePlan.retire,
                   ae.RuntimePlan.clear):
            self.assertEqual(
                _assigned_to_objective(fn), [],
                f"{fn.__name__} must never assign to an objective attribute")

    def test_proposal_validation_reads_but_never_writes_the_objective(self):
        """The objective is an input to validation, never an output of it."""
        plan = ae.RuntimePlan("Add two items to the cart, then complete checkout.")
        before = plan.objective
        plan.adopt_model_plan(["add three items instead"],
                              ["A"], "https://x.test/", "p")
        self.assertEqual(plan.objective, before,
                         "a rejected proposal must leave the objective alone")

    def test_a_requirement_cannot_become_the_objective(self):
        """Even a planner requirement identical to the objective text is only
        an item; the objective field is untouched."""
        goal = self._goal("Do X then Y")
        plan = goal.ensure_runtime_plan(["X"], "https://x.test/", "p")
        plan.adopt_model_plan(["Do X then Y"], ["A"], "https://x.test/", "p")
        self.assertEqual(goal.objective, "Do X then Y")

    def test_split_objective_never_changes_the_source_string(self):
        original = "Add two items to the cart, then complete checkout"
        ae.split_objective(original)
        self.assertEqual(original, "Add two items to the cart, then complete checkout")


class TestReq4_FinalEvidenceIsIndependentOfPlanner(unittest.TestCase):
    """4. User-provided final evidence remains independent of the planner."""

    USER_EVIDENCE = {"url_contains": ["checkout-complete"],
                     "text_contains": ["Thank you for your order"]}

    def _goal(self):
        goal = ae.TestGoal({"objective": "Complete checkout",
                            "evidence": dict(self.USER_EVIDENCE)})
        plan = goal.ensure_runtime_plan(["Checkout"], "https://x.test/cart", "Checkout")
        self.assertIsNotNone(plan)
        return goal, plan

    def test_plan_adoption_leaves_final_evidence_untouched(self):
        goal, plan = self._goal()
        plan.adopt_model_plan(
            ["Something entirely different"], ["A"], "https://x.test/", "p",
            replace_keys=["checkout"],
        )
        self.assertEqual(goal.evidence, self.USER_EVIDENCE)

    def test_planner_evaluation_does_not_touch_final_evidence(self):
        goal, plan = self._goal()
        plan.evaluate(["A"], "https://x.test/", "p", structural_changed=True,
                      evaluate_evidence=goal._evaluate_evidence)
        self.assertEqual(goal.evidence, self.USER_EVIDENCE)

    def test_planner_cannot_inject_evidence_keys(self):
        """A planner entry carrying an 'evidence' key must not be honoured."""
        goal = ae.TestGoal({"objective": "Complete checkout", "evidence": {}})
        plan = goal.ensure_runtime_plan(["Checkout"], "https://x.test/c", "Checkout")
        # Evidence for plan rows is always re-derived mechanically by
        # _make_items; a caller-supplied evidence dict is never trusted.
        src = inspect.getsource(ae.RuntimePlan._make_items)
        self.assertIn("_derive_evidence_from_observation", src)
        self.assertNotIn("entry.get(\"evidence\")", src)

    def test_final_evidence_survives_plan_clear(self):
        goal, plan = self._goal()
        plan.clear()
        self.assertEqual(goal.evidence, self.USER_EVIDENCE)

    def test_derived_plan_evidence_is_mechanical_not_model_authored(self):
        """Evidence for a plan row must come from page observation only."""
        src = inspect.getsource(ae._derive_evidence_from_observation)
        for forbidden in ("ask_ai", "ollama", "chat("):
            self.assertNotIn(forbidden, src,
                             "evidence derivation must never call the model")


class TestReq5_UncertaintyIsExplicit(unittest.TestCase):
    """5. The system must represent uncertainty and unverifiable requirements
    explicitly — and identically on both sub-goal paths."""

    ROW_KEYS = {"index", "describe", "done", "verified", "verifiable",
                "unverifiable", "evidence"}

    def _keys(self, rows):
        return set(rows[0].keys())

    def test_configured_step_rows_expose_uncertainty(self):
        goal = ae.TestGoal({"objective": "Do it",
                            "steps": [{"describe": "step one"}]})
        rows = goal.remaining_work(
            page_state={"available_elements": ["A"]},
            page_text="t", url="https://x.test/", structural_changed=True,
        )
        self.assertTrue(self.ROW_KEYS.issubset(self._keys(rows)),
                        f"missing uncertainty keys; got {self._keys(rows)}")

    def test_runtime_plan_rows_expose_uncertainty(self):
        goal = ae.TestGoal({"objective": "Do it", "evidence": {}})
        rows = goal.remaining_work(
            page_state={"available_elements": ["A"]},
            page_text="t", url="https://x.test/", structural_changed=True,
        )
        self.assertTrue(self.ROW_KEYS.issubset(self._keys(rows)),
                        f"missing uncertainty keys; got {self._keys(rows)}")

    def test_both_paths_share_one_row_shape(self):
        """Contract uniformity: consumers must not branch on which path ran."""
        steps = ae.TestGoal({"objective": "Do it",
                             "steps": [{"describe": "step one"}]})
        rows_a = steps.remaining_work(
            page_state={"available_elements": ["A"]},
            page_text="t", url="https://x.test/", structural_changed=True,
        )
        plan = ae.TestGoal({"objective": "Do it", "evidence": {}})
        rows_b = plan.remaining_work(
            page_state={"available_elements": ["A"]},
            page_text="t", url="https://x.test/", structural_changed=True,
        )
        self.assertEqual(self._keys(rows_a), self._keys(rows_b),
                         "explicit-step and runtime-plan rows must agree")

    def test_a_step_with_no_evidence_is_unverifiable(self):
        goal = ae.TestGoal({"objective": "Do it",
                            "steps": [{"describe": "step one"}]})
        rows = goal.remaining_work(
            page_state={"available_elements": ["A"]},
            page_text="t", url="https://x.test/", structural_changed=True,
        )
        self.assertFalse(rows[0]["verifiable"])
        self.assertTrue(rows[0]["unverifiable"],
                        "an unobservable requirement must say so explicitly")
        self.assertFalse(rows[0]["done"],
                         "unverifiable must never be reported as done")

    def test_a_step_with_evidence_is_verifiable(self):
        goal = ae.TestGoal({
            "objective": "Do it",
            "steps": [{"describe": "reach done",
                       "evidence": {"url_contains": ["done"]}}],
        })
        rows = goal.remaining_work(
            page_state={"available_elements": ["A"]},
            page_text="t", url="https://x.test/done", structural_changed=True,
        )
        self.assertTrue(rows[0]["verifiable"])
        self.assertFalse(rows[0]["unverifiable"])

    def test_contradiction_is_reported(self):
        goal = ae.TestGoal({
            "objective": "Do it",
            "steps": [{"describe": "no errors",
                       "evidence": {"text_not_contains": ["Error"]}}],
        })
        rows = goal.remaining_work(
            page_state={"available_elements": ["A"]},
            page_text="Error: invalid", url="https://x.test/", structural_changed=True,
        )
        self.assertTrue(rows[0]["contradicted"])
        self.assertFalse(rows[0]["done"])

    def test_verified_step_stays_done_and_not_unverifiable(self):
        goal = ae.TestGoal({
            "objective": "Do it",
            "steps": [{"describe": "reach done",
                       "evidence": {"url_contains": ["done"]}}],
        })
        rows = goal.remaining_work(
            page_state={"available_elements": ["A"]},
            page_text="t", url="https://x.test/done", structural_changed=True,
        )
        self.assertTrue(rows[0]["verified"])
        self.assertTrue(rows[0]["done"])
        self.assertFalse(rows[0]["unverifiable"])

    def test_navigator_prompt_marks_unverifiable_steps(self):
        """The [UNVERIFIABLE] annotation must actually be reachable."""
        goal = ae.TestGoal({"objective": "Do it",
                            "steps": [{"describe": "impossible thing"}]})
        rows = goal.remaining_work(
            page_state={"available_elements": ["A"]},
            page_text="t", url="https://x.test/", structural_changed=True,
        )
        with mock.patch.object(ae.ollama, "chat", return_value={
            "message": {"content": '{"best_choice": "A", "reasoning": "x"}'}}
        ):
            import io as _io
            import contextlib
            with contextlib.redirect_stdout(_io.StringIO()):
                ae.ask_ai_navigator(
                    ["A"], "goal", [], remaining_work=rows,
                )
        src = inspect.getsource(ae.ask_ai_navigator)
        self.assertIn("UNVERIFIABLE", src)

    def test_status_constants_are_distinct(self):
        for a, b in (("GOAL_PASS", "GOAL_FAIL"),
                     ("GOAL_PASS", "GOAL_BLOCKED"),
                     ("GOAL_FAIL", "GOAL_BLOCKED")):
            self.assertNotEqual(getattr(ae, a), getattr(ae, b))


class TestReq6_BackwardCompatibility(unittest.TestCase):
    """6. Preserve compatibility with existing configuration and CLI behaviour."""

    def test_step_row_legacy_keys_still_present(self):
        """Consumers reading index/describe/done/evidence must not break."""
        goal = ae.TestGoal({
            "objective": "Do it",
            "steps": [{"describe": "step one",
                       "evidence": {"url_contains": ["done"]}}],
        })
        rows = goal.remaining_work(
            page_state={"available_elements": ["A"]},
            page_text="t", url="https://x.test/done", structural_changed=True,
        )
        for key in ("index", "describe", "done", "evidence"):
            self.assertIn(key, rows[0], f"legacy key {key} must survive")
        self.assertEqual(rows[0]["index"], 1)
        self.assertEqual(rows[0]["describe"], "step one")

    def test_max_steps_default_is_the_single_authoritative_one(self):
        """The guard this replaced pinned a literal 40 that no run ever used.

        `TestGoal.max_steps` was written and never read; the run loop computed
        its own bound separately, so an omitted budget gave 25 while a
        malformed one gave 40 and neither was necessarily enforced. The default
        is now DEFAULT_MAX_STEPS in both places, and this asserts they agree
        rather than pinning a bare number that could drift again.
        """
        self.assertEqual(ae.TestGoal({"objective": "x"}).max_steps,
                         ae.DEFAULT_MAX_STEPS)
        self.assertEqual(ae.resolve_search_depth({}), ae.DEFAULT_MAX_STEPS)

    def test_empty_objective_yields_no_rows(self):
        """No objective and no steps must stay completely inert."""
        goal = ae.TestGoal({})
        self.assertEqual(
            goal.remaining_work(page_state={"available_elements": ["A"]},
                               page_text="t", url="u", structural_changed=True),
            [],
        )

    def test_objective_only_run_still_seeds_a_plan(self):
        goal = ae.TestGoal({"objective": "Add two items, then complete checkout",
                            "evidence": {}})
        rows = goal.remaining_work(
            page_state={"available_elements": ["Add to cart"]},
            page_text="t", url="https://x.test/i", structural_changed=True,
        )
        self.assertTrue(rows, "objective-only runs must still produce sub-goals")
        self.assertIsNotNone(goal.runtime_plan)


if __name__ == "__main__":
    unittest.main(verbosity=2)