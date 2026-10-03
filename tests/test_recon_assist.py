"""Checkpoint 5.4 — memory-assisted task execution.

The acceptance gate is a comparison between equivalent runs with memory enabled
and disabled. These tests pin the rules that make such a comparison meaningful:
memory may reorder but never add or remove a candidate, memory is never
authoritative, and the comparison reports regressions rather than only wins.
"""

import shutil
import tempfile
import unittest

import reconnaissance as r


class MemoryHintTests(unittest.TestCase):
    """A hint is advisory by construction and cannot be promoted."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reconhint_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _memory(self, **kwargs):
        return r.SiteMemory("https://shop.test/", directory=self.tmp, **kwargs)

    def test_hint_is_never_authoritative(self):
        mem = self._memory()
        record = mem.remember("n2", {"title": "About"},
                              kind=r.MEM_PAGE_SUMMARY)
        hint = r.MemoryHint(record, r.MEM_FRESH)
        self.assertFalse(hint.authoritative)

    def test_fresh_and_aging_hints_are_actionable(self):
        mem = self._memory()
        record = mem.remember("n2", {"title": "About"},
                              kind=r.MEM_PAGE_SUMMARY)
        self.assertTrue(r.MemoryHint(record, r.MEM_FRESH).is_actionable)
        self.assertTrue(r.MemoryHint(record, r.MEM_AGING).is_actionable)

    def test_stale_hint_is_not_actionable(self):
        mem = self._memory()
        record = mem.remember("n2", {"title": "About"},
                              kind=r.MEM_PAGE_SUMMARY)
        # Stale memory must not influence an action without revalidation.
        self.assertFalse(r.MemoryHint(record, r.MEM_STALE).is_actionable)

    def test_hints_carry_freshness_and_provenance(self):
        mem = self._memory()
        mem.remember("n2", {"title": "About"}, kind=r.MEM_PAGE_SUMMARY,
                     source_observation="obs-7")
        hints = r.memory_hints(mem)
        self.assertEqual(len(hints), 1)
        payload = hints[0].to_dict()
        self.assertEqual(payload["freshness"], r.MEM_FRESH)
        self.assertEqual(payload["source_observation"], "obs-7")
        self.assertFalse(payload["authoritative"])

    def test_hints_from_disabled_memory_are_empty(self):
        mem = self._memory(enabled=False)
        self.assertEqual(r.memory_hints(mem), [])


class CandidateOrderingTests(unittest.TestCase):
    """Memory reorders the live candidate list and nothing else."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reconorder_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ordering_never_adds_a_candidate(self):
        mem = r.SiteMemory("https://shop.test/", directory=self.tmp)
        mem.remember("not-in-observation", {"x": 1},
                     kind=r.MEM_PAGE_SUMMARY)
        hint = r.memory_hints(mem)[0]
        candidates = ["a", "b"]
        ordered = r.apply_memory_to_candidates(candidates, [hint])
        # Memory cannot introduce a candidate the live observation never saw.
        self.assertEqual(sorted(ordered), ["a", "b"])

    def test_ordering_never_removes_a_candidate(self):
        mem = r.SiteMemory("https://shop.test/", directory=self.tmp)
        mem.remember("c", {"x": 1}, kind=r.MEM_PAGE_SUMMARY)
        hint = r.memory_hints(mem)[0]
        candidates = ["a", "b", "c"]
        ordered = r.apply_memory_to_candidates(candidates, [hint])
        self.assertEqual(sorted(ordered), ["a", "b", "c"])

    def test_boost_moves_a_candidate_earlier(self):
        mem = r.SiteMemory("https://shop.test/", directory=self.tmp)
        mem.remember("c", {"x": 1}, kind=r.MEM_PAGE_SUMMARY)
        hint = r.memory_hints(mem)[0]
        ordered = r.apply_memory_to_candidates(["a", "b", "c", "d"], [hint])
        self.assertLess(ordered.index("c"), 3)

    def test_boost_is_bounded(self):
        mem = r.SiteMemory("https://shop.test/", directory=self.tmp)
        mem.remember("z", {"x": 1}, kind=r.MEM_PAGE_SUMMARY)
        hint = r.memory_hints(mem)[0]
        candidates = [f"c{i}" for i in range(10)] + ["z"]
        ordered = r.apply_memory_to_candidates(candidates, [hint], max_boost=2)
        # A single hint boosts by one position. It cannot drag a candidate from
        # last all the way to the front.
        self.assertEqual(ordered.index("z"), 9)
        self.assertEqual(sorted(ordered), sorted(candidates))

    def test_second_hint_gets_a_larger_bounded_boost(self):
        mem = r.SiteMemory("https://shop.test/", directory=self.tmp)
        mem.remember("a", {"x": 1}, kind=r.MEM_PAGE_SUMMARY)
        mem.remember("z", {"x": 1}, kind=r.MEM_PAGE_SUMMARY)
        hints = r.memory_hints(mem)
        candidates = ["a"] + [f"c{i}" for i in range(10)] + ["z"]
        ordered = r.apply_memory_to_candidates(candidates, hints, max_boost=2)
        self.assertEqual(sorted(ordered), sorted(candidates))
        # Both moved, neither jumped the list.
        self.assertLess(ordered.index("z"), len(candidates) - 1)

    def test_stale_hints_do_not_reorder(self):
        mem = r.SiteMemory("https://shop.test/", directory=self.tmp,
                           ttl_seconds=1)
        mem.remember("c", {"x": 1}, kind=r.MEM_PAGE_SUMMARY)
        stale = r.MemoryHint(
            next(iter(mem.records.values())), r.MEM_STALE)
        candidates = ["a", "b", "c"]
        self.assertEqual(r.apply_memory_to_candidates(candidates, [stale]),
                         candidates)

    def test_empty_inputs_pass_through(self):
        self.assertEqual(r.apply_memory_to_candidates([], []), [])
        self.assertEqual(r.apply_memory_to_candidates(["a"], []), ["a"])


