"""Offline unit tests for the generic element discovery, node identity,
goal verification and loop-prevention layers.

These require neither a live website nor Ollama. Run with:

    .\\venv311\\Scripts\\python.exe -m unittest discover -s tests -v

Test coverage maps to the required scenarios:
  1. same URL + same elements, different cart counts -> distinct nodes
  2. same URL + same elements, different non-sensitive form values -> distinct nodes
  3. no semantic config -> original structural behavior preserved
  4. semantic extraction failure -> safe fallback, no crash
  5. repeated (node, action, destination) -> loop penalty escalates
  6. genuine state change -> repeated action is NOT a loop
  7. sensitive fields excluded from state, hashes and reports
"""

import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae


# --------------------------------------------------------------------------
# Fakes: a minimal Playwright page double for the async DOM helpers.
# --------------------------------------------------------------------------

class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    async def count(self):
        if self.page.raise_on_evaluate:
            raise RuntimeError("simulated Playwright failure")
        return self.page.count_for(self.selector)

    async def is_visible(self, timeout=None):
        return True

    async def click(self, timeout=None):
        self.page.clicked.append(self.selector)


class FakePage:
    """Stands in for a Playwright Page.

    `evaluate` returns a canned payload for the extractor and the semantic
    extractor; `raise_on_evaluate` simulates an extraction failure.
    """

    def __init__(self, extract_payload=None, semantic_payload=None,
                 raise_on_evaluate=False):
        self.extract_payload = extract_payload or {"records": [], "mechanical_safety_tags": {}}
        self.semantic_payload = semantic_payload
        self.raise_on_evaluate = raise_on_evaluate
        self.clicked = []
        self.url = "https://example.test/page"
        self._counts = {}

    def count_for(self, selector):
        return self._counts.get(selector, 1)

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def evaluate(self, script, args=None):
        if self.raise_on_evaluate:
            raise RuntimeError("simulated Playwright failure")
        # Distinguish the extractor from the semantic extractor by the
        # distinctive ARITY/SIGNATURE of each script, not by prose: matching a
        # bare word like "semantic" also matches a comment, which made this
        # double silently return the wrong payload once a comment was edited.
        if "cartSelector, formFieldSelector, sensitiveMarkers" in script:
            if self.semantic_payload is None:
                raise RuntimeError("semantic extraction unavailable")
            return self.semantic_payload
        return self.extract_payload

    async def title(self):
        return "Example"


def rec(agent_id, name, role="button", disabled=False):
    return {
        "agent_id": agent_id, "tag": "button", "role": role, "name": name,
        "type": "", "disabled": disabled, "value": "",
        "placeholder": "", "aria_label": "", "testid": "",
    }


# --------------------------------------------------------------------------
# 1 + 2 + 3 + 6: node identity
# --------------------------------------------------------------------------

class TestNodeIdentity(unittest.TestCase):
    URL = "https://example.test/catalog"
    ELEMENTS = ["Search", "Dune", "Emma"]

    def test_1_different_cart_counts_are_distinct_nodes(self):
        """Same URL + same elements, different cart count -> different node."""
        sig_0 = {"cart_count": 0, "form_values": {}}
        sig_1 = {"cart_count": 1, "form_values": {}}

        n0, s0 = ae.compute_full_node_id(self.URL, self.ELEMENTS, sig_0)
        n1, s1 = ae.compute_full_node_id(self.URL, self.ELEMENTS, sig_1)

        self.assertNotEqual(
            n0, n1,
            "A cart change must produce a distinct node identity, otherwise the "
            "agent cannot tell 'cart empty' from 'cart holding an item'."
        )
        self.assertEqual(s0["cart_count"], 0)
        self.assertEqual(s1["cart_count"], 1)

    def test_2_different_form_values_are_distinct_nodes(self):
        """Same URL + same elements, different non-sensitive form values."""
        sig_a = {"cart_count": None, "form_values": {"q": ae.hash_form_value("Dune")}}
        sig_b = {"cart_count": None, "form_values": {"q": ae.hash_form_value("Emma")}}

        na, _ = ae.compute_full_node_id(self.URL, self.ELEMENTS, sig_a)
        nb, _ = ae.compute_full_node_id(self.URL, self.ELEMENTS, sig_b)

        self.assertNotEqual(
            na, nb,
            "Different non-sensitive form values must yield different nodes so a "
            "search/filter step is observable as progress."
        )

    def test_3_no_semantic_config_preserves_structural_behavior(self):
        """Absent/empty/None signals must reproduce the original hash exactly."""
        structural = ae.compute_node_hash(self.URL, self.ELEMENTS)

        for empty in (None, {}, {"cart_count": None, "form_values": {}}):
            node_id, signature = ae.compute_full_node_id(self.URL, self.ELEMENTS, empty)
            self.assertEqual(
                node_id, structural,
                "With no semantic signals the node id must equal the original "
                "structural hash, so existing behavior is preserved."
            )
            self.assertEqual(signature, {})

    def test_semantic_layer_only_splits_never_merges(self):
        """Semantic signals must never merge two structurally distinct nodes."""
        a = ae.compute_node_hash(self.URL, ["Search", "Dune"])
        b = ae.compute_node_hash(self.URL, ["Search", "Dune", "Emma"])

        sig = {"cart_count": 3, "form_values": {}}
        na, _ = ae.compute_full_node_id(self.URL, ["Search", "Dune"], sig)
        nb, _ = ae.compute_full_node_id(self.URL, ["Search", "Dune", "Emma"], sig)

        self.assertNotEqual(a, b, "precondition: the two pages differ structurally")
        self.assertNotEqual(na, nb, "a semantic signature must not collapse them")


