"""Checkpoint 5.3 — persistent website memory.

Covers the acceptance gate directly: persistence, retrieval, freshness,
isolation, and deletion. Also pins the two properties that make the rest of the
design honest — memory refuses to store secrets, and memory is scoped so one
site's facts can never be read back under another site's name.
"""

import json
import os
import shutil
import tempfile
import time
import unittest

import reconnaissance as r


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reconmem_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def memory(self, url="https://shop.test/", **kwargs):
        kwargs.setdefault("directory", self.tmp)
        return r.SiteMemory(url, **kwargs)


class SecretRefusalTests(MemoryTestCase):
    """Memory must never persist credentials, tokens, or sensitive values."""

    def test_refuses_password_key(self):
        mem = self.memory()
        record = mem.remember("password", "hunter2", kind=r.MEM_PAGE_SUMMARY)
        self.assertIsNone(record)
        self.assertEqual(mem.rejected_secrets, 1)

    def test_refuses_secret_shaped_value_under_innocent_key(self):
        mem = self.memory()
        # A token pasted under "note" is still a token. Value-side matching is
        # what catches the dishonest case.
        record = mem.remember("note", "eyJhbGciOiJIUzI1NiJ9.abcdefgh",
                              kind=r.MEM_PAGE_SUMMARY)
        self.assertIsNone(record)

    def test_refuses_session_cookie_key(self):
        mem = self.memory()
        self.assertIsNone(mem.remember("session_cookie", "abc",
                                       kind=r.MEM_OUTCOME))

    def test_allows_ordinary_page_facts(self):
        mem = self.memory()
        record = mem.remember("n1", {"title": "Shop"},
                              kind=r.MEM_PAGE_SUMMARY)
        self.assertIsNotNone(record)

    def test_redacts_nested_secret_inside_allowed_record(self):
        mem = self.memory()
        # Defence in depth: even if a caller hands memory a structure that
        # contains a secret, the secret does not reach disk.
        mem.remember("n1", {"title": "Shop", "password": "hunter2"},
                     kind=r.MEM_PAGE_SUMMARY)
        saved = mem.save()
        with open(saved, "r", encoding="utf-8") as handle:
            written = json.load(handle)
        blob = json.dumps(written)
        self.assertNotIn("hunter2", blob)

    def test_looks_secret_is_two_sided(self):
        self.assertTrue(r.looks_secret("password", "anything"))
        self.assertTrue(r.looks_secret("note", "1234567890123456"))
        self.assertFalse(r.looks_secret("title", "Home"))


class PersistenceTests(MemoryTestCase):
    """Memory survives a save/load round trip, and only for the right site."""

    def test_save_and_reload_round_trip(self):
        mem = self.memory()
        mem.remember("n1", {"title": "Shop"}, kind=r.MEM_PAGE_SUMMARY)
        path = mem.save()
        self.assertTrue(os.path.exists(path))

        reloaded = self.memory().load()
        found = reloaded.recall(kind=r.MEM_PAGE_SUMMARY, key="n1")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0].value, {"title": "Shop"})

    def test_recall_filters_by_kind_and_key(self):
        mem = self.memory()
        mem.remember("n1", {"a": 1}, kind=r.MEM_PAGE_SUMMARY)
        mem.remember("n1->n2", {"b": 2}, kind=r.MEM_NAVIGATION)
        self.assertEqual(len(mem.recall(kind=r.MEM_NAVIGATION)), 1)
        self.assertEqual(len(mem.recall(key="n1")), 1)
        self.assertEqual(len(mem.recall()), 2)

    def test_disabled_memory_stores_nothing_and_saves_no_file(self):
        mem = self.memory(enabled=False)
        self.assertIsNone(mem.remember("n1", {"a": 1}))
        self.assertIsNone(mem.save())
        self.assertEqual(len(mem.recall()), 0)

    def test_unknown_record_kind_is_refused(self):
        mem = self.memory()
        self.assertIsNone(mem.remember("n1", {"a": 1}, kind="not_a_kind"))

    def test_corrupt_file_does_not_crash_and_yields_empty(self):
        mem = self.memory()
        path = mem.storage_path()
        os.makedirs(self.tmp, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not valid json")

        loaded = mem.load()
        # A memory store is an optimisation; an optimisation must never be a
        # reason a run crashes.
        self.assertEqual(len(loaded.recall()), 0)

    def test_schema_mismatch_is_ignored_rather_than_guessed(self):
        mem = self.memory()
        path = mem.storage_path()
        os.makedirs(self.tmp, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 999, "records": []}, handle)

        loaded = mem.load()
        self.assertEqual(len(loaded.recall()), 0)


