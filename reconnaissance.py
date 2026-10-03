"""Website reconnaissance and persistent website memory.

================================== SCOPE ===================================
This module adds a SEPARATE mode. It is not wired into `run_pathfinder_agent`
and shares no mutable state with it. Reconnaissance explores a site to build a
picture of its structure; it never decides whether a user task succeeded, and
it never mutates the task loop. Adding this file cannot change how an ordinary
task run behaves, because nothing in the task path calls into it.

=========================== THE RECONNAISSANCE RULE =========================
Reconnaissance is READ-ONLY exploration. It may navigate. It may observe. It
may record structure. It may NOT:

  * submit a form;
  * fill a field, especially a credential or payment field;
  * purchase, book, subscribe, donate, send, publish, delete, or transfer;
  * change account settings;
  * accept a cookie/consent banner or a native dialog on the user's behalf.

Traversal is therefore restricted to same-origin hyperlink navigation, which
has no side effect. Anything that reads as a commitment is recorded as an
unexplored area with a reason, not taken. The conservative default is the point:
an unexplored area is an honest gap in a report, whereas an accidental purchase
is not recoverable by writing a better report afterwards.

=========================== BUDGETS AND STOPPING ============================
Every run declares its limits up front (`ReconBudget`) and stops the moment any
one of them is reached. A run that hits a limit says so; it never presents a
truncated crawl as exhaustive discovery.

================================ SAFETY BARRIERS =============================
Reconnaissance stops safely — records the area, does not proceed — when it
meets authentication, a consent gate, an access-control challenge, or a
consequential control. These are boundaries the tool does not cross on the
user's behalf, and they are detected mechanically from the observation, never
inferred from a model's wording.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urldefrag, urljoin, urlparse, urlunparse

from playwright.async_api import async_playwright, \
    TimeoutError as PlaywrightTimeoutError

from automation_engine import (
    CONSEQUENTIAL_WORDS,
    SENSITIVE_FIELD_MARKERS,
    compute_full_node_id,
    detect_access_control_barrier,
    extract_page_elements,
    wait_for_page_settled,
)


# ---------------------------------------------------------------------------
# Reconnaissance budgets.
#
# Every limit has a default that is small on purpose. Reconnaissance exists to
# answer "what is this site made of", not to crawl it exhaustively, and a
# default that promises completeness is a default that will be violated by any
# real site. Each limit is independent: exhausting one stops the run and names
# which one, so a truncated crawl is always attributable.
# ---------------------------------------------------------------------------

RECON_MAX_PAGES = 25
RECON_MAX_TRANSITIONS = 60
RECON_MAX_DEPTH = 3
RECON_MAX_SECONDS = 300
RECON_MAX_MODEL_CALLS = 40
RECON_MAX_STORED_CHARS = 20000

# Per-page discovery bounds, so one page with a thousand links cannot consume
# the whole run. Overflow is counted and reported, never silently dropped.
RECON_MAX_LINKS_PER_PAGE = 40
RECON_MAX_LINK_TEXT_CHARS = 80
RECON_MAX_FORM_FIELDS = 25


class ReconBudget:
    """Explicit, independently-enforced limits for one reconnaissance run.

    The counters are monotone: a limit is checked before the work it limits, so
    `can_visit_page()` returning True always means the visit fits inside the
    budget. `limit_reached()` reports the FIRST exhausted limit, which is what
    the report uses to say why the crawl stopped rather than claiming it
    finished.
    """

    __slots__ = ("max_pages", "max_transitions", "max_depth", "max_seconds",
                 "max_model_calls", "max_stored_chars",
                 "pages_visited", "transitions", "model_calls",
                 "stored_chars", "_started_at")

    def __init__(self, *, max_pages=None, max_transitions=None, max_depth=None,
                 max_seconds=None, max_model_calls=None,
                 max_stored_chars=None):
        self.max_pages = _bounded_positive(
            max_pages, RECON_MAX_PAGES, "recon max pages")
        self.max_transitions = _bounded_positive(
            max_transitions, RECON_MAX_TRANSITIONS, "recon max transitions")
        self.max_depth = _bounded_positive(
            max_depth, RECON_MAX_DEPTH, "recon max depth")
        self.max_seconds = _bounded_positive(
            max_seconds, RECON_MAX_SECONDS, "recon max seconds")
        self.max_model_calls = _bounded_positive(
            max_model_calls, RECON_MAX_MODEL_CALLS, "recon max model calls")
        self.max_stored_chars = _bounded_positive(
            max_stored_chars, RECON_MAX_STORED_CHARS, "recon max stored chars")
        self.reset()

    def reset(self):
        self.pages_visited = 0
        self.transitions = 0
        self.model_calls = 0
        self.stored_chars = 0
        self._started_at = time.monotonic()
        return self

    # -- checks -------------------------------------------------------------

    def elapsed_seconds(self):
        return time.monotonic() - self._started_at

    def can_visit_page(self):
        return self.pages_visited < self.max_pages

    def can_transition(self):
        return self.transitions < self.max_transitions

    def can_go_deeper(self, depth):
        return depth < self.max_depth

    def within_time(self):
        return self.elapsed_seconds() < self.max_seconds

    def can_call_model(self):
        return self.model_calls < self.max_model_calls

    def can_store(self, chars):
        """Whether `chars` more of content still fit in the storage budget.

        Reports False rather than truncating mid-record: a half-written page
        summary is worse than an absent one, because it reads as if the page
        had less on it than it did.
        """
        if chars <= 0:
            return True
        return (self.stored_chars + chars) <= self.max_stored_chars

    # -- recording ----------------------------------------------------------

    def record_page(self):
        self.pages_visited += 1
        return self.pages_visited

    def record_transition(self):
        self.transitions += 1
        return self.transitions

    def record_model_call(self):
        self.model_calls += 1
        return self.model_calls

    def record_stored(self, chars):
        self.stored_chars += max(0, int(chars))
        return self.stored_chars

    def limit_reached(self):
        """Name the first exhausted limit, or None if the run is still live."""
        if not self.within_time():
            return ("time", f"wall-clock budget of {self.max_seconds}s reached")
        if self.pages_visited >= self.max_pages:
            return ("pages", f"page budget of {self.max_pages} reached")
        if self.transitions >= self.max_transitions:
            return ("transitions",
                    f"transition budget of {self.max_transitions} reached")
        if self.model_calls >= self.max_model_calls:
            return ("model_calls",
                    f"model-call budget of {self.max_model_calls} reached")
        if self.stored_chars >= self.max_stored_chars:
            return ("storage",
                    f"stored-content budget of {self.max_stored_chars} chars "
                    "reached")
        return None

    def to_dict(self):
        return {
            "max_pages": self.max_pages,
            "max_transitions": self.max_transitions,
            "max_depth": self.max_depth,
            "max_seconds": self.max_seconds,
            "max_model_calls": self.max_model_calls,
            "max_stored_chars": self.max_stored_chars,
            "pages_visited": self.pages_visited,
            "transitions": self.transitions,
            "model_calls": self.model_calls,
            "stored_chars": self.stored_chars,
        }


def _bounded_positive(value, default, label):
    """Coerce a budget to a positive int, warning rather than crashing.

    A malformed budget must not crash a run with a traceback, and must not be
    silently replaced by a number the user did not ask for.
    """
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        print(f"[Warning] {label} must be a whole number; got {value!r}. "
              f"Using {default}.")
        return default
    if parsed < 1:
        print(f"[Warning] {label} must be at least 1; got {parsed}. "
              f"Using {default}.")
        return default
    return parsed


# ---------------------------------------------------------------------------
# Stop reasons. These are the only ways a reconnaissance run ends, and every one
# of them is recorded and reported. There is no path that ends a run without a
# reason, because "it just stopped" is indistinguishable from "it thought it
# was done".
# ---------------------------------------------------------------------------

RECON_DONE_EXHAUSTED = "finished_frontier_exhausted"
RECON_DONE_PAGES = "page_budget_reached"
RECON_DONE_TRANSITIONS = "transition_budget_reached"
RECON_DONE_DEPTH = "depth_budget_reached"
RECON_DONE_TIME = "time_budget_reached"
RECON_DONE_MODEL_CALLS = "model_call_budget_reached"
RECON_DONE_STORAGE = "storage_budget_reached"
RECON_DONE_NO_CANDIDATES = "no_further_safe_navigation"
RECON_DONE_AUTH = "stopped_at_authentication"
RECON_DONE_CONSENT = "stopped_at_consent_gate"
RECON_DONE_ACCESS_CONTROL = "stopped_at_access_control"
RECON_DONE_CONSEQUENTIAL = "stopped_at_consequential_action"
RECON_DONE_START_UNREACHABLE = "start_url_unreachable"
RECON_DONE_ERROR = "stopped_by_error"

# A run that stopped for one of these reasons did NOT see the whole site, and
# the report says so in those words rather than implying coverage.
RECON_INCOMPLETE_REASONS = frozenset({
    RECON_DONE_PAGES, RECON_DONE_TRANSITIONS, RECON_DONE_DEPTH,
    RECON_DONE_TIME, RECON_DONE_MODEL_CALLS, RECON_DONE_STORAGE,
    RECON_DONE_NO_CANDIDATES, RECON_DONE_AUTH, RECON_DONE_CONSENT,
    RECON_DONE_ACCESS_CONTROL, RECON_DONE_CONSEQUENTIAL,
    RECON_DONE_START_UNREACHABLE, RECON_DONE_ERROR,
})


# ---------------------------------------------------------------------------
# Safety barriers.
#
# A barrier is a place where exploration stops and the area is recorded as
# unexplored. Detection is mechanical, from the observation's own controls and
# URL — never from a model's phrasing, and never from the page asserting that
# it needs something.
# ---------------------------------------------------------------------------

BARRIER_AUTH = "authentication"
BARRIER_CONSENT = "consent"
BARRIER_ACCESS_CONTROL = "access_control"
BARRIER_CONSEQUENTIAL = "consequential"


class ReconBarrier:
    """A boundary reconnaissance refused to cross, with the reason recorded."""

    __slots__ = ("kind", "reason", "url", "detail")

    def __init__(self, kind, reason, url="", detail=""):
        self.kind = kind
        self.reason = reason
        self.url = url or ""
        self.detail = detail or ""

    def to_dict(self):
        return {"kind": self.kind, "reason": self.reason, "url": self.url,
                "detail": self.detail}

    def __repr__(self):
        return f"ReconBarrier({self.kind!r}, {self.reason!r})"


# Consent surfaces, matched on what a control offers to do. A page that shows
# one of these is asking for a decision that belongs to the user, so the crawl
# stops there instead of clicking through it.
_CONSENT_MARKERS = (
    "accept all", "accept cookies", "accept & continue", "i agree",
    "agree to", "allow all", "consent", "cookie policy", "privacy policy",
    "terms of service", "terms of use", "gdpr", "sign up to continue",
    "continue as", "enable javascript", "we use cookies",
)

# Link hrefs that are not pages and must never be traversed.
_NON_PAGE_SCHEMES = ("mailto:", "tel:", "javascript:", "sms:", "callto:",
                     "data:", "blob:", "file:")


def detect_recon_barrier(observation, *, records=None, page_text=""):
    """Report the first safety barrier visible in an observation.

    Returns a `ReconBarrier`, or None when nothing in the observation marks a
    boundary. Order matters: access control is checked before consent and
    authentication because a challenge page frequently also carries cookie
    buttons, and reporting "consent" for a CAPTCHA would understate the wall.

    The access-control check reuses the task engine's own detector rather than
    duplicating it, so reconnaissance and task execution agree on where a
    boundary is and cannot drift apart.
    """
    if observation is None:
        return None

    url = getattr(observation, "url", "") or ""

    access = detect_access_control_barrier(observation)
    if access:
        return ReconBarrier(
            BARRIER_ACCESS_CONTROL,
            "the page presents an access-control challenge, which this tool "
            "does not solve, bypass, or work around",
            url=url, detail=str(access))

    records = list(records if records is not None
                   else (observation.discovery.get("records") or []))

    # Authentication: a password field, or a username field alongside one.
    has_password = False
    has_username = False
    for record in records:
        probe = " ".join(str(record.get(k) or "") for k in
                         ("name", "id", "placeholder", "aria_label", "type",
                          "autocomplete")).lower()
        if "password" in probe:
            has_password = True
        if record.get("type") in ("password",):
            has_password = True
        if "username" in probe or record.get("autocomplete") == "username":
            has_username = True
    if has_password:
        return ReconBarrier(
            BARRIER_AUTH,
            "the page asks for credentials; reconnaissance never types them "
            "and never signs in",
            url=url,
            detail="a password field is present"
                   + (" alongside a username field" if has_username else ""))

    # Consent: a control or page text offering to accept terms/cookies.
    for record in records:
        probe = " ".join(str(record.get(k) or "") for k in
                         ("name", "aria_label", "placeholder", "value",
                          "testid")).lower()
        for marker in _CONSENT_MARKERS:
            if marker in probe:
                return ReconBarrier(
                    BARRIER_CONSENT,
                    "the page offers a consent or terms decision that belongs "
                    "to the user; it was not accepted on their behalf",
                    url=url, detail=f"matched {marker!r} on a control")
    text_probe = (page_text or "").lower()
    if text_probe:
        for marker in _CONSENT_MARKERS:
            if marker in text_probe[:2000]:
                return ReconBarrier(
                    BARRIER_CONSENT,
                    "the page references a consent or terms decision that "
                    "belongs to the user; it was not accepted on their behalf",
                    url=url, detail=f"matched {marker!r} in page text")

    return None


def is_consequential_link(label, href=""):
    """Whether navigating to this link reads as a commitment.

    Mechanical, and biased toward "yes". A link whose text says it would place
    an order, delete something, or transfer money is not explored, even though
    following a URL technically cannot itself place an order — because the
    point of reconnaissance is to describe the site to a human, and a report
    that walked through a checkout flow reads as an endorsement of it.

    Returns (bool, reason). A non-empty reason means the link was left
    unexplored and the reason goes in the report.
    """
    haystack = f"{label or ''} {href or ''}".lower()
    if not haystack.strip():
        return True, "the link has no readable text to classify it by"
    for word in CONSEQUENTIAL_WORDS:
        if word in haystack:
            return True, (f"its text reads as a commitment (matched {word!r}); "
                          "not traversed, because reconnaissance does not walk "
                          "through consequential flows")
    return False, ""


def normalize_url(url, base=None):
    """Reduce a URL to a comparable form, dropping the fragment.

    The fragment is dropped because a `#section` anchor does not change which
    page is loaded, and keeping it would make one page look like several and
    waste the page budget on anchors. Query strings and the trailing slash are
    preserved: those genuinely can select different content.
    """
    if not url:
        return ""
    candidate = url.strip()
    if base:
        try:
            candidate = urljoin(base, candidate)
        except Exception:
            return ""
    try:
        # urldefrag returns (url_with_fragment, fragment); keep the first.
        clean, _fragment = urldefrag(candidate)
    except Exception:
        clean = candidate
    try:
        parts = urlparse(clean)
    except Exception:
        return clean
    # Percent-decoding keeps one page as one node whether its URL arrived
    # encoded (as a browser reports it) or literal (as it was typed).
    return urlunparse((parts.scheme, parts.netloc, unquote(parts.path or ""),
                       parts.params, parts.query, ""))


def _authority_of(url):
    """Return the comparable authority of a URL, or "" if it has none.

    http(s) URLs are identified by scheme+host+port. file:// URLs have an EMPTY
    authority — every local file shares netloc "" — so comparing them by
    authority would make every local file look off-site and put them all in one
    memory bucket. For those the containing directory is the meaningful scope,
    which is what makes a local fixture crawlable and keeps separate local sites
    from sharing memory.
    """
    try:
        parts = urlparse(url or "")
    except Exception:
        return ""
    scheme = (parts.scheme or "").lower()
    if parts.netloc:
        return f"{scheme}://{parts.netloc.lower()}"
    if scheme == "file":
        return f"file://{_file_root(url) or '/'}"
    return ""


def is_same_origin(url, origin_url):
    """Whether a URL belongs to the site `origin_url` defines.

    For http(s) that is scheme+host+port, so every path on the host is in
    scope. For file:// there is no authority at all, so the containing
    directory of the ORIGIN is the scope and a candidate is in scope when it
    lies beneath it — the same relationship a path has to a host. Comparing
    directory-to-directory instead would make a page in a subfolder look
    off-site, which is wrong for a web path and wrong for a local fixture.
    """
    try:
        candidate = urlparse(url or "")
        origin = urlparse(origin_url or "")
    except Exception:
        return False
    if not (origin.scheme or ""):
        return False
    if candidate.netloc or origin.netloc:
        return _authority_of(url) == _authority_of(origin_url)
    if (origin.scheme or "").lower() != "file":
        return False
    root = _file_root(origin_url)
    target = _file_root(url)
    return bool(root and target) and (target == root
                                      or target.startswith(root + "/"))


def _file_root(url):
    """The containing directory of a file:// URL, without a trailing slash.

    Percent-encoding is decoded first. A browser reports a path containing a
    space as `%20`, while a path typed on the command line keeps the literal
    space — and those are the same directory. Without decoding, one local site
    would land in two memory buckets depending on how its URL was written.
    """
    try:
        parts = urlparse(url or "")
    except Exception:
        return ""
    path = unquote(parts.path or "").replace("\\", "/")
    directory = os.path.dirname(path)
    return directory.rstrip("/").lower()


def is_traversable_link(href):
    """Whether an href could plausibly lead to another page.

    Filters out non-page schemes (mailto:, tel:, javascript: and friends) and
    bare fragments. Both would otherwise be queued as "pages" and burn the
    budget on things that are not pages.
    """
    raw = (href or "").strip()
    if not raw:
        return False
    if raw.startswith("#"):
        return False
    lowered = raw.lower()
    for scheme in _NON_PAGE_SCHEMES:
        if lowered.startswith(scheme):
            return False
    return True


def site_key_for(url):
    """A stable, non-identifying storage key for a site's scope.

    Derived from the site's authority — origin for a web site, containing
    directory for a local file — so every page of one site shares a memory
    bucket while two different sites never can. Hashed, so the filename on disk
    does not leak the URL the user was visiting.
    """
    authority = _authority_of(url)
    if not authority:
        authority = str(url or "unknown")
    digest = hashlib.sha256(authority.encode("utf-8")).hexdigest()[:16]
    return f"site_{digest}"


# ---------------------------------------------------------------------------
# Minimal in-browser discovery for reconnaissance.
#
# Separate from the task engine's DOM_EXTRACT_JS on purpose: reconnaissance
# needs links and form SHAPE, not the task engine's actionable-control list,
# and reusing that extractor here would blur the line between the two modes.
# Like it, values are never read out of sensitive fields — only the fact that
# such a field exists.
# ---------------------------------------------------------------------------

RECON_DISCOVERY_JS = """(args) => {
    const [maxLinks, maxLinkText, maxFields, sensitiveMarkers] = args;
    const links = [];
    let linkOverflow = 0;
    const forms = [];
    let hasPasswordField = false;

    function boundText(t) {
        const s = (t || '').replace(/\\s+/g, ' ').trim();
        return s.length > maxLinkText ? s.slice(0, maxLinkText) + '...' : s;
    }

    function isSensitive(el) {
        let probe = '';
        try {
            probe = [
                el.getAttribute('name'), el.id, el.getAttribute('id'),
                el.getAttribute('placeholder'), el.getAttribute('aria-label'),
                el.getAttribute('autocomplete'), el.getAttribute('type')
            ].filter(Boolean).join(' ').toLowerCase();
        } catch (e) { probe = ''; }
        if (!probe) return false;
        return sensitiveMarkers.some(m => probe.indexOf(m) !== -1);
    }

    function isVisible(el) {
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return false;
        const style = window.getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') return false;
        if (parseFloat(style.opacity || '1') === 0) return false;
        return true;
    }

    const anchorNodes = Array.from(document.querySelectorAll('a[href]'));
    for (const a of anchorNodes) {
        const href = a.getAttribute('href') || '';
        if (!href) continue;
        if (!isVisible(a)) continue;
        let resolved = '';
        try { resolved = new URL(href, window.location.href).href; }
        catch (e) { resolved = href; }
        if (links.length < maxLinks) {
            links.push({
                href: href,
                resolved: resolved,
                text: boundText(a.innerText || a.textContent || a.getAttribute('aria-label') || ''),
                title: boundText(a.getAttribute('title') || ''),
                rel: (a.getAttribute('rel') || '')
            });
        } else {
            linkOverflow += 1;
        }
    }

    // Form SHAPE only: field names, types, and whether a sensitive field is
    // present. No field value is ever read, so nothing personal is captured by
    // recording that a form exists.
    const formNodes = Array.from(document.querySelectorAll('form'));
    for (const form of formNodes) {
        const fields = [];
        let fieldOverflow = 0;
        const fieldNodes = Array.from(
            form.querySelectorAll('input, select, textarea'));
        for (const field of fieldNodes) {
            const type = (field.getAttribute('type') || field.tagName.toLowerCase()).toLowerCase();
            const name = boundText(
                field.getAttribute('name') || field.getAttribute('id') ||
                field.getAttribute('aria-label') ||
                field.getAttribute('placeholder') || field.tagName.toLowerCase());
            const sensitive = isSensitive(field);
            if (sensitive) {
                hasPasswordField = true;
                // Name and type are safe; the value never leaves the browser.
                fields.push({name: name, type: type, sensitive: true, value: 'redacted'});
            } else if (fields.length < maxFields) {
                fields.push({name: name, type: type, sensitive: false, value: 'omitted'});
            } else {
                fieldOverflow += 1;
            }
        }
        let action = '';
        try { action = form.getAttribute('action') || ''; } catch (e) { action = ''; }
        const method = (form.getAttribute('method') || 'get').toLowerCase();
        const submitCount = form.querySelectorAll(
            'button[type=submit], input[type=submit], button:not([type])').length;
        forms.push({
            fields: fields,
            field_overflow: fieldOverflow,
            action: boundText(action),
            method: method,
            submit_count: submitCount,
            has_password: fields.some(f => f.sensitive)
        });
    }

    let title = '';
    try { title = document.title || ''; } catch (e) { title = ''; }

    return {
        url: window.location.href,
        title: title,
        links: links,
        link_overflow: linkOverflow,
        forms: forms,
        has_password_field: hasPasswordField
    };
}"""


async def recon_discover_page(page, *, max_links=None, max_fields=None):
    """Read links and form shape off the live page. Never raises.

    On failure it returns an empty discovery tagged with `extraction_failed`,
    so the caller can tell "this page genuinely has no links" apart from "we
    could not read this page" — the same distinction the task engine insists on.
    """
    _links = max_links or RECON_MAX_LINKS_PER_PAGE
    _fields = max_fields or RECON_MAX_FORM_FIELDS
    try:
        raw = await page.evaluate(
            RECON_DISCOVERY_JS,
            [_links, RECON_MAX_LINK_TEXT_CHARS, _fields,
             list(SENSITIVE_FIELD_MARKERS)],
        )
    except Exception as exc:
        return {"url": "", "title": "", "links": [], "link_overflow": 0,
                "forms": [], "has_password_field": False,
                "extraction_failed": f"{type(exc).__name__}: {exc}"}
    if not isinstance(raw, dict):
        return {"url": "", "title": "", "links": [], "link_overflow": 0,
                "forms": [], "has_password_field": False,
"extraction_failed": "discovery returned a non-object"}
    raw.setdefault("links", [])
    raw.setdefault("forms", [])
    raw["extraction_failed"] = None
    return raw


# ---------------------------------------------------------------------------
# The website graph.
#
# Deliberately a plain in-memory structure, not a graph library. The project
# needs three things a library would not give for free: a node identity that is
# NOT the URL, an edge that records whether it was walked or merely seen, and a
# hard separation between what was observed and what was inferred. All three are
# small; the bookkeeping is what matters, and a framework would mostly obscure
# it.
#
# The identity rule matters most on a dynamic application, where one URL
# renders a different page depending on what the user did. `compute_full_node_id`
# combines the structural hash with the semantic signature, so `/orders` before
# and after an item is added are two nodes with one URL between them. Keying on
# URL alone would collapse them and report one page where there were two.
# ---------------------------------------------------------------------------

# How an edge relates two nodes. These are kept distinct because "we clicked it
# and landed here" and "there is a link that appears to go there" support very
# different claims, and a report that blurs them is overstating what was seen.
EDGE_TRAVERSED = "traversed"      # walked, destination observed
EDGE_OBSERVED = "observed"        # a link to it exists on the page; not walked
EDGE_INFERRED = "inferred"        # a relationship deduced, never walked
EDGE_BLOCKED = "blocked"          # deliberately not walked, with a reason

# Whether a claim rests on something that happened or on reasoning about it.
FACT = "observed_fact"
INFERENCE = "inference"

# Confidence bands. Deliberately coarse: reconnaissance cannot measure
# probability, and a two-decimal float here would be a number nobody earned.
CONFIDENCE_CONFIRMED = "confirmed"      # observed directly, on this run
CONFIDENCE_LIKELY = "likely"            # observed once, or inferred from a
                                        # well-supported link
CONFIDENCE_UNCERTAIN = "uncertain"      # suggested, never walked or verified
CONFIDENCE_UNKNOWN = "unknown"


class ReconNode:
    """One page-state observed during reconnaissance.

    A node is a page AND an application state. It records where it was seen,
    how deep it was, what was on it, and what is known about it that is not
    directly observed — each fact tagged so the report can tell them apart.
    """

    __slots__ = ("node_id", "url", "title", "depth", "first_seen", "last_seen",
                 "visit_count", "observation_ids", "interactive_labels",
                 "forms", "semantic_signature", "inferred_role", "barrier",
                 "notes", "observation_summaries")

    def __init__(self, node_id, url="", title="", depth=0, *, first_seen=None,
                 observation_id=None):
        self.node_id = node_id
        self.url = url or ""
        self.title = title or ""
        self.depth = depth
        self.first_seen = first_seen
        self.last_seen = first_seen
        self.visit_count = 1
        self.observation_ids = [observation_id] if observation_id is not None \
            else []
        # Labels seen on the page. Bounded by the caller, never by chance.
        self.interactive_labels = []
        # Form SHAPE, never form values.
        self.forms = []
        self.semantic_signature = None
        # What this page probably IS (login, listing, detail...). Always an
        # inference, never a fact, and labelled as such wherever it is shown.
        self.inferred_role = None
        # A barrier that stopped exploration here, if any.
        self.barrier = None
        self.notes = []
        # Bounded history of what was seen on each visit, so a revisit that
        # changes the page updates the node instead of being ignored.
        self.observation_summaries = []

    def record_visit(self, *, url=None, title=None, timestamp=None,
                     observation_id=None, interactive_labels=None,
                     forms=None, semantic_signature=None, depth=None):
        """Update the node from a fresh observation of the same state.

        A revisit is a first-class event, not a no-op: `visit_count` and the
        retained observation history are what let a later report say "this page
        was seen three times and changed after the second visit" instead of
        collapsing three sightings into one.
        """
        self.visit_count += 1
        self.last_seen = timestamp if timestamp is not None else self.last_seen
        if url:
            self.url = url
        if title:
            self.title = title
        if depth is not None:
            self.depth = min(self.depth, depth)
        if observation_id is not None:
            self.observation_ids.append(observation_id)
        if semantic_signature is not None:
            self.semantic_signature = semantic_signature
        if interactive_labels:
            merged = list(self.interactive_labels)
            for label in interactive_labels:
                if label not in merged:
                    merged.append(label)
            self.interactive_labels = merged
        if forms:
            merged_forms = list(self.forms)
            for form in forms:
                if form not in merged_forms:
                    merged_forms.append(form)
            self.forms = merged_forms
        return self

    def to_dict(self):
        return {
            "node_id": self.node_id,
            "url": self.url,
            "title": self.title,
            "depth": self.depth,
            "visit_count": self.visit_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "observation_ids": list(self.observation_ids),
            "interactive_labels": list(self.interactive_labels),
            "forms": [dict(f) for f in self.forms],
            "semantic_signature": self.semantic_signature,
            "inferred_role": self.inferred_role,
            "inferred_role_basis": INFERENCE,
            "barrier": self.barrier,
            "observation_summaries": list(self.observation_summaries),
        }


class ReconEdge:
    """A relationship between two page-states, and how well it is known.

    `kind` says what happened; `basis` says what the claim rests on. An edge is
    only ever TRAVERSED once a destination node has actually been observed
    arriving from this edge — a link's presence is not traversal, and the two
    are never conflated here.
    """

    __slots__ = ("source_id", "target_id", "href", "label", "kind", "basis",
                 "confidence", "target_url", "reason", "traversed_at",
                 "attempt_count", "detail")

    def __init__(self, source_id, target_id=None, href="", label="", *,
                 kind=EDGE_OBSERVED, basis=FACT, confidence=CONFIDENCE_LIKELY,
                 target_url="", reason=""):
        self.source_id = source_id
        self.target_id = target_id
        self.href = href or ""
        self.label = label or ""
        self.kind = kind
        self.basis = basis
        self.confidence = confidence
        self.target_url = target_url or ""
        # Why an edge was left unwalked, when it was.
        self.reason = reason or ""
        self.traversed_at = None
        self.attempt_count = 0
        self.detail = ""

    @property
    def is_traversed(self):
        return self.kind == EDGE_TRAVERSED

    def mark_traversed(self, target_id, *, timestamp=None, detail=""):
        self.kind = EDGE_TRAVERSED
        self.basis = FACT
        self.confidence = CONFIDENCE_CONFIRMED
        self.target_id = target_id
        self.traversed_at = timestamp
        if detail:
            self.detail = detail
        return self

    def to_dict(self):
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "href": self.href,
            "label": self.label,
            "kind": self.kind,
            "basis": self.basis,
            "confidence": self.confidence,
            "target_url": self.target_url,
            "reason": self.reason,
            "traversed_at": self.traversed_at,
            "attempt_count": self.attempt_count,
            "is_traversed": self.is_traversed,
        }


class ReconGraph:
    """Nodes and edges discovered by one reconnaissance run.

    Nodes are keyed by their structural+semantic identity, not by URL, so the
    same URL in two different application states stays two nodes. Edges are
    deduplicated on (source, target-or-url, label) so that a navigation link
    repeated in a header and a footer is one relationship with a count, not two
    relationships that inflate the site map.
    """

    def __init__(self, start_url=""):
        self.start_url = start_url or ""
        self.nodes = {}
        self.edges = []
        # (source, key) -> edge, for deduplication.
        self._edge_index = {}
        # URL -> set of node ids seen at that URL. Lets the report say "this URL
        # rendered N distinct states" instead of implying one URL is one page.
        self.url_index = {}
        # Areas deliberately not explored, each with a reason.
        self.unexplored = []

    # -- nodes --------------------------------------------------------------

    def add_node(self, node_id, *, url="", title="", depth=0, timestamp=None,
                 observation_id=None, interactive_labels=None, forms=None,
                 semantic_signature=None):
        """Add a node, or fold the visit into the node that already exists."""
        if node_id in self.nodes:
            node = self.nodes[node_id]
            node.record_visit(url=url, title=title, timestamp=timestamp,
                              observation_id=observation_id,
                              interactive_labels=interactive_labels,
                              forms=forms, semantic_signature=semantic_signature,
                              depth=depth)
        else:
            node = ReconNode(node_id, url=url, title=title, depth=depth,
                             first_seen=timestamp,
                             observation_id=observation_id)
            node.interactive_labels = list(interactive_labels or [])
            node.forms = list(forms or [])
            node.semantic_signature = semantic_signature
            self.nodes[node_id] = node
        if url:
            self.url_index.setdefault(url, set()).add(node_id)
        return node

    def get(self, node_id):
        return self.nodes.get(node_id)

    def mark_barrier(self, node_id, barrier):
        node = self.nodes.get(node_id)
        if node is not None:
            node.barrier = barrier.to_dict() if barrier else None
        return node

    def note(self, node_id, text):
        node = self.nodes.get(node_id)
        if node is not None:
            node.notes.append(text)
        return node

    def set_inferred_role(self, node_id, role):
        """Attach an inferred description of what a page appears to be.

        Stored separately from observed labels so the report can present it as
        an inference. Reconnaissance may suggest what a page is for; it never
        asserts it.
        """
        node = self.nodes.get(node_id)
        if node is not None:
            node.inferred_role = role
        return node

    def states_for_url(self, url):
        return sorted(self.url_index.get(url, set()))

    def node_at_url(self, url):
        """A single node id at a URL, or None when it is ambiguous or absent.

        Returning None for an ambiguous URL is deliberate: the two states at one
        URL are genuinely different pages, and quietly picking one would let a
        caller act on a state it did not identify.
        """
        ids = self.url_index.get(url)
        if not ids or len(ids) != 1:
            return None
        return next(iter(ids))

    # -- edges --------------------------------------------------------------

    @staticmethod
    def _edge_key(source_id, label, target_url, href):
        return (source_id, (label or "").strip().lower(),
                target_url or "", href or "")

    def add_edge(self, source_id, *, target_id=None, href="", label="",
                 kind=EDGE_OBSERVED, basis=FACT,
                 confidence=CONFIDENCE_LIKELY, target_url="", reason=""):
        """Add or update an edge. Duplicate links fold into one relationship."""
        key = self._edge_key(source_id, label, target_url, href)
        existing = self._edge_index.get(key)
        if existing is not None:
            existing.attempt_count += 1
            # A later, better-evidenced view of the same relationship wins:
            # traversed beats observed, and confirmed beats likely.
            if _rank(kind) > _rank(existing.kind):
                existing.kind = kind
                existing.basis = basis
                existing.confidence = confidence
                if target_id:
                    existing.target_id = target_id
            return existing
        edge = ReconEdge(source_id, target_id=target_id, href=href, label=label,
                         kind=kind, basis=basis, confidence=confidence,
                         target_url=target_url, reason=reason)
        edge.attempt_count = 1
        self._edge_index[key] = edge
        self.edges.append(edge)
        return edge

    def mark_edge_traversed(self, edge, target_id, *, timestamp=None):
        return edge.mark_traversed(target_id, timestamp=timestamp)

    def edges_from(self, node_id, *, kind=None):
        return [e for e in self.edges
                if e.source_id == node_id and (kind is None or e.kind == kind)]

    def edges_to(self, node_id):
        return [e for e in self.edges if e.target_id == node_id]

    # -- unexplored ---------------------------------------------------------

    def add_unexplored(self, url, reason, *, source_node_id="", kind=""):
        """Record an area deliberately not explored, and why.

        Storing the reason matters more than the URL. A report that lists gaps
        without reasons reads as incompleteness; one that lists them with
        reasons reads as a decision, which is what it was.
        """
        self.unexplored.append({
            "url": url or "",
            "reason": reason or "",
            "source_node_id": source_node_id or "",
            "kind": kind or "",
        })
        return self.unexplored

    # -- export -------------------------------------------------------------

    @property
    def traversed_edge_count(self):
        return sum(1 for e in self.edges if e.is_traversed)

    def urls_with_multiple_states(self):
        """URLs that rendered more than one distinct state.

        This is the concrete payoff of not keying on URL: a same-URL/different-
        state entry is exactly the dynamic-application case the identity rule
        exists to handle.
        """
        return sorted(u for u, ids in self.url_index.items() if len(ids) > 1)

    def to_dict(self):
        return {
            "start_url": self.start_url,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
            "unexplored": list(self.unexplored),
            "stats": {
                "node_count": len(self.nodes),
                "edge_count": len(self.edges),
                "traversed_edges": self.traversed_edge_count,
                "observed_only_edges": sum(
                    1 for e in self.edges if e.kind == EDGE_OBSERVED),
                "blocked_edges": sum(1 for e in self.edges
                                     if e.kind == EDGE_BLOCKED),
                "urls_with_multiple_states": len(
                    self.urls_with_multiple_states()),
                "unexplored_count": len(self.unexplored),
            },
        }


def _rank(kind):
    """Ordering used when two observations of one relationship disagree."""
    return {EDGE_INFERRED: 0, EDGE_OBSERVED: 1, EDGE_BLOCKED: 1,
            EDGE_TRAVERSED: 2}.get(kind, 0)


def infer_page_role(node, forms=None):
    """Suggest what a page appears to be for.

    Every value returned here is an INFERENCE from observed structure — a
    password field suggests a login page, it does not make one — and the caller
    is expected to record it through `set_inferred_role`, which labels it.
    Returns "" when nothing is distinctive enough to guess, because a guess with
    no basis is worse than no guess.
    """
    forms = forms or node.forms or []
    labels = " ".join(node.interactive_labels or []).lower()
    title = (node.title or "").lower()
    haystack = f"{title} {labels}"

    if any(f.get("has_password") for f in forms):
        return "authentication"
    for marker in ("sign in", "log in", "login", "register", "sign up",
                   "create account"):
        if marker in haystack:
            return "authentication"
    for marker in ("add to cart", "checkout", "buy now", "place order",
                   "cart", "basket", "shopping"):
        if marker in haystack:
            return "commerce"
    for marker in ("search", "filter", "sort by"):
        if marker in haystack:
            return "search_or_listing"
    for marker in ("add to", "new", "create", "submit", "save"):
        if marker in haystack:
            return "entry_or_form"
    return ""


# ---------------------------------------------------------------------------
# Persistent website memory.
#
# THE SMALLEST VIABLE IMPLEMENTATION, DELIBERATELY.
#
# This is one JSON file per site. Not a database, not an index, not an
# embedding store. The project's actual requirement is "remember a few verified
# facts per site, forget them when they go stale, and never leak them sideways"
# — and a single readable, inspectable, deletable file satisfies that in full.
# Anything heavier would add a schema migration story, a corruption story, and
# an operator story, in exchange for lookup speed this workload does not need.
# The file is also what makes the safety properties testable: a test can assert
# on exactly what was persisted, which it cannot do against an opaque store.
#
# MEMORY IS A HINT, NEVER AN AUTHORITY.
# Nothing stored here can establish a fact about the current page. It records
# what was true when it was written; the live observation is what is true now.
# Every retrieval is therefore stamped with its freshness, and every suggestion
# is explicitly marked advisory. A run that skips revalidation and treats memory
# as current is a bug, not a fast path.
# ---------------------------------------------------------------------------

MEMORY_SCHEMA_VERSION = 1
MEMORY_DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60  # one week
MEMORY_DEFAULT_DIR = "recon_memory"


# --------------------------------------------------------------------------
# Containment advice for a custom memory directory.
#
# This warns; it never redirects and never refuses. Memory is written exactly
# where the operator asked, because silently moving a store is worse than
# telling someone their chosen location is shareable. The warning exists
# because `--recon-memory-dir` accepts any path, creates it if missing, and
# an in-repository location that nothing ignores is one `git add .` away from
# publishing per-site reconnaissance data about a third-party site.
# --------------------------------------------------------------------------

def _git_toplevel(cwd):
    try:
        completed = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", "replace").strip() or None


def _git_ignores(path, cwd):
    """True/False, or None when Git could not be consulted."""
    try:
        completed = subprocess.run(
            ["git", "-C", cwd, "check-ignore", "-q", path],
            capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    return None


def _is_within(child, parent):
    # realpath, not abspath: Windows short names (C:\Users\YUVRAJ~1) and long
    # names (C:\Users\YUVRAJ SINGH) denote one directory but never compare
    # equal, so abspath here would report every path as "outside the project"
    # and suppress the warning entirely.
    try:
        real_child = os.path.realpath(child)
        real_parent = os.path.realpath(parent)
        common = os.path.commonpath([real_child, real_parent])
    except ValueError:      # different drives on Windows
        return False
    return os.path.normcase(common) == os.path.normcase(real_parent)


def warn_if_memory_dir_is_shared(directory, cwd=None, project_root=None):
    """Warn once, at construction, if this store would live inside the project
    and nothing ignores it. Returns the warning text, or None if none is due.

    `project_root` is injectable so callers and tests share one decision
    without depending on where git happens to discover a repository. Prints
    nothing itself. The message names the directory and nothing else -- never
    a record, a site URL or any stored value.
    """
    cwd = cwd or os.getcwd()
    if directory == MEMORY_DEFAULT_DIR:
        return None                     # the default is ignored by policy

    absolute = os.path.realpath(os.path.join(cwd, directory))
    if project_root is None:
        project_root = _git_toplevel(cwd) or cwd
    if not _is_within(absolute, project_root):
        return None                     # outside the project: not shareable

    ignored = _git_ignores(directory, cwd)
    if ignored is True:
        return None
    if ignored is False:
        return (f"[Memory] WARNING: memory directory {directory!r} is inside "
                f"this project and is not ignored by git. It will hold "
                f"per-site reconnaissance data. Add it to .gitignore, or pass "
                f"--recon-memory-dir with a path outside the repository, "
                f"before committing.")
    return (f"[Memory] WARNING: memory directory {directory!r} is inside this "
            f"project and git could not be consulted to confirm it is "
            f"ignored. Confirm it stays out of version control.")

# Record kinds. Kept as named constants so a reader can see the whole vocabulary
# of what this store is allowed to contain.
MEM_PAGE_SUMMARY = "page_summary"
MEM_NAVIGATION = "navigation"
MEM_FORM_SHAPE = "form_shape"
MEM_OUTCOME = "outcome"
MEM_FAILURE_POINT = "failure_point"

MEMORY_KINDS = (MEM_PAGE_SUMMARY, MEM_NAVIGATION, MEM_FORM_SHAPE, MEM_OUTCOME,
                MEM_FAILURE_POINT)

# Freshness states returned by recall().
MEM_FRESH = "fresh"
MEM_AGING = "aging"        # past half the TTL, still usable, flagged
MEM_STALE = "stale"        # past the TTL; must be revalidated before use
MEM_EXPIRED = "expired"

# Revalidation outcomes.
REV_MATCH = "matches_current"
REV_CHANGED = "changed_since_recording"
REV_STALE = "stale_not_revalidated"
REV_ABSENT = "not_present_now"

# Value patterns that must never be written, whatever the surrounding structure
# claims them to be. Matching is on the KEY first and the VALUE second, because
# a secret is usually named honestly by its field.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"^eyJ[A-Za-z0-9_\-]{10,}\."),        # JWT
    re.compile(r"^(sk|pk|ghp|gho|xox[baprs])[-_]"),  # common API key prefixes
    re.compile(r"^[0-9a-f]{32,}$", re.I),             # hex digests / hashes
    re.compile(r"^\d{13,19}$"),                       # card-length digit runs
)

# Replaced in-place wherever a secret-shaped value is found, rather than stored.
_REDACTED = "[redacted: not stored]"


def looks_secret(key, value):
    """Whether a (key, value) pair is something this store must not keep.

    Two-sided on purpose. Key-side catches the honest name ("password",
    "session_token"); value-side catches the dishonest one (a token pasted under
    "note"). Returning True for either is the safe direction: refusing to store
    a harmless string costs a line of re-discovery, while storing a credential
    costs an account.
    """
    key_text = str(key or "").lower()
    for marker in SENSITIVE_FIELD_MARKERS:
        if marker in key_text:
            return True
    # Separators are normalised to spaces first: a word-boundary match would
    # miss "session_cookie" and "api-key", which is precisely the naming a real
    # site would use for exactly the values that must not be stored.
    normalised = re.sub(r"[^a-z0-9]+", " ", key_text)
    for marker in ("credential", "password", "passphrase", "cookie", "session",
                   "token", "secret", "private key", "bearer", "jwt"):
        if marker in normalised:
            return True
    text = str(value if value is not None else "").strip()
    if not text:
        return False
    if text.lower() in ("present", "absent", "redacted", "omitted"):
        return False
    for pattern in _SECRET_VALUE_PATTERNS:
        if pattern.match(text):
            return True
    return False


def redact_for_storage(value, _depth=0):
    """Return a copy of `value` with secret-shaped entries replaced.

    Applied on the way IN, so nothing sensitive is ever written to disk even if
    a caller hands memory a structure it should not have. Bounded in depth so a
    deeply nested or self-referential structure cannot turn redaction into a
    hang; past the bound the value is replaced rather than walked, because an
    unbounded structure is itself a reason not to store it.
    """
    if _depth > 6:
        return _REDACTED
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if looks_secret(key, item):
                out[key] = _REDACTED
            else:
                out[key] = redact_for_storage(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_for_storage(item, _depth + 1) for item in value]
    if isinstance(value, str) and looks_secret("value", value):
        return _REDACTED
    return value


class MemoryRecord:
    """One remembered fact, with the provenance that makes it checkable.

    `confidence` and `source_observation` are not decoration. A fact with no
    provenance cannot be revalidated, and a fact that cannot be revalidated
    cannot be distinguished from a guess once the site changes.
    """

    __slots__ = ("key", "kind", "value", "confidence", "source_observation",
                 "source_url", "created_at", "last_seen", "access_count",
                 "site_key", "namespace")

    def __init__(self, key, kind, value, *, confidence=CONFIDENCE_LIKELY,
                 source_observation="", source_url="", created_at=None,
                 site_key="", namespace=""):
        self.key = key
        self.kind = kind
        self.value = value
        self.confidence = confidence
        self.source_observation = source_observation or ""
        self.source_url = source_url or ""
        self.created_at = created_at if created_at is not None else time.time()
        self.last_seen = self.created_at
        self.access_count = 0
        self.site_key = site_key or ""
        self.namespace = namespace or ""

    def age_seconds(self, now=None):
        now = time.time() if now is None else now
        return max(0.0, now - self.created_at)

    def freshness(self, now=None, ttl_seconds=None):
        """Classify this record's age against the TTL.

        Four states, not two, because "recent" and "unverified-old" need
        different treatment: aging records may still be suggested with a warning,
        stale records must be revalidated before use, and expired ones should
        not be returned at all.
        """
        ttl = MEMORY_DEFAULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        age = self.age_seconds(now)
        if age >= ttl:
            return MEM_EXPIRED
        if age >= ttl / 2:
            return MEM_AGING
        return MEM_FRESH

    def touch(self, now=None):
        self.last_seen = time.time() if now is None else now
        self.access_count += 1
        return self

    def downgrade(self, new_confidence):
        """Lower confidence without deleting.

        A contradicted memory is worth keeping: "this used to be true and now is
        not" is more useful to a future run than an absence, and it stops the
        same stale claim being re-learned as if it were new.
        """
        if _confidence_rank(new_confidence) < _confidence_rank(self.confidence):
            self.confidence = new_confidence
        return self

    def to_dict(self):
        return {
            "key": self.key,
            "kind": self.kind,
            "value": self.value,
            "confidence": self.confidence,
            "source_observation": self.source_observation,
            "source_url": self.source_url,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "access_count": self.access_count,
            "site_key": self.site_key,
            "namespace": self.namespace,
        }

    @classmethod
    def from_dict(cls, data):
        record = cls(
            data.get("key"), data.get("kind"), data.get("value"),
            confidence=data.get("confidence", CONFIDENCE_LIKELY),
            source_observation=data.get("source_observation", ""),
            source_url=data.get("source_url", ""),
            created_at=data.get("created_at"),
            site_key=data.get("site_key", ""),
            namespace=data.get("namespace", ""),
        )
        record.last_seen = data.get("last_seen", record.created_at)
        record.access_count = int(data.get("access_count", 0) or 0)
        return record


def _confidence_rank(confidence):
    return {CONFIDENCE_UNKNOWN: 0, CONFIDENCE_UNCERTAIN: 1,
            CONFIDENCE_LIKELY: 2, CONFIDENCE_CONFIRMED: 3}.get(confidence, 1)


class SiteMemory:
    """Optional, isolated, inspectable, deletable per-site memory.

    Isolation is enforced three ways, because silently sharing what a user did
    on one site with another site is the exact failure this is meant to prevent:

      1. The storage file is keyed by a hash of the site ORIGIN, so different
         sites cannot reach each other's files in the first place.
      2. Every record carries its `site_key` and `namespace`, and loading drops
         anything that does not match the store it was loaded into — so a
         hand-edited or copied file cannot inject another site's facts.
      3. `recall()` filters by the same key, so even an in-memory mix-up returns
         nothing rather than the wrong site's data.
    """

    def __init__(self, site_url="", *, directory=None, enabled=True,
                 ttl_seconds=None, namespace=""):
        self.site_url = site_url or ""
        self.site_key = site_key_for(self.site_url) if self.site_url else ""
        self.namespace = namespace or ""
        self.enabled = bool(enabled)
        self.ttl_seconds = (MEMORY_DEFAULT_TTL_SECONDS if ttl_seconds is None
                            else int(ttl_seconds))
        self.directory = directory or MEMORY_DEFAULT_DIR
        self.records = {}
        # Raised once, here, because __init__ runs once per run. Warning inside
        # save() or remember() would repeat on every write.
        self.containment_warning = warn_if_memory_dir_is_shared(self.directory)
        if self.containment_warning:
            print(self.containment_warning)
        # Counters an operator can inspect: what was refused, and why.
        self.rejected_secrets = 0
        self.dropped_foreign = 0
        self.expired_removed = 0

    # -- storage ------------------------------------------------------------

    def storage_path(self):
        """Path to this site's memory file, or None when memory is disabled."""
        if not self.enabled or not self.site_key:
            return None
        name = f"{self.site_key}"
        if self.namespace:
            name = f"{name}.{_slug(self.namespace)}"
        return os.path.join(self.directory, f"{name}.json")

    def exists(self):
        path = self.storage_path()
        return bool(path and os.path.exists(path))

    def load(self):
        """Load this site's records, dropping anything not ours.

        A corrupt or unreadable file is reported and treated as empty, never as
        a reason to crash a run: memory is an optimisation, and an optimisation
        is not allowed to be a dependency.
        """
        self.records = {}
        self.dropped_foreign = 0
        path = self.storage_path()
        if not path or not os.path.exists(path):
            return self
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:
            print(f"[Memory] Could not read {path} ({type(exc).__name__}: "
                  f"{exc}); continuing with no memory for this site.")
            return self
        if not isinstance(data, dict):
            print(f"[Memory] {path} is not a memory document; ignoring it.")
            return self
        if data.get("schema_version") != MEMORY_SCHEMA_VERSION:
            print(f"[Memory] {path} uses schema "
                  f"{data.get('schema_version')!r}, expected "
                  f"{MEMORY_SCHEMA_VERSION}; ignoring it rather than guessing.")
            return self
        for raw in data.get("records") or []:
            if not isinstance(raw, dict):
                continue
            record = MemoryRecord.from_dict(raw)
            if not self._owns(record):
                self.dropped_foreign += 1
                continue
            self.records[self._key_for(record)] = record
        return self

    def save(self):
        """Persist this site's records. Returns the path, or None if disabled."""
        path = self.storage_path()
        if not path:
            return None
        try:
            os.makedirs(self.directory, exist_ok=True)
            document = {
                "schema_version": MEMORY_SCHEMA_VERSION,
                "site_key": self.site_key,
                "namespace": self.namespace,
                "site_url": self.site_url,
                "written_at": time.time(),
                "records": [r.to_dict() for r in self.records.values()],
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, default=str)
        except Exception as exc:
            print(f"[Memory] Could not write {path} ({type(exc).__name__}: "
                  f"{exc}); this run's memory was not persisted.")
            return None
        return path

    def _owns(self, record):
        """Whether a record belongs to THIS store.

        A record with no site key at all is treated as foreign rather than
        adopted: guessing that an unlabelled fact is ours is precisely how
        unrelated sites end up sharing memory.
        """
        if record.site_key and record.site_key != self.site_key:
            return False
        if not record.site_key:
            return False
        if record.namespace != self.namespace:
            return False
        return True

    @staticmethod
    def _key_for(record):
        return f"{record.kind}:{record.key}"

    # -- writing ------------------------------------------------------------

    def remember(self, key, value, *, kind=MEM_PAGE_SUMMARY,
                 confidence=CONFIDENCE_LIKELY, source_observation="",
                 source_url=""):
        """Store one verified fact. Refuses anything secret-shaped.

        Returns the stored `MemoryRecord`, or None when the write was refused —
        in which case the caller has learned nothing new but also persisted
        nothing harmful.
        """
        if not self.enabled:
            return None
        if kind not in MEMORY_KINDS:
            print(f"[Memory] Refusing to store unknown record kind {kind!r}.")
            return None
        if looks_secret(key, value):
            self.rejected_secrets += 1
            print(f"[Memory] Refusing to store {key!r}: it is credential- or "
                  "token-shaped. Nothing was written.")
            return None
        safe_value = redact_for_storage(value)
        now = time.time()
        record_key = self._key_for(MemoryRecord(key, kind, None))
        existing = self.records.get(record_key)
        if existing is not None:
            # Rewriting an existing fact refreshes it and keeps the original
            # creation time, so "first seen" stays honest.
            existing.value = safe_value
            existing.last_seen = now
            existing.confidence = confidence
            existing.source_observation = source_observation or \
                existing.source_observation
            existing.source_url = source_url or existing.source_url
            return existing
        record = MemoryRecord(key, kind, safe_value, confidence=confidence,
                              source_observation=source_observation,
                              source_url=source_url, created_at=now,
                              site_key=self.site_key,
                              namespace=self.namespace)
        self.records[record_key] = record
        return record

    # -- reading ------------------------------------------------------------

    def recall(self, *, kind=None, key=None, include_expired=False, now=None):
        """Return matching records, each stamped with its freshness.

        Expired records are omitted by default: a caller that wants them has to
        say so, so "this was old" cannot reach an action by accident.
        """
        if not self.enabled:
            return []
        now = time.time() if now is None else now
        out = []
        for record in self.records.values():
            if not self._owns(record):
                continue
            if kind is not None and record.kind != kind:
                continue
            if key is not None and record.key != key:
                continue
            state = record.freshness(now, self.ttl_seconds)
            if state == MEM_EXPIRED and not include_expired:
                continue
            record.touch(now)
            out.append((record, state))
        return out

    def revalidate(self, record, current_value=None, *, now=None):
        """Check a remembered fact against what is on the page right now.

        Returns (outcome, record). A stale record MUST go through this before it
        influences anything — that is the whole point of storing a timestamp.
        """
        now = time.time() if now is None else now
        state = record.freshness(now, self.ttl_seconds)
        if state == MEM_EXPIRED:
            return (REV_STALE, record.downgrade(CONFIDENCE_UNKNOWN))
        if current_value is None:
            return (REV_STALE, record)
        if current_value == record.value:
            record.last_seen = now
            return (REV_MATCH, record)
        # The site changed. Downgrade rather than delete: the contradiction is
        # itself the useful fact, and it must not be re-learned as new.
        return (REV_CHANGED, record.downgrade(CONFIDENCE_UNKNOWN))

    def expire(self, now=None):
        """Delete records past the TTL. Returns how many were removed."""
        now = time.time() if now is None else now
        doomed = [k for k, r in self.records.items()
                  if r.freshness(now, self.ttl_seconds) == MEM_EXPIRED]
        for key in doomed:
            del self.records[key]
        self.expired_removed += len(doomed)
        return len(doomed)

    def clear(self):
        """Forget everything for this site. Returns how many records went."""
        count = len(self.records)
        self.records = {}
        path = self.storage_path()
        if path and os.path.exists(path):
            try:
                os.remove(path)
                return count
            except Exception as exc:
                print(f"[Memory] Cleared in memory but could not delete {path} "
                      f"({type(exc).__name__}: {exc}).")
                return count
        return count

    def inspect(self):
        """A summary an operator can read to decide whether to trust it."""
        now = time.time()
        by_kind = {}
        by_freshness = {}
        for record in self.records.values():
            if not self._owns(record):
                continue
            by_kind[record.kind] = by_kind.get(record.kind, 0) + 1
            state = record.freshness(now, self.ttl_seconds)
            by_freshness[state] = by_freshness.get(state, 0) + 1
        return {
            "enabled": self.enabled,
            "site_url": self.site_url,
            "site_key": self.site_key,
            "namespace": self.namespace,
            "path": self.storage_path(),
            "exists": self.exists(),
            "record_count": len(self.records),
            "by_kind": by_kind,
            "by_freshness": by_freshness,
            "ttl_seconds": self.ttl_seconds,
            "rejected_secrets": self.rejected_secrets,
            "dropped_foreign_records": self.dropped_foreign,
        }

    # -- ingestion ----------------------------------------------------------

    def absorb_graph(self, graph, *, max_chars=None):
        """Turn a finished recon graph into stored memory.

        Only DURABLE, structural facts are kept: page summaries, confirmed
        navigations, and form shapes. Per-run observations and inferred roles
        are deliberately excluded — they describe one crawl, not the site, and
        storing them would let a single odd visit become the site's "known"
        state.
        """
        if not self.enabled or graph is None:
            return 0
        max_chars = max_chars if max_chars is not None else RECON_MAX_STORED_CHARS
        stored = 0
        budget = {"left": max_chars}
        for node in graph.nodes.values():
            if budget["left"] <= 0:
                break
            summary = {
                "title": node.title,
                "interactive_labels": list(node.interactive_labels)[:20],
                "has_forms": bool(node.forms),
                "visit_count": node.visit_count,
            }
            size = len(json.dumps(summary, default=str))
            if size > budget["left"]:
                break
            budget["left"] -= size
            stored += 1 if self.remember(
                node.node_id, summary, kind=MEM_PAGE_SUMMARY,
                confidence=(CONFIDENCE_CONFIRMED if node.visit_count > 1
                            else CONFIDENCE_LIKELY),
                source_observation=str(node.observation_ids[:1]),
                source_url=node.url) else 0
            if node.forms:
                form_shape = [{
                    "field_names": [f.get("name") for f in form.get("fields", [])],
                    "method": form.get("method"),
                    "submit_count": form.get("submit_count"),
                    "has_password": form.get("has_password"),
                } for form in node.forms]
                size = len(json.dumps(form_shape, default=str))
                if size <= budget["left"]:
                    budget["left"] -= size
                    stored += 1 if self.remember(
                        f"forms:{node.node_id}", form_shape,
                        kind=MEM_FORM_SHAPE,
                        confidence=CONFIDENCE_LIKELY,
                        source_observation=str(node.observation_ids[:1]),
                        source_url=node.url) else 0
        for edge in graph.edges:
            if budget["left"] <= 0:
                break
            if not edge.is_traversed or not edge.target_id:
                continue
            fact = {"from": edge.source_id, "to": edge.target_id,
                    "via": edge.label}
            size = len(json.dumps(fact, default=str))
            if size > budget["left"]:
                break
            budget["left"] -= size
            stored += 1 if self.remember(
                f"{edge.source_id}->{edge.target_id}", fact,
                kind=MEM_NAVIGATION,
                confidence=CONFIDENCE_CONFIRMED,
                source_observation=str(edge.traversed_at or ""),
                source_url=edge.target_url) else 0
        self.used_chars = max_chars - budget["left"]
        return stored


