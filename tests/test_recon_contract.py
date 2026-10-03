"""Checkpoints 5.1 and 5.5 — the reconnaissance contract and its report.

5.1's acceptance gate is that reconnaissance has explicit budgets and explicit
stopping conditions; 5.5's is that the report describes what was seen without
claiming coverage it does not have. Both are asserted here.
"""

import unittest

import reconnaissance as r


class BudgetTests(unittest.TestCase):
    """Every limit is explicit, independent, and checked before the work."""

    def test_defaults_are_positive_and_explicit(self):
        budget = r.ReconBudget()
        self.assertEqual(budget.max_pages, r.RECON_MAX_PAGES)
        self.assertEqual(budget.max_transitions, r.RECON_MAX_TRANSITIONS)
        self.assertEqual(budget.max_depth, r.RECON_MAX_DEPTH)
        self.assertEqual(budget.max_seconds, r.RECON_MAX_SECONDS)
        self.assertEqual(budget.max_model_calls, r.RECON_MAX_MODEL_CALLS)
        self.assertEqual(budget.max_stored_chars, r.RECON_MAX_STORED_CHARS)
        for field in ("max_pages", "max_transitions", "max_depth",
                      "max_seconds", "max_model_calls", "max_stored_chars"):
            self.assertGreaterEqual(getattr(budget, field), 1)

    def test_custom_budgets_are_honoured(self):
        budget = r.ReconBudget(max_pages=3, max_depth=2)
        self.assertEqual(budget.max_pages, 3)
        self.assertEqual(budget.max_depth, 2)

    def test_invalid_budget_falls_back_and_warns(self):
        budget = r.ReconBudget(max_pages="not-a-number")
        self.assertEqual(budget.max_pages, r.RECON_MAX_PAGES)
        budget = r.ReconBudget(max_pages=0)
        self.assertEqual(budget.max_pages, r.RECON_MAX_PAGES)

    def test_can_visit_page_reflects_budget(self):
        budget = r.ReconBudget(max_pages=2)
        self.assertTrue(budget.can_visit_page())
        budget.record_page()
        self.assertTrue(budget.can_visit_page())
        budget.record_page()
        self.assertFalse(budget.can_visit_page())

    def test_transition_budget_is_independent_of_page_budget(self):
        budget = r.ReconBudget(max_pages=100, max_transitions=1)
        budget.record_page()
        budget.record_page()
        self.assertTrue(budget.can_transition())
        budget.record_transition()
        self.assertFalse(budget.can_transition())
        # The page budget was not consulted to answer this.
        self.assertTrue(budget.can_visit_page())

    def test_depth_check(self):
        budget = r.ReconBudget(max_depth=2)
        self.assertTrue(budget.can_go_deeper(1))
        self.assertFalse(budget.can_go_deeper(2))

    def test_model_call_budget_is_independent(self):
        budget = r.ReconBudget(max_model_calls=1)
        self.assertTrue(budget.can_call_model())
        budget.record_model_call()
        self.assertFalse(budget.can_call_model())

    def test_stored_content_budget(self):
        budget = r.ReconBudget(max_stored_chars=10)
        self.assertTrue(budget.can_store(6))
        budget.record_stored(6)
        self.assertTrue(budget.can_store(4))
        self.assertFalse(budget.can_store(5))

    def test_limit_reached_names_the_exhausted_limit(self):
        budget = r.ReconBudget(max_pages=1)
        budget.record_page()
        self.assertEqual(budget.limit_reached()[0], "pages")

    def test_limit_reached_is_none_while_live(self):
        self.assertIsNone(r.ReconBudget().limit_reached())

    def test_zero_length_store_always_allowed(self):
        budget = r.ReconBudget(max_stored_chars=1)
        budget.record_stored(1)
        self.assertTrue(budget.can_store(0))

    def test_budget_to_dict_exposes_all_counters(self):
        snapshot = r.ReconBudget().to_dict()
        for key in ("max_pages", "max_transitions", "max_depth",
                    "max_seconds", "max_model_calls", "max_stored_chars",
                    "pages_visited", "transitions", "model_calls",
                    "stored_chars"):
            self.assertIn(key, snapshot)


class StopReasonTests(unittest.TestCase):
    """There is a named reason for every way a run can end."""

    def test_incomplete_reasons_exclude_exhausted_frontier(self):
        # Finishing the frontier is the only reason that does NOT mean the
        # report must disclaim coverage.
        self.assertNotIn(r.RECON_DONE_EXHAUSTED, r.RECON_INCOMPLETE_REASONS)
        self.assertIn(r.RECON_DONE_PAGES, r.RECON_INCOMPLETE_REASONS)
        self.assertIn(r.RECON_DONE_AUTH, r.RECON_INCOMPLETE_REASONS)
        self.assertIn(r.RECON_DONE_ACCESS_CONTROL,
                      r.RECON_INCOMPLETE_REASONS)
        self.assertIn(r.RECON_DONE_TIME, r.RECON_INCOMPLETE_REASONS)


