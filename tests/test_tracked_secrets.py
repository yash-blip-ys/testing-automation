"""Tracked-file credential audit.

The existing privacy tests prove the engine does not *print* or *store* a
credential. None of them can catch a real secret committed to the repository:
a leaked key is just a string literal sitting in a source file, and every
runtime assertion in the suite passes happily while it is there. This module
closes that gap.

Two tiers, and the split is the whole design:

Tier 1 is structural. Private keys and provider token prefixes have no
legitimate appearance in this project, so they carry no allowlist at all.
Tier 2 is a generic `password = "..."`-style assignment, and it is the *only*
tier an allowlist entry can suppress. Because suppression is by exact value,
allowlisting a benign string can never hide a real provider token -- a real
`AKIA...` key does not equal `AKIAEXAMPLE123`.

The allowlist below is deliberately small and each entry names the test or
fixture that owns it. Anything not listed is reported for review rather than
silently permitted, so a new secret has to be argued for in a diff.

Values are never printed. A finding reports path, line, rule name and an
8-hex fingerprint of the value, which is enough to locate the problem and not
enough to leak it into CI logs.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from reconnaissance import looks_secret  # noqa: E402

TIER1_PATTERNS = {
    "private_key": re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY"),
    "aws_access_key_id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github_token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "stripe_live_key": re.compile(r"\b[sr]k_live_[A-Za-z0-9]{10,}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "json_web_token": re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}\b"),
}

# Key-name driven. Reuses reconnaissance.looks_secret so this audit and the
# memory store cannot drift apart on what counts as sensitive.
#
# "auth" is deliberately absent: it is a prefix of author, authenticated and
# auth_state, which are ordinary identifiers, not credentials.
_SECRET_KEY_WORDS = (
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "access_key", "private_key", "credential",
)

# A `*_selector` key names a CSS/XPath locator, not a credential. Matching the
# inner word in "password_selector" is a false positive, not a finding.
_NON_SECRET_KEY_SUFFIXES = (
    "_selector", "_locator", "_xpath", "_css", "_query", "_aria_label",
    "_placeholder", "_field", "_name", "_marker", "_type", "_testid",
)

# Values that delegate to the environment rather than embedding a literal.
# Split because `$` is a prefix marker -- treating it as a substring would let
# a real password containing `$` slip through unexamined.
_INDIRECT_PREFIXES = ("$", "%(", "{{", "<")
_INDIRECT_SUBSTRINGS = ("os.environ", "getenv", "ENV[", "process.env")

_ASSIGNMENT = re.compile(
    r"(?P<key>[A-Za-z0-9_.\-]*"
    r"(?:" + "|".join(_SECRET_KEY_WORDS) + r")"
    r"[A-Za-z0-9_.\-]*)"
    r"\s*[:=]\s*"
    r"(?:\"(?P<dq>[^\"\n]*)\"|'(?P<sq>[^'\n]*)')",
    re.I,
)

# --------------------------------------------------------------------------
# Allowlist. Exact values only, two documented groups, no patterns.
# --------------------------------------------------------------------------

# Publicly documented demo account for the SauceDemo practice sandbox. Not a
# secret: it is printed in that site's own documentation and is reset
# continuously. Tracked in fixtures/ (four files) and README.md. Approved by
# the project owner on 2026-10-02 for these exact files.
PUBLIC_DEMO_CREDENTIALS = {
    "standard_user": "SauceDemo's public demo username. Paired with the value "
                     "below; neither is sensitive.",
    "secret_sauce": "SauceDemo's public demo password. 6 occurrences across "
                    "fixtures/ and README.md.",
}

# Synthetic canaries that exist precisely so a privacy test can prove the value
# does not leak. Removing one would break the test that owns it.
SYNTHETIC_TEST_CANARIES = {
    "hunter2": "Standard placeholder password; canary in test_agent_core and "
               "test_recon_memory.",
    "hunter2-secret": "Canary in test_agent_core.py (password+api_key pair).",
    "hunter2-SUPER-SECRET": "Canary in test_shadow_replan.py.",
    "anything": "Non-secret placeholder meaning 'any value'; used to prove a "
                "field is treated as sensitive regardless of content.",
    "your_password_here": "README placeholder, instructs the reader to fill "
                          "in their own value.",
    "AKIAEXAMPLE123": "Deliberately fake AWS-key-shaped value used to prove "
                      "key-name detection; contains 'EXAMPLE' and is not a "
                      "real key.",
    "sup3rs3cret-do-not-log": "The canary in test_contract_config.py; the "
                              "string whose absence from output that test "
                              "asserts.",
}

ALLOWED_VALUES = set(PUBLIC_DEMO_CREDENTIALS) | set(SYNTHETIC_TEST_CANARIES)

_SKIP_DIRS = {
    "venv311", "__pycache__", ".git", "recon_memory", ".pytest_cache",
    "node_modules", ".kilo", "reports",
}
_MAX_BYTES = 512 * 1024


def _fingerprint(value):
    """Stable, non-reversible-enough label so a value can be recognised
    across runs without ever being printed."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]


