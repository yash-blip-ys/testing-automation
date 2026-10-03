"""Checkpoint 6.7.7 — clean-checkout reproducibility verification.

The question is not "does it work on my machine" but "does it work for someone
who has only the files this project ships". A suite that passes locally while
depending on an untracked helper, a leftover artefact, or a machine-local
directory has not been verified at all.

This copies the project into a fresh directory, then runs the full suite there.
Anything the suite needs that is not in the copy fails loudly, which is the
point.

File selection is Git-aware. Inside a checkout, the candidate set is the Git
index (`git ls-files`), so the export contains exactly the staged, reproducible
project state: new files that were staged are included, and untracked local
artefacts -- leftover scan reports, one-off probe scripts, editor droppings --
cannot slip into it. `.gitignore` alone is not sufficient and is not relied on
here: it governs *untracked* files only, so a generated report that someone
once committed would still be listed. The generated/local exclusions below are
therefore applied as a second pass on top of the index.

Outside a checkout (no `.git`, or Git unavailable) selection falls back to
walking the tree with the same exclusions, which is the previous behaviour.

Note that `unittest discover` includes the 9 browser-dependent tests in
`tests/test_occlusion_live.py`. They drive a real Chromium against a local
`file:///` fixture, so a clean copy verifies browser-dependence too -- but they
are not live external-website tests, and this is not evidence about any real
website.

    python -m benchmark.clean_checkout
    python -m benchmark.clean_checkout --keep     # leave the copy for inspection
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

# Paths that must NOT be needed to run the offline suite. A test suite that
# depends on any of these is not reproducible from a clean checkout.
EXCLUDED_DIRS = {
    "venv311", "__pycache__", ".git", "recon_memory", ".pytest_cache",
    "node_modules", ".kilo", "reports",
}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".log")
EXCLUDED_NAMES = {".env", ".DS_Store"}


def should_skip(path):
    name = os.path.basename(path)
    if name in EXCLUDED_NAMES or name in EXCLUDED_DIRS:
        return True
    return name.endswith(EXCLUDED_SUFFIXES)


def is_excluded(relative_path):
    """Whether a path is a generated or machine-local artefact.

    Checks every path component, not just the basename: `reports/x.json` must
    be excluded because of its parent directory, and `should_skip` on the
    basename alone would not notice.
    """
    parts = os.path.normpath(relative_path).split(os.sep)
    if any(part in EXCLUDED_DIRS for part in parts[:-1]):
        return True
    return should_skip(parts[-1])


def _git_index_files(source):
    """Files in this repository's Git index, or None when Git cannot answer.

    The top-level check is load-bearing. `git ls-files` walks *up* to find a
    repository, so running it against a directory that merely sits inside some
    unrelated checkout would export that repository's index -- silently the
    wrong file set. Requiring the toplevel to be the directory being exported
    turns that into a visible fallback instead of a quietly untrustworthy
    reproducibility check.
    """

    def run(args):
        try:
            return subprocess.run(["git", "-C", source] + args,
                                  capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None

    top = run(["rev-parse", "--show-toplevel"])
    if top is None or top.returncode != 0:
        return None
    reported = top.stdout.decode("utf-8", "replace").strip()
    # realpath, not abspath: Windows hands out 8.3 short names (C:\Users\YUVRAJ~1)
    # while git reports the long form (C:\Users\YUVRAJ SINGH). Those name the
    # same directory but never compare equal, which would quietly demote every
    # checkout to the walk fallback.
    if os.path.normcase(os.path.realpath(reported)) != \
            os.path.normcase(os.path.realpath(source)):
        return None

    listed = run(["ls-files", "-z"])
    if listed is None or listed.returncode != 0:
        return None
    return [p.replace("\\", "/") for p
            in listed.stdout.decode("utf-8", "replace").split("\0") if p]


def _walk_all(source):
    """Every file under `source`, for when Git metadata is unavailable."""
    found = []
    for root, dirs, names in os.walk(source):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for name in names:
            relative = os.path.relpath(os.path.join(root, name), source)
            found.append(relative.replace("\\", "/"))
    return found


def _git_ignored_paths(source, candidates):
    """Subset of `candidates` matched by .gitignore, *including tracked ones*.

    `check-ignore` without --no-index skips files already in the index, and
    .gitignore has never applied to a tracked file. That gap is the whole
    reason a generated report can be committed once and then ride along in
    every release export forever. Batched into one call because this runs per
    export, not per file.
    """
    if not candidates:
        return set()
    try:
        completed = subprocess.run(
            ["git", "-C", source, "check-ignore", "--no-index", "--stdin", "-z"],
            input="\0".join(candidates).encode("utf-8", "replace"),
            capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return set()
    if completed.returncode not in (0, 1):
        return set()
    return {p for p in completed.stdout.decode("utf-8", "replace").split("\0")
            if p}


def shipped_files(source):
    """Relative paths of the files that make up a release of this project.

    Index-backed inside a checkout, walk-backed outside one. Either way .gitignore
    is applied to tracked files as well as untracked ones, and the
    generated/local exclusions below are applied on top, so the result is the
    reproducible project state and never machine-local debris.
    """
    candidates = _git_index_files(source)
    if candidates is None:
        candidates = _walk_all(source)
    else:
        ignored = _git_ignored_paths(source, candidates)
        if ignored:
            candidates = [p for p in candidates if p not in ignored]
    return sorted(p for p in candidates if not is_excluded(p))


def copy_tree(source, destination):
    """Copy only what would ship. Returns the list of copied files."""
    copied = []
    for relative in shipped_files(source):
        full = os.path.join(source, relative)
        if not os.path.isfile(full):
            continue
        target = os.path.join(destination, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(full, target)
        copied.append(relative)
    return copied


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true",
                        help="keep the clean copy for inspection")
    parser.add_argument("--source", default=".")
    args = parser.parse_args(argv)

    source = os.path.abspath(args.source)
    destination = tempfile.mkdtemp(prefix="clean_checkout_")
    copied = copy_tree(source, destination)
    selection = "git index" if _git_index_files(source) is not None \
        else "directory walk (no git index)"

    print("=" * 78)
    print("CLEAN-CHECKOUT VERIFICATION (Checkpoint 6.7.7)")
    print("=" * 78)
    print(f"source      : {source}")
    print(f"clean copy  : {destination}")
    print(f"files copied: {len(copied)}")
    print(f"selection   : {selection}")
    print(f"excluded    : {', '.join(sorted(EXCLUDED_DIRS))}")
    print()

    for required in ("automation_engine.py", "benchmark", "tests", "fixtures",
                     "reconnaissance.py", "requirements.txt"):
        # copied paths always use "/" so they compare equal regardless of
        # platform separator; os.sep here silently reported every directory
        # missing on Windows.
        prefix = required + "/"
        present = required in copied or any(p.startswith(prefix) for p in copied)
        print(f"  [{'ok' if present else 'MISSING'}] {required}")

    print()
    print("running the full suite from the clean copy ...")
    # `discover` includes the 9 browser-dependent tests in
    # tests/test_occlusion_live.py, so this is the full suite, not an
    # offline-only one. An earlier version of this script printed "the
    # offline suite" above this call while doing exactly this, which is how
    # 1020 tests came to be reported as offline when 9 of them need a
    # browser. Labelled accurately rather than quietly narrowed.
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=destination, env=env, capture_output=True, text=True)

    tail = (completed.stderr or "").strip().splitlines()[-6:]
    for line in tail:
        print(f"  {line}")

    ok = completed.returncode == 0
    print()
    print("=" * 78)
    print(f"FULL SUITE FROM CLEAN COPY: {'PASS' if ok else 'FAIL'}")
    print("=" * 78)
    if args.keep:
        print(f"clean copy kept at {destination}")
    else:
        shutil.rmtree(destination, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())