class LinkSafetyTests(unittest.TestCase):
    """Reconnaissance traverses links and nothing else."""

    def test_consequential_links_are_not_traversed(self):
        for label in ("Delete account", "Checkout", "Place order",
                      "Subscribe", "Sign out", "Transfer funds"):
            flagged, reason = r.is_consequential_link(label)
            self.assertTrue(flagged, f"{label!r} should be flagged")
            self.assertTrue(reason)

    def test_ordinary_links_are_traversable(self):
        for label in ("About us", "Products", "Documentation", "Contact"):
            flagged, reason = r.is_consequential_link(label)
            self.assertFalse(flagged, f"{label!r} should be traversable")
            self.assertEqual(reason, "")

    def test_unreadable_link_defaults_to_not_traversable(self):
        # No readable text is not evidence of benign; the conservative default
        # is what stops an unclassifiable control becoming a silent bypass.
        flagged, reason = r.is_consequential_link("")
        self.assertTrue(flagged)
        self.assertTrue(reason)

    def test_non_page_schemes_are_not_traversable(self):
        for href in ("mailto:a@b.com", "tel:+123", "javascript:void(0)",
                     "#anchor"):
            self.assertFalse(r.is_traversable_link(href), href)

    def test_real_page_links_are_traversable(self):
        for href in ("/about", "https://x.test/a", "a.html", "?q=1"):
            self.assertTrue(r.is_traversable_link(href), href)

    def test_same_origin_boundary(self):
        self.assertTrue(r.is_same_origin("https://a.test/x",
                                         "https://a.test/y"))
        self.assertFalse(r.is_same_origin("https://b.test", "https://a.test"))
        self.assertFalse(r.is_same_origin("http://a.test", "https://a.test"))

    def test_normalize_drops_fragment_but_keeps_query(self):
        self.assertEqual(r.normalize_url("https://a.test/x#frag"),
                         "https://a.test/x")
        self.assertEqual(r.normalize_url("https://a.test/x?q=1"),
                         "https://a.test/x?q=1")

    def test_normalize_resolves_relative_against_base(self):
        self.assertEqual(r.normalize_url("/about", "https://a.test/home"),
                         "https://a.test/about")

    def test_local_files_in_one_directory_share_a_scope(self):
        # Every file:// URL has an EMPTY authority, so comparing by authority
        # would make every local file look off-site and stop a local fixture
        # from being crawlable at all.
        base = "file:///C:/site/index.html"
        self.assertTrue(r.is_same_origin("file:///C:/site/about.html", base))
        self.assertTrue(r.is_same_origin("file:///C:/site/sub/deep.html", base))

    def test_local_files_in_different_directories_do_not_share_a_scope(self):
        self.assertFalse(r.is_same_origin(
            "file:///C:/other/about.html",
            "file:///C:/site/index.html"))

    def test_local_file_pages_share_one_memory_bucket(self):
        # Same directory => one site key, so the pages of one local site share
        # memory and two separate local sites cannot.
        key_a = r.site_key_for("file:///C:/site/index.html")
        key_b = r.site_key_for("file:///C:/site/about.html")
        key_c = r.site_key_for("file:///C:/other/index.html")
        self.assertEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_c)

    def test_encoded_and_literal_urls_are_one_site(self):
        # A browser reports a space as %20; a path typed on the command line
        # keeps it literal. Those are the same directory, so they must be one
        # memory bucket and one page identity.
        encoded = "file:///C:/Users/Some%20One/site/index.html"
        literal = "file:///C:/Users/Some One/site/index.html"
        self.assertEqual(r.site_key_for(encoded), r.site_key_for(literal))
        self.assertEqual(r.normalize_url(encoded), r.normalize_url(literal))
        self.assertTrue(r.is_same_origin(encoded, literal))

    def test_http_pages_share_one_memory_bucket_per_host(self):
        self.assertEqual(r.site_key_for("https://a.test/x/y"),
                         r.site_key_for("https://a.test/z"))
        self.assertNotEqual(r.site_key_for("https://a.test/x"),
                            r.site_key_for("https://b.test/x"))


class UrlDisplayTests(unittest.TestCase):
    """Long local URLs stay readable in the report."""

    def test_long_local_url_shows_its_tail(self):
        shown = r._display_url(
            "file:///C:/Users/someone/Desktop/a/fixture/index.html")
        self.assertIn("fixture/index.html", shown)
        self.assertLess(len(shown), 64)

    def test_short_url_is_unchanged(self):
        self.assertEqual(r._display_url("https://a.test/x"), "https://a.test/x")

    def test_empty_url_renders_as_dash(self):
        self.assertEqual(r._display_url(""), "—")