class Finding(object):
    __slots__ = ("rule", "path", "line", "fingerprint")

    def __init__(self, rule, path, line, fingerprint):
        self.rule = rule
        self.path = path
        self.line = line
        self.fingerprint = fingerprint

    def render(self):
        """Never includes the value. This is what a CI log will contain."""
        return "%s:%d: %s (fingerprint %s)" % (
            self.path, self.line, self.rule, self.fingerprint)

    def __repr__(self):
        return "Finding(%s)" % self.render()


def scan_text(text, path="<memory>"):
    """Return findings for one file's contents."""
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in TIER1_PATTERNS.items():
            if pattern.search(line):
                match = pattern.search(line)
                findings.append(Finding(rule, path, number,
                                        _fingerprint(match.group(0))))
        for match in _ASSIGNMENT.finditer(line):
            key = match.group("key").lower()
            value = match.group("dq")
            if value is None:
                value = match.group("sq")
            if key.endswith(_NON_SECRET_KEY_SUFFIXES):
                continue
            if len(value) < 4 or value in ALLOWED_VALUES:
                continue
            if any(value.startswith(m) for m in _INDIRECT_PREFIXES):
                continue
            if any(m in value for m in _INDIRECT_SUBSTRINGS):
                continue
            if looks_secret(key, value):
                findings.append(Finding("hardcoded_" + match.group("key").lower(),
                                        path, number, _fingerprint(value)))
    return findings