def _slug(text):
    return re.sub(r"[^a-z0-9_.-]+", "_", str(text).lower())[:48] or "default"


# ---------------------------------------------------------------------------
# Checkpoint 5.4 — memory-assisted execution.
#
# Memory may say where to LOOK. It may never say what is TRUE, and it may never
# decide that a task is finished. Both rules are structural rather than
# advisory: `MemoryHint` carries `authoritative=False` on every instance, and
# `apply_memory_to_candidates` can only REORDER a candidate list that was built
# from the live observation. There is no call in this module that lets a memory
# value satisfy evidence, because there is no code path where one could.
# ---------------------------------------------------------------------------


class MemoryHint:
    """A suggestion from memory about where to look next.

    `authoritative` is False on construction and there is no setter. A hint that
    could be promoted into a fact would be a memory that could silently
    overrule a live observation, which is the failure this design exists to
    prevent.
    """

    __slots__ = ("record", "freshness", "message", "authoritative")

    def __init__(self, record, freshness, message=""):
        self.record = record
        self.freshness = freshness
        self.message = message
        self.authoritative = False

    @property
    def is_actionable(self):
        """Whether this hint may influence ordering.

        Stale and expired hints are excluded here rather than at the call site,
        so there is one place where the rule lives and no caller can forget it.
        """
        return self.freshness in (MEM_FRESH, MEM_AGING)

    def to_dict(self):
        return {
            "key": self.record.key,
            "kind": self.record.kind,
            "freshness": self.freshness,
            "message": self.message,
            "authoritative": self.authoritative,
            "confidence": self.record.confidence,
            "source_observation": self.record.source_observation,
            "age_seconds": round(self.record.age_seconds(), 3),
        }


