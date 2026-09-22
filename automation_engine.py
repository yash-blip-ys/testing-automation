import asyncio
import ollama
import json
import re
import hashlib
import os
import sys
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# -----------------------------------------------------------------------------
# REVERT / TUNING KNOBS (top of file = easy to find and edit)
#
# Every new behavior in this file has a kill-switch constant below. Set the
# constant to turn off that feature and behavior reverts to the prior version
# without needing to restore the backup file.
# -----------------------------------------------------------------------------

# Kill-switch: If False, AI scoring is PERMANENTLY cached on first node visit
# exactly like the old behavior. If True, we INVALIDATE cached scores when we
# return to a previously-visited node after enough workflow progress happened
# (the recent_actions log is longer than the snapshot taken at first visit by
# at least STALE_SCORE_REASK_THRESHOLD actions). This is what prevents the
# regressive "cost 1 assigned on page 1 is still cost 1 on page 8" bug.
ENABLE_STALE_SCORE_INVALIDATION = True
STALE_SCORE_REASK_THRESHOLD = 4

# Kill-switch: If False, skips the MECHANICAL external-domain tagger entirely
# (fallback to old behavior). When True, ANY <a> whose href origin differs
# from window.location.origin gets an automatic safety_tag=-1 during element
# extraction. This catches Twitter/Facebook/LinkedIn/saucelabs.com/ANY
# off-domain link with 100% accuracy, zero LLM cost, zero hardcoded keywords,
# fully site-agnostic.
ENABLE_MECHANICAL_EXTERNAL_DETECTION = True

# Kill-switch: If False, the AI navigator prompt does NOT include the negative
# semantic tagging request and safety tags are only what the mechanical layer
# above produces. If True, the AI is ASKED to classify every available
# element as 0 / -1 / -2. Mechanical tags always win over AI tags for items
# the machine can prove (external links).
ENABLE_AI_SEMANTIC_SAFETY_TAGS = True

# Kill-switch: If False, the AI gets ONLY the original prompt inputs (goal +
# recent actions + buttons list). If True, the AI additionally receives:
#   - the current full page URL
#   - the current page <title>
#   - a 400-char preview of body innerText (reads the actual page content!)
#   - step depth within max_search_depth (so it knows "we're close to the end")
# This is the fix for "LLM hallucinated 'Continue' on step-2 page" — it had
# no idea what page it was on, just a list of buttons.
ENABLE_FULL_PAGE_CONTEXT_FOR_AI = True

# Numeric semantic safety codes (exactly what you specified):
SAFETY_EXTERNAL_SITE = -1      # leads off-domain (different website)
SAFETY_DESTRUCTIVE = -2        # remove product, delete account, cancel sub, etc.

# Edge costs that correspond to the safety codes (heavy, but <999 so AI can
# still explicitly rank them as #1 choice and override if truly needed).
COST_FOR_SAFETY_EXTERNAL = 50      # -1  → heavy, not permanently blocked
COST_FOR_SAFETY_DESTRUCTIVE = 998  # -2  → near-blocked (only AI #1 overrides)

# After a click, if the page state hash did NOT change (same URL + same
# elements), the action accomplished nothing. +50 instantly (not +2).
FUTILE_ACTION_PENALTY = 50

# Click / scroll timeout for a single attempt. 4s normal; 30s freeze = gone.
CLICK_ATTEMPT_TIMEOUT_MS = 4000

# How many of the agent's most recent actions get shown back to the AI.
RECENT_ACTIONS_MEMORY = 5

# Expected runtime (warning-only — never blocks execution).
EXPECTED_PYTHON_VERSION = (3, 11)
EXPECTED_VENV_MARKER = "venv311"

# Kill-switch: If False, flat int-only edge_weights (int cost per (node, edge)) are used
# exactly like the original behavior and the new per-edge metadata objects below are
# never created, recorded, or consulted. Set False for 100% pre-refactor behavior.
ENABLE_EDGE_METADATA_TRACKING = True

# Result-string constants used inside edge["last_result"]. Site-agnostic; no product or
# site names are hardcoded anywhere — inferred mechanically from page/DOM signals only.
RESULT_SUCCESS_NODE_CHANGED = "node_changed"
RESULT_SUCCESS_SAME_NODE = "same_node_no_state_change"
RESULT_FAILURE_CLICK_EXCEPTION = "click_exception"
RESULT_FAILURE_NOT_VISIBLE = "element_not_visible"
RESULT_FAILURE_SCROLL_TIMEOUT = "scroll_into_view_failed"
RESULT_CART_COUNT_INCREASED = "cart_count_increased"    # inferred from .shopping_cart_badge text grow
RESULT_CART_COUNT_DECREASED = "cart_count_decreased"    # inferred from badge shrink
RESULT_FORM_SUBMITTED = "form_submitted"                 # inferred from input disappearing post-click
RESULT_VICTORY_HIT = "victory_condition_match"

# Success-rate thresholds for automatic penalties/blacklisting.
EDGE_LOW_SUCCESS_THRESHOLD = 0.35          # success rate below this → extra penalty
EDGE_CONSECUTIVE_FAILURE_BLACKLIST = 3     # 3 failures without any success → 999 blacklist

# -----------------------------------------------------------------------------
# STATE MANAGER (Mechanical, LLM-free)
#
# Owns: current_state, previous_state, state_history, visited_nodes, transition_history
# The LLM never writes to these structures — it only reads snapshots that the
# StateManager explicitly exports (page context blocks, not direct dict access).
# -----------------------------------------------------------------------------

STATE_PHASE_INIT = "init"
STATE_PHASE_AUTHENTICATED = "authenticated"
STATE_PHASE_EXPLORATION = "exploration"
STATE_PHASE_PRE_INTERACTION = "pre_interaction"
STATE_PHASE_POST_INTERACTION = "post_interaction"
STATE_PHASE_VICTORY = "victory"
STATE_PHASE_BACKTRACK = "backtrack"
STATE_PHASE_EXHAUSTED = "exhausted"
STATE_PHASE_ERROR = "error"


class State:
    """A single mechanical snapshot of the agent's environment.

    Fully deterministic from page DOM + URL + step counters. The LLM may READ
    a curated subset of these fields via the navigator prompt, but it never
    constructs or mutates a State object.
    """

    __slots__ = (
        "node_hash",
        "url",
        "page_title",
        "available_elements",
        "element_count",
        "cart_badge_count",
        "form_input_count",
        "step_index",
        "phase",
        "timestamp",
        "action_count_at_creation",
        "victory_match",
        "snapshot_notes",
    )

    def __init__(self, *, node_hash, url, page_title=None, available_elements=None,
                 element_count=0, cart_badge_count=0, form_input_count=0,
                 step_index=0, phase=STATE_PHASE_INIT,
                 action_count_at_creation=0, victory_match=False,
                 snapshot_notes=None):
        self.node_hash = node_hash
        self.url = url
        self.page_title = page_title or ""
        self.available_elements = list(available_elements) if available_elements else []
        self.element_count = element_count if element_count else len(self.available_elements)
        self.cart_badge_count = cart_badge_count
        self.form_input_count = form_input_count
        self.step_index = step_index
        self.phase = phase
        self.timestamp = datetime.now()
        self.action_count_at_creation = action_count_at_creation
        self.victory_match = victory_match
        self.snapshot_notes = snapshot_notes or ""

    def to_dict(self):
        return {
            "node_hash": self.node_hash,
            "url": self.url,
            "page_title": self.page_title,
            "element_count": self.element_count,
            "cart_badge_count": self.cart_badge_count,
            "form_input_count": self.form_input_count,
            "step_index": self.step_index,
            "phase": self.phase,
            "timestamp": self.timestamp.isoformat(),
            "action_count_at_creation": self.action_count_at_creation,
            "victory_match": self.victory_match,
            "snapshot_notes": self.snapshot_notes,
            "available_elements_count": len(self.available_elements),
        }


class Transition:
    """Record of a single attempted state transition.

    Mechanical: populated from (pre_state + chosen_edge + execution outcome)
    only. LLM ranking decisions are recorded verbatim from the navigator's
    return payload but the Transition object itself is assembled inside
    StateManager.record_transition().
    """

    __slots__ = (
        "sequence_id",
        "source_node_hash",
        "destination_node_hash",
        "action_label",
        "assigned_cost",
        "outcome",
        "last_result_tag",
        "overlay_recovery_used",
        "attempt_count_for_action",
        "cart_delta",
        "form_input_delta",
        "timestamp",
        "failure_reason",
    )

    def __init__(self, *, sequence_id, source_node_hash, action_label,
                 assigned_cost, destination_node_hash=None, outcome="pending",
                 last_result_tag=None, overlay_recovery_used=False,
                 attempt_count_for_action=0, cart_delta=0, form_input_delta=0,
                 failure_reason=None):
        self.sequence_id = sequence_id
        self.source_node_hash = source_node_hash
        self.destination_node_hash = destination_node_hash
        self.action_label = action_label
        self.assigned_cost = assigned_cost
        self.outcome = outcome  # success | failed_no_state_change | failed_click | backtracked | victory
        self.last_result_tag = last_result_tag
        self.overlay_recovery_used = overlay_recovery_used
        self.attempt_count_for_action = attempt_count_for_action
        self.cart_delta = cart_delta
        self.form_input_delta = form_input_delta
        self.timestamp = datetime.now()
        self.failure_reason = failure_reason

    def to_dict(self):
        return {
            "sequence_id": self.sequence_id,
            "source_node_hash": self.source_node_hash,
            "destination_node_hash": self.destination_node_hash,
            "action_label": self.action_label,
            "assigned_cost": self.assigned_cost,
            "outcome": self.outcome,
            "last_result_tag": self.last_result_tag,
            "overlay_recovery_used": self.overlay_recovery_used,
            "attempt_count_for_action": self.attempt_count_for_action,
            "cart_delta": self.cart_delta,
            "form_input_delta": self.form_input_delta,
            "timestamp": self.timestamp.isoformat(),
            "failure_reason": self.failure_reason,
        }