def _git_tracked_files(repo):
    """Tracked paths, or None when git cannot answer for exactly this repo.

    The top-level check is not defensive padding. `git ls-files` walks *up*
    to find a repository, so auditing a directory that merely sits inside
    some unrelated checkout -- a clean-copy export under a home directory that
    happens to be a repo, for instance -- silently succeeds while listing
    nothing. The audit then passes having read zero files. Requiring the
    toplevel to match is what turns that into a visible fallback.
    """
    def run(args):
        try:
            return subprocess.run(["git", "-C", repo] + args,
                                  capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None

    top = run(["rev-parse", "--show-toplevel"])
    if top is None or top.returncode != 0:
        return None
    reported = top.stdout.decode("utf-8", "replace").strip()
    # realpath, not abspath: Windows 8.3 short names and long names denote one
    # directory but never compare equal, which would silently demote a real
    # checkout to the walk fallback.
    if os.path.normcase(os.path.realpath(reported)) != \
            os.path.normcase(os.path.realpath(repo)):
        return None

    listed = run(["ls-files", "-z"])
    if listed is None or listed.returncode != 0:
        return None
    paths = [p for p in listed.stdout.decode("utf-8", "replace").split("\0") if p]
    # A repo that reports no tracked files while containing files on disk is
    # the same silent-no-op in a different shape.
    if not paths and any(names for _, _, names in os.walk(repo)):
        return None
    return paths


def tracked_files(repo):
    """Files the project ships. Uses git when available so the audit covers
    exactly what would be cloned, and falls back to a directory walk because
    `benchmark/clean_checkout.py` excludes .git from the copy it verifies."""
    tracked = _git_tracked_files(repo)
    if tracked is not None:
        return tracked
    found = []
    for root, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in names:
            if name.endswith((".pyc", ".pyo", ".log")):
                continue
            full = os.path.join(root, name)
            found.append(os.path.relpath(full, repo))
    return found


def _readable_text(path):
    try:
        if os.path.getsize(path) > _MAX_BYTES:
            return None
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    return raw.decode("utf-8", "replace")


def scan_repo(repo=None):
    repo = repo or _ROOT
    findings = []
    for relative in sorted(tracked_files(repo)):
        text = _readable_text(os.path.join(repo, relative))
        if text is None:
            continue
        findings.extend(scan_text(text, relative))
    return findings


class TrackedFileCredentialAudit(unittest.TestCase):
    """The audit itself, run over everything the project ships."""

    def test_no_hardcoded_credentials_in_tracked_files(self):
        findings = scan_repo()
        self.assertEqual(
            [], findings,
            "tracked files contain credential-shaped values; move them to the "
            "environment or add a documented allowlist entry:\n  "
            + "\n  ".join(f.render() for f in findings))

    def test_the_audit_actually_reads_files(self):
        """Guards against a silently-empty file list making this pass for the
        wrong reason -- a botched git call or a bad root would otherwise look
        identical to a clean repository."""
        paths = tracked_files(_ROOT)
        self.assertGreater(len(paths), 20,
                           "expected to audit the whole project, got %d paths"
                           % len(paths))
        joined = "\n".join(paths)
        for expected in ("automation_engine.py", "reconnaissance.py"):
            self.assertIn(expected, joined,
                          "%s must be inside the audited set" % expected)

    def test_audit_does_not_flag_its_own_source(self):
        """Positive-control samples are assembled from fragments so this file
        contains no complete high-entropy literal. If that ever stops being
        true, the audit is flagging itself and the allowlist needs review."""
        self.assertEqual([], scan_text(
            _readable_text(os.path.abspath(__file__)),
            os.path.basename(__file__)))

    def test_discovery_refuses_a_nested_unrelated_repository(self):
        """Regression: `git ls-files` walks up to find a repo. Auditing a
        directory that merely sits inside some other checkout returned that
        repo's index -- empty for our files -- so the audit passed having read
        nothing. Discovery must fall back to walking instead."""
        outer = tempfile.mkdtemp(prefix="secrets_outer_")
        inner = os.path.join(outer, "export")
        os.makedirs(inner)
        try:
            subprocess.run(["git", "init", "-q", outer], capture_output=True,
                           timeout=60)
            with open(os.path.join(inner, "mod.py"), "w") as handle:
                handle.write("x = 1\n")
            discovered = tracked_files(inner)
            self.assertNotEqual([], discovered,
                                "nested directory must not be audited as empty")
            self.assertTrue(any(p.endswith("mod.py") for p in discovered),
                            "fallback must find the file on disk")
        finally:
            shutil.rmtree(outer, ignore_errors=True)


class DetectorPositiveControlTests(unittest.TestCase):
    """An audit that finds nothing proves nothing unless it can be shown to
    find something. Each sample is written to a temp file outside the repo and
    scanned, so the detector is exercised without the literal ever living in a
    tracked file."""

    # Assembled from fragments: a complete literal here would trip this
    # module's own Tier 1 patterns.
    _SAMPLES = {
        "private_key": ["-----BEGIN ", "RSA ", "PRIVATE KEY-----"],
        "aws_access_key_id": ["AKIA", "IOSFODNN7EXAMPLE"],
        "github_token": ["ghp_", "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8"],
        "slack_token": ["xox", "b-", "1234567890-abcdefghij"],
        "stripe_live_key": ["sk", "_live_", "aBcD1234eFgH5678"],
        "google_api_key": ["AI", "za", "SyD1234567890abcdefghijklmnopqrstuv"],
        "json_web_token": ["eyJ", "hbGciOiJIUzI1NiJ9", ".",
                           "eyJzdWIiOiIxMjM0NTY3ODkwI", ".",
                           "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"],
    }

    @staticmethod
    def _write(body):
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        handle.write(body)
        handle.close()
        return handle.name

    def test_each_structural_detector_fires(self):
        for rule, fragments in sorted(self._SAMPLES.items()):
            with self.subTest(rule=rule):
                body = "".join(fragments)
                path = self._write("leaked = " + body + "\n")
                try:
                    findings = scan_text(_readable_text(path), "sample.txt")
                finally:
                    os.unlink(path)
                self.assertTrue(
                    any(f.rule == rule for f in findings),
                    "%s detector did not fire on its own sample" % rule)

    def test_generic_password_assignment_is_detected(self):
        value = "Tr0ub4dor" + "&3xyz!"
        path = self._write('password = "%s"\n' % value)
        try:
            findings = scan_text(_readable_text(path), "sample.txt")
        finally:
            os.unlink(path)
        self.assertEqual(1, len(findings), "expected exactly one finding")
        self.assertEqual("hardcoded_password", findings[0].rule)

    def test_allowlisted_values_are_not_flagged(self):
        for value in sorted(ALLOWED_VALUES):
            with self.subTest(value_len=len(value)):
                path = self._write('password = "%s"\n' % value)
                try:
                    findings = scan_text(_readable_text(path), "sample.txt")
                finally:
                    os.unlink(path)
                self.assertEqual([], findings,
                                 "allowlisted value was flagged: "
                                 + "; ".join(f.render() for f in findings))

    def test_failure_output_never_contains_the_value(self):
        """A leak discovered by CI must not be copied into the CI log."""
        sentinel = "Zx9" + "Qw7Lp2Vr4Tn6"
        path = self._write('api_key = "%s"\n' % sentinel)
        try:
            findings = scan_text(_readable_text(path), "sample.txt")
        finally:
            os.unlink(path)
        self.assertEqual(1, len(findings))
        rendered = findings[0].render()
        self.assertNotIn(sentinel, rendered)
        self.assertNotIn(sentinel, repr(findings[0]))
        self.assertNotIn(sentinel, str([f.render() for f in findings]))
        self.assertIn("fingerprint", rendered)


class FalsePositiveControlTests(unittest.TestCase):
    """Shapes that look secret-shaped but are not, and would make the audit
    unusable if reported."""

    def test_selector_keys_are_not_credentials(self):
        body = ('password_selector = "input[type=password]"\n'
                'username_selector = "input[name=user]"\n')
        self.assertEqual([], scan_text(body, "config.json"))

    def test_environment_indirection_is_not_a_leak(self):
        for value in ('"${DB_PASSWORD}"', '"os.environ[\'API_KEY\']"',
                      '"$SECRET_TOKEN"', '"{{ vault_api_key }}"'):
            with self.subTest(value=value):
                body = "password = %s\n" % value
                self.assertEqual([], scan_text(body, "settings.py"))

    def test_usernames_are_not_secrets(self):
        """The approved demo pair is a username and a password; only the
        password half is sensitive, and the username must stay clean so the
        allowlist is not wider than it needs to be."""
        body = 'credentials = {"username": "standard_user"}\n'
        self.assertEqual([], scan_text(body, "creds.json"))
        self.assertIn("standard_user", ALLOWED_VALUES)
        self.assertTrue(PUBLIC_DEMO_CREDENTIALS["standard_user"],
                        "each allowlist entry must carry its reason")

    def test_empty_and_trivial_values_are_ignored(self):
        self.assertEqual([], scan_text('password = ""\napi_key = "x"\n'))

    def test_ordinary_non_secret_code_is_ignored(self):
        body = ('def hash_form_value(value):\n'
                '    """Credentials must never be printed."""\n'
                '    token = value\n'
                '    return hashlib.sha256(token.encode()).hexdigest()\n')
        self.assertEqual([], scan_text(body, "engine.py"))


class ReconMemoryIgnoreRuleTests(unittest.TestCase):
    """F6: the ignore rule that keeps per-site, machine-local reconnaissance
    observations out of version control has to actually be in force."""

    def test_no_tracked_file_lives_under_recon_memory(self):
        if _git_tracked_files(_ROOT) is None:
            self.skipTest("not a git checkout; nothing to verify")
        offenders = [p for p in _git_tracked_files(_ROOT)
                     if p.startswith("recon_memory" + os.sep)
                     or p.startswith("recon_memory/")]
        self.assertEqual([], offenders,
                         "per-site reconnaissance memory must never be tracked")

    def test_the_ignore_rule_is_declared(self):
        gitignore = os.path.join(_ROOT, ".gitignore")
        if not os.path.exists(gitignore):
            self.skipTest("no .gitignore in this checkout")
        with open(gitignore, encoding="utf-8", errors="replace") as handle:
            body = handle.read()
        self.assertIn("recon_memory/", body,
                      "recon_memory/ must be ignored: it holds per-site "
                      "machine-local observations")


if __name__ == "__main__":
    unittest.main()