"""Reconnaissance CLI wiring.

Pins the property that matters most for Checkpoint 5's completion gate:
reconnaissance is a SEPARATE mode, and adding it did not change how an ordinary
task invocation is parsed or dispatched.
"""

import contextlib
import io
import unittest

import automation_engine as ae


def _capture(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = fn(*args)
    return result, out.getvalue()


class ReconFlagTests(unittest.TestCase):
    def test_recon_flag_is_off_by_default(self):
        opts, _ = _capture(ae._parse_cli, [])
        # The task path is unchanged when --recon is absent.
        self.assertFalse(opts["recon"])

    def test_task_only_invocation_is_unchanged(self):
        opts, _ = _capture(ae._parse_cli, [
            "--config", "c.json", "--url", "https://x.test",
            "--goal", "do the thing", "--max-steps", "12"])
        self.assertEqual(opts["config"], "c.json")
        self.assertEqual(opts["url"], "https://x.test")
        self.assertEqual(opts["goal"], "do the thing")
        self.assertEqual(opts["max_steps"], "12")
        self.assertFalse(opts["recon"])

    def test_recon_flag_parses(self):
        opts, _ = _capture(ae._parse_cli, ["--recon", "--url", "https://x.test"])
        self.assertTrue(opts["recon"])
        self.assertEqual(opts["url"], "https://x.test")

    def test_recon_budget_flags_parse(self):
        opts, _ = _capture(ae._parse_cli, [
            "--recon", "--recon-max-pages", "5", "--recon-max-depth", "1",
            "--recon-max-transitions", "7", "--recon-max-seconds", "30"])
        self.assertEqual(opts["recon_max_pages"], "5")
        self.assertEqual(opts["recon_max_depth"], "1")
        self.assertEqual(opts["recon_max_transitions"], "7")
        self.assertEqual(opts["recon_max_seconds"], "30")

    def test_memory_flags_parse(self):
        opts, _ = _capture(ae._parse_cli, ["--recon", "--recon-memory"])
        self.assertTrue(opts["recon_memory"])

        opts, _ = _capture(ae._parse_cli, ["--recon", "--recon-no-memory"])
        self.assertTrue(opts["recon_no_memory"])

        opts, _ = _capture(ae._parse_cli,
                           ["--recon", "--recon-memory-dir", "mem"])
        self.assertEqual(opts["recon_memory_dir"], "mem")

    def test_conflicting_memory_flags_resolve_to_no_memory(self):
        opts, out = _capture(ae._parse_cli,
                             ["--recon", "--recon-memory", "--recon-no-memory"])
        self.assertFalse(opts["recon_memory"])
        self.assertIn("will not use memory", out)

    def test_help_documents_recon(self):
        opts, out = _capture(ae._parse_cli, ["--help"])
        self.assertIsNone(opts)
        self.assertIn("--recon", out)

    def test_recon_flag_without_value_is_an_error(self):
        for flag in ("--recon-max-pages", "--recon-max-depth",
                     "--recon-max-transitions", "--recon-max-seconds"):
            opts, out = _capture(ae._parse_cli, [flag])
            self.assertIsNone(opts, flag)
            self.assertIn("requires a value", out)

    def test_unknown_flag_does_not_abort_recon(self):
        # A typo must not silently turn a recon request into a task run.
        opts, out = _capture(ae._parse_cli, ["--recon", "--nonsense"])
        self.assertTrue(opts["recon"])
        self.assertIn("Ignoring unrecognised", out)


if __name__ == "__main__":
    unittest.main()