class VisitedNodeRecord:
    """Mechanical ledger entry for a single discovered node hash.

    This is the authoritative visited_nodes record — it replaces the previous
    state_graph + state_graph_metadata split dicts and the ad-hoc
    edge_metadata_store keys. Nothing else may claim a node is "visited".
    """

    __slots__ = (
        "node_hash",
        "first_seen_step",
        "last_seen_step",
        "visit_count",
        "first_action_count",
        "last_action_count",
        "first_url",
        "last_url",
        "incoming_edges",
        "outgoing_edges_attempted",
        "outgoing_edges_succeeded",
        "tagged_externals_count",
    )

    def __init__(self, *, node_hash, first_seen_step, first_action_count, first_url):
        self.node_hash = node_hash
        self.first_seen_step = first_seen_step
        self.last_seen_step = first_seen_step
        self.visit_count = 1
        self.first_action_count = first_action_count
        self.last_action_count = first_action_count
        self.first_url = first_url
        self.last_url = first_url
        self.incoming_edges = set()
        self.outgoing_edges_attempted = set()
        self.outgoing_edges_succeeded = set()
        self.tagged_externals_count = 0

    def touch(self, step, action_count, url):
        self.last_seen_step = step
        self.visit_count += 1
        self.last_action_count = action_count
        self.last_url = url

    def to_dict(self):
        return {
            "node_hash": self.node_hash,
            "first_seen_step": self.first_seen_step,
            "last_seen_step": self.last_seen_step,
            "visit_count": self.visit_count,
            "first_action_count": self.first_action_count,
            "last_action_count": self.last_action_count,
            "first_url": self.first_url,
            "last_url": self.last_url,
            "incoming_edges_count": len(self.incoming_edges),
            "outgoing_attempted": sorted(self.outgoing_edges_attempted),
            "outgoing_succeeded": sorted(self.outgoing_edges_succeeded),
            "tagged_externals_count": self.tagged_externals_count,
        }


class StateManager:
    """Authoritative, mechanical owner of the agent's state ledger.

    Public slots that callers MAY read (not write):
      .current_state      — State object for the most recent snapshot
      .previous_state     — State object for the snapshot before current
      .state_history      — list[State] in chronological order
      .visited_nodes      — dict[node_hash -> VisitedNodeRecord]
      .transition_history — list[Transition] in chronological order

    Nothing else in the codebase is allowed to track "visited" or "previous"
    independently. Kill-switches still exist for the NAVIGATION logic (AI
    scoring, safety tags, etc.), but the LEDGER is the exclusive authority.
    """

    def __init__(self, max_search_depth):
        self.max_search_depth = max_search_depth

        # ---- The Five Pillars ------------------------------------------------
        self.current_state = None
        self.previous_state = None
        self.state_history = []
        self.visited_nodes = {}   # node_hash -> VisitedNodeRecord
        self.transition_history = []  # list[Transition]
        # ---------------------------------------------------------------------

        self._transition_counter = 0
        self._action_counter = 0  # distinct from step_index — counts every chosen edge
        self._breadcrumb_stack = []  # kept as a mechanical mirror of go_back() calls
        self._final_status = "RUNNING"

    # ------------------------------------------------------------------ #
    # Metadata helpers (read-only exports for the rest of the engine)    #
    # ------------------------------------------------------------------ #

    @property
    def steps_taken(self):
        return len(self.transition_history)

    @property
    def unique_nodes_discovered(self):
        return len(self.visited_nodes)

    @property
    def action_count(self):
        return self._action_counter

    @property
    def breadcrumb_depth(self):
        return len(self._breadcrumb_stack)

    def get_visited_record(self, node_hash):
        return self.visited_nodes.get(node_hash)

    def was_visited_before(self, node_hash):
        return node_hash in self.visited_nodes

    def get_action_count_at_first_visit(self, node_hash):
        rec = self.visited_nodes.get(node_hash)
        return rec.first_action_count if rec else None

    def actions_since_first_visit(self, node_hash):
        rec = self.visited_nodes.get(node_hash)
        if rec is None:
            return 0
        return self._action_counter - rec.first_action_count

    def export_state_for_report(self):
        return {
            "final_status": self._final_status,
            "total_transitions": self.steps_taken,
            "unique_nodes": self.unique_nodes_discovered,
            "state_history_count": len(self.state_history),
            "transition_history_count": len(self.transition_history),
            "current_node": self.current_state.node_hash if self.current_state else None,
            "previous_node": self.previous_state.node_hash if self.previous_state else None,
        }

    def export_trajectory_table_rows(self):
        rows = []
        for t in self.transition_history:
            src = self.visited_nodes.get(t.source_node_hash)
            url = src.last_url if src else ""
            rows.append({
                "step": t.sequence_id,
                "node": t.source_node_hash,
                "url": url,
                "action": t.action_label,
                "cost": t.assigned_cost,
            })
        return rows

    def export_visited_nodes_summary(self):
        return [rec.to_dict() for rec in self.visited_nodes.values()]

    # ------------------------------------------------------------------ #
    # Core lifecycle mutators — the ONLY five ways to write state        #
    # ------------------------------------------------------------------ #

    def snapshot_initial(self, *, node_hash, url, page_title=None,
                         available_elements=None, cart_badge_count=0,
                         form_input_count=0, step_index=0,
                         externals_count=0):
        """Called exactly once: after authentication, first exploration snapshot."""
        state = State(
            node_hash=node_hash, url=url, page_title=page_title,
            available_elements=available_elements,
            cart_badge_count=cart_badge_count,
            form_input_count=form_input_count,
            step_index=step_index,
            phase=STATE_PHASE_EXPLORATION,
            action_count_at_creation=self._action_counter,
            snapshot_notes="initial post-auth snapshot",
        )
        self._commit_state(state, externals_count=externals_count)
        return state

    def snapshot_before_action(self, *, node_hash, url, page_title=None,
                               available_elements=None, cart_badge_count=0,
                               form_input_count=0, step_index=0,
                               externals_count=0, victory_match=False,
                               snapshot_notes=""):
        """Called at the TOP of each main-loop iteration, before any edge is chosen."""
        notes = snapshot_notes
        if victory_match:
            phase = STATE_PHASE_VICTORY
            notes = (notes + " | victory match detected").strip(" |")
        else:
            phase = STATE_PHASE_EXPLORATION
        state = State(
            node_hash=node_hash, url=url, page_title=page_title,
            available_elements=available_elements,
            cart_badge_count=cart_badge_count,
            form_input_count=form_input_count,
            step_index=step_index,
            phase=phase,
            action_count_at_creation=self._action_counter,
            victory_match=victory_match,
            snapshot_notes=notes,
        )
        self._commit_state(state, externals_count=externals_count)
        return state

    def record_intent(self, *, source_node_hash, action_label, assigned_cost):
        """Called right after an edge is chosen, before the click attempt.

        Opens a pending Transition. Returns the new sequence_id.
        """
        self._action_counter += 1
        self._transition_counter += 1
        t = Transition(
            sequence_id=self._transition_counter,
            source_node_hash=source_node_hash,
            action_label=action_label,
            assigned_cost=assigned_cost,
            outcome="pending",
        )
        self.transition_history.append(t)

        # Bookkeep the intent against the source node ledger
        src_rec = self.visited_nodes.get(source_node_hash)
        if src_rec is not None:
            src_rec.outgoing_edges_attempted.add(action_label)

        self._breadcrumb_stack.append(source_node_hash)
        return t.sequence_id

    def record_outcome(self, sequence_id, *, destination_node_hash, outcome,
                       last_result_tag=None, cart_delta=0, form_input_delta=0,
                       overlay_recovery_used=False, attempt_count_for_action=0,
                       failure_reason=None):
        """Called after the click attempt (success or failure). Finalises Transition."""
        for t in reversed(self.transition_history):
            if t.sequence_id == sequence_id:
                t.destination_node_hash = destination_node_hash
                t.outcome = outcome
                t.last_result_tag = last_result_tag
                t.cart_delta = cart_delta
                t.form_input_delta = form_input_delta
                t.overlay_recovery_used = overlay_recovery_used
                t.attempt_count_for_action = attempt_count_for_action
                t.failure_reason = failure_reason
                # Node ledger bookkeeping
                if outcome in ("success", "victory"):
                    src_rec = self.visited_nodes.get(t.source_node_hash)
                    if src_rec is not None:
                        src_rec.outgoing_edges_succeeded.add(t.action_label)
                    dst_rec = self.visited_nodes.get(destination_node_hash)
                    if dst_rec is not None:
                        dst_rec.incoming_edges.add(t.action_label)
                elif outcome == "failed_no_state_change" and self._breadcrumb_stack:
                    # Same-node no-op: pop the breadcrumb since we didn't move
                    self._breadcrumb_stack.pop()
                elif outcome in ("failed_click", "failed_not_visible", "failed_scroll"):
                    if self._breadcrumb_stack:
                        self._breadcrumb_stack.pop()
                return t
        return None

    def record_backtrack(self, from_node_hash, to_node_hash, reason="dead_end"):
        """Mechanical mirror of page.go_back(). Opens its own Transition."""
        self._action_counter += 1
        self._transition_counter += 1
        t = Transition(
            sequence_id=self._transition_counter,
            source_node_hash=from_node_hash,
            destination_node_hash=to_node_hash,
            action_label=f"__BACKTRACK__:{reason}",
            assigned_cost=999,
            outcome="backtracked",
            last_result_tag="backtrack",
            failure_reason=reason,
        )
        self.transition_history.append(t)
        # Pop twice: once for the push before the failed click, once for
        # __BACKTRACK__ itself replacing that layer.
        if self._breadcrumb_stack and self._breadcrumb_stack[-1] == from_node_hash:
            self._breadcrumb_stack.pop()
        # The backtrack destination is being re-entered; callers should call
        # snapshot_before_action() after go_back() settles the page.
        return t.sequence_id

    def set_final_status(self, status):
        """One-time write: SUCCESS_TARGET_REACHED / MAX_DEPTH_EXHAUSTED / etc."""
        self._final_status = status

    def pop_breadcrumb(self):
        if self._breadcrumb_stack:
            return self._breadcrumb_stack.pop()
        return None

    # ------------------------------------------------------------------ #
    # Internal commit helpers                                            #
    # ------------------------------------------------------------------ #

    def _commit_state(self, state, externals_count=0):
        # Shift pointers BEFORE appending so pointers always match history indices
        self.previous_state = self.current_state
        self.current_state = state
        self.state_history.append(state)

        # --- The authoritative visited_nodes update ----------------------
        node_hash = state.node_hash
        if node_hash in self.visited_nodes:
            self.visited_nodes[node_hash].touch(
                state.step_index,
                self._action_counter,
                state.url,
            )
            if externals_count > self.visited_nodes[node_hash].tagged_externals_count:
                self.visited_nodes[node_hash].tagged_externals_count = externals_count
        else:
            rec = VisitedNodeRecord(
                node_hash=node_hash,
                first_seen_step=state.step_index,
                first_action_count=self._action_counter,
                first_url=state.url,
            )
            rec.tagged_externals_count = externals_count
            self.visited_nodes[node_hash] = rec

        # Link last transition's destination if we can (mechanical, best-effort)
        if self.transition_history and self.transition_history[-1].outcome == "pending":
            # Caller should have record_outcome()d before snapshot, but guard anyway
            pass