# --------------------------------------------------------------------------
# 4: extraction failure never crashes and falls back safely
# --------------------------------------------------------------------------

class TestExtractionFallback(unittest.TestCase):
    def test_4_dom_extraction_failure_returns_safe_empty(self):
        page = FakePage(raise_on_evaluate=True)
        out = asyncio.get_event_loop().run_until_complete(
            ae.extract_page_elements(page, "button")
        )
        self.assertEqual(out["labels"], [])
        self.assertEqual(out["records"], [])
        self.assertEqual(out["count"], 0)

    def test_4_semantic_extraction_failure_returns_empty(self):
        cfg = ae._parse_semantic_config({"semantic_signals": {
            "cart_count_selector": ".badge",
        }})
        # simulate JS-side failure
        page = FakePage(semantic_payload=None)
        out = asyncio.get_event_loop().run_until_complete(
            ae.extract_semantic_signals(page, cfg)
        )
        self.assertEqual(out, {}, "a failed extraction must yield no signals")

    def test_4_extraction_failure_falls_back_to_structural_node(self):
        cfg = ae._parse_semantic_config({"semantic_signals": {
            "cart_count_selector": ".badge",
        }})
        page = FakePage(semantic_payload=None)
        failed = asyncio.get_event_loop().run_until_complete(
            ae.extract_semantic_signals(page, cfg)
        )
        url, els = "https://example.test/x", ["A", "B"]
        node_id, signature = ae.compute_full_node_id(url, els, failed)
        self.assertEqual(node_id, ae.compute_node_hash(url, els))
        self.assertEqual(signature, {})

    def test_4_unconfigured_semantic_config_is_a_noop(self):
        cfg = ae._parse_semantic_config({})
        self.assertIsNone(cfg["cart_selector"])
        self.assertIsNone(cfg["form_field_selector"])

    def test_4_partial_semantic_config_does_not_halve_extraction(self):
        """Only cart configured -> form signals absent, cart still observed."""
        cfg = ae._parse_semantic_config({"semantic_signals": {
            "cart_count_selector": ".badge",
        }})
        page = FakePage(semantic_payload={"cart_count": 4})
        out = asyncio.get_event_loop().run_until_complete(
            ae.extract_semantic_signals(page, cfg)
        )
        self.assertEqual(out, {"cart_count": 4})
        self.assertNotIn("form_values", out)

    def test_4_non_numeric_cart_text_is_dropped(self):
        cfg = ae._parse_semantic_config({"semantic_signals": {
            "cart_count_selector": ".badge",
        }})
        page = FakePage(semantic_payload={"cart_count": "not-a-number"})
        out = asyncio.get_event_loop().run_until_complete(
            ae.extract_semantic_signals(page, cfg)
        )
        self.assertEqual(out, {}, "unparseable values must not crash or fake a state")


# --------------------------------------------------------------------------
# 7: sensitive values never reach state, hashes, logs or reports
# --------------------------------------------------------------------------

