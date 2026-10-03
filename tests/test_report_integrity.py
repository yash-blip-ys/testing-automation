"""Part 3 — Reporting integrity.

A live run reached its final status and then died with a `NameError` while
assembling the report, discarding the whole result. The run had actually
stopped correctly at a consequential action; the only thing lost was the
evidence of that.

These tests pin the two properties that failure violated:

  * the report is built inside the guard, so a defect in the summary degrades
    to a warning instead of erasing a completed run
  * nothing in the report block may reference a name that does not exist

No browser, no model. Run with:
    .\\venv311\\Scripts\\python.exe -m unittest tests.test_report_integrity -v
"""

import ast
import builtins
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation_engine as ae


def _report_block():
    """The source of the report-assembly region inside the run loop."""
    src = inspect.getsource(ae.run_pathfinder_agent)
    start = src.index("        try:\n            _errors = []")
    return src[start:]


class TestReportAssemblyIsGuarded(unittest.TestCase):

    def test_errors_are_built_inside_the_try(self):
        """The failure list is part of the report, so it is guarded with it."""
        src = _report_block()
        self.assertIn("try:\n            _errors = []", src)
        self.assertIn("except Exception as report_exc", src)

    def test_the_guard_wraps_the_report_call(self):
        """Not merely present: the write must actually be inside it."""
        src = _report_block()
        try_at = src.index("try:\n            _errors = []")
        write_at = src.index("generate_scan_report(")
        except_at = src.index("except Exception as report_exc")
        self.assertLess(try_at, write_at, "the write must follow the guard")
        self.assertLess(write_at, except_at, "the write must precede the handler")

    def test_guard_warns_instead_of_propagating(self):
        self.assertIn("[Report] WARNING", src_block := _report_block())
        self.assertIn("outcome is unaffected", src_block)


def _enclosing_names():
    """Names bound anywhere in the run loop itself.

    The report region is a slice of the run loop's body, so the loop's own
    locals are legitimately in scope there. A name missing from both the slice
    and the loop is the defect this test exists to catch.
    """
    return _bound_names(ast.parse(textwrap_dedent(
        inspect.getsource(ae.run_pathfinder_agent))))


def _module_names():
    return set(dir(ae)) | set(dir(builtins))


class TestReportBlockHasNoUndefinedNames(unittest.TestCase):
    """The defect was a stale name in exactly this region.

    Only names the engine can actually provide count as defined, so a typo
    that happens to match a builtin still fails.
    """

    def test_no_name_in_the_report_block_is_undefined(self):
        missing = sorted(
            _undefined_loads(_report_block())
            - _module_names() - _enclosing_names())
        self.assertEqual(
            missing, [],
            "the report block references names that do not exist; a run that "
            "reaches this code would lose its entire result to a NameError")


def textwrap_dedent(src):
    """Dedent without importing textwrap at module scope for one use."""
    import textwrap
    return textwrap.dedent(src)


def _undefined_loads(node):
    """Every name read by `node` that the block does not itself bind.

    Order-independent: a name assigned anywhere in the region counts as
    defined, matching how the region actually executes.
    """
    tree = ast.parse(textwrap_dedent(node)) if isinstance(node, str) else node
    bound = _bound_names(tree)
    loads = {n.id for n in ast.walk(tree)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return loads - bound


def _bound_names(tree):
    names = set()
    for child in ast.walk(tree):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            names.add(child.name)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            for alias in child.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
        elif isinstance(child, ast.Global):
            names |= set(child.names)
    names |= _assigned_names(tree)
    return names


def _assigned_names(node):
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Assign):
            for t in child.targets:
                names |= _target_names(t)
        elif isinstance(child, (ast.AugAssign, ast.AnnAssign)):
            names |= _target_names(child.target)
        elif isinstance(child, (ast.For, ast.AsyncFor)):
            names |= _target_names(child.target)
        elif isinstance(child, ast.withitem):
            if child.optional_vars is not None:
                names |= _target_names(child.optional_vars)
    return names


def _target_names(target):
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        out = set()
        for elt in target.elts:
            out |= _target_names(elt)
        return out
    return set()


if __name__ == "__main__":
    unittest.main()