# -----------------------------------------------------------------------------


def check_environment():
    """Warns loudly but never blocks on Python/venv mismatch."""
    version_ok = sys.version_info[:2] == EXPECTED_PYTHON_VERSION
    venv_ok = EXPECTED_VENV_MARKER in sys.prefix or EXPECTED_VENV_MARKER in sys.executable
    if not version_ok or not venv_ok:
        print("=" * 70)
        print("[WARNING] Environment mismatch detected.")
        print(f"  Running interpreter : {sys.executable}")
        print(f"  Running version     : {sys.version.split()[0]}")
        print(f"  Expected            : Python {EXPECTED_PYTHON_VERSION[0]}.{EXPECTED_PYTHON_VERSION[1]}"
              f" inside a '{EXPECTED_VENV_MARKER}' virtual environment")
        print("  Activate the venv before running this script.")
        print("=" * 70)
    return version_ok and venv_ok


def compute_node_hash(url, elements):
    state_string = f"{url}|{','.join(sorted(elements))}"
    return hashlib.md5(state_string.encode('utf-8')).hexdigest()[:10]


def infer_action_type(element_text):
    """Site-agnostic mechanical action-type classifier. Returns ADD_ITEM, NAV,
    FORM_ACTION, REMOVE_ITEM, MODAL_ACTION, EXTERNAL_SOCIAL, UNKNOWN.
    No product/site names hardcoded — works purely on token/pattern heuristics."""
    text = element_text.lower().strip()
    if any(tok in text for tok in ["add to cart", "add", "buy", "purchase", "select"]):
        return "ADD_ITEM"
    if any(tok in text for tok in ["checkout", "finish", "submit", "confirm", "save",
                                   "continue", "next", "proceed", "place order", "pay"]):
        return "FORM_ACTION"
    if any(tok in text for tok in ["remove", "delete", "cancel", "reset", "clear", "undo"]):
        return "REMOVE_ITEM"
    if any(tok in text for tok in ["open menu", "close menu", "all items", "logout",
                                   "log out", "sign out", "sign in", "log in",
                                   "reset app state", "about"]):
        return "MODAL_ACTION"
    if any(tok in text for tok in ["twitter", "facebook", "linkedin", "instagram",
                                   "youtube", "x.com", "tiktok", "github", "reddit"]):
        return "EXTERNAL_SOCIAL"
    if any(tok in text for tok in ["cart", "shopping cart", "back to products",
                                   "continue shopping", "open"]):
        return "NAV"
    return "NAV"


def create_edge_metadata(element_text):
    """New-edge record. Shape matches the user-specified schema exactly but adds
    AI_ranked_cost so the metadata object still tracks the LLM's subjective cost.
    destination and last_result start as None until the click result is known."""
    return {
        "action": infer_action_type(element_text),
        "element": element_text,
        "attempts": 0,
        "successes": 0,
        "failures": 0,
        "consecutive_failures": 0,
        "last_result": None,
        "destination": None,
        "cost": 10,
    }


def record_edge_result(edge, success_bool, result_string, destination_node=None,
                       updated_cost=None):
    """Mutates edge dict in-place. Always increments attempts; on success bumps
    successes and zeroes consecutive_failures; on failure bumps failures and
    consecutive_failures. Updates destination / last_result / cost."""
    edge["attempts"] += 1
    edge["last_result"] = result_string
    if destination_node is not None:
        edge["destination"] = destination_node
    if success_bool:
        edge["successes"] += 1
        edge["consecutive_failures"] = 0
    else:
        edge["failures"] += 1
        edge["consecutive_failures"] += 1
    if updated_cost is not None:
        edge["cost"] = updated_cost


def compute_current_edge_cost(edge, base_ai_cost):
    """Returns the int cost for this edge given both its subjective base_ai_cost
    (from LLM ranking / fallback defaults) and its empirical performance stats.
    Mechanical rule, site-agnostic: low success rates and consecutive failures
    drive cost UP; high success history modestly nudges cost DOWN."""
    cost = int(base_ai_cost)
    if not ENABLE_EDGE_METADATA_TRACKING:
        return cost
    if edge is None:
        return cost
    attempts = edge.get("attempts", 0)
    if attempts <= 0:
        return cost
    successes = edge.get("successes", 0)
    failures = edge.get("failures", 0)
    last_result = edge.get("last_result")
    consec = edge.get("consecutive_failures", 0)

    success_rate = (successes / attempts) if attempts > 0 else 0.0

    # Modest discount for edges that empirically work (≥2 attempts, >80% success).
    if attempts >= 2 and success_rate >= 0.80:
        cost = max(1, cost - 1)
    # Steep penalty for edges with a very poor success track record.
    if attempts >= 2 and success_rate < EDGE_LOW_SUCCESS_THRESHOLD:
        cost += 30
    # Penalty per consecutive failure pattern (stuck edge).
    if consec >= 1:
        cost += 5 * consec
    # Hard blacklist if an edge is repeatedly failing without any success.
    if consec >= EDGE_CONSECUTIVE_FAILURE_BLACKLIST and successes == 0:
        return 999
    # Specific last-result penalties.
    if last_result in (RESULT_FAILURE_CLICK_EXCEPTION, RESULT_FAILURE_NOT_VISIBLE,
                       RESULT_FAILURE_SCROLL_TIMEOUT):
        cost += 10
    if last_result == RESULT_SUCCESS_SAME_NODE:
        cost += 25
    if last_result == RESULT_VICTORY_HIT and successes >= 1:
        cost = max(1, cost - 2)
    if last_result == RESULT_CART_COUNT_INCREASED and successes >= 1:
        cost = max(1, cost - 1)
    if last_result == RESULT_CART_COUNT_DECREASED and successes >= 1:
        cost += 10
    # Safety ceiling (still <999 so explicit LLM #1 choice always wins override).
    if cost > 998:
        cost = 998
    return cost