class TestSensitiveValueHandling(unittest.TestCase):
    def test_7_markers_detect_typical_secret_fields(self):
        for field in ("password", "Password", "user_password", "auth-token",
                      "api_key", "secret_key", "card_number", "cvv", "otp"):
            self.assertTrue(
                ae.is_sensitive_field(field),
                f"'{field}' must be treated as sensitive"
            )

    def test_7_ordinary_fields_are_not_marked_sensitive(self):
        for field in ("firstname", "lastname", "zip", "email",
                      "search", "q", "username", "comment"):
            self.assertFalse(
                ae.is_sensitive_field(field),
                f"'{field}' must not be treated as sensitive"
            )

    def test_7_sensitive_field_values_never_appear_in_a_signature(self):
        raw = {"form_values": {
            "password": "hunter2-secret",
            "api_key": "AKIAEXAMPLE123",
            "q": "Dune",
        }}
        sig = ae.compute_semantic_signature("https://example.test", ["A"], raw)
        blob = json.dumps(sig)
        self.assertNotIn("hunter2-secret", blob)
        self.assertNotIn("AKIAEXAMPLE123", blob)

    def test_7_sensitive_fields_excluded_from_hash_but_count_tracked(self):
        """Excluded fields must not silently merge genuinely different states.

        If a page changes ONLY in its password field, we must not treat that as
        a new application state (password is not observable behavior) AND we
        must not leak the value. A sentinel keeps such states distinct without
        storing anything sensitive.
        """
        with_secret = ae.compute_semantic_signature("u", ["A"], {
            "form_values": {"password": "hunter2"}
        })
        without = ae.compute_semantic_signature("u", ["A"], {
            "form_values": {"password": ""}
        })
        self.assertNotEqual(
            with_secret["digest"], without["digest"],
            "a filled secret field must still register as a state difference"
        )
        self.assertNotIn("hunter2", json.dumps(with_secret))

    def test_7_hash_form_value_is_one_way_and_stable(self):
        h1 = ae.hash_form_value("Dune")
        h2 = ae.hash_form_value("Dune")
        self.assertEqual(h1, h2, "hashing must be stable across runs")
        self.assertNotIn("Dune", h1, "the raw value must not survive hashing")
        self.assertIsNone(ae.hash_form_value(""), "empty values contribute nothing")
        self.assertIsNone(ae.hash_form_value(None))


# --------------------------------------------------------------------------
# 5 + 6: loop prevention
# --------------------------------------------------------------------------