class IsolationTests(MemoryTestCase):
    """Memory never silently crosses sites or namespaces."""

    def test_two_sites_use_different_files(self):
        a = self.memory("https://shop.test/")
        b = self.memory("https://bank.test/")
        self.assertNotEqual(a.storage_path(), b.storage_path())

    def test_recall_never_returns_another_sites_record(self):
        bank = self.memory("https://bank.test/")
        bank.remember("acct", {"balance": "hidden"}, kind=r.MEM_PAGE_SUMMARY)
        bank.save()

        # A different site opening the same directory must see nothing.
        shop = self.memory("https://shop.test/")
        shop.load()
        self.assertEqual(len(shop.recall()), 0)

    def test_foreign_record_dropped_on_load(self):
        shop = self.memory("https://shop.test/")
        os.makedirs(self.tmp, exist_ok=True)
        with open(shop.storage_path(), "w", encoding="utf-8") as handle:
            json.dump({
                "schema_version": r.MEMORY_SCHEMA_VERSION,
                "site_key": "site_somethingelse",
                "records": [r.MemoryRecord(
                    "n1", r.MEM_PAGE_SUMMARY, {"x": 1},
                    site_key="site_somethingelse").to_dict()],
            }, handle)

        loaded = shop.load()
        self.assertEqual(len(loaded.recall()), 0)
        self.assertEqual(loaded.dropped_foreign, 1)

    def test_unlabelled_record_is_treated_as_foreign(self):
        # An unlabelled fact is NOT adopted as ours. Guessing that it belongs
        # here is exactly how unrelated sites end up sharing memory.
        shop = self.memory("https://shop.test/")
        os.makedirs(self.tmp, exist_ok=True)
        with open(shop.storage_path(), "w", encoding="utf-8") as handle:
            json.dump({
                "schema_version": r.MEMORY_SCHEMA_VERSION,
                "records": [r.MemoryRecord(
                    "n1", r.MEM_PAGE_SUMMARY, {"x": 1}).to_dict()],
            }, handle)

        loaded = shop.load()
        self.assertEqual(len(loaded.recall()), 0)
        self.assertEqual(loaded.dropped_foreign, 1)

    def test_namespace_separates_users(self):
        alice = self.memory(namespace="alice")
        bob = self.memory(namespace="bob")
        self.assertNotEqual(alice.storage_path(), bob.storage_path())

        alice.remember("n1", {"owner": "alice"}, kind=r.MEM_PAGE_SUMMARY)
        alice.save()

        bob.load()
        self.assertEqual(len(bob.recall()), 0)


