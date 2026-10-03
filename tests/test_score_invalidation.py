"""Offline tests for goal-aware edge-score invalidation.

The regression this guards: a node's cached edge weights answer the question
that was asked when they were produced. When the outstanding sub-goals change,
those scores no longer answer the current question and must be recomputed.

Run with:
    .\\venv311\\Scripts\\python.exe -m unittest tests.test_score_invalidation -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae


class _Loop:
    """Minimal stand-in exposing the loop-local helpers used for scoring.

    Mirrors the real module so the invalidation rule can be pinned without a
    browser. `decide` reproduces the branch order in the agent's loop.
    """

    def __init__(self, state_mgr, stale_enabled=True, threshold=6):
        self.state_mgr = state_mgr
        self.stale_enabled = stale_enabled
        self.threshold = threshold
        self.node_goal_context = {}
        self.scored_nodes = set()
        self.ai_calls = 0

    def decide(self, current_node, rw, goal_ctx):
        need_ai_call = False
        goal_ctx_now = goal_ctx
        goal_ctx_prev = self.node_goal_context.get(current_node)
        goal_ctx_changed = (
            goal_ctx_now is not None
            and goal_ctx_prev is not None
            and goal_ctx_now != goal_ctx_prev
        )
        if current_node not in self.scored_nodes:
            need_ai_call = True
        elif goal_ctx_changed:
            need_ai_call = True
        elif self.stale_enabled:
            rec = self.state_mgr.get_visited_record(current_node)
            first = rec.first_action_count if rec else 0
            if self.state_mgr.action_count - first >= self.threshold:
                need_ai_call = True
        if need_ai_call:
            self.ai_calls += 1
            self.scored_nodes.add(current_node)
            if goal_ctx_now is not None:
                self.node_goal_context[current_node] = goal_ctx_now
        return need_ai_call


def _mgr():
    return ae.StateManager(max_search_depth=30)


class TestFirstScoringAlwaysConsultsTheNavigator(unittest.TestCase):
    def test_brand_new_node_is_scored(self):
        loop = _Loop(_mgr())
        self.assertTrue(loop.decide("n1", [], (1, 2)))

    def test_ledger_visit_does_not_preempt_first_scoring(self):
        """The regression: the ledger records the node before scoring runs.

        snapshot_before_action marks a node visited, so a visited-node check
        would wrongly conclude the node had already been scored and skip the
        navigator entirely.
        """
        mgr = _mgr()
        mgr.snapshot_before_action(node_hash="n1", url="u", page_title="t",
                                   available_elements=["A"], cart_badge_count=0,
                                   form_input_count=0, step_index=0,
                                   externals_count=0)
        self.assertTrue(mgr.was_visited_before("n1"),
                        "precondition: the ledger knows this node")
        loop = _Loop(mgr)
        self.assertTrue(loop.decide("n1", [], (1, 2)),
                        "first scoring must still consult the navigator")

    def test_second_visit_without_context_change_skips_the_ai(self):
        loop = _Loop(_mgr(), threshold=99)
        loop.decide("n1", [], (1, 2))
        before = loop.ai_calls
        self.assertFalse(loop.decide("n1", [], (1, 2)))
        self.assertEqual(loop.ai_calls, before)


class TestGoalContextInvalidation(unittest.TestCase):
    def test_changed_outstanding_steps_force_a_rescore(self):
        loop = _Loop(_mgr(), threshold=99)
        loop.decide("n1", [], (1, 2, 3))
        self.assertTrue(loop.decide("n1", [], (2, 3)),
                        "a shorter remaining list invalidates cached scores")

    def test_unchanged_context_does_not_force_a_rescore(self):
        loop = _Loop(_mgr(), threshold=99)
        loop.decide("n1", [], (2, 3))
        self.assertFalse(loop.decide("n1", [], (2, 3)))

    def test_first_scoring_still_records_context(self):
        loop = _Loop(_mgr())
        loop.decide("n1", [], (1, 2))
        self.assertEqual(loop.node_goal_context["n1"], (1, 2))

    def test_no_steps_configured_never_invalidates(self):
        """Configs without sub-goals must keep the original caching."""
        loop = _Loop(_mgr(), threshold=99)
        loop.decide("n1", [], None)
        before = loop.ai_calls
        self.assertFalse(loop.decide("n1", [], None))
        self.assertEqual(loop.ai_calls, before)

    def test_context_change_resets_stale_score_dependence(self):
        mgr = _mgr()
        loop = _Loop(mgr, threshold=99)
        loop.decide("n1", [], (1, 2, 3))
        mgr._action_counter = 200
        self.assertTrue(loop.decide("n1", [], (3,)),
                        "progress must re-rank edges even far from the last score")


class TestProgressOrdering(unittest.TestCase):
    def test_each_verified_step_invalidates_on_the_next_page(self):
        """Walking the SauceDemo flow must rescore at every stage."""
        loop = _Loop(_mgr(), threshold=999)
        stages = [(1, 2, 3, 4, 5), (2, 3, 4, 5), (3, 4, 5), (4, 5), (5,)]
        node = "inventory"
        for ctx in stages:
            self.assertTrue(loop.decide(node, [], ctx),
                            f"context {ctx} must trigger a rescore")
        self.assertEqual(loop.ai_calls, len(stages))


if __name__ == "__main__":
    unittest.main(verbosity=2)