class TestLoopPrevention(unittest.TestCase):
    def test_5_repeated_triple_escalates_penalty(self):
        mgr = ae.StateManager(max_search_depth=25)
        self.assertEqual(mgr.record_loop_attempt("A", "Click", "B"), 0)
        first = mgr.record_loop_attempt("A", "Click", "B")
        second = mgr.record_loop_attempt("A", "Click", "B")
        third = mgr.record_loop_attempt("A", "Click", "B")

        self.assertGreater(first, 0, "a repeat must be detected")
        self.assertGreater(second, first, "the penalty must escalate")
        self.assertGreater(third, second, "and keep escalating")

    def test_5_penalty_is_capped(self):
        mgr = ae.StateManager(max_search_depth=100)
        for _ in range(60):
            mgr.record_loop_attempt("A", "Click", "B")
        self.assertLessEqual(mgr.record_loop_attempt("A", "Click", "B"),
                             ae.LOOP_PAIR_PENALTY_CAP)

    def test_5_history_is_bounded(self):
        mgr = ae.StateManager(max_search_depth=25)
        for i in range(40):
            mgr.record_loop_attempt(f"n{i}", "Click", "x")
        self.assertLessEqual(len(mgr._loop_history), ae.LOOP_HISTORY_LIMIT)

    def test_6_genuine_state_change_is_not_a_loop(self):
        """Same (node, action) landing on a NEW state is legitimate."""
        mgr = ae.StateManager(max_search_depth=25)
        url, els = "https://example.test/catalog", ["Search"]
        n0, _ = ae.compute_full_node_id(url, els, {"cart_count": 0})
        n1, _ = ae.compute_full_node_id(url, els, {"cart_count": 1})
        n2, _ = ae.compute_full_node_id(url, els, {"cart_count": 2})

        self.assertEqual(mgr.record_loop_attempt(n0, "Add to cart", n1), 0)
        self.assertEqual(
            mgr.record_loop_attempt(n0, "Add to cart", n2), 0,
            "adding a second item changes the cart count, so the destination "
            "differs and this must NOT be treated as an unproductive loop"
        )
        self.assertEqual(
            mgr.record_loop_attempt(n0, "Add to cart", n1), 0,
            "returning to an earlier state is still not the same triple being "
            "repeated consecutively in the bounded window"
        )

    def test_6_different_actions_from_same_node_are_independent(self):
        mgr = ae.StateManager(max_search_depth=25)
        mgr.record_loop_attempt("A", "Search", "B")
        mgr.record_loop_attempt("A", "Filter", "B")
        self.assertEqual(mgr.record_loop_attempt("A", "Search", "B"), 0)

    def test_toggle_loop_between_known_nodes_is_penalized(self):
        """Open/close toggles change the node but never make progress.

        Each click legitimately lands on a different node, so the consecutive
        triple detector stays quiet. record_repeat() is what catches this: the
        same edge keeps landing on an already-visited node, so the agent is
        shuttling between states it already knows rather than advancing.
        """
        mgr = ae.StateManager(max_search_depth=25)
        # Pretend the agent has already seen both menu states.
        for node in ("menu_closed", "menu_open"):
            mgr.snapshot_before_action(node_hash=node, url="https://x.test/app")

        # First traversal of this edge is never penalized.
        self.assertEqual(mgr.record_repeat("menu_closed", "Open Menu", "menu_open"), 0)

        # Same edge again, and this time it lands back on a known node.
        second = mgr.record_repeat("menu_closed", "Open Menu", "menu_closed")
        self.assertGreater(
            second, 0,
            "an edge that only reaches already-visited states is unproductive "
            "even when each individual click changed the DOM"
        )

        # And the penalty escalates rather than staying flat.
        third = mgr.record_repeat("menu_closed", "Open Menu", "menu_closed")
        self.assertGreater(third, second)

    def test_productive_repeat_to_new_node_is_not_penalized(self):
        """Adding a second cart item yields a new state: that is progress."""
        mgr = ae.StateManager(max_search_depth=25)
        mgr.snapshot_before_action(node_hash="catalog_empty", url="https://x.test/c")

        # First add: new destination, and the edge is new -> no penalty.
        self.assertEqual(mgr.record_repeat("catalog_empty", "Add to cart", "catalog_1"), 0)
        # Second add from the SAME source reaches a different NEW node.
        mgr.snapshot_before_action(node_hash="catalog_1", url="https://x.test/c")
        self.assertEqual(
            mgr.record_repeat("catalog_empty", "Add to cart", "catalog_2"), 0,
            "reaching a never-seen node is real progress, not a loop"
        )

    def test_repeat_counter_resets_after_progress(self):
        mgr = ae.StateManager(max_search_depth=25)
        mgr.snapshot_before_action(node_hash="n0", url="u")
        mgr.record_repeat("n0", "Add", "n1")          # new edge, 0
        mgr.snapshot_before_action(node_hash="n1", url="u")
        mgr.record_repeat("n0", "Add", "n1")          # revisit known -> 1
        self.assertEqual(
            mgr.record_repeat("n0", "Add", "brand_new"), 0,
            "after making progress the edge's reputation must reset"
        )


# --------------------------------------------------------------------------
# Element discovery: grounded, generic, no site knowledge
# --------------------------------------------------------------------------

class TestElementDiscovery(unittest.TestCase):
    def _run(self, payload):
        page = FakePage(extract_payload=payload)
        return asyncio.get_event_loop().run_until_complete(
            ae.extract_page_elements(page, "button")
        )

    def test_records_become_labels_with_agent_ids(self):
        out = self._run({"records": [rec("1", "Search"), rec("2", "Filter")],
                          "mechanical_safety_tags": {}})
        self.assertEqual(out["labels"], ["Search", "Filter"])
        self.assertEqual(out["by_label"]["Search"]["agent_id"], "1")
        self.assertEqual(out["hints"]["Search"], "agent_id")
        self.assertEqual(out["count"], 2)

    def test_disabled_elements_are_reported_separately(self):
        out = self._run({"records": [rec("1", "Pay", disabled=True),
                                     rec("2", "Search")],
                          "mechanical_safety_tags": {}})
        self.assertEqual(out["disabled"], {"Pay"})
        self.assertIn("Pay", out["labels"], "still visible, just flagged")

    def test_duplicate_labels_do_not_silently_shadow_each_other(self):
        """Two buttons sharing a name must not produce an ambiguous mapping."""
        out = self._run({"records": [rec("1", "Add"), rec("2", "Add")],
                          "mechanical_safety_tags": {}})
        self.assertEqual(out["labels"], ["Add"], "labels are de-duplicated")
        self.assertEqual(out["count"], 2, "both real elements are still counted")
        self.assertEqual(out["by_label"]["Add"]["agent_id"], "1",
                         "we keep the first; count exposes the ambiguity")

    def test_untagged_external_link_is_preserved_by_mechanical_detector(self):
        out = self._run({
            "records": [rec("1", "Docs"), rec("2", "Search")],
            "mechanical_safety_tags": {"Docs": -1},
        })
        self.assertEqual(out["mechanical_safety_tags"], {"Docs": -1})

    def test_empty_page_yields_empty_but_valid_structure(self):
        out = self._run({"records": [], "mechanical_safety_tags": {}})
        self.assertEqual(out["labels"], [])
        self.assertIsInstance(out["disabled"], set)


