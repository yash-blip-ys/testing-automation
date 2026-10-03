"""Checkpoint 1.3 — Configuration and CLI integrity.

Pins the contract between a run's configuration, the command line, and the
objective that actually drives execution and verification:

  * CLI values take precedence over config, in a predictable order
  * a missing value for a known flag is an actionable error, not silence
  * an unrecognised argument is reported, not silently swallowed
  * malformed values degrade with a visible warning, never a bare traceback
  * absent optional settings stay absent and never fabricate user intent
  * secrets are never logged

No browser, no model. Run with:
    .\\venv311\\Scripts\\python.exe -m unittest tests.test_contract_config -v
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae


def _capture(fn, *args, **kwargs):
    """Run fn with stdout captured; return (result, printed_output)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


class TestCliPrecedence(unittest.TestCase):
    """CLI wins over config; each flag is independent."""

    def test_defaults(self):
        opts, _ = _capture(ae._parse_cli, [])
        self.assertEqual(opts["config"], "sites_config.json")
        self.assertIsNone(opts["url"])
        self.assertIsNone(opts["goal"])
        self.assertIsNone(opts["max_steps"])

    def test_all_flags_parse(self):
        opts, _ = _capture(ae._parse_cli, [
            "--config", "c.json", "--url", "https://x.test",
            "--goal", "do the thing", "--max-steps", "12",
        ])
        self.assertEqual(opts["config"], "c.json")
        self.assertEqual(opts["url"], "https://x.test")
        self.assertEqual(opts["goal"], "do the thing")
        self.assertEqual(opts["max_steps"], "12")

    def test_help_returns_no_run(self):
        opts, out = _capture(ae._parse_cli, ["--help"])
        self.assertIsNone(opts)
        self.assertIn("--config", out)

    def test_flag_without_value_is_an_error_not_silence(self):
        for flag in ("--config", "--url", "--goal", "--max-steps"):
            opts, out = _capture(ae._parse_cli, [flag])
            self.assertIsNone(opts, f"{flag} with no value must not start a run")
            self.assertIn("requires a value", out)

    def test_unknown_argument_is_reported(self):
        opts, out = _capture(ae._parse_cli, ["--nonsense"])
        self.assertIsNotNone(opts, "an unknown flag must not abort the run")
        self.assertIn("Ignoring unrecognised", out)
        self.assertIn("--nonsense", out)

    def test_bare_positional_is_reported(self):
        opts, out = _capture(ae._parse_cli, ["stray"])
        self.assertIn("stray", out)

    def test_unknown_argument_does_not_swallow_known_ones(self):
        opts, _ = _capture(ae._parse_cli, ["--nonsense", "--goal", "real goal"])
        self.assertEqual(opts["goal"], "real goal",
                         "an unknown flag must not disturb known ones")

    def test_goal_with_spaces_is_one_value(self):
        opts, _ = _capture(ae._parse_cli, ["--goal", "add a hat, then pay"])
        self.assertEqual(opts["goal"], "add a hat, then pay")


class TestConfigOverridePrecedence(unittest.TestCase):
    """Ordering between file config and CLI, established explicitly."""

    def _load(self, cfg, **kwargs):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            json.dump(cfg, fh)
            path = fh.name
        try:
            result, out = _capture(ae.load_config, path, **kwargs)
            return result, out
        finally:
            os.unlink(path)

    def test_url_override_wins(self):
        cfg, _ = self._load({"portal_url": "https://from-file.test"},
                            url_override="https://from-cli.test")
        self.assertEqual(cfg["portal_url"], "https://from-cli.test")

    def test_url_override_supplies_site_name_when_absent(self):
        cfg, _ = self._load({}, url_override="https://from-cli.test")
        self.assertEqual(cfg["site_name"], "https://from-cli.test")

    def test_url_override_does_not_clobber_a_real_site_name(self):
        cfg, _ = self._load({"site_name": "My Site"},
                            url_override="https://from-cli.test")
        self.assertEqual(cfg["site_name"], "My Site")

    def test_goal_override_sets_context_and_objective(self):
        cfg, _ = self._load({"portal_url": "https://x.test"},
                            goal_override="Find the return policy")
        self.assertEqual(cfg["ai_context"], "Find the return policy")
        self.assertEqual(cfg["test_goal"]["objective"], "Find the return policy")

    def test_goal_override_never_overwrites_an_authored_objective(self):
        cfg, _ = self._load(
            {"portal_url": "https://x.test",
             "test_goal": {"objective": "Authored objective"}},
            goal_override="different goal")
        self.assertEqual(cfg["test_goal"]["objective"], "Authored objective")
        self.assertEqual(cfg["ai_context"], "different goal",
                         "the prompt context still follows the CLI goal")

    def test_max_steps_override_wins_over_config(self):
        cfg, _ = self._load(
            {"portal_url": "https://x.test", "test_goal": {"max_steps": 5}},
            max_steps_override="9")
        self.assertEqual(cfg["test_goal"]["max_steps"], 9)

    def test_max_steps_override_creates_test_goal_if_absent(self):
        cfg, _ = self._load({"portal_url": "https://x.test"},
                            max_steps_override="7")
        self.assertEqual(cfg["test_goal"]["max_steps"], 7)

    def test_max_steps_config_preserved_without_override(self):
        cfg, _ = self._load({"portal_url": "https://x.test",
                             "test_goal": {"max_steps": 3}})
        self.assertEqual(cfg["test_goal"]["max_steps"], 3)

    def test_malformed_max_steps_override_warns_and_defaults(self):
        cfg, out = self._load({"portal_url": "https://x.test"},
                              max_steps_override="not-a-number")
        self.assertEqual(cfg["test_goal"]["max_steps"], ae.DEFAULT_MAX_STEPS)
        self.assertIn("--max-steps", out)

    def test_zero_or_negative_max_steps_override_warns(self):
        for bad in ("0", "-4"):
            cfg, out = self._load({"portal_url": "https://x.test"},
                                  max_steps_override=bad)
            self.assertEqual(cfg["test_goal"]["max_steps"], ae.DEFAULT_MAX_STEPS)
            self.assertIn("at least 1", out)

    def test_goal_and_max_steps_combine(self):
        cfg, _ = self._load({"portal_url": "https://x.test"},
                            goal_override="do it", max_steps_override="11")
        self.assertEqual(cfg["test_goal"]["objective"], "do it")
        self.assertEqual(cfg["test_goal"]["max_steps"], 11)


