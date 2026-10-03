"""Offline tests for tolerant navigator JSON handling.

Small local models emit almost-valid JSON. These tests cover the mechanical
repairs and the salvage path, and pin the safety property that recovery must
never invent a classification.

Run with:
    .\\venv311\\Scripts\\python.exe -m unittest tests.test_navigator_json -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae

ELEMENTS = ["Cart, 1 items", "Add to cart", "Open Menu"]


def _ask(content):
    """Run the navigator against a canned model response."""
    calls = []

    def fake_chat(model, messages):
        calls.append(messages[0]["content"])
        return {"message": {"content": content}}

    with mock.patch.object(ae.ollama, "chat", fake_chat):
        result = ae.ask_ai_navigator(
            ELEMENTS, "goal", [],
            mechanical_safety_tags={e: 0 for e in ELEMENTS},
        )
    return result, calls


class TestJsonRepair(unittest.TestCase):
    def test_trailing_comma_is_repaired(self):
        bad = '{"best_choice": "Cart, 1 items", "ranked_backup": [],}'
        self.assertEqual(
            ae.json.loads(ae._repair_json_text(bad))["best_choice"],
            "Cart, 1 items",
        )

    def test_trailing_comma_in_array_is_repaired(self):
        bad = '{"a": [1, 2, ], "b": 2}'
        self.assertEqual(ae.json.loads(ae._repair_json_text(bad))["a"], [1, 2])

    def test_valid_json_is_unchanged(self):
        good = '{"best_choice": "Open Menu"}'
        self.assertEqual(ae._repair_json_text(good), good)


class TestSalvage(unittest.TestCase):
    def test_salvages_best_choice_from_broken_json(self):
        broken = ('{"best_choice": "Cart, 1 items"\n'
                  '"ranked_backup": ["Add to cart"], "reasoning": "need the cart",}')
        out = ae._salvage_navigator_choice(broken, ELEMENTS)
        self.assertEqual(out["best_choice"], "Cart, 1 items")
        self.assertEqual(out["ranked_backup"], ["Add to cart"])

    def test_salvage_refuses_unobserved_element(self):
        broken = '{"best_choice": "Secret Checkout", "reasoning": "x"}'
        self.assertIsNone(ae._salvage_navigator_choice(broken, ELEMENTS),
                          "recovery must never introduce an unobserved element")

    def test_salvage_never_invents_safety_tags(self):
        broken = '{"best_choice": "Add to cart", "reasoning": "x"}'
        out = ae._salvage_navigator_choice(broken, ELEMENTS)
        self.assertEqual(out["safety_tags"], {},
                         "recovery must not guess a safety classification")

    def test_salvage_deduplicates_and_drops_self_reference(self):
        broken = ('{"best_choice": "Add to cart", "ranked_backup": '
                  '["Add to cart", "Open Menu", "Open Menu", "Ghost"]}')
        out = ae._salvage_navigator_choice(broken, ELEMENTS)
        self.assertEqual(out["ranked_backup"], ["Open Menu"])

    def test_salvage_handles_escaped_quotes(self):
        broken = '{"best_choice": "Cart, 1 items", "reasoning": "the \\"cart\\" first"}'
        out = ae._salvage_navigator_choice(broken, ELEMENTS)
        self.assertEqual(out["best_choice"], "Cart, 1 items")


class TestNavigatorRecovery(unittest.TestCase):
    def test_malformed_json_still_yields_a_decision(self):
        """The regression: a broken response must not void the decision."""
        broken = ('{"best_choice": "Cart, 1 items"\n'
                  '"ranked_backup": ["Add to cart"], "reasoning": "cart first",}')
        result, _ = _ask(broken)
        self.assertIsNotNone(result)
        self.assertEqual(result["best_choice"], "Cart, 1 items")

    def test_unrepairable_json_with_unknown_choice_is_rejected(self):
        broken = '{"best_choice": "Not A Real Button", "ranked_backup": [}'
        result, calls = _ask(broken)
        self.assertIsNone(result)
        self.assertGreaterEqual(len(calls), 1)

    def test_valid_json_unchanged_behaviour(self):
        good = ('{"best_choice": "Add to cart", "ranked_backup": ["Cart, 1 items"], '
                '"reasoning": "x", "safety_tags": {"Cart, 1 items": 0, '
                '"Add to cart": 0, "Open Menu": 0}}')
        result, _ = _ask(good)
        self.assertEqual(result["best_choice"], "Add to cart")
        self.assertEqual(result["safety_tags"]["Add to cart"], 0)

    def test_recovered_decision_passes_validation(self):
        broken = '{"best_choice": "Cart, 1 items", "ranked_backup": ["Ghost"],}'
        result, _ = _ask(broken)
        self.assertIsNotNone(result)
        # Recovered backups are constrained to observed elements.
        self.assertTrue(all(e in ELEMENTS for e in result["ranked_backup"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)