# --------------------------------------------------------------------------
# Goal verification: PASS / FAIL / BLOCKED from observable evidence
# --------------------------------------------------------------------------

def _evaluate(goal_cfg, elements, text, url, structural_changed=False, steps=0):
    goal = ae.TestGoal(goal_cfg)
    return goal.evaluate(
        page_state={"available_elements": elements},
        page_text=text, url=url,
        structural_changed=structural_changed, steps_taken=steps,
    )


class TestGoalVerification(unittest.TestCase):
    PASS_GOAL = {
        "objective": "Open the book detail page.",
        "evidence": {"text_contains": ["Availability"], "steps_min": 1},
    }
    FAIL_GOAL = {
        "objective": "Open the book detail page.",
        "evidence": {"text_not_contains": ["No results found"]},
    }

    def test_pass_requires_evidence_not_just_a_click(self):
        """A click with no observable evidence must NOT be a PASS."""
        status, evidence = _evaluate(
            self.PASS_GOAL, ["Search"], "Search page", "https://x.test/",
            structural_changed=True, steps=1,
        )
        self.assertEqual(status, ae.GOAL_BLOCKED)

    def test_pass_when_expected_text_appears(self):
        status, evidence = _evaluate(
            self.PASS_GOAL, ["Back to results", "Availability"],
            "Dune by Frank Herbert Availability In stock",
            "https://x.test/#/book/dune", steps=1,
        )
        self.assertEqual(status, ae.GOAL_PASS)
        self.assertIn("text_contains", evidence)

    def test_fail_when_contradicting_text_present(self):
        status, evidence = _evaluate(
            self.FAIL_GOAL, ["Search"], "No results found for 'zzz'",
            "https://x.test/", steps=2,
        )
        self.assertEqual(status, ae.GOAL_FAIL)
        self.assertIn("No results found", evidence)

    def test_negative_evidence_overrides_positive(self):
        goal = {"objective": "x", "evidence": {
            "text_contains": ["Availability"],
            "text_not_contains": ["No results found"],
        }}
        status, _ = _evaluate(goal, [], "Availability No results found", "u")
        self.assertEqual(status, ae.GOAL_FAIL)

    def test_unconfigured_goal_is_blocked_not_pass(self):
        status, evidence = _evaluate({}, ["anything"], "text", "u")
        self.assertEqual(status, ae.GOAL_BLOCKED)
        self.assertIn("no goal", evidence)

    def test_url_evidence(self):
        goal = {"objective": "x", "evidence": {"url_contains": ["checkout-complete"]}}
        status, _ = _evaluate(goal, [], "done", "https://x.test/checkout-complete.html")
        self.assertEqual(status, ae.GOAL_PASS)

    def test_state_changed_evidence_requires_real_change(self):
        goal = {"objective": "x", "evidence": {"state_changed": True}}
        self.assertEqual(_evaluate(goal, [], "t", "u", False)[0], ae.GOAL_BLOCKED)
        self.assertEqual(_evaluate(goal, [], "t", "u", True)[0], ae.GOAL_PASS)

    def test_element_presence_evidence(self):
        goal = {"objective": "x", "evidence": {"element_present": ["Availability"]}}
        self.assertEqual(_evaluate(goal, ["Availability"], "t", "u")[0], ae.GOAL_PASS)
        self.assertEqual(_evaluate(goal, ["Search"], "t", "u")[0], ae.GOAL_BLOCKED)

    def test_steps_min_prevents_early_pass(self):
        goal = {"objective": "x", "evidence": {"text_contains": ["ok"], "steps_min": 3}}
        self.assertEqual(_evaluate(goal, [], "ok", "u", steps=1)[0], ae.GOAL_BLOCKED)
        self.assertEqual(_evaluate(goal, [], "ok", "u", steps=3)[0], ae.GOAL_PASS)


# --------------------------------------------------------------------------
# Configuration compatibility
# --------------------------------------------------------------------------