def memory_hints(memory, *, kind=None, key=None, now=None):
    """Build advisory hints from memory, newest-confidence first.

    Freshness travels with every hint, because a hint whose age is hidden is a
    hint whose age gets ignored.
    """
    if memory is None or not getattr(memory, "enabled", False):
        return []
    hints = []
    for record, state in memory.recall(kind=kind, key=key, now=now):
        message = ""
        if state == MEM_AGING:
            message = ("recorded earlier and past half its lifetime; treat "
                       "as a starting guess only")
        elif state == MEM_STALE:
            message = ("older than the revalidation window; revalidate "
                       "against the live page before relying on it")
        hints.append(MemoryHint(record, state, message))
    hints.sort(key=lambda h: (_confidence_rank(h.record.confidence),
                              h.record.created_at), reverse=True)
    return hints


def apply_memory_to_candidates(candidates, hints, *, max_boost=2):
    """Reorder live candidates using memory hints. Never adds or removes one.

    The candidate list is built from the current observation and is the only
    thing that decides what may be acted on. Memory may move a candidate
    earlier by a bounded number of positions; it cannot introduce a candidate
    the observation did not produce, and it cannot remove one. That is what
    keeps memory a suggestion rather than an authority.

    Moves are applied from the deepest position upward so that moving one item
    earlier never shifts the not-yet-moved item it was measured against — the
    same guarantee a stable reorder gives, expressed as an explicit move rather
    than a sort key, where a tie would silently swallow a one-position boost.
    """
    result = list(candidates or [])
    if not hints or not result:
        return result

    boost = {}
    for rank, hint in enumerate(hints):
        if not hint.is_actionable:
            continue
        target = str(hint.record.key or "").strip()
        if not target:
            continue
        # Earlier hints move a candidate further, capped by max_boost.
        distance = min(int(max_boost), rank + 1)
        if distance > boost.get(target, 0):
            boost[target] = distance

    if not boost:
        return result

    movable = []
    for index, candidate in enumerate(result):
        distance = boost.get(str(candidate), 0)
        if distance > 0:
            movable.append((index, candidate, distance))
    for index, candidate, distance in sorted(movable, reverse=True):
        result.pop(index)
        result.insert(max(0, index - distance), candidate)
    return result