def build_per_node_edge_performance_block(node_hash, edge_metadata_store,
                                           available_elements, max_items=25):
    """Returns a multi-line string describing empirical stats for every element
    on the current node that has at least 1 prior attempt. Injected verbatim
    into the LLM prompt context so the AI can see history BEFORE choosing."""
    if not ENABLE_EDGE_METADATA_TRACKING:
        return None
    lines = []
    count = 0
    for elem in available_elements:
        key = (node_hash, elem)
        edge = edge_metadata_store.get(key)
        if edge is None or edge["attempts"] == 0:
            continue
        rate = (100.0 * edge["successes"] / edge["attempts"]) if edge["attempts"] > 0 else 0.0
        dest = edge["destination"] if edge["destination"] else "?"
        lines.append(
            f"  * '{elem}'  action={edge['action']}  attempts={edge['attempts']} "
            f"successes={edge['successes']} failures={edge['failures']} "
            f"success_rate={rate:.0f}% consecutive_failures={edge['consecutive_failures']} "
            f"last_result={edge['last_result']} leads_to_node={dest} "
            f"current_cost={edge['cost']}"
        )
        count += 1
        if count >= max_items:
            break
    if not lines:
        return None
    return (
        "PER_NODE_EDGE_PERFORMANCE_HISTORY (empirical stats from THIS run only; "
        "prefer edges with higher success rates and avoid "
        f"consecutive_failures≥2):\n" + "\n".join(lines)
    )


async def safe_wait_for_load(page, timeout_ms=8000):
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except PlaywrightTimeoutError:
            pass


def _validate_navigator_response(parsed, available_elements):
    if not isinstance(parsed, dict):
        return "response was not a JSON object"
    best_choice = parsed.get("best_choice")
    if not isinstance(best_choice, str):
        return "'best_choice' is missing or not a string"
    if best_choice not in available_elements:
        return f"'best_choice' ({best_choice!r}) is not one of the elements actually on screen"
    ranked_backup = parsed.get("ranked_backup", [])
    if not isinstance(ranked_backup, list) or not all(isinstance(x, str) for x in ranked_backup):
        return "'ranked_backup' must be a list of strings"
    safety_tags = parsed.get("safety_tags")
    if ENABLE_AI_SEMANTIC_SAFETY_TAGS:
        if not isinstance(safety_tags, dict):
            return "'safety_tags' must be a JSON object mapping element -> integer tag"
        for k, v in safety_tags.items():
            if not isinstance(k, str) or not isinstance(v, int):
                return "'safety_tags' values must be integers: 0 safe, -1 external, -2 destructive"
            if v not in (0, SAFETY_EXTERNAL_SITE, SAFETY_DESTRUCTIVE):
                return f"'safety_tags' value {v!r} for {k!r} not in allowed set (0, -1, -2)"
    return None


def ask_ai_navigator(available_elements, goal, recent_actions,
                     page_url=None, page_title=None, page_text_snippet=None,
                     step_index=None, max_steps=None,
                     mechanical_safety_tags=None,
                     edge_performance_block=None):
    """
    The ALL-NEW Navigator.

    KEY DIFFERENCE vs old version: when ENABLE_FULL_PAGE_CONTEXT_FOR_AI is
    True, the LLM now knows WHAT PAGE IT'S ON, not just a blind list of
    buttons. The "Continue was hallucinated on checkout-step-2" class of bug
    goes away because the AI literally reads:
        CURRENT PAGE URL: .../checkout-step-two.html
        PAGE TITLE: Swag Labs
        PAGE PREVIEW: "Checkout: Overview  Sauce Labs Backpack  ... Cancel  Finish"
    And the prompt explicitly reminds it: "You are on step X/25 of the run."

    When ENABLE_AI_SEMANTIC_SAFETY_TAGS is True, the AI must ALSO classify
    every available element as 0 (safe) / -1 (external site) / -2 (destructive
    undo-progress action). Mechanical tags from element extraction are passed
    in as mechanical_safety_tags so the AI can see what the system ALREADY
    proved (e.g. "this is off-domain") and is less likely to misclassify.

    When ENABLE_EDGE_METADATA_TRACKING is True AND edge_performance_block is
    provided, the AI additionally sees EMPIRICAL PERFORMANCE HISTORY for each
    element with prior attempts (attempts/successes/failures/success_rate/
    last_result/leads_to_node/current_cost) so it can avoid edges with
    known-poor success rates and prefer edges with proven outcomes.
    """
    if mechanical_safety_tags is None:
        mechanical_safety_tags = {}

    recent_actions_text = (
        "; ".join(recent_actions[-RECENT_ACTIONS_MEMORY:])
        if recent_actions else "none yet — this is the FIRST step of the run"
    )

    context_block = ""
    if ENABLE_FULL_PAGE_CONTEXT_FOR_AI:
        page_context_parts = []
        if page_url:
            page_context_parts.append(f"CURRENT PAGE URL: {page_url}")
        if page_title:
            page_context_parts.append(f"PAGE TITLE: {page_title}")
        if step_index is not None and max_steps is not None:
            page_context_parts.append(f"RUN DEPTH: you are on step {step_index + 1} of {max_steps} total allowed steps")
        if page_text_snippet:
            page_context_parts.append(f"PAGE CONTENT PREVIEW (first ~400 chars of visible body text):\n{page_text_snippet}")
        if mechanical_safety_tags:
            mt_preview = {k: v for k, v in list(mechanical_safety_tags.items()) if v != 0}
            if mt_preview:
                t = lambda v: "EXTERNAL_SITE(-1)" if v == SAFETY_EXTERNAL_SITE else "DESTRUCTIVE(-2)"
                page_context_parts.append(
                    "MECHANICALLY PROVEN SAFETY TAGS (the system already confirmed these facts; "
                    "your safety_tags output MUST match or exceed them; do NOT downgrade a "
                    "mechanically proven external/destructive item to 0):\n"
                    + json.dumps({k: f"{v} ({t(v)})" for k, v in mt_preview.items()}, indent=2)
                )
        if edge_performance_block:
            page_context_parts.append(edge_performance_block)
        if page_context_parts:
            context_block = "\nCONTEXT ABOUT THE PAGE YOU ARE CURRENTLY ON (use this to avoid hallucinating buttons from previous pages AND to prefer edges with empirically higher success rates):\n" + "\n".join(page_context_parts) + "\n"

    safety_tag_instruction = ""
    safety_tag_shape = ""
    if ENABLE_AI_SEMANTIC_SAFETY_TAGS:
        safety_tag_instruction = (
            f"\nFinally, you MUST classify EVERY available element with an integer SAFETY TAG: "
            f"0 = safe in-app navigation, "
            f"{SAFETY_EXTERNAL_SITE} (= {SAFETY_EXTERNAL_SITE}) = clicking leaves the current website/domain, "
            f"{SAFETY_DESTRUCTIVE} (= {SAFETY_DESTRUCTIVE}) = destructive / undo-progress actions "
            f"(examples: remove item from cart, cancel order/form, reset/wipe app state, delete account, "
            f"sign out/log out, cancel subscription, close without saving). "
            f"Any element MECHANICALLY PROVEN above as external/destructive MUST receive the matching "
            f"tag in your output; you cannot downgrade those. Any element that leads to a third-party "
            f"social network, marketing site, documentation page, blog, or anything off-main-app = {SAFETY_EXTERNAL_SITE}.\n"
        )
        safety_tag_shape = (
            ', "safety_tags": {"<element1>": 0, "<element2>": -1, "<element3>": -2, ...} '
            '(one entry for EVERY element in the AVAILABLE CLICKABLE OPTIONS list, no omissions)'
        )

    base_prompt = (
        f"USER GOAL: {goal}\n\n"
        f"RECENT ACTIONS ALREADY TAKEN (do not blindly repeat these; consider the full flow): {recent_actions_text}\n"
        f"{context_block}\n"
        f"AVAILABLE CLICKABLE OPTIONS ON SCREEN:\n{available_elements}\n\n"
        "INSTRUCTIONS:\n"
        "Given the CURRENT PAGE CONTEXT above (including any empirical edge performance stats), pick the ONE option that is the most logical NEXT STEP "
        "TOWARD THE GOAL, taking into account what page you are on and how far through the workflow you already are. "
        "WARNING: It is a CRITICAL ERROR to pick a button that existed on a PREVIOUS page that has already been "
        "navigated past (e.g. do NOT say 'Continue' on the final review step where only 'Finish' and 'Cancel' exist — "
        "read the options list and page content carefully).\n\n"
        "Then rank the remaining options as backups, best first, in case the top choice fails to execute. "
        "Give a one-sentence reason for your top choice. If PER_NODE_EDGE_PERFORMANCE_HISTORY is present, "
        "strongly deprioritize edges with consecutive_failures >= 2 OR success_rate < 50% unless no better option exists.\n"
        f"{safety_tag_instruction}\n"
        "Respond with ONLY a raw JSON object in exactly this shape:\n"
        '{"best_choice": "<exact text of one option>", '
        '"ranked_backup": ["<exact text>", "..."], '
        '"reasoning": "<one short sentence>"'
        f"{safety_tag_shape}"
        "}"
    )

    prompt = base_prompt
    for attempt in range(2):
        try:
            response = ollama.chat(model='llama3.2', messages=[{'role': 'user', 'content': prompt}])
            content = response['message']['content'].strip()
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if not json_match:
                error = "no JSON object found in the response"
            else:
                parsed = json.loads(json_match.group(0))
                error = _validate_navigator_response(parsed, available_elements)
                if error is None:
                    return parsed
        except Exception as e:
            error = f"exception while calling the model: {e}"

        print(f"[Navigator] Invalid response on attempt {attempt + 1}: {error}. Retrying with correction.")
        prompt = (
            base_prompt
            + f"\n\nYour previous response was invalid: {error}. "
              "Respond again with ONLY the corrected raw JSON object. Make sure you include ALL required keys, "
              "especially 'safety_tags' with one entry per available element if requested."
        )

    print("[Navigator] Could not get a valid decision after retrying. Falling back to a deterministic default.")
    return None