class TestConfigCompatibility(unittest.TestCase):
    LEGACY = {
        "site_name": "X", "portal_url": "https://x.test",
        "ai_context": "goal", "credentials": {},
        "victory_conditions": {"text_matches": ["done"]},
        "form_autofill": [], "target_elements_query": "button",
    }

    def test_legacy_config_loads_without_new_keys(self):
        cfg = ae.load_config.__wrapped__ if hasattr(ae.load_config, "__wrapped__") else None
        # load_config reads a file; verify the parser helpers tolerate absence.
        sem = ae._parse_semantic_config(self.LEGACY)
        self.assertIsNone(sem["cart_selector"])
        self.assertIsNone(sem["form_field_selector"])
        goal = ae.TestGoal(self.LEGACY.get("test_goal"))
        self.assertFalse(goal.is_configured())

    def test_semantic_config_accepts_both_key_spellings(self):
        a = ae._parse_semantic_config({"semantic_signals": {"cart_selector": ".x"}})
        b = ae._parse_semantic_config({"semantic_signals": {"cart_count_selector": ".x"}})
        self.assertEqual(a["cart_selector"], b["cart_selector"])

    def test_malformed_semantic_config_is_ignored(self):
        for bad in (None, [], "nope", 5):
            out = ae._parse_semantic_config({"semantic_signals": bad})
            self.assertIsNone(out["cart_selector"])


class TestCostFloor(unittest.TestCase):
    """The LLM's #1 ranking must not erase accumulated loop evidence."""

    def test_floor_defaults_to_zero(self):
        mgr = ae.StateManager(max_search_depth=25)
        self.assertEqual(mgr.get_edge_cost_floor("A", "Open Menu"), 0)

    def test_floor_is_raised_and_never_lowered(self):
        mgr = ae.StateManager(max_search_depth=25)
        mgr.raise_edge_cost_floor("A", "Open Menu", 50)
        self.assertEqual(mgr.get_edge_cost_floor("A", "Open Menu"), 50)
        # A later, smaller penalty must not weaken an existing floor.
        mgr.raise_edge_cost_floor("A", "Open Menu", 5)
        self.assertEqual(mgr.get_edge_cost_floor("A", "Open Menu"), 50)
        # A larger one escalates it.
        mgr.raise_edge_cost_floor("A", "Open Menu", 120)
        self.assertEqual(mgr.get_edge_cost_floor("A", "Open Menu"), 120)

    def test_floor_is_scoped_per_node_and_action(self):
        mgr = ae.StateManager(max_search_depth=25)
        mgr.raise_edge_cost_floor("A", "Open Menu", 50)
        self.assertEqual(mgr.get_edge_cost_floor("B", "Open Menu"), 0)
        self.assertEqual(mgr.get_edge_cost_floor("A", "Checkout"), 0)

    def test_futile_action_raises_the_floor(self):
        """A click that changed nothing must not be re-favoured on revisit."""
        mgr = ae.StateManager(max_search_depth=25)
        mgr.raise_edge_cost_floor("nodeA", "Add to cart", ae.FUTILE_ACTION_PENALTY)
        self.assertGreaterEqual(
            mgr.get_edge_cost_floor("nodeA", "Add to cart"), 1,
            "the floor must at least prevent a cost of 1 being assigned",
        )

    def test_repeat_penalty_is_reflected_in_the_floor(self):
        """End-to-end: a toggling edge accrues a floor that blocks free re-ranking."""
        mgr = ae.StateManager(max_search_depth=25)
        for node in ("closed", "open"):
            mgr.snapshot_before_action(node_hash=node, url="u")

        mgr.record_repeat("closed", "Open Menu", "open")        # first, no penalty
        penalty = mgr.record_repeat("closed", "Open Menu", "closed")
        self.assertGreater(penalty, 0)
        mgr.raise_edge_cost_floor("closed", "Open Menu", penalty)
        self.assertGreater(mgr.get_edge_cost_floor("closed", "Open Menu"), 0)


class TestFileUrlResolution(unittest.TestCase):
    def test_http_urls_untouched(self):
        self.assertEqual(ae._resolve_file_url("https://a.test/x"), "https://a.test/x")
        self.assertEqual(ae._resolve_file_url("http://a.test/x"), "http://a.test/x")

    def test_relative_path_becomes_file_url(self):
        out = ae._resolve_file_url("fixtures/x/index.html")
        self.assertTrue(out.startswith("file://"))
        self.assertIn("fixtures/x/index.html", out.replace("\\", "/"))


if __name__ == "__main__":
    unittest.main(verbosity=2)