class TestInvalidConfigurationIsActionable(unittest.TestCase):
    def test_missing_file_names_the_file(self):
        result, out = _capture(ae.load_config, "definitely_missing_config.json")
        self.assertIsNone(result)
        self.assertIn("definitely_missing_config.json", out)

    def test_malformed_json_is_reported(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            fh.write("{ not valid json ")
            path = fh.name
        try:
            result, out = _capture(ae.load_config, path)
        finally:
            os.unlink(path)
        self.assertIsNone(result)
        self.assertIn("malformed", out)

    def test_malformed_max_steps_never_raises(self):
        for bad in ("abc", "twelve", [], {}, object()):
            goal, out = _capture(ae.TestGoal, {"objective": "x", "max_steps": bad})
            self.assertEqual(goal.max_steps, ae.DEFAULT_MAX_STEPS,
                             f"malformed budget {bad!r} must not crash the run")
        self.assertIn("must be a whole number", out)

    def test_valid_max_steps_is_untouched(self):
        self.assertEqual(ae.TestGoal({"objective": "x", "max_steps": 30}).max_steps, 30)
        self.assertEqual(ae.TestGoal({"objective": "x", "max_steps": "30"}).max_steps, 30)

    def test_config_without_portal_url_is_rejected_by_the_runner(self):
        """The runner is what enforces this; load_config must not invent a URL."""
        cfg, _ = _capture(ae.load_config, _write({"site_name": "x"}))
        self.assertNotIn("portal_url", cfg)

    def test_non_dict_config_is_tolerated(self):
        for bad in (None, "text", 42, []):
            goal = ae.TestGoal(bad)
            self.assertEqual(goal.objective, "")
            self.assertEqual(goal.max_steps, ae.DEFAULT_MAX_STEPS)


def _write(obj):
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8")
    json.dump(obj, fh)
    fh.close()
    return fh.name


class TestMissingOptionalSettingsStayAbsent(unittest.TestCase):
    """Absent optional settings must never fabricate user intent."""

    def test_semantic_signals_absent(self):
        for cfg in ({}, {"semantic_signals": {}}, {"semantic_signals": None}):
            parsed = ae._parse_semantic_config(cfg)
            self.assertIsNone(parsed["cart_selector"])
            self.assertIsNone(parsed["form_field_selector"])

    def test_explicit_null_selectors_stay_none(self):
        parsed = ae._parse_semantic_config({
            "semantic_signals": {"cart_count_selector": None,
                                 "form_values_selector": None}})
        self.assertIsNone(parsed["cart_selector"])
        self.assertIsNone(parsed["form_field_selector"])

    def test_configured_selectors_are_used(self):
        parsed = ae._parse_semantic_config({
            "semantic_signals": {"cart_count_selector": ".badge"}})
        self.assertEqual(parsed["cart_selector"], ".badge")

    def test_no_credentials_means_no_auth_attempt_is_configured(self):
        cfg, _ = _capture(ae.load_config, _write({"portal_url": "https://x.test"}))
        self.assertEqual(cfg.get("credentials", {}), {},
                         "credentials must stay absent, never invented")

    def test_goal_without_evidence_has_no_success_criteria(self):
        """A missing evidence block must not become a default success rule."""
        cfg, _ = _capture(ae.load_config, _write({"portal_url": "https://x.test"}),
                          goal_override="do something")
        self.assertEqual(cfg["test_goal"]["evidence"], {})
        goal = ae.TestGoal(cfg["test_goal"])
        status, _ = goal.evaluate(
            page_state={"available_elements": ["A"]},
            page_text="A", url="https://x.test/", structural_changed=True)
        self.assertNotEqual(status, ae.GOAL_PASS,
                            "no evidence block must never yield PASS")


class TestObjectiveOnlyOperation(unittest.TestCase):
    """The objective alone must be enough to drive a run."""

    def test_goal_only_config_produces_a_runnable_goal(self):
        cfg, _ = _capture(ae.load_config, _write({"portal_url": "https://x.test"}),
                          goal_override="Add a hat to the cart")
        goal = ae.TestGoal(cfg["test_goal"])
        self.assertTrue(goal.is_configured())
        self.assertEqual(goal.steps, [])
        rows = goal.remaining_work(
            page_state={"available_elements": ["Add to cart"]},
            page_text="Products", url="https://x.test/", structural_changed=True)
        self.assertTrue(rows, "objective-only must still yield sub-goals")

    def test_victory_conditions_are_not_synthesised(self):
        cfg, _ = _capture(ae.load_config, _write({"portal_url": "https://x.test"}),
                          goal_override="do something")
        self.assertEqual(cfg.get("victory_conditions", {}), {},
                         "victory conditions are user-provided only")


class TestSecretsAreNotLogged(unittest.TestCase):
    """Credentials must never reach stdout, state, or reports."""

    SECRET = "sup3rs3cret-do-not-log"

    def test_engine_never_prints_credentials(self):
        import inspect
        src = inspect.getsource(ae)
        for pattern in ('print(f"[Auth] {_creds',
                        'print(config', 'print(creds',
                        f'print("{self.SECRET}"'):
            self.assertNotIn(pattern, src,
                             f"engine must not log credentials via {pattern!r}")

    def test_auth_path_prints_presence_only(self):
        import inspect
        src = inspect.getsource(ae.run_pathfinder_agent)
        self.assertIn("Password field detected", src)
        # The password value itself is only ever passed to fill().
        self.assertIn('_pass_loc.fill(_creds["password"])', src)

    def test_report_does_not_receive_the_config(self):
        import inspect
        src = inspect.getsource(ae.run_pathfinder_agent)
        block = src[src.index("generate_scan_report("):][:600]
        self.assertNotIn("credentials", block)
        self.assertNotIn("config[", block)

    def test_sensitive_fields_are_hashed_not_stored(self):
        digest = ae.hash_form_value("hunter2")
        self.assertNotIn("hunter2", digest)
        self.assertTrue(ae._looks_like_digest(digest))

    def test_sensitive_field_markers_are_honoured(self):
        for name in ("password", "api_key", "card_number", "otp"):
            self.assertTrue(ae.is_sensitive_field(name), f"{name} must be sensitive")
        self.assertFalse(ae.is_sensitive_field("username"))

    def test_sensitive_field_values_never_reach_the_signature(self):
        """A sensitive field contributes presence only, never its value."""
        signals = {"cart_count": None,
                   "form_values": {"#name": "Ada Lovelace",
                                   "#password": ae.SENSITIVE_PRESENT}}
        with contextlib.redirect_stdout(io.StringIO()):
            sig = ae.compute_semantic_signature("https://x.test/", [], signals)
        blob = repr(sig)
        self.assertNotIn("Ada Lovelace", blob,
                         "a form value must never appear in plaintext")
        self.assertIn(ae.SENSITIVE_PRESENT, blob)
        self.assertNotIn("hunter2", blob)

    def test_non_sensitive_value_is_hashed_even_if_caller_passes_plaintext(self):
        """Defence in depth: the signature hashes whatever it is handed."""
        signals = {"cart_count": None, "form_values": {"#name": "Ada Lovelace"}}
        with contextlib.redirect_stdout(io.StringIO()):
            sig = ae.compute_semantic_signature("https://x.test/", [], signals)
        self.assertNotIn("Ada Lovelace", repr(sig))
        self.assertTrue(ae._looks_like_digest(sig["form_values"]["#name"]))

    def test_non_sensitive_values_are_one_way_hashed(self):
        digest = ae.hash_form_value("hunter2")
        self.assertNotIn("hunter2", digest)
        self.assertTrue(ae._looks_like_digest(digest))


if __name__ == "__main__":
    unittest.main(verbosity=2)