class ComparisonTests(unittest.TestCase):
    """The A/B comparison reports regressions, not just improvements."""

    def test_no_difference_is_reported_as_such(self):
        run = {"model_calls": 5, "transitions": 3, "correct": 1}
        result = r.compare_memory_runs(run, run)
        self.assertEqual(result["verdict"], "no measurable difference")
        self.assertEqual(result["delta_model_calls"], 0)

    def test_fewer_model_calls_with_same_correctness_is_helpful(self):
        result = r.compare_memory_runs(
            {"model_calls": 4, "transitions": 3, "correct": 1},
            {"model_calls": 6, "transitions": 4, "correct": 1})
        self.assertEqual(result["delta_model_calls"], -2)
        self.assertIn("helpful", result["verdict"])

    def test_falling_correctness_is_a_regression(self):
        # A memory layer that saves calls while losing correctness has made the
        # tool worse, and the summary must say so.
        result = r.compare_memory_runs(
            {"model_calls": 4, "transitions": 3, "correct": 0},
            {"model_calls": 6, "transitions": 4, "correct": 1})
        self.assertIn("REGRESSION", result["verdict"])

    def test_acting_on_stale_memory_is_a_regression(self):
        result = r.compare_memory_runs(
            {"model_calls": 4, "correct": 1, "stale_memory_used": 1},
            {"model_calls": 6, "correct": 1, "stale_memory_used": 0})
        self.assertIn("REGRESSION", result["verdict"])

    def test_ignored_stale_memory_is_not_a_regression(self):
        result = r.compare_memory_runs(
            {"model_calls": 4, "correct": 1, "stale_memory_ignored": 3},
            {"model_calls": 6, "correct": 1})
        self.assertNotIn("REGRESSION", result["verdict"])

    def test_call_saving_with_improved_correctness_is_helpful(self):
        result = r.compare_memory_runs(
            {"model_calls": 4, "transitions": 2, "correct": 2},
            {"model_calls": 6, "transitions": 5, "correct": 1})
        self.assertIn("helpful", result["verdict"])

    def test_correctness_gain_without_call_saving_is_not_a_proven_benefit(self):
        result = r.compare_memory_runs(
            {"model_calls": 6, "transitions": 5, "correct": 2},
            {"model_calls": 6, "transitions": 5, "correct": 1})
        self.assertNotIn("helpful", result["verdict"])
        self.assertIn("not yet a proven benefit", result["verdict"])

    def test_malformed_run_dicts_do_not_crash(self):
        result = r.compare_memory_runs({"model_calls": None}, {})
        self.assertEqual(result["delta_model_calls"], 0)


if __name__ == "__main__":
    unittest.main()