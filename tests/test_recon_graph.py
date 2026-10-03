"""Checkpoint 5.2 — the website graph.

Covers the acceptance gate for 5.2: repeated URLs with changed state, revisited
pages, duplicate links, and uncertain edges. Also pins the fact/inference
separation and the traversed/suggested distinction, because both are claims the
report makes to a reader.
"""

import unittest

import reconnaissance as r


class NodeIdentityTests(unittest.TestCase):
    """A page-state is identified by structure+state, never by URL alone."""

    def test_same_url_different_structure_is_two_nodes(self):
        from automation_engine import compute_full_node_id
        graph = r.ReconGraph("https://shop.test/")

        home_id, _ = compute_full_node_id("https://shop.test/", ["Shop"])
        cart_id, _ = compute_full_node_id("https://shop.test/",
                                          ["Shop", "Checkout"])
        graph.add_node(home_id, url="https://shop.test/", title="Shop")
        graph.add_node(cart_id, url="https://shop.test/", title="Shop")

        self.assertNotEqual(home_id, cart_id)
        self.assertEqual(len(graph.nodes), 2)
        self.assertEqual(graph.urls_with_multiple_states(),
                         ["https://shop.test/"])

    def test_node_at_url_is_ambiguous_when_url_has_many_states(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/", title="empty")
        graph.add_node("n2", url="https://shop.test/", title="with cart")

        # Two states at one URL genuinely are two pages. Returning one of them
        # would let a caller act on a state it never identified.
        self.assertIsNone(graph.node_at_url("https://shop.test/"))
        self.assertEqual(graph.states_for_url("https://shop.test/"),
                         ["n1", "n2"])

    def test_node_at_url_returns_unique_state(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/", title="only")
        self.assertEqual(graph.node_at_url("https://shop.test/"), "n1")


class RevisitTests(unittest.TestCase):
    """A revisited page updates its node rather than being ignored."""

    def test_revisit_increments_visit_count_and_merges_labels(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/", title="Shop",
                       interactive_labels=["Home"])
        graph.add_node("n1", url="https://shop.test/", title="Shop",
                       interactive_labels=["Cart"])

        node = graph.get("n1")
        self.assertEqual(node.visit_count, 2)
        self.assertEqual(len(graph.nodes), 1)
        self.assertIn("Home", node.interactive_labels)
        self.assertIn("Cart", node.interactive_labels)

    def test_revisit_keeps_original_creation_time(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/", timestamp=100.0)
        graph.add_node("n1", url="https://shop.test/", timestamp=200.0)
        node = graph.get("n1")
        self.assertEqual(node.first_seen, 100.0)
        self.assertEqual(node.last_seen, 200.0)

    def test_revisit_keeps_shallowest_depth(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/", depth=3)
        graph.add_node("n1", url="https://shop.test/", depth=1)
        self.assertEqual(graph.get("n1").depth, 1)


class EdgeTests(unittest.TestCase):
    """Edges record what happened and what the claim rests on."""

    def test_observed_link_is_not_traversed(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/")
        edge = graph.add_edge("n1", href="/about", label="About",
                              kind=r.EDGE_OBSERVED, target_url=
                              "https://shop.test/about")

        self.assertFalse(edge.is_traversed)
        self.assertEqual(edge.kind, r.EDGE_OBSERVED)
        # "Seen on a page" and "walked to" are different claims.
        self.assertNotEqual(edge.kind, r.EDGE_TRAVERSED)

    def test_marking_traversed_promotes_to_confirmed_fact(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/")
        edge = graph.add_edge("n1", href="/about", label="About",
                              kind=r.EDGE_OBSERVED)
        graph.mark_edge_traversed(edge, "n2", timestamp=123.0)

        self.assertTrue(edge.is_traversed)
        self.assertEqual(edge.target_id, "n2")
        self.assertEqual(edge.confidence, r.CONFIDENCE_CONFIRMED)
        self.assertEqual(edge.basis, r.FACT)
        self.assertEqual(edge.traversed_at, 123.0)

    def test_duplicate_links_fold_into_one_relationship(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/")
        # The same nav link rendered in a header and a footer is ONE
        # relationship with a count, not two that inflate the site map.
        first = graph.add_edge("n1", href="/about", label="About",
                               target_url="https://shop.test/about")
        second = graph.add_edge("n1", href="/about", label="About",
                                target_url="https://shop.test/about")

        self.assertIs(first, second)
        self.assertEqual(len(graph.edges), 1)
        self.assertEqual(second.attempt_count, 2)

    def test_traversed_view_wins_over_observed_on_duplicate(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/")
        first = graph.add_edge("n1", href="/about", label="About",
                               kind=r.EDGE_OBSERVED,
                               target_url="https://shop.test/about")
        second = graph.add_edge("n1", href="/about", label="About",
                                kind=r.EDGE_TRAVERSED, target_id="n2",
                                target_url="https://shop.test/about")

        self.assertIs(first, second)
        self.assertEqual(second.kind, r.EDGE_TRAVERSED)
        self.assertEqual(second.target_id, "n2")

    def test_inferred_edge_is_labelled_as_inference(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/")
        edge = graph.add_edge("n1", label="maybe cart", kind=r.EDGE_INFERRED,
                              basis=r.INFERENCE,
                              confidence=r.CONFIDENCE_UNCERTAIN)
        self.assertEqual(edge.basis, r.INFERENCE)
        self.assertEqual(edge.confidence, r.CONFIDENCE_UNCERTAIN)
        self.assertFalse(edge.is_traversed)

    def test_edges_from_and_to_filter_by_node(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/")
        graph.add_node("n2", url="https://shop.test/next")
        edge = graph.add_edge("n1", href="/next", label="Next",
                              target_url="https://shop.test/next")
        graph.mark_edge_traversed(edge, "n2")

        self.assertEqual(len(graph.edges_from("n1")), 1)
        self.assertEqual(len(graph.edges_to("n2")), 1)
        self.assertEqual(graph.edges_from("n2"), [])


class FactVersusInferenceTests(unittest.TestCase):
    """Observed facts and inferred relationships are kept apart."""

    def test_inferred_page_role_is_stored_separately_from_observations(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/login", title="Sign in")
        graph.set_inferred_role("n1", "authentication")

        node = graph.get("n1").to_dict()
        self.assertEqual(node["inferred_role"], "authentication")
        # Labeled as inference wherever it is reported.
        self.assertEqual(node["inferred_role_basis"], r.INFERENCE)
        # Observed labels are untouched by the inference.
        self.assertEqual(node["interactive_labels"], [])

    def test_infer_page_role_from_password_form(self):
        graph = r.ReconGraph("https://shop.test/")
        node = graph.add_node("n1", url="https://shop.test/login",
                              title="Sign in")
        forms = [{"fields": [{"name": "password", "sensitive": True}],
                  "has_password": True}]
        self.assertEqual(r.infer_page_role(node, forms), "authentication")

    def test_infer_page_role_returns_empty_without_evidence(self):
        graph = r.ReconGraph("https://shop.test/")
        node = graph.add_node("n1", url="https://shop.test/x", title="")
        # No guess is better than a guess with no basis.
        self.assertEqual(r.infer_page_role(node, []), "")

    def test_infer_page_role_from_commerce_labels(self):
        graph = r.ReconGraph("https://shop.test/")
        node = graph.add_node("n1", url="https://shop.test/",
                              interactive_labels=["Add to cart", "Checkout"])
        self.assertEqual(r.infer_page_role(node, []), "commerce")


class UnexploredTests(unittest.TestCase):
    """Areas not explored are recorded with the reason they were skipped."""

    def test_unexplored_records_reason(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_unexplored("https://shop.test/checkout",
                             "consequential; not traversed", kind="consequential")
        self.assertEqual(len(graph.unexplored), 1)
        self.assertIn("consequential", graph.unexplored[0]["reason"])

    def test_graph_export_counts_edges_by_kind(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/")
        graph.add_edge("n1", href="/a", label="A", kind=r.EDGE_OBSERVED,
                       target_url="https://shop.test/a")
        graph.add_edge("n1", href="/b", label="B", kind=r.EDGE_BLOCKED,
                       target_url="https://shop.test/b", reason="consequential")
        stats = graph.to_dict()["stats"]
        self.assertEqual(stats["observed_only_edges"], 1)
        self.assertEqual(stats["blocked_edges"], 1)
        self.assertEqual(stats["traversed_edges"], 0)

    def test_urls_with_multiple_states_detects_dynamic_apps(self):
        graph = r.ReconGraph("https://shop.test/cart")
        graph.add_node("a", url="https://shop.test/cart")
        graph.add_node("b", url="https://shop.test/cart")
        graph.add_node("c", url="https://shop.test/other")
        self.assertEqual(graph.urls_with_multiple_states(),
                         ["https://shop.test/cart"])


if __name__ == "__main__":
    unittest.main()