class FreshnessTests(MemoryTestCase):
    """Stale memory is flagged, not silently served as current."""

    def test_freshness_states_by_age(self):
        record = r.MemoryRecord("n1", r.MEM_PAGE_SUMMARY, {"a": 1},
                                created_at=0)
        self.assertEqual(record.freshness(now=0, ttl_seconds=100), r.MEM_FRESH)
        self.assertEqual(record.freshness(now=60, ttl_seconds=100), r.MEM_AGING)
        self.assertEqual(record.freshness(now=150, ttl_seconds=100),
                         r.MEM_EXPIRED)

    def test_expired_records_not_returned_by_default(self):
        mem = self.memory(ttl_seconds=1)
        mem.remember("n1", {"a": 1}, kind=r.MEM_PAGE_SUMMARY)
        time.sleep(1.2)
        self.assertEqual(len(mem.recall()), 0)
        self.assertEqual(len(mem.recall(include_expired=True)), 1)

    def test_revalidate_match_refreshes_record(self):
        mem = self.memory()
        record = mem.remember("n1", {"a": 1}, kind=r.MEM_PAGE_SUMMARY)
        outcome, checked = mem.revalidate(record, {"a": 1})
        self.assertEqual(outcome, r.REV_MATCH)

    def test_revalidate_change_downgrades_confidence(self):
        mem = self.memory()
        record = mem.remember("n1", {"a": 1},
                              kind=r.MEM_PAGE_SUMMARY,
                              confidence=r.CONFIDENCE_CONFIRMED)
        outcome, checked = mem.revalidate(record, {"a": 2})
        self.assertEqual(outcome, r.REV_CHANGED)
        # Contradiction is retained, not deleted — it stops the same stale
        # claim being re-learned as if it were new.
        self.assertEqual(checked.confidence, r.CONFIDENCE_UNKNOWN)

    def test_revalidate_expired_returns_stale(self):
        mem = self.memory(ttl_seconds=1)
        record = mem.remember("n1", {"a": 1}, kind=r.MEM_PAGE_SUMMARY)
        time.sleep(1.2)
        outcome, _ = mem.revalidate(record, {"a": 1})
        self.assertEqual(outcome, r.REV_STALE)

    def test_expire_removes_old_records(self):
        mem = self.memory(ttl_seconds=1)
        mem.remember("old", {"a": 1}, kind=r.MEM_PAGE_SUMMARY)
        time.sleep(1.2)
        self.assertEqual(mem.expire(), 1)
        self.assertEqual(len(mem.records), 0)

    def test_rewriting_refreshes_but_keeps_creation_time(self):
        mem = self.memory()
        record = mem.remember("n1", {"a": 1}, kind=r.MEM_PAGE_SUMMARY)
        first_seen = record.created_at
        time.sleep(0.01)
        again = mem.remember("n1", {"a": 2}, kind=r.MEM_PAGE_SUMMARY)
        self.assertIs(record, again)
        self.assertEqual(again.created_at, first_seen)


class InspectionAndDeletionTests(MemoryTestCase):
    """Memory can be inspected and cleared by an operator."""

    def test_inspect_summarises_contents(self):
        mem = self.memory()
        mem.remember("n1", {"a": 1}, kind=r.MEM_PAGE_SUMMARY)
        summary = mem.inspect()
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["record_count"], 1)
        self.assertEqual(summary["by_kind"][r.MEM_PAGE_SUMMARY], 1)

    def test_clear_empties_records_and_deletes_file(self):
        mem = self.memory()
        mem.remember("n1", {"a": 1}, kind=r.MEM_PAGE_SUMMARY)
        path = mem.save()
        self.assertTrue(os.path.exists(path))

        removed = mem.clear()
        self.assertEqual(removed, 1)
        self.assertEqual(len(mem.records), 0)
        self.assertFalse(os.path.exists(path))


class GraphIngestionTests(MemoryTestCase):
    """absorb_graph keeps durable structure, not one-off crawl detail."""

    def _graph(self):
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/", title="Shop",
                       interactive_labels=["Home"],
                       forms=[{"fields": [{"name": "q", "sensitive": False}],
                               "method": "get", "submit_count": 1,
                               "has_password": False}])
        graph.add_node("n2", url="https://shop.test/about", title="About")
        edge = graph.add_edge("n1", href="/about", label="About",
                              target_url="https://shop.test/about")
        graph.mark_edge_traversed(edge, "n2")
        return graph

    def test_absorb_stores_pages_forms_and_confirmed_navigation(self):
        mem = self.memory()
        stored = mem.absorb_graph(self._graph())
        self.assertGreater(stored, 0)
        self.assertEqual(len(mem.recall(kind=r.MEM_PAGE_SUMMARY)), 2)
        self.assertEqual(len(mem.recall(kind=r.MEM_FORM_SHAPE)), 1)
        self.assertEqual(len(mem.recall(kind=r.MEM_NAVIGATION)), 1)

    def test_absorb_skips_untraversed_edges(self):
        # An "observed" link is a suggestion, not a verified relationship; it
        # must not be stored as a confirmed navigation.
        graph = r.ReconGraph("https://shop.test/")
        graph.add_node("n1", url="https://shop.test/")
        graph.add_edge("n1", href="/maybe", label="Maybe", kind=r.EDGE_OBSERVED,
                       target_url="https://shop.test/maybe")
        mem = self.memory()
        mem.absorb_graph(graph)
        self.assertEqual(len(mem.recall(kind=r.MEM_NAVIGATION)), 0)

    def test_absorb_into_disabled_memory_stores_nothing(self):
        mem = self.memory(enabled=False)
        self.assertEqual(mem.absorb_graph(self._graph()), 0)


if __name__ == "__main__":
    unittest.main()