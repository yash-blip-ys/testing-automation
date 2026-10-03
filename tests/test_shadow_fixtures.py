"""Part 4.4 — Shadow replanning, driven by deterministic fixtures.

The activation decision in Part 4.5 must rest on reproducible inputs, not on
whatever a model happened to produce during one live run. These tests replay
fixtures/shadow_replan_cases.json against `would_adopt`, which cannot mutate the
plan.

They also assert the property the whole exercise rests on: replaying every
fixture leaves the plan byte-identical.

Run with:
    .\\venv311\\Scripts\\python.exe -m unittest tests.test_shadow_fixtures -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "fixtures", "shadow_replan_cases.json")


def _load():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


class TestFixtureFileIsUsable(unittest.TestCase):

    def setUp(self):
        self.data = _load()

    def test_the_fixture_declares_a_page_and_cases(self):
        self.assertIn("objective", self.data)
        self.assertIn("page", self.data)
        self.assertTrue(self.data["cases"])

    def test_every_case_is_fully_specified(self):
        for case in self.data["cases"]:
            self.assertIn("name", case)
            self.assertIn("why", case, f"{case['name']} must say why it exists")
            self.assertIn("proposal", case)
            self.assertIn("expect", case)

    def test_case_names_are_unique(self):
        names = [c["name"] for c in self.data["cases"]]
        self.assertEqual(len(names), len(set(names)))

    def test_the_historical_failure_is_represented(self):
        """The regression must remain in the fixture set forever."""
        self.assertTrue(any(
            "historical_failure" in c["name"] for c in self.data["cases"]),
            "the checkout-never-reached failure mode must stay covered")


class TestFixtureReplay(unittest.TestCase):
    """One test method per case, so a failure names the case that broke."""

    def setUp(self):
        self.data = _load()
        self.page = self.data["page"]
        self.plan = ae.RuntimePlan(self.data["objective"])
        self.plan.seed_from_objective(
            self.page["elements"], self.page["url"], self.page["text"])

    def _run(self, name):
        case = next(c for c in self.data["cases"] if c["name"] == name)
        before = ([dict(i) for i in self.plan.items], dict(self.plan._verified),
                  self.plan.replans, list(self.plan.proposal_log))
        evaluator = ae.ShadowEvaluator(enabled=True)
        record = evaluator.evaluate(
            self.plan, case["proposal"], step=1,
            elements=self.page["elements"], url=self.page["url"],
            page_text=self.page["text"],
            replace_keys=case.get("replace_keys"))
        after = ([dict(i) for i in self.plan.items], dict(self.plan._verified),
                 self.plan.replans, list(self.plan.proposal_log))
        self.assertEqual(before, after,
                         f"{name}: replay must leave the plan untouched")
        return case, record

    def _assert(self, case, record):
        for key, expected in case["expect"].items():
            if key == "reason_contains":
                self.assertIn(expected, record.get("reason", ""),
                              f"{case['name']}: reason should mention "
                              f"{expected!r}")
            else:
                self.assertEqual(record.get(key), expected,
                                 f"{case['name']}: {key}")

    def test_historical_failure_checkout_replaced_by_visible_controls(self):
        case, record = self._run(
            "historical_failure_checkout_replaced_by_visible_controls")
        self._assert(case, record)
        # The whole point of the case: nothing outstanding is lost, because
        # nothing was adopted, and the filler that caused the refusal is
        # reported rather than quietly discarded.
        self.assertIn("complete checkout",
                      [i["requirement"] for i in self.plan.items])
        self.assertEqual(record["plan_after"],
                         [ae.normalise_requirement(i["requirement"])
                          for i in self.plan.items],
                         "a refused proposal must leave the plan as it was")

    def test_plausible_reroute_accepted(self):
        case, record = self._run("plausible_reroute_accepted")
        self._assert(case, record)

    def test_unrelated_work_displaces_outstanding_requirement(self):
        case, record = self._run("unrelated_work_displaces_outstanding_requirement")
        self._assert(case, record)

    def test_safety_violating_route_refused(self):
        case, record = self._run("safety_violating_route_refused")
        self._assert(case, record)
        self.assertTrue(any("safety policy" in d for d in record["dropped"]),
                        "the refusal must name the policy it hit")

    def test_quantity_contradiction_refused(self):
        case, record = self._run("quantity_contradiction_refused")
        self._assert(case, record)
        self.assertTrue(any("quantity" in d for d in record["dropped"]))

    def test_plan_growth_measured(self):
        case, record = self._run("plan_growth_measured")
        self._assert(case, record)

    def test_no_proposal_at_all(self):
        case, record = self._run("no_proposal_at_all")
        self._assert(case, record)

    def test_every_case_in_the_file_is_covered_by_a_test(self):
        """A new fixture case with no test would silently go unmeasured."""
        covered = {m[len("test_"):] for m in dir(self)
                   if m.startswith("test_")}
        for case in self.data["cases"]:
            self.assertIn(case["name"], covered,
                          f"fixture case {case['name']!r} has no test method")


class TestAggregateSignal(unittest.TestCase):
    """What the fixture set as a whole says about activation."""

    def setUp(self):
        self.data = _load()
        self.page = self.data["page"]

    def test_replay_yields_a_mixed_signal_rather_than_a_clean_yes(self):
        """Activation needs more than "most proposals looked fine".

        If every case were admissible, the fixture set would be telling us
        nothing. The set must contain refusals AND acceptances, so the decision
        has to weigh both.
        """
        verdicts = []
        for case in self.data["cases"]:
            plan = ae.RuntimePlan(self.data["objective"])
            plan.seed_from_objective(self.page["elements"],
                                     self.page["url"], self.page["text"])
            record = ae.ShadowEvaluator(enabled=True).evaluate(
                plan, case["proposal"], step=1,
                elements=self.page["elements"], url=self.page["url"],
                page_text=self.page["text"],
                replace_keys=case.get("replace_keys"))
            verdicts.append(record["would_adopt"])
        self.assertIn(True, verdicts, "the set must contain acceptable routes")
        self.assertIn(False, verdicts, "the set must contain refusals")

    def test_no_case_in_the_set_loses_work_without_saying_so(self):
        """Every displacement must be reported, never absorbed silently."""
        for case in self.data["cases"]:
            plan = ae.RuntimePlan(self.data["objective"])
            plan.seed_from_objective(self.page["elements"],
                                     self.page["url"], self.page["text"])
            record = ae.ShadowEvaluator(enabled=True).evaluate(
                plan, case["proposal"], step=1,
                elements=self.page["elements"], url=self.page["url"],
                page_text=self.page["text"],
                replace_keys=case.get("replace_keys"))
            self.assertEqual(
                record["preserves_outstanding"], not record["lost_outstanding"],
                f"{case['name']}: preserve flag and lost list must agree")

    def test_replaying_the_whole_set_never_spends_replan_budget(self):
        for case in self.data["cases"]:
            plan = ae.RuntimePlan(self.data["objective"])
            plan.seed_from_objective(self.page["elements"],
                                     self.page["url"], self.page["text"])
            evaluator = ae.ShadowEvaluator(enabled=True)
            for _ in range(5):
                evaluator.evaluate(
                    plan, case["proposal"], step=1,
                    elements=self.page["elements"], url=self.page["url"],
                    page_text=self.page["text"],
                    replace_keys=case.get("replace_keys"))
            self.assertEqual(plan.replans, 0,
                             f"{case['name']}: shadow replay spent replan budget")


if __name__ == "__main__":
    unittest.main()