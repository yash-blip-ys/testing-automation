"""F8 — containment of reconnaissance memory, and Git-aware release packaging.

Two separate defects are covered here.

Packaging (`benchmark.clean_checkout`): selection used to walk the filesystem
and reimplement ignore rules with its own hardcoded list, so the two mechanisms
silently disagreed. `scan_report_*.md` and `recon_report_*.md` are git-ignored
but were being copied into the reproducibility export anyway -- 7 such files at
the time of writing. Selection is now index-backed inside a checkout.

Memory directory: `--recon-memory-dir` accepts any path and creates it, so an
in-repository location that nothing ignores is one `git add .` away from
publishing per-site reconnaissance data. The tool warns; it never redirects.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from benchmark import clean_checkout as cc  # noqa: E402
import reconnaissance as R  # noqa: E402
from reconnaissance import (  # noqa: E402
    MEMORY_DEFAULT_DIR, SiteMemory, warn_if_memory_dir_is_shared,
)

_GIT = shutil.which("git")


def _git_available():
    if _GIT is None:
        return False
    try:
        return subprocess.run(["git", "--version"], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


requires_git = unittest.skipUnless(
    _git_available(), "git is unavailable in this environment")


class Sandbox(object):
    """A throwaway project, optionally a git repository."""

    def __init__(self, git=True):
        self.root = tempfile.mkdtemp(prefix="f8box_")
        self.git = git and _git_available()

    def write(self, relative, body="x = 1\n"):
        full = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(body)
        return full

    def stage(self, *relative, force=False):
        args = ["git", "-C", self.root, "add"]
        if force:
            args.append("-f")      # required to stage an ignored file at all
        subprocess.run(args + ["--"] + list(relative),
                       capture_output=True, timeout=60)

    def commit(self):
        subprocess.run(
            ["git", "-C", self.root, "-c", "user.email=t@t", "-c",
             "user.name=t", "commit", "-qm", "seed"], capture_output=True,
            timeout=60)

    def ignore(self, pattern):
        with open(os.path.join(self.root, ".gitignore"), "a",
                  encoding="utf-8") as handle:
            handle.write(pattern + "\n")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def _run_git(root, *args):
    return subprocess.run(["git", "-C", root] + list(args),
                          capture_output=True, timeout=60)


# --------------------------------------------------------------------------
# Fix 1 -- Git-aware packaging
# --------------------------------------------------------------------------

class ShippedFilesSelectionTests(unittest.TestCase):
    """Selection must be the staged, reproducible project state -- never a
    walk over whatever happens to be lying around locally."""

    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        # Every case here is about index-backed selection, so the box must be a
        # real checkout; the walk fallback has its own test.
        if _git_available():
            subprocess.run(["git", "init", "-q", self.sandbox.root],
                           capture_output=True, timeout=60)

    def _shipped(self):
        return set(cc.shipped_files(self.sandbox.root))

    @requires_git
    def test_a_staged_new_file_is_included(self):
        """The whole point of staging: a file nobody has committed yet still
        has to ship, or the export is not the state being released."""
        self.sandbox.write(".gitignore", "scratch/\n")
        self.sandbox.stage(".gitignore")
        self.sandbox.write("tests/test_new.py")
        self.sandbox.stage("tests/test_new.py")
        self.assertIn("tests/test_new.py", self._shipped())

    @requires_git
    def test_an_untracked_local_file_is_excluded(self):
        self.sandbox.write("leftover_probe.py")
        self.assertNotIn("leftover_probe.py", self._shipped())

    @requires_git
    def test_a_gitignored_report_is_excluded(self):
        self.sandbox.write(".gitignore", "scan_report_*.md\n")
        self.sandbox.stage(".gitignore")
        self.sandbox.write("scan_report_2026-01-01_00-00-00.md")
        self.assertNotIn("scan_report_2026-01-01_00-00-00.md", self._shipped())

    @requires_git
    def test_recon_report_is_excluded(self):
        self.sandbox.write(".gitignore", "recon_report_*.md\n")
        self.sandbox.stage(".gitignore")
        self.sandbox.write("recon_report_2026-01-01-00-00-00.md")
        self.assertNotIn("recon_report_2026-01-01-00-00-00.md", self._shipped())

    @requires_git
    def test_a_staged_report_is_still_excluded(self):
        """`.gitignore` does not apply to files already in the index. Someone
        who once force-added a generated report would otherwise smuggle it back
        into every release export."""
        self.sandbox.write(".gitignore", "scan_report_*.md\n")
        self.sandbox.stage(".gitignore")
        self.sandbox.write("scan_report_committed.md")
        self.sandbox.stage("scan_report_committed.md", force=True)
        self.assertIn("scan_report_committed.md", _run_git(
            self.sandbox.root, "ls-files").stdout.decode().split(),
            "precondition: the report really is staged")
        self.assertNotIn("scan_report_committed.md", self._shipped(),
                         "index membership must not bypass the local rules")

    @requires_git
    def test_generated_directories_are_excluded_by_component(self):
        self.sandbox.write(".gitignore", "reports/\n")
        self.sandbox.stage(".gitignore")
        self.sandbox.write("reports/scan.json")
        self.sandbox.stage("reports/scan.json", force=True)
        self.assertNotIn("reports/scan.json", self._shipped())

    @requires_git
    def test_recon_memory_directory_is_excluded(self):
        self.sandbox.write(".gitignore", "recon_memory/\n")
        self.sandbox.stage(".gitignore")
        self.sandbox.write("recon_memory/site_abc.json")
        self.sandbox.stage("recon_memory/site_abc.json", force=True)
        self.assertNotIn("recon_memory/site_abc.json", self._shipped())

    @requires_git
    def test_staged_fixtures_and_benchmarks_are_not_omitted(self):
        for relative in ("fixtures/occlusion/index.html",
                         "benchmark/runner.py", "tests/test_recon_memory.py"):
            with self.subTest(path=relative):
                self.sandbox.write(relative)
                self.sandbox.stage(relative)
                self.assertIn(relative, self._shipped())

    def test_walk_fallback_when_git_metadata_is_absent(self):
        """No `.git` at all: selection must still work, by walking."""
        sandbox = Sandbox(git=False)
        self.addCleanup(sandbox.cleanup)
        sandbox.write("tests/test_a.py")
        sandbox.write("scratch/probe.py")
        sandbox.write("notes.pyc")
        shipped = set(cc.shipped_files(sandbox.root))
        self.assertIn("tests/test_a.py", shipped)
        self.assertNotIn("notes.pyc", shipped,
                         "bytecode must be excluded on both paths")

    def test_nested_directory_inside_an_unrelated_repo_is_not_used(self):
        """Regression shape of the credential audit's bug: `git ls-files`
        walks up. Exporting a subdirectory of some other repository must fall
        back to walking that subdirectory, not export the outer index."""
        if not _git_available():
            self.skipTest("git unavailable")
        outer = Sandbox()
        self.addCleanup(outer.cleanup)
        outer.write("real_module.py")
        subprocess.run(["git", "init", "-q", outer.root], capture_output=True,
                       timeout=60)
        inner = os.path.join(outer.root, "export")
        os.makedirs(inner)
        with open(os.path.join(inner, "local_only.py"), "w") as handle:
            handle.write("y = 2\n")
        shipped = set(cc.shipped_files(inner))
        self.assertNotIn("real_module.py", shipped,
                         "must not export the outer repository's files")
        self.assertIn("local_only.py", shipped,
                      "must fall back to walking the requested directory")

    @requires_git
    def test_real_project_selection_matches_the_staged_index(self):
        """The live project must export its index, not local debris."""
        shipped = set(cc.shipped_files(_ROOT))
        if cc._git_index_files(_ROOT) is None:
            self.skipTest("not inside a git checkout")
        for expected in ("automation_engine.py", "reconnaissance.py",
                         "tests/test_tracked_secrets.py",
                         "fixtures/occlusion/index.html",
                         "benchmark/clean_checkout.py", ".gitignore"):
            self.assertIn(expected, shipped)
        self.assertNotIn("recon_ab_probe.py", shipped)
        self.assertNotIn("shadow_probe.py", shipped)
        for path in shipped:
            self.assertFalse(path.startswith("scan_report_"), path)


# --------------------------------------------------------------------------
# Fix 2 -- unsafe custom memory directory warning
# --------------------------------------------------------------------------

class MemoryDirectoryWarningTests(unittest.TestCase):

    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        subprocess.run(["git", "init", "-q", self.sandbox.root],
                       capture_output=True, timeout=60)
        # Pin git to this sandbox so ignore rules come from the sandbox's own
        # .gitignore. Without this the answers depend on whatever repository
        # happens to enclose the temp directory -- this machine's home
        # directory is itself a checkout, and it already ignored one of the
        # paths these tests assert about.
        self._previous_git_dir = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = os.path.join(self.sandbox.root, ".git")
        self.addCleanup(self._restore_git_dir)

    def _restore_git_dir(self):
        if self._previous_git_dir is None:
            os.environ.pop("GIT_DIR", None)
        else:
            os.environ["GIT_DIR"] = self._previous_git_dir

    def _warn(self, directory):
        return warn_if_memory_dir_is_shared(
            directory, cwd=self.sandbox.root, project_root=self.sandbox.root)

    def test_default_directory_never_warns(self):
        self.sandbox.ignore("recon_memory/")
        self.assertIsNone(self._warn(MEMORY_DEFAULT_DIR))

    def test_ignored_custom_directory_does_not_warn(self):
        self.sandbox.ignore("custom_memory/")
        self.assertIsNone(self._warn("custom_memory/"))

    def test_unignored_custom_directory_inside_project_warns(self):
        warning = self._warn("shared_memory/")
        self.assertIsNotNone(warning, "an unignored in-project dir must warn")
        self.assertIn("shared_memory/", warning)
        self.assertIn(".gitignore", warning)

    def test_directory_outside_the_project_does_not_warn(self):
        with tempfile.TemporaryDirectory() as outside:
            self.assertIsNone(self._warn(os.path.join(outside, "recon_memory")))

    def test_warning_never_includes_stored_values(self):
        """The message names the directory and nothing else. A warning printed
        on every run must not become a channel for site data."""
        warning = self._warn("shared_memory/")
        self.assertNotIn("site_key", warning)
        self.assertNotIn("records", warning)
        self.assertNotIn("http", warning)

    def test_missing_git_metadata_is_handled(self):
        """Git unable to answer: treat the cwd as the project and say so
        honestly, rather than letting "unknown" read as "safe".

        GIT_DIR points at a path that does not exist, which is how git
        reports unavailable metadata. GIT_CEILING_DIRECTORIES is not usable
        for this: it does not stop discovery when the repository sits above
        the ceiling on this machine.
        """
        os.environ["GIT_DIR"] = os.path.join(self.sandbox.root, "no_such_dir")
        self.assertIsNone(R._git_toplevel(self.sandbox.root),
                          "precondition: git must fail to answer")
        self.assertIsNone(R._git_ignores("shared_memory/", self.sandbox.root),
                          "precondition: ignore status must be unknown")
        warning = warn_if_memory_dir_is_shared(
            "shared_memory/", cwd=self.sandbox.root,
            project_root=self.sandbox.root)
        self.assertIsNotNone(warning, "unknown status must not read as safe")
        self.assertIn("could not be consulted", warning)

    def test_default_directory_is_silent_even_without_git(self):
        """The default location is ignored by policy; with git unavailable
        that policy check must not turn into noise on every run."""
        os.environ["GIT_DIR"] = os.path.join(self.sandbox.root, "absent")
        self.assertIsNone(self._warn(MEMORY_DEFAULT_DIR))

    def test_warning_is_emitted_once_per_store_not_per_write(self):
        """SiteMemory.__init__ runs once per run. Asserting the message is
        emitted at construction, and that save() does not reprint it.

        A real store resolves its directory from the process working
        directory, so this has to actually run from inside the sandbox.
        """
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.chdir(self.sandbox.root):
            with contextlib.redirect_stdout(buffer):
                memory = SiteMemory("https://example.com/",
                                    directory="shared_memory")
                first = buffer.getvalue()
                for _ in range(3):
                    memory.save()
        self.assertIn("WARNING", first)
        self.assertEqual(1, buffer.getvalue().count("WARNING"),
                         "the warning must not repeat on every write")

    def test_memory_still_writes_where_it_was_told(self):
        """The fix advises; it must not relocate or refuse. Pointing the store
        at an unignored in-project path has to keep working."""
        target = os.path.join(self.sandbox.root, "shared_memory")
        memory = SiteMemory("https://example.com/", directory=target)
        written = memory.save()
        self.assertIsNotNone(written)
        self.assertEqual(os.path.realpath(target),
                         os.path.dirname(os.path.realpath(written)))


if __name__ == "__main__":
    unittest.main()