def generate_scan_report(site_name, target_goal, status, total_steps, nodes_discovered, trajectory_log):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    report_filename = f"scan_report_{timestamp}.md"
    status_text = "SUCCESS" if "SUCCESS" in status else "FAILED"
    markdown_content = f"""# Autonomous Pathfinding Agent Execution Report
**Timestamp:** {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}  
**Target Application:** {site_name}  
**Status:** {status_text} ({status})  

---

## Objective
> {target_goal}

## Summary Metrics
| Metric | Value |
| :--- | :--- |
| **Total Transitions Executed** | {total_steps} |
| **Unique Graph Nodes Discovered** | {nodes_discovered} |

---

## Execution Trajectory (Path Log)

| Step | Source Node | Current URL | Action Taken (Edge) | Assigned Cost |
| :--- | :--- | :--- | :--- | :--- |
"""
    for entry in trajectory_log:
        markdown_content += f"| {entry['step']} | `{entry['node']}` | [{entry['url']}]({entry['url']}) | **{entry['action']}** | `{entry['cost']}` |\n"
    markdown_content += "\n\n---\n*Report generated automatically by Directed State-Graph Pathfinder Agent Framework.*"
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write(markdown_content)
    print(f"\n[REPORT GENERATED] File saved successfully: {os.path.abspath(report_filename)}")


async def close_blocking_overlays(page, target_text):
    """Generic overlay closer — Sauce Labs bm-menu + any [role='dialog'] modals."""
    menu_related_actions = {"open menu", "close menu", "all items", "about", "logout", "reset app state"}
    if target_text.lower() in menu_related_actions:
        return
    try:
        overlay_selectors = [
            ".bm-menu-wrap[aria-hidden='false']",
            "[role='dialog']:visible",
        ]
        for sel in overlay_selectors:
            overlay = page.locator(sel).first
            if await overlay.count() > 0 and await overlay.is_visible(timeout=500):
                print(f"[Overlay] Detected open overlay ({sel}) blocking the page — closing it first.")
                closed = False
                for close_sel in [".bm-cross-button", "[aria-label*='close' i]", "button:has-text('Close')"]:
                    close_btn = page.locator(close_sel).first
                    if await close_btn.count() > 0 and await close_btn.is_visible(timeout=500):
                        await close_btn.click(timeout=2000)
                        closed = True
                        break
                if not closed:
                    await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
    except Exception:
        pass


async def click_with_overlay_recovery(page, locator, target_text):
    try:
        await locator.click(timeout=CLICK_ATTEMPT_TIMEOUT_MS)
        return
    except PlaywrightTimeoutError:
        print(f"[Overlay] Click on '{target_text}' was blocked — attempting recovery.")
        await close_blocking_overlays(page, target_text)
        await locator.click(timeout=CLICK_ATTEMPT_TIMEOUT_MS)


async def build_locator_for_edge(page, edge_text, element_hints):
    """
    Generic selector resolver. element_hints[label] = which attribute produced
    the label ('text', 'value', 'placeholder', 'id', 'special'). We try the
    hinted selector FIRST for maximum accuracy, then fall back.
    """
    if edge_text == "Shopping Cart":
        loc = page.locator(".shopping_cart_link").first
        try:
            if await loc.count() > 0 and await loc.is_visible(timeout=500):
                return loc, True
        except Exception:
            pass
    hint = element_hints.get(edge_text)
    if hint == "id":
        escaped = edge_text.replace('"', '\\"')
        try:
            loc = page.locator(f'[id="{escaped}"]').first
            if await loc.count() > 0 and await loc.is_visible(timeout=500):
                return loc, True
        except Exception:
            pass
    if hint == "value":
        escaped = edge_text.replace('"', '\\"')
        try:
            loc = page.locator(f'input[value="{escaped}"]').first
            if await loc.count() > 0 and await loc.is_visible(timeout=500):
                return loc, True
        except Exception:
            pass
    strategies = [
        ('text', lambda t: page.locator(f'text="{t}"').first),
        ('input_value', lambda t: page.locator(f'input[value="{t}"]').first),
        ('css_id', lambda t: page.locator(f'[id="{t}"]').first),
        ('a_href_id', lambda t: page.locator(f'a[id="{t}"]').first),
    ]
    for _, build in strategies:
        try:
            loc = build(edge_text)
            if await loc.count() > 0 and await loc.is_visible(timeout=500):
                return loc, True
        except Exception:
            continue
    return page.locator(f'text="{edge_text}"').first, False


async def run_pathfinder_agent():
    check_environment()

    try:
        with open("sites_config.json", "r") as f:
            config = json.load(f)
    except FileNotFoundError:
        print("[Error] The file 'sites_config.json' was not found in the working directory.")
        return
    except json.JSONDecodeError:
        print("[Error] 'sites_config.json' contains malformed syntax and could not be parsed.")
        return

    print(f"[Initialization] Initializing Directed State-Graph Pathfinder Agent for: {config.get('site_name', 'Target App')}")

    max_search_depth = 25

    # ---------------------------------------------------------------------
    # STATE MANAGER — authoritative mechanical ledger. LLM never writes here.
    # Owns: current_state / previous_state / state_history /
    #       visited_nodes / transition_history
    # ---------------------------------------------------------------------
    state_mgr = StateManager(max_search_depth=max_search_depth)

    scan_status = "MAX_DEPTH_EXHAUSTED"
    # Backwards-compat read-only mirrors: populated from StateManager exports
    # on the fly via property access. Kept for generate_scan_report() legacy
    # call site; prefer state_mgr.export_trajectory_table_rows() in new code.
    trajectory_log = None

    victory_text_matches = config.get("victory_conditions", {}).get("text_matches", [])
    victory_url_subs = config.get("victory_conditions", {}).get("url_substrings", [])
    autofill_rules = config.get("form_autofill", [])
    target_selectors = config.get("target_elements_query", "button, a")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()

        page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))

        print(f"[*] Navigating to initial root node: {config['portal_url']}")
        try:
            await page.goto(config["portal_url"], wait_until="domcontentloaded", timeout=20000)
        except PlaywrightTimeoutError:
            print("[Warning] Initial page load was slow; proceeding with what has loaded so far.")
        await safe_wait_for_load(page)

        try:
            await page.locator("[placeholder*='user' i], input[type='text'], input[type='email']").first.fill(config["credentials"]["username"])
            await page.locator("[placeholder*='pass' i], input[type='password']").first.fill(config["credentials"]["password"])
            login_selectors = ["#login-button", "input[type='submit']", "button[type='submit']", "button:has-text('Log In')", "button:has-text('Sign In')"]
            authenticated = False
            for selector in login_selectors:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click()
                        authenticated = True
                        break
                except Exception:
                    continue
            if not authenticated:
                await page.keyboard.press("Enter")
            await safe_wait_for_load(page)
            print("[+] Root node authenticated. Entering graph exploration phase.")
        except Exception as e:
            print(f"[Error] Authentication Node Failure: {e}")
            await browser.close()
            return

        # ---------------------------------------------------------------------
        # Edge cost & metadata ledgers — navigation-only concerns (not state).
        # The STATE MANAGER owns the visited / transition / history ledgers;
        # these dicts own purely how much an edge costs to try next time.
        # ---------------------------------------------------------------------
        edge_weights = {}
        edge_metadata_store = {}   # (node_hash, element_str) -> edge dict (user schema)

        # Mechanical DOM-signal snapshots for last_result inference (kept
        # between loop iterations — these are NAVIGATION / scoring inputs,
        # not authoritative state snapshots; state lives in state_mgr only).
        prev_cart_badge = None
        prev_form_input_count = None
        pending_transition_seq = None  # sequence_id from record_intent()

        # Helper: if metadata enabled, ensure an edge dict exists and return it;
        # if disabled, return None. Callers treat None as "no metadata path".
        def get_or_create_edge(node, elem):
            if not ENABLE_EDGE_METADATA_TRACKING:
                return None
            key = (node, elem)
            if key not in edge_metadata_store:
                edge_metadata_store[key] = create_edge_metadata(elem)
            return edge_metadata_store[key]

        # ---- State Manager: INITIAL post-auth snapshot --------------------
        # Run a first-pass extract + hash so StateManager's 5 pillars are warm
        # before the first loop iteration.
        await safe_wait_for_load(page)
        _init_url = page.url
        _init_extract = await page.evaluate("""(args) => {
            const [selectorQuery, enableMechanical] = args;
            const elements = Array.from(document.querySelectorAll(selectorQuery));
            const visible = elements.filter(el => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            });
            const labels = [];
            const mechanical_safety_tags = {};
            const pageOrigin = window.location.origin;
            for (const el of visible) {
                let label;
                if (el.classList.contains('shopping_cart_link') || el.closest('.shopping_cart_link')) {
                    label = 'Shopping Cart';
                } else if (el.innerText && el.innerText.trim().length > 1) {
                    label = el.innerText.trim();
                } else if (el.value && String(el.value).trim().length > 1) {
                    label = String(el.value).trim();
                } else if (el.placeholder && String(el.placeholder).trim().length > 1) {
                    label = String(el.placeholder).trim();
                } else if (el.id && el.id.trim().length > 1) {
                    label = el.id.trim();
                } else {
                    continue;
                }
                if (label.includes('btn_secondary')) continue;
                if (!labels.includes(label)) {
                    labels.push(label);
                    if (enableMechanical) {
                        const href = el.getAttribute && el.getAttribute('href');
                        if (href && (href.startsWith('http:') || href.startsWith('https:') || href.startsWith('//'))) {
                            try {
                                const linkUrl = new URL(href, window.location.href);
                                if (linkUrl.origin !== pageOrigin) {
                                    mechanical_safety_tags[label] = -1;
                                }
                            } catch(e) {}
                        }
                    }
                }
            }
            return { labels, mechanical_safety_tags };
        }""", [target_selectors, ENABLE_MECHANICAL_EXTERNAL_DETECTION])
        _init_elements = _init_extract["labels"]
        _init_externals = sum(1 for v in _init_extract.get("mechanical_safety_tags", {}).values() if v == SAFETY_EXTERNAL_SITE)
        _init_hash = compute_node_hash(_init_url, _init_elements)
        try:
            _init_title = await page.title()
        except Exception:
            _init_title = None
        try:
            _cart = page.locator(".shopping_cart_badge").first
            _init_cart = int((await _cart.text_content(timeout=600) or "").strip()) if await _cart.count() > 0 and await _cart.is_visible(timeout=400) else 0
        except Exception:
            _init_cart = 0
        try:
            _init_forms = await page.locator("input[type='text'], input[type='number'], textarea, input:not([type])").count()
        except Exception:
            _init_forms = 0
        state_mgr.snapshot_initial(
            node_hash=_init_hash, url=_init_url, page_title=_init_title,
            available_elements=_init_elements, cart_badge_count=_init_cart,
            form_input_count=_init_forms, step_index=0,
            externals_count=_init_externals,
        )
        print(f"[StateMgr] Ledger initialized. Root node: {_init_hash} | "
              f"visited_nodes={state_mgr.unique_nodes_discovered} | "
              f"state_history={len(state_mgr.state_history)}")

        for step in range(max_search_depth):
            await safe_wait_for_load(page)
            await asyncio.sleep(1.5)

            current_url = page.url
            current_ui_text = await page.evaluate("() => document.body.innerText")
            # Mechanical DOM signals for last_result inference (scoring-only,
            # NOT authoritative state — state is committed via StateManager).
            try:
                cart_badge_el = page.locator(".shopping_cart_badge").first
                if await cart_badge_el.count() > 0 and await cart_badge_el.is_visible(timeout=500):
                    cart_badge_text = (await cart_badge_el.text_content(timeout=800) or "").strip()
                    current_cart_badge = int(cart_badge_text) if cart_badge_text.isdigit() else 0
                else:
                    current_cart_badge = 0
            except Exception:
                current_cart_badge = 0
            try:
                current_form_input_count = await page.locator(
                    "input[type='text'], input[type='number'], textarea, input:not([type])"
                ).count()
            except Exception:
                current_form_input_count = 0

            # --- EXTRACT ELEMENTS + MECHANICAL SAFETY TAGS -----------------
            extract_result = await page.evaluate("""(args) => {
                const [selectorQuery, enableMechanical] = args;
                const elements = Array.from(document.querySelectorAll(selectorQuery));
                const visible = elements.filter(el => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                });
                const labels = [];
                const hints = {};
                const mechanical_safety_tags = {};
                const pageOrigin = window.location.origin;
                for (const el of visible) {
                    let label;
                    let source;
                    if (el.classList.contains('shopping_cart_link') || el.closest('.shopping_cart_link')) {
                        label = 'Shopping Cart';
                        source = 'special';
                    } else if (el.innerText && el.innerText.trim().length > 1) {
                        label = el.innerText.trim();
                        source = 'text';
                    } else if (el.value && String(el.value).trim().length > 1) {
                        label = String(el.value).trim();
                        source = 'value';
                    } else if (el.placeholder && String(el.placeholder).trim().length > 1) {
                        label = String(el.placeholder).trim();
                        source = 'placeholder';
                    } else if (el.id && el.id.trim().length > 1) {
                        label = el.id.trim();
                        source = 'id';
                    } else {
                        continue;
                    }
                    if (label.includes('btn_secondary')) continue;
                    if (!labels.includes(label)) {
                        labels.push(label);
                        hints[label] = source;
                        if (enableMechanical) {
                            const href = el.getAttribute && el.getAttribute('href');
                            if (href && (href.startsWith('http:') || href.startsWith('https:') || href.startsWith('//'))) {
                                try {
                                    const linkUrl = new URL(href, window.location.href);
                                    if (linkUrl.origin !== pageOrigin) {
                                        mechanical_safety_tags[label] = -1;
                                    }
                                } catch(e) {}
                            }
                        }
                    }
                }
                return { labels, hints, mechanical_safety_tags };
            }""", [target_selectors, ENABLE_MECHANICAL_EXTERNAL_DETECTION])
            available_elements = extract_result["labels"]
            element_hints = extract_result["hints"]
            mechanical_safety_tags = extract_result.get("mechanical_safety_tags", {})
            externals_count = sum(1 for v in mechanical_safety_tags.values() if v == SAFETY_EXTERNAL_SITE)
            if ENABLE_MECHANICAL_EXTERNAL_DETECTION and externals_count > 0:
                externals = [k for k, v in mechanical_safety_tags.items() if v == SAFETY_EXTERNAL_SITE]
                print(f"[Safety] Mechanical detector tagged {len(externals)} off-domain elements: {externals}")

            current_node = compute_node_hash(current_url, available_elements)

            # --- VICTORY CHECK (before snapshot so the snapshot captures victory phase) ---
            matched_text = any(msg.lower() in current_ui_text.lower() for msg in victory_text_matches)
            matched_url = any(sub.lower() in current_url.lower() for sub in victory_url_subs)

            # --- RESOLVE PENDING TRANSITION from previous iteration ---------
            # If we had a record_intent() open, finalise it now that we know
            # the post-click node hash, cart delta, and outcome class.
            if pending_transition_seq is not None:
                # last pending intent was from state_mgr.previous_state.node_hash
                _prev_node_hash = state_mgr.previous_state.node_hash if state_mgr.previous_state else None
                _prev_action = None
                for _pt in reversed(state_mgr.transition_history):
                    if _pt.sequence_id == pending_transition_seq:
                        _prev_action = _pt.action_label
                        break

                if matched_text or matched_url:
                    # Victory from the previous click
                    cart_delta = (current_cart_badge - prev_cart_badge) if prev_cart_badge is not None else 0
                    form_delta = (current_form_input_count - prev_form_input_count) if prev_form_input_count is not None else 0
                    state_mgr.record_outcome(
                        pending_transition_seq,
                        destination_node_hash="VICTORY",
                        outcome="victory",
                        last_result_tag=RESULT_VICTORY_HIT,
                        cart_delta=cart_delta,
                        form_input_delta=form_delta,
                    )
                    # Also write VICTORY into the edge-metadata ledger for backwards compat
                    if _prev_node_hash is not None and _prev_action is not None:
                        _m = get_or_create_edge(_prev_node_hash, _prev_action)
                        if _m is not None:
                            record_edge_result(_m, True, RESULT_VICTORY_HIT, None)
                            _m["destination"] = "VICTORY"
                elif current_node == _prev_node_hash:
                    # Futile action / same-node — heavy penalty
                    _src = _prev_node_hash
                    _act = _prev_action
                    old_cost = 0
                    if _src is not None and _act is not None:
                        old_cost = edge_weights.get((_src, _act), 0)
                        edge_weights[(_src, _act)] = old_cost + FUTILE_ACTION_PENALTY
                        _m = get_or_create_edge(_src, _act)
                        if _m is not None:
                            record_edge_result(_m, False, RESULT_SUCCESS_SAME_NODE,
                                               destination_node=current_node,
                                               updated_cost=old_cost + FUTILE_ACTION_PENALTY)
                    state_mgr.record_outcome(
                        pending_transition_seq,
                        destination_node_hash=current_node,
                        outcome="failed_no_state_change",
                        last_result_tag=RESULT_SUCCESS_SAME_NODE,
                        cart_delta=0,
                        form_input_delta=0,
                        failure_reason="same_node_hash_after_click",
                    )
                    if _act is not None:
                        print(f"[LoopGuard] Last action '{_act}' produced no state change. "
                              f"Penalizing: {old_cost} -> {old_cost + FUTILE_ACTION_PENALTY}")
                else:
                    # State actually changed — infer and commit success
                    inferred = RESULT_SUCCESS_NODE_CHANGED
                    if prev_cart_badge is not None:
                        if current_cart_badge > prev_cart_badge:
                            inferred = RESULT_CART_COUNT_INCREASED
                        elif current_cart_badge < prev_cart_badge:
                            inferred = RESULT_CART_COUNT_DECREASED
                    elif prev_form_input_count is not None and current_form_input_count < prev_form_input_count:
                        inferred = RESULT_FORM_SUBMITTED
                    cart_delta = (current_cart_badge - prev_cart_badge) if prev_cart_badge is not None else 0
                    form_delta = (current_form_input_count - prev_form_input_count) if prev_form_input_count is not None else 0
                    state_mgr.record_outcome(
                        pending_transition_seq,
                        destination_node_hash=current_node,
                        outcome="success",
                        last_result_tag=inferred,
                        cart_delta=cart_delta,
                        form_input_delta=form_delta,
                    )
                    # Also keep edge_metadata_store in sync (backwards-compat scoring)
                    if _prev_node_hash is not None and _prev_action is not None:
                        _m = get_or_create_edge(_prev_node_hash, _prev_action)
                        if _m is not None:
                            new_cost = edge_weights.get((_prev_node_hash, _prev_action), _m["cost"])
                            record_edge_result(_m, True, inferred,
                                               destination_node=current_node,
                                               updated_cost=new_cost)
                pending_transition_seq = None

            # Now handle the victory BRANCH after resolving the pending transition
            if matched_text or matched_url:
                # --- STATE MANAGER: snapshot with victory phase -------------
                try:
                    _pg_title = await page.title()
                except Exception:
                    _pg_title = None
                state_mgr.snapshot_before_action(
                    node_hash=current_node, url=current_url, page_title=_pg_title,
                    available_elements=available_elements,
                    cart_badge_count=current_cart_badge,
                    form_input_count=current_form_input_count,
                    step_index=step,
                    externals_count=externals_count,
                    victory_match=True,
                    snapshot_notes="victory condition match",
                )
                state_mgr.set_final_status("SUCCESS_TARGET_REACHED")
                print(f"\n[SUCCESS] Targeted Destination Node Reached in {step} structural transitions!")
                scan_status = "SUCCESS_TARGET_REACHED"
                break

            # --- STATE MANAGER: BEFORE-ACTION SNAPSHOT ----------------------
            # Commits current_state / previous_state / state_history / visited_nodes
            # This is the authoritative ledger write — nothing else writes these.
            try:
                _pg_title = await page.title()
            except Exception:
                _pg_title = None
            state_mgr.snapshot_before_action(
                node_hash=current_node, url=current_url, page_title=_pg_title,
                available_elements=available_elements,
                cart_badge_count=current_cart_badge,
                form_input_count=current_form_input_count,
                step_index=step,
                externals_count=externals_count,
                victory_match=False,
            )
            steps_taken = state_mgr.steps_taken  # authoritative counter

            prev_cart_badge = current_cart_badge
            prev_form_input_count = current_form_input_count

            print(f"\n[Node: {current_node}] URL: {current_url} | Active Structural Edges: {available_elements}")
            print(f"[StateMgr] state_history={len(state_mgr.state_history)} | "
                  f"visited_nodes={state_mgr.unique_nodes_discovered} | "
                  f"transitions={state_mgr.steps_taken} | "
                  f"prev_hash={state_mgr.previous_state.node_hash if state_mgr.previous_state else 'none'}")

            # --- Autofill forms ---
            form_inputs = await page.locator("input[type='text'], input[type='number'], textarea, input:not([type])").all()
            if form_inputs:
                for el in form_inputs:
                    placeholder = (await el.get_attribute("placeholder") or "").lower()
                    id_attr = (await el.get_attribute("id") or "").lower()
                    name_attr = (await el.get_attribute("name") or "").lower()
                    combined_attributes = f"{placeholder} {id_attr} {name_attr}"
                    for rule in autofill_rules:
                        if any(keyword.lower() in combined_attributes for keyword in rule["keywords"]):
                            current_val = await el.input_value()
                            if current_val != rule["value"]:
                                await el.fill(rule["value"])
                            break

            # --- SCORING: decide if fresh AI call or use cached weights ----
            # Authoritative action count lives in StateManager.action_count —
            # the stale-score engine consults it via actions_since_first_visit().
            need_ai_call = False
            if not state_mgr.was_visited_before(current_node):
                # First visit — always ask AI
                need_ai_call = True
            else:
                if ENABLE_STALE_SCORE_INVALIDATION:
                    actions_since = state_mgr.actions_since_first_visit(current_node)
                    if actions_since >= STALE_SCORE_REASK_THRESHOLD:
                        _first_act = state_mgr.get_action_count_at_first_visit(current_node)
                        print(f"[StaleScore] Node {current_node} was scored at action-count={_first_act}, "
                              f"now at action-count={state_mgr.action_count}. "
                              f"Context materially changed; re-asking AI.")
                        # Re-stamp the first_action_count via a fresh ledger touch
                        _rec = state_mgr.get_visited_record(current_node)
                        if _rec is not None:
                            _rec.first_action_count = state_mgr.action_count
                        need_ai_call = True

            # Build the empirical performance block BEFORE asking the AI, so the
            # LLM can see attempts/successes/failures and avoid known-bad edges.
            per_node_perf_block = build_per_node_edge_performance_block(
                current_node, edge_metadata_store, available_elements
            )
            if per_node_perf_block:
                # Short print so user sees that it's being consulted.
                n_lines = per_node_perf_block.count("\n")
                print(f"[EdgeMemory] Found {n_lines} elements with prior empirical performance history on this node.")

            if need_ai_call:
                # Apply the IRREVERSIBLE hard block (original logic) before anything else.
                irreversible_blocklist = [
                    "log out", "logout", "sign out", "delete account",
                    "cancel subscription", "deactivate account",
                ]
                safe_elements = [
                    e for e in available_elements
                    if not any(k in e.lower() for k in irreversible_blocklist)
                ]
                for e in available_elements:
                    if e not in safe_elements:
                        edge_weights[(current_node, e)] = 999
                        m = get_or_create_edge(current_node, e)
                        if m is not None:
                            m["cost"] = 999

                # Ensure metadata records exist (for performance tracking in AI prompt):
                for e in safe_elements:
                    get_or_create_edge(current_node, e)

                # Gather FULL PAGE CONTEXT for the AI (only when enabled):
                page_title = None
                page_text_snippet = None
                if ENABLE_FULL_PAGE_CONTEXT_FOR_AI:
                    try:
                        page_title = await page.title()
                    except Exception:
                        page_title = None
                    page_text_snippet = (current_ui_text or "").strip().replace("\n", " ")[:420]
                    if len(current_ui_text or "") > 420:
                        page_text_snippet += "..."

                # Read-only snapshot from StateManager for the AI prompt.
                # The AI sees it but never writes back to the ledger directly.
                _recent_actions_snapshot = [
                    t.action_label for t in state_mgr.transition_history
                    if not t.action_label.startswith("__BACKTRACK__")
                ][-RECENT_ACTIONS_MEMORY:]

                decision = ask_ai_navigator(
                    safe_elements,
                    config["ai_context"],
                    _recent_actions_snapshot,
                    page_url=current_url,
                    page_title=page_title,
                    page_text_snippet=page_text_snippet,
                    step_index=step,
                    max_steps=max_search_depth,
                    mechanical_safety_tags=mechanical_safety_tags,
                    edge_performance_block=per_node_perf_block,
                )

                ai_ranked = set()
                if decision is not None:
                    ai_ranked.add(decision["best_choice"])
                    edge_weights[(current_node, decision["best_choice"])] = 1
                    m_best = get_or_create_edge(current_node, decision["best_choice"])
                    if m_best is not None:
                        m_best["cost"] = 1
                    for rank, edge in enumerate(decision.get("ranked_backup", []), start=2):
                        if edge in safe_elements:
                            edge_weights[(current_node, edge)] = rank
                            ai_ranked.add(edge)
                            m_r = get_or_create_edge(current_node, edge)
                            if m_r is not None:
                                m_r["cost"] = rank

                    # --- Apply SAFETY TAGS (MECHANICAL wins over AI) --------
                    # Priority order:
                    #   1. Mechanical tag (-1 from origin mismatch): always wins (fact, not opinion).
                    #   2. AI's explicit #1 ranking: overrides safety cost (agent explicitly chose it).
                    #   3. AI's own safety_tags output.
                    #   4. Default 10.
                    ai_safety_tags_raw = decision.get("safety_tags", {}) if ENABLE_AI_SEMANTIC_SAFETY_TAGS else {}

                    for e in safe_elements:
                        if e in ai_ranked and e == decision["best_choice"]:
                            pass  # AI #1 pick stays cost 1 regardless of tag (explicit choice)
                        else:
                            mec_tag = mechanical_safety_tags.get(e, 0)
                            ai_tag = ai_safety_tags_raw.get(e, 0) if isinstance(ai_safety_tags_raw, dict) else 0
                            final_tag = 0
                            if mec_tag != 0:
                                final_tag = mec_tag
                            elif ai_tag in (SAFETY_EXTERNAL_SITE, SAFETY_DESTRUCTIVE):
                                final_tag = ai_tag
                            if final_tag == SAFETY_EXTERNAL_SITE and e not in ai_ranked:
                                edge_weights[(current_node, e)] = COST_FOR_SAFETY_EXTERNAL
                                m_s = get_or_create_edge(current_node, e)
                                if m_s is not None:
                                    m_s["cost"] = COST_FOR_SAFETY_EXTERNAL
                            elif final_tag == SAFETY_DESTRUCTIVE and e not in ai_ranked:
                                edge_weights[(current_node, e)] = COST_FOR_SAFETY_DESTRUCTIVE
                                m_s = get_or_create_edge(current_node, e)
                                if m_s is not None:
                                    m_s["cost"] = COST_FOR_SAFETY_DESTRUCTIVE

                    # --- Default for unranked non-tagged elements ----------
                    for e in safe_elements:
                        edge_weights.setdefault((current_node, e), 10)
                        m_s = get_or_create_edge(current_node, e)
                        if m_s is not None and (current_node, e) in edge_weights:
                            m_s["cost"] = edge_weights[(current_node, e)]
                    for e in safe_elements:
                        if e not in ai_ranked and e not in mechanical_safety_tags:
                            aitag = 0
                            if ENABLE_AI_SEMANTIC_SAFETY_TAGS and isinstance(decision.get("safety_tags"), dict):
                                aitag = decision["safety_tags"].get(e, 0)
                            if aitag == 0:
                                edge_weights.setdefault((current_node, e), 10)
                                m_s = get_or_create_edge(current_node, e)
                                if m_s is not None:
                                    m_s["cost"] = edge_weights.setdefault((current_node, e), m_s["cost"])
                else:
                    # Fallback — AI failed twice. Use mechanical + generic progress hints.
                    print("[Fallback] AI decision unavailable — applying mechanical tags + progress tiebreak.")
                    progress_keywords = [
                        "finish", "submit", "confirm", "checkout", "continue",
                        "save", "next", "proceed", "place order",
                    ]
                    for e in safe_elements:
                        mec_tag = mechanical_safety_tags.get(e, 0)
                        if mec_tag == SAFETY_EXTERNAL_SITE:
                            c = COST_FOR_SAFETY_EXTERNAL
                        elif mec_tag == SAFETY_DESTRUCTIVE:
                            c = COST_FOR_SAFETY_DESTRUCTIVE
                        elif any(k in e.lower() for k in progress_keywords):
                            c = 8
                        else:
                            c = 10
                        edge_weights.setdefault((current_node, e), c)
                        m_s = get_or_create_edge(current_node, e)
                        if m_s is not None:
                            m_s["cost"] = c

            # Final edge cost = min over available_elements of
            # compute_current_edge_cost(metadata_edge_dict, base_subjective_cost).
            # Subjective edge_weights stay intact so kill-switch = False still gives
            # original behavior exactly.
            def effective_cost(elem):
                key = (current_node, elem)
                base = edge_weights.get(key, 10)
                meta = edge_metadata_store.get(key) if ENABLE_EDGE_METADATA_TRACKING else None
                return compute_current_edge_cost(meta, base)

            valid_edges = [edge for edge in available_elements if effective_cost(edge) < 999]
            if not valid_edges:
                print(f"[Dead End] Node {current_node} fully exhausted. Backtracking...")
                _backtrack_from = current_node
                _backtrack_to = state_mgr.pop_breadcrumb()
                if _backtrack_to is not None:
                    # Mechanical backtrack — record into transition_history
                    state_mgr.record_backtrack(_backtrack_from, _backtrack_to, reason="dead_end_all_edges_999")
                    await page.go_back()
                    # Reset DOM-signal snapshots because the new page is old history
                    prev_cart_badge = None
                    prev_form_input_count = None
                    pending_transition_seq = None
                    continue
                else:
                    state_mgr.set_final_status("GRAPH_COMPLETELY_EXHAUSTED")
                    print("[Error] Complete accessible graph workspace exhausted.")
                    scan_status = "GRAPH_COMPLETELY_EXHAUSTED"
                    break

            best_edge = min(valid_edges, key=effective_cost)
            chosen_final_cost = effective_cost(best_edge)
            print(f"-> Traversing Edge: '{best_edge}' (Path Cost: {chosen_final_cost})")

            # --- STATE MANAGER: record intent (opens pending Transition) ---
            # This is the authoritative write to transition_history. The LLM
            # did NOT touch these structures — its JSON output was pure input
            # to the cost function above; the ledger mutation happens here.
            pending_transition_seq = state_mgr.record_intent(
                source_node_hash=current_node,
                action_label=best_edge,
                assigned_cost=chosen_final_cost,
            )

            _click_failed_outcome = None
            _click_failed_reason = None
            try:
                locator, resolved_ok = await build_locator_for_edge(page, best_edge, element_hints)
                if not resolved_ok:
                    print(f"[Locator] No selector matched '{best_edge}' before attempt — will try generic but expect possible failure.")
                try:
                    if await locator.count() == 0 or not await locator.is_visible(timeout=700):
                        raise RuntimeError(f"Element '{best_edge}' not visible after all selector fallbacks.")
                except Exception as e:
                    edge_weights[(current_node, best_edge)] = 999
                    m_fail = get_or_create_edge(current_node, best_edge)
                    if m_fail is not None:
                        record_edge_result(m_fail, False, RESULT_FAILURE_NOT_VISIBLE,
                                           updated_cost=999)
                    _click_failed_outcome = "failed_not_visible"
                    _click_failed_reason = f"element_not_visible: {e}"
                    raise
                try:
                    await locator.scroll_into_view_if_needed(timeout=CLICK_ATTEMPT_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    edge_weights[(current_node, best_edge)] = 999
                    m_fail = get_or_create_edge(current_node, best_edge)
                    if m_fail is not None:
                        record_edge_result(m_fail, False, RESULT_FAILURE_SCROLL_TIMEOUT,
                                           updated_cost=999)
                    _click_failed_outcome = "failed_scroll"
                    _click_failed_reason = "scroll_into_view_timeout"
                    raise RuntimeError(f"scroll_into_view timed out for '{best_edge}' — likely behind overlay or detached.")
                await click_with_overlay_recovery(page, locator, best_edge)
                # Breadcrumb was pushed inside record_intent() already; mirror
                # for the legacy edge_metadata ledger + cost bump on success.
                bumped = edge_weights.get((current_node, best_edge), 1) + 2
                edge_weights[(current_node, best_edge)] = bumped
                m_ok = get_or_create_edge(current_node, best_edge)
                if m_ok is not None:
                    m_ok["cost"] = bumped
            except Exception as edge_err:
                print(f"[Error] Traversal Boundary Blocked: {edge_err}")
                final_fail_cost = 999
                edge_weights[(current_node, best_edge)] = final_fail_cost
                m_fail = get_or_create_edge(current_node, best_edge)
                if m_fail is not None and m_fail.get("attempts", 0) > 0 and m_fail["last_result"] in (
                    RESULT_FAILURE_NOT_VISIBLE, RESULT_FAILURE_SCROLL_TIMEOUT,
                ):
                    m_fail["cost"] = final_fail_cost
                elif m_fail is not None:
                    record_edge_result(m_fail, False, RESULT_FAILURE_CLICK_EXCEPTION,
                                       updated_cost=final_fail_cost)
                # Close out the pending Transition as failed in the ledger.
                if _click_failed_outcome is None:
                    _click_failed_outcome = "failed_click"
                if _click_failed_reason is None:
                    _click_failed_reason = str(edge_err)[:220]
                state_mgr.record_outcome(
                    pending_transition_seq,
                    destination_node_hash=current_node,  # never moved
                    outcome=_click_failed_outcome,
                    last_result_tag=RESULT_FAILURE_CLICK_EXCEPTION,
                    cart_delta=0,
                    form_input_delta=0,
                    failure_reason=_click_failed_reason,
                )
                pending_transition_seq = None

        # --- End of main loop ----------------------------------------------
        # Final bookkeeping: if we left the loop because max_search_depth was
        # exhausted, stamp the StateManager final status accordingly.
        if scan_status == "MAX_DEPTH_EXHAUSTED":
            state_mgr.set_final_status("MAX_DEPTH_EXHAUSTED")
        # Also guarantee that any still-pending transition is closed.
        if pending_transition_seq is not None:
            # This should not happen; loop exit with pending means last
            # iteration didn't reach post-click resolution. Close defensively.
            state_mgr.record_outcome(
                pending_transition_seq,
                destination_node_hash=(
                    state_mgr.current_state.node_hash if state_mgr.current_state else None
                ),
                outcome="failed_click",
                last_result_tag=RESULT_FAILURE_CLICK_EXCEPTION,
                failure_reason="loop_exit_with_pending_transition",
            )
            pending_transition_seq = None

        print("\n===== DIRECTED PATHFINDING TRANSACTION MATRIX COMPLETE =====")
        if ENABLE_EDGE_METADATA_TRACKING:
            n_with_history = sum(1 for ed in edge_metadata_store.values() if ed["attempts"] > 0)
            print(f"[EdgeMemory] Post-run summary: {len(edge_metadata_store)} tracked edge records, "
                  f"{n_with_history} with empirical history.")
        _ledger = state_mgr.export_state_for_report()
        print(f"[StateMgr] Ledger summary: {json.dumps(_ledger, indent=2, default=str)}")
        await browser.close()

        # ---- REPORT: authoritative data now comes from StateManager ----
        report_trajectory_rows = state_mgr.export_trajectory_table_rows()
        # Filter out mechanical __BACKTRACK__ pseudo-actions from the
        # human-facing trajectory table (still preserved in transition_history).
        report_rows_filtered = [
            r for r in report_trajectory_rows
            if not r["action"].startswith("__BACKTRACK__")
        ]
        generate_scan_report(
            site_name=config.get("site_name", "Target Application"),
            target_goal=config["ai_context"],
            status=scan_status,
            total_steps=len(report_rows_filtered),
            nodes_discovered=state_mgr.unique_nodes_discovered,
            trajectory_log=report_rows_filtered,
        )

if __name__ == "__main__":
    asyncio.run(run_pathfinder_agent())