def compare_memory_runs(with_memory, without_memory):
    """Compare two equivalent runs and report the difference honestly.

    Used for the Checkpoint 5.4 acceptance gate. `with_memory` and
    `without_memory` are dicts of the same shape:
    {model_calls, transitions, pages_visited, correct, stale_memory_ignored,
     stale_memory_used}.

    The comparison deliberately reports every metric rather than only the one
    that looks good. A memory layer that cuts model calls while quietly raising
    the failure rate has made the tool worse, and a summary that only showed
    the saving would hide exactly the regression that matters.
    """
    def _num(run, key):
        try:
            return int(run.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _diff(key):
        return _num(with_memory, key) - _num(without_memory, key)

    model_delta = _diff("model_calls")
    transition_delta = _diff("transitions")
    correct_delta = _diff("correct")
    stale_ignored = _num(with_memory, "stale_memory_ignored")
    stale_used = _num(with_memory, "stale_memory_used")

    helped = model_delta < 0 and transition_delta <= 0
    if stale_used > 0:
        verdict = ("REGRESSION: memory was acted on after it was stale, "
                   "which must never happen")
    elif correct_delta < 0:
        verdict = "REGRESSION: correctness fell with memory enabled"
    elif helped and correct_delta > 0:
        verdict = ("helpful: fewer model calls and no loss of correctness "
                   "(correct outcomes did not fall)")
    elif helped:
        verdict = "helpful: fewer model calls with no loss of correctness"
    elif correct_delta > 0:
        verdict = ("correctness improved, but memory did not reduce model "
                   "calls or transitions — not yet a proven benefit")
    else:
        verdict = "no measurable difference"
    return {
        "with_memory": dict(with_memory or {}),
        "without_memory": dict(without_memory or {}),
        "delta_model_calls": model_delta,
        "delta_transitions": transition_delta,
        "delta_correct": correct_delta,
        "stale_memory_ignored": stale_ignored,
        "stale_memory_used": stale_used,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# The reconnaissance runner.
#
# A breadth-first walk of same-origin hyperlinks. Breadth-first because the
# question a reader has is "what is this site made of", and breadth answers that
# with breadth; depth-first would spend the whole budget down one branch.
#
# The runner performs NO form submission, NO field filling, and NO click on a
# consequential control. Its only action is `goto` on a same-origin link. That
# restriction is the safety property; everything else here is bookkeeping.
# ---------------------------------------------------------------------------

class ReconRunner:
    """Drives one reconnaissance run and returns a `ReconGraph`.

    Budget and barrier checks are made BEFORE each navigation, so a run stops
    on a limit rather than discovering it had exceeded one afterwards.
    """

    def __init__(self, budget=None, *, headless=True, viewport=None,
                 launch_args=None, max_links_per_page=None):
        self.budget = budget or ReconBudget()
        self.headless = headless
        self.viewport = viewport or {"width": 1280, "height": 720}
        self.launch_args = list(launch_args or
                                ["--disable-blink-features=AutomationControlled"])
        self.max_links_per_page = max_links_per_page or RECON_MAX_LINKS_PER_PAGE
        # Populated during a run, read by the report.
        self.stop_reason = ""
        self.stop_detail = ""
        self.barriers = []
        self.failures = []
        self.visited = []
        self.discovery_failures = []
        self._seen_urls = set()

    # -- traversal ----------------------------------------------------------

    async def _navigate(self, page, url, *, timeout_ms=20000):
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=timeout_ms)
        except PlaywrightTimeoutError:
            # A slow page is not a dead page. Proceeding with what loaded keeps
            # the observation honest, and the timeout is recorded rather than
            # swallowed.
            self.failures.append(f"slow load (timeout) for {url}")
            return True
        except Exception as exc:
            self.failures.append(f"could not open {url}: "
                                 f"{type(exc).__name__}: {exc}")
            return False
        await wait_for_page_settled(page)
        return True

    async def _observe(self, page):
        """Read the bounded, safety-checked view of the current page.

        Uses the task engine's own observation extractor so reconnaissance and
        task execution agree on what counts as a control, a sensitive field, and
        an access-control challenge.
        """
        selectors = ("button, a, [role='button'], [role='link'], input, select, "
                     "textarea, [onclick]")
        discovery = await extract_page_elements(page, selectors, True)
        return discovery

    def _candidate_links(self, graph, node, recon_data, origin_url):
        """Turn a page's links into graph edges and a traversal frontier.

        This is where safety is actually applied. Off-site links, consequential
        links, and links behind a barrier are recorded as edges/unexplored
        areas with reasons, and only same-origin, non-consequential links reach
        the frontier.
        """
        frontier = []
        seen_targets = set()
        links = recon_data.get("links") or []
        for link in links:
            href = link.get("href") or ""
            label = link.get("text") or link.get("title") or ""
            if not is_traversable_link(href):
                graph.add_unexplored(
                    href, "not a page link (anchor, mailto/tel/javascript "
                          "scheme, or empty)", source_node_id=node.node_id,
                    kind="non_page_link")
                continue
            resolved = normalize_url(link.get("resolved") or href, origin_url)
            if not resolved:
                graph.add_unexplored(href, "could not be resolved to an "
                                          "absolute URL",
                                     source_node_id=node.node_id,
                                     kind="unresolvable")
                continue
            if not is_same_origin(resolved, origin_url):
                graph.add_edge(node.node_id, href=href, label=label,
                               kind=EDGE_OBSERVED, basis=FACT,
                               confidence=CONFIDENCE_LIKELY,
                               target_url=resolved,
                               reason="off-site; not traversed")
                graph.add_unexplored(resolved,
                                     "off-site link; reconnaissance stayed "
                                     "on the named origin",
                                     source_node_id=node.node_id,
                                     kind="off_site")
                continue
            consequential, reason = is_consequential_link(label, href)
            if consequential:
                graph.add_edge(node.node_id, href=href, label=label,
                               kind=EDGE_BLOCKED, basis=FACT,
                               confidence=CONFIDENCE_CONFIRMED,
                               target_url=resolved, reason=reason)
                graph.add_unexplored(resolved, reason,
                                     source_node_id=node.node_id,
                                     kind="consequential")
                continue
            if resolved in seen_targets:
                continue
            seen_targets.add(resolved)
            edge = graph.add_edge(node.node_id, href=href, label=label,
                                  kind=EDGE_OBSERVED, basis=FACT,
                                  confidence=CONFIDENCE_LIKELY,
                                  target_url=resolved)
            frontier.append((resolved, label, edge, node.depth + 1))
        return frontier

    async def run(self, page, start_url, origin_url=None):
        """Walk the site from `start_url`, returning the discovered `ReconGraph`."""
        origin = origin_url or start_url
        graph = ReconGraph(start_url=start_url)
        # (url, label, incoming_edge, depth). The starting page is depth 0.
        frontier = [(normalize_url(start_url, origin), "", None, 0)]

        while frontier:
            if not self.budget.can_transition():
                self.stop_reason = RECON_DONE_TRANSITIONS
                self.stop_detail = self.budget.limit_reached()[1]
                break
            if not self.budget.within_time():
                self.stop_reason = RECON_DONE_TIME
                self.stop_detail = self.budget.limit_reached()[1]
                break
            if not self.budget.can_visit_page():
                self.stop_reason = RECON_DONE_PAGES
                self.stop_detail = self.budget.limit_reached()[1]
                break

            url, label, incoming_edge, depth = frontier.pop(0)
            if url in self._seen_urls:
                continue
            # Depth is a budget, checked before the navigation rather than
            # after it, so a deep site cannot spend the page budget reaching
            # the level the depth limit was meant to prevent.
            if depth > self.budget.max_depth:
                graph.add_unexplored(
                    url, f"beyond the depth limit of {self.budget.max_depth}",
                    kind="depth_limit")
                continue
            self._seen_urls.add(url)

            self.budget.record_page()
            self.visited.append(url)

            if not await self._navigate(page, url):
                continue
            self.budget.record_transition()

            discovery = await self._observe(page)
            recon_data = await recon_discover_page(page,
                                                  max_links=self.max_links_per_page)
            if recon_data.get("extraction_failed"):
                self.discovery_failures.append(
                    f"link/form discovery failed on {url}: "
                    f"{recon_data['extraction_failed']}")

            title = recon_data.get("title") or ""
            node_id, signature = compute_full_node_id(
                url, discovery.get("options") or [], None)
            forms = recon_data.get("forms") or []
            node = graph.add_node(
                node_id, url=url, title=title, depth=depth,
                timestamp=time.time(),
                observation_id=discovery.get("observation_id"),
                interactive_labels=list(discovery.get("options") or [])[:40],
                forms=forms, semantic_signature=signature)

            # The edge that led here is only promoted to TRAVERSED now, once
            # this destination node actually exists and has been observed. A
            # link seen on the previous page is not traversal; arriving is.
            if incoming_edge is not None:
                graph.mark_edge_traversed(incoming_edge, node_id,
                                          timestamp=time.time())

            barrier = detect_recon_barrier(
                _observation_from(discovery, url, title),
                records=discovery.get("records") or [],
                page_text="")
            if barrier:
                self.barriers.append(barrier)
                graph.mark_barrier(node_id, barrier)
                graph.add_unexplored(
                    url, f"{barrier.reason} ({barrier.kind})",
                    source_node_id=node_id, kind=barrier.kind)
                # Stop the whole run at a hard boundary; do not push past it by
                # finding some other route in.
                self.stop_reason = {
                    BARRIER_ACCESS_CONTROL: RECON_DONE_ACCESS_CONTROL,
                    BARRIER_AUTH: RECON_DONE_AUTH,
                    BARRIER_CONSENT: RECON_DONE_CONSENT,
                }.get(barrier.kind, RECON_DONE_CONSEQUENTIAL)
                self.stop_detail = barrier.reason
                break

            node_role = infer_page_role(node, forms)
            if node_role:
                graph.set_inferred_role(node_id, node_role)

            frontier.extend(self._candidate_links(graph, node, recon_data,
                                                  origin))

        if not self.stop_reason:
            if not self.budget.limit_reached():
                self.stop_reason = RECON_DONE_EXHAUSTED
                self.stop_detail = "no further same-origin, non-consequential " \
                                  "links remained to visit"
        return graph


def _observation_from(discovery, url, title):
    """Build a minimal PageObservation-shaped view for barrier detection.

    Only the fields `detect_access_control_barrier` reads are populated. This
    avoids importing the whole observation class into the recon path while still
    reusing the engine's own barrier logic verbatim.
    """

    class _Obs:
        def __init__(self):
            self.url = url
            self.title = title
            self.discovery = discovery or {}

        def hidden_names(self):
            hidden = (self.discovery.get("hidden") or [])
            return [h.get("name") for h in hidden if h.get("name")]

    return _Obs()


# ---------------------------------------------------------------------------
# Checkpoint 5.5 — the reconnaissance report.
#
# The report's job is to describe what was seen WITHOUT implying the crawl was
# exhaustive. Every one of these sections carries the caveat it needs: the
# coverage line, the confidence column on inferred roles, and the explicit note
# that budgets stopped the walk. A report that says "Mapped 25 pages" without
# saying "the page budget stopped this at 25 of an unknown total" reads as a
# complete map, and is not one.
# ---------------------------------------------------------------------------

RECON_SUMMARY_TEMPLATE = """# Reconnaissance Report

**Started from:** {start_url}
**Finished:** {finished_at}
**Stop reason:** {stop_reason}
{detail_line}
**Coverage:** {pages_visited} page(s) visited, {node_count} distinct page-state(s), \
{traversed} relationship(s) walked, {observed} link(s) seen but not walked, \
{blocked} deliberately not walked.

{coverage_caveat}

## Pages and states visited

| Node | URL | Depth | Title | Visits | Type |
| :--- | :--- | ---: | :--- | ---: | :--- |
{node_rows}

## Navigation relationships

Each relationship is labelled by how it is known: a **walked** relationship was
traversed and its destination directly observed; a **suggested** relationship is
a link that was seen on a page but never followed, so where it leads is an
assumption, not a fact.

{edge_rows}

## Interactive elements and forms

{form_rows}

## Actions deliberately not taken

Reconnaissance does not submit forms, fill fields, accept consent, sign in, or
walk through checkout. It navigated only by following same-origin links.

{unexplored_rows}

## Confidence and freshness

Confidence is coarse and earned: **confirmed** means observed directly during
this run, **likely** means seen once or strongly implied by a link, **uncertain**
means suggested but never verified, **unknown** means remembered from a previous
run that no longer matches this one. Page *roles* in the tables above are
inferences from observed structure, not observed facts.

{freshness_rows}

## Limits and failures

{limit_rows}
"""


def _display_url(url, limit=64):
    """Render a URL readably inside a markdown cell.

    A local fixture URL carries a full absolute path, which is long, identical
    for every page, and useless once truncated to a prefix — every row would
    read `file:///C:/Users/...`. For those, the trailing directory plus the file
    name identifies the page; for web URLs the readable part is the tail of the
    path anyway, so the same tail rule works for both.
    """
    text = str(url or "")
    if not text:
        return "—"
    if len(text) <= limit:
        return text
    parsed = None
    try:
        parsed = urlparse(text)
    except Exception:
        parsed = None
    tail = ""
    if parsed is not None:
        path = (parsed.path or "").rstrip("/")
        segments = [s for s in path.split("/") if s]
        if parsed.query:
            segments = segments + ["?" + parsed.query]
        if segments:
            tail = "/".join(segments[-2:])
    if not tail:
        tail = text[-limit:]
    prefix = "…/" if len(text) > len(tail) else ""
    return prefix + tail


def _barrier_row(barrier):
    """Normalise a barrier to the row shape the unexplored table expects.

    Accepts either a `ReconBarrier` or an already-serialised dict, so a caller
    that already converted them does not have to do it twice.
    """
    data = barrier.to_dict() if isinstance(barrier, ReconBarrier) else dict(barrier)
    return {"url": data.get("url", ""), "reason": data.get("reason", ""),
            "kind": data.get("kind", ""), "source_node_id": ""}


def _md_cell(text, limit=90):
    text = str(text or "").replace("|", "/").replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


def build_recon_report(graph, budget, stop_reason, *, stop_detail="",
                       barriers=None, failures=None, start_url="",
                       discovery_failures=None):
    """Render the reconnaissance report as markdown text.

    Returns the text; the caller decides whether to write it to disk. Kept pure
    so the report's honesty can be asserted in tests without touching a file.
    """
    graphs = graph.to_dict()
    nodes = graphs["nodes"]
    edges = graphs["edges"]
    stats = graphs["stats"]

    if not stop_reason:
        stop_reason = RECON_DONE_EXHAUSTED
    incomplete = stop_reason in RECON_INCOMPLETE_REASONS
    coverage_caveat = (
        "**This was NOT an exhaustive map of the site.** Exploration ended "
        f"because the run hit `{stop_reason}`, not because the site was fully "
        "covered. Areas below are marked unexplained, suggested, or not walked, "
        "and many more pages likely exist that were never reached."
        if incomplete else
        "Exploration reached the end of its safe frontier within budget. This "
        "still is not proof the site has no other pages — only that no further "
        "same-origin, non-consequential link was found from where it looked.")

    node_rows = "\n".join(
        f"| `{_md_cell(n['node_id'], 12)}` | {_md_cell(_display_url(n['url']))} | "
        f"{n['depth']} | {_md_cell(n['title'])} | {n['visit_count']} | "
        f"{_md_cell(n.get('inferred_role') or '—')} *(inferred)* |"
        for n in nodes) or "| — | (no pages visited) | | | | |"

    def _edge_label(edge):
        kind = edge["kind"]
        tag = {"traversed": "walked", "observed": "suggested",
               "inferred": "inferred", "blocked": "not walked"}.get(kind, kind)
        dest = edge.get("target_id") or edge.get("target_url") or "?"
        return f"{tag} ({edge['confidence']})"

    edge_rows = "\n".join(
        f"| `{_md_cell(e['source_id'], 12)}` | {_md_cell(e['label'] or e['href'])} "
        f"| `{_md_cell(e.get('target_id') or '—', 12)}` | {_edge_label(e)} |"
        for e in edges) or "| — | (no relationships recorded) | | |"

    form_rows_lines = []
    for n in nodes:
        if not n.get("forms"):
            continue
        for form in n["forms"]:
            names = [f.get("name") for f in form.get("fields", [])][:8]
            shown = ", ".join(f"`{_md_cell(x, 30)}`" for x in names) or "—"
            form_rows_lines.append(
                f"- `{_md_cell(n['node_id'], 12)}` — "
                f"{form.get('method', 'get').upper()} form, "
                f"{len(form.get('fields', []))} field(s), "
                f"{form.get('submit_count', 0)} submit control(s)"
                + (", **asks for credentials**" if form.get("has_password")
                   else "")
                + f" — fields: {shown}")
    form_rows = "\n".join(form_rows_lines) or \
        "No forms were observed on the pages visited."

    unexplored = graphs["unexplored"] + [
        _barrier_row(b) for b in (barriers or [])]
    unexplored_rows = "\n".join(
        f"- {_display_url(u.get('url') or u.get('kind'))} — not walked: "
        f"{u.get('reason')}" for u in unexplored) or \
        "None — every discovered link was either walked or safely skipped."

    fresh_rows = []
    for n in nodes:
        conf = "confirmed (observed this run)"
        if n.get("inferred_role"):
            conf += f"; role '{n['inferred_role']}' is an inference"
        fresh_rows.append(f"- `{_md_cell(n['node_id'], 12)}` — {conf}")
    freshness_rows = "\n".join(fresh_rows) or "No pages were observed."

    limit_rows = [
        f"- **Budget:** pages {budget.pages_visited}/{budget.max_pages}, "
        f"transitions {budget.transitions}/{budget.max_transitions}, "
        f"depth {budget.max_depth}, time {budget.max_seconds}s, "
        f"model calls {budget.model_calls}/{budget.max_model_calls}, "
        f"stored content {budget.stored_chars}/{budget.max_stored_chars} chars.",
        f"- **Stop reason:** {stop_reason}"
        + (f" — {stop_detail}" if stop_detail else ""),
    ]
    if stats["urls_with_multiple_states"]:
        limit_rows.append(
            f"- **Dynamic states:** {stats['urls_with_multiple_states']} URL(s) "
            "rendered more than one distinct page-state during this run, which "
            "is why pages are identified by structure+state and not by URL alone.")
    if discovery_failures:
        limit_rows.append(
            "- **Discovery failures:** some pages could not be fully read; their "
            f"link/form data is incomplete ({len(discovery_failures)} page(s)).")
    if failures:
        for f in failures[:10]:
            limit_rows.append(f"- {f}")

    detail_line = (f"**Why it stopped:** {stop_detail}\n" if stop_detail else "")

    return RECON_SUMMARY_TEMPLATE.format(
        start_url=start_url or graph.start_url,
        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        stop_reason=stop_reason,
        detail_line=detail_line,
        pages_visited=budget.pages_visited,
        node_count=stats["node_count"],
        traversed=stats["traversed_edges"],
        observed=stats["observed_only_edges"],
        blocked=stats["blocked_edges"],
        coverage_caveat=coverage_caveat,
        node_rows=node_rows,
        edge_rows=edge_rows,
        form_rows=form_rows,
        unexplored_rows=unexplored_rows,
        freshness_rows=freshness_rows,
        limit_rows="\n".join(limit_rows),
    )


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

async def run_reconnaissance(start_url, *, budget=None, headless=True,
                             memory=None, config_path=None,
                             on_complete=None):
    """Run one reconnaissance pass and return (graph, report_text).

    `start_url` is required and is the only site this run will touch: every
    candidate must be same-origin with it. `memory`, if given, is loaded and
    saved around the run; it is optional and disabling it changes nothing else.
    """
    budget = budget or ReconBudget()
    graph = None
    runner = ReconRunner(budget, headless=headless)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless,
                                          args=runner.launch_args)
        context = await browser.new_context(viewport=runner.viewport)
        page = await context.new_page()
        # A dialog blocks every subsequent Playwright call until answered.
        # Reconnaissance never accepts one; it always dismisses, because the
        # only question a reconnaissance run may answer to a modal prompt is
        # "no".
        page.on("dialog", lambda d: asyncio.ensure_future(_dismiss(d)))
        try:
            graph = await runner.run(page, start_url)
        finally:
            await browser.close()

    if memory is not None and getattr(memory, "enabled", False):
        memory.load()
        memory.absorb_graph(graph)
        memory.save()

    report = build_recon_report(
        graph, budget, runner.stop_reason,
        stop_detail=runner.stop_detail,
        barriers=runner.barriers,
        failures=runner.failures,
        start_url=start_url,
        discovery_failures=runner.discovery_failures)

    if on_complete is not None:
        on_complete(graph, report)
    return graph, report


async def _dismiss(dialog):
    try:
        await dialog.dismiss()
    except Exception:
        pass


def write_recon_report(report_text, directory="."):
    """Write the report to a timestamped markdown file and return its path."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(directory, f"recon_report_{stamp}.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report_text)
    return path