class BarrierDetectionTests(unittest.TestCase):
    """Reconnaissance stops at auth, consent, access control, commitment."""

    def _obs(self, records, url="https://a.test/x", title="Page"):
        return r._observation_from({"records": records}, url, title)

    def test_password_field_is_an_auth_barrier(self):
        obs = self._obs([{"name": "password", "type": "password"}])
        barrier = r.detect_recon_barrier(obs, records=obs.discovery["records"])
        self.assertIsNotNone(barrier)
        self.assertEqual(barrier.kind, r.BARRIER_AUTH)

    def test_consent_control_is_a_consent_barrier(self):
        records = [{"name": "Accept all cookies", "role": "button"}]
        obs = self._obs(records)
        barrier = r.detect_recon_barrier(obs, records=records)
        self.assertEqual(barrier.kind, r.BARRIER_CONSENT)

    def test_access_control_host_is_detected(self):
        obs = self._obs([], url="https://challenges.cloudflare.com/x")
        barrier = r.detect_recon_barrier(obs)
        self.assertEqual(barrier.kind, r.BARRIER_ACCESS_CONTROL)

    def test_access_control_marker_in_title(self):
        obs = self._obs([], title="Just a moment... Verify you are human")
        barrier = r.detect_recon_barrier(obs)
        self.assertEqual(barrier.kind, r.BARRIER_ACCESS_CONTROL)

    def test_ordinary_page_has_no_barrier(self):
        obs = self._obs([{"name": "Products", "role": "link"},
                         {"name": "About", "role": "link"}])
        self.assertIsNone(r.detect_recon_barrier(obs, records=obs.discovery["records"]))

    def test_auth_checked_before_consent(self):
        # A login page often also has cookie buttons; reporting "consent" there
        # would understate the wall.
        records = [{"name": "password", "type": "password"},
                   {"name": "Accept cookies", "role": "button"}]
        obs = self._obs(records)
        barrier = r.detect_recon_barrier(obs, records=records)
        self.assertEqual(barrier.kind, r.BARRIER_AUTH)

    def test_barrier_carries_url_and_reason(self):
        obs = self._obs([{"name": "password", "type": "password"}],
                        url="https://a.test/login")
        barrier = r.detect_recon_barrier(obs, records=obs.discovery["records"])
        self.assertEqual(barrier.url, "https://a.test/login")
        self.assertTrue(barrier.reason)


class ReportTests(unittest.TestCase):
    """The report describes what was seen without overstating coverage."""

    def _graph(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/", title="Shop",
                       interactive_labels=["Home"])
        graph.add_node("n2", url="https://shop.test/about", title="About")
        edge = graph.add_edge("n1", href="/about", label="About",
                              target_url="https://shop.test/about")
        graph.mark_edge_traversed(edge, "n2")
        graph.set_inferred_role("n1", "commerce")
        return graph

    def test_report_lists_pages_and_relationships(self):
        report = r.build_recon_report(self._graph(), r.ReconBudget(),
                                      r.RECON_DONE_EXHAUSTED,
                                      start_url="https://shop.test/")
        self.assertIn("https://shop.test/", report)
        self.assertIn("About", report)
        self.assertIn("Pages and states visited", report)

    def test_report_marks_inferred_roles_as_inferences(self):
        report = r.build_recon_report(self._graph(), r.ReconBudget(),
                                      r.RECON_DONE_EXHAUSTED)
        self.assertIn("*(inferred)*", report)

    def test_report_disclaims_coverage_when_budget_stopped(self):
        report = r.build_recon_report(self._graph(), r.ReconBudget(),
                                      r.RECON_DONE_PAGES,
                                      stop_detail="page budget reached")
        self.assertIn("NOT an exhaustive map", report)
        self.assertIn("page_budget_reached", report)

    def test_report_disclaims_coverage_at_a_barrier(self):
        report = r.build_recon_report(self._graph(), r.ReconBudget(),
                                      r.RECON_DONE_AUTH)
        self.assertIn("NOT an exhaustive map", report)

    def test_report_states_what_it_deliberately_did_not_do(self):
        report = r.build_recon_report(self._graph(), r.ReconBudget(),
                                      r.RECON_DONE_EXHAUSTED)
        # The read-only posture is stated, so a reader cannot mistake a map for
        # an execution.
        self.assertIn("does not submit forms", report)

    def test_report_lists_unexplored_areas_with_reasons(self):
        graph = self._graph()
        graph.add_unexplored("https://shop.test/checkout",
                             "consequential", kind="consequential")
        report = r.build_recon_report(graph, r.ReconBudget(),
                                      r.RECON_DONE_EXHAUSTED)
        self.assertIn("not walked", report)
        self.assertIn("checkout", report)

    def test_report_includes_budget_and_limits(self):
        report = r.build_recon_report(self._graph(), r.ReconBudget(),
                                      r.RECON_DONE_EXHAUSTED)
        self.assertIn("Budget:", report)

    def test_report_records_barriers(self):
        report = r.build_recon_report(
            self._graph(), r.ReconBudget(), r.RECON_DONE_AUTH,
            barriers=[r.ReconBarrier(r.BARRIER_AUTH, "sign-in required")])
        self.assertIn("sign-in required", report)


if __name__ == "__main__":
    unittest.main()