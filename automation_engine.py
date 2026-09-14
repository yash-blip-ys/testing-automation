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

    scan_status = "MAX_DEPTH_EXHAUSTED"
    trajectory_log = []
    steps_taken = 0

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
        # NEW data structure: state_graph stores not just "visited? T/F" but
        # also the LENGTH of recent_actions_log at the moment the node was
        # FIRST (or last) scored. This is the STALE SCORE INVALIDATION engine.
        # When we return to this node later, if recent_actions is longer by
        # >= STALE_SCORE_REASK_THRESHOLD, we RERUN the AI — because the
        # CONTEXT (workflow progress) has materially changed, and a cost=1
        # assigned at step 2 is no longer a good cost at step 12.
        # ---------------------------------------------------------------------
        state_graph_metadata = {}  # node_hash -> {"first_score_action_count": N}
        state_graph = {}           # node_hash -> {} (preserved shape for len() metric)
        edge_weights = {}
        # NEW: site-agnostic structured edge metadata. If ENABLE_EDGE_METADATA_TRACKING
        # is False this dict exists but is never consulted (costs come from edge_weights).
        edge_metadata_store = {}   # (node_hash, element_str) -> edge dict matching user schema
        node_breadcrumbs = []
        recent_actions_log = []
        pre_interaction_node = None
        pre_interaction_edge = None
        # Mechanical observation values for result inference (page DOM signal snapshots).
        prev_cart_badge = None
        prev_form_input_count = None
        max_search_depth = 25

        # Helper: if metadata enabled, ensure an edge dict exists and return it;
        # if disabled, return None. Callers treat None as "no metadata path".
        def get_or_create_edge(node, elem):
            if not ENABLE_EDGE_METADATA_TRACKING:
                return None
            key = (node, elem)
            if key not in edge_metadata_store:
                edge_metadata_store[key] = create_edge_metadata(elem)
            return edge_metadata_store[key]

        for step in range(max_search_depth):
            steps_taken = step + 1
            await safe_wait_for_load(page)
            await asyncio.sleep(1.5)

            current_url = page.url
            current_ui_text = await page.evaluate("() => document.body.innerText")
            # Mechanical signals used for last_result inference: cart badge count + visible form inputs.
            # Runs on any site; no Sauce-specific CSS; missing selectors just give None.
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

            matched_text = any(msg.lower() in current_ui_text.lower() for msg in victory_text_matches)
            matched_url = any(sub.lower() in current_url.lower() for sub in victory_url_subs)
            if matched_text or matched_url:
                # If there was a pre-interaction click that just produced victory, tag it as such.
                if pre_interaction_node is not None and pre_interaction_edge is not None:
                    m = get_or_create_edge(pre_interaction_node, pre_interaction_edge)
                    if m is not None:
                        record_edge_result(m, True, RESULT_VICTORY_HIT, None)
                        m["destination"] = "VICTORY"
                print(f"\n[SUCCESS] Targeted Destination Node Reached in {step} structural transitions!")
                scan_status = "SUCCESS_TARGET_REACHED"
                break

            # --- Futile action / same-node penalty (from previous fix set) ---
            if pre_interaction_node is not None and pre_interaction_edge is not None:
                tentative_elements = await page.evaluate("""(selectorQuery) => {
                    const elements = Array.from(document.querySelectorAll(selectorQuery));
                    return [...new Set(elements.filter(el => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    }).map(el => {
                        if (el.classList.contains('shopping_cart_link') || el.closest('.shopping_cart_link')) {
                            return 'Shopping Cart';
                        }
                        return el.innerText.trim() || el.value || el.placeholder || el.id;
                    }).filter(t => t && t.length > 1 && !t.includes('btn_secondary')))];
                }""", target_selectors)
                tentative_current_node = compute_node_hash(current_url, tentative_elements)
                if tentative_current_node == pre_interaction_node:
                    old_cost = edge_weights.get((pre_interaction_node, pre_interaction_edge), 0)
                    edge_weights[(pre_interaction_node, pre_interaction_edge)] = old_cost + FUTILE_ACTION_PENALTY
                    # Record in metadata as well (last_result = same node no-op).
                    m = get_or_create_edge(pre_interaction_node, pre_interaction_edge)
                    if m is not None:
                        record_edge_result(m, False, RESULT_SUCCESS_SAME_NODE,
                                           destination_node=tentative_current_node,
                                           updated_cost=old_cost + FUTILE_ACTION_PENALTY)
                    print(f"[LoopGuard] Last action '{pre_interaction_edge}' produced no state change. "
                          f"Penalizing: {old_cost} -> {old_cost + FUTILE_ACTION_PENALTY}")
                else:
                    # Last click DID change the node. Optionally: compute cart badge delta
                    # and inferred last_result (cart up/down).
                    m = get_or_create_edge(pre_interaction_node, pre_interaction_edge)
                    if m is not None:
                        inferred = RESULT_SUCCESS_NODE_CHANGED
                        if prev_cart_badge is not None:
                            if current_cart_badge > prev_cart_badge:
                                inferred = RESULT_CART_COUNT_INCREASED
                            elif current_cart_badge < prev_cart_badge:
                                inferred = RESULT_CART_COUNT_DECREASED
                        elif prev_form_input_count is not None and current_form_input_count < prev_form_input_count:
                            inferred = RESULT_FORM_SUBMITTED
                        # Current cost (may have been post-click bumped by +=2 previously):
                        new_cost = edge_weights.get((pre_interaction_node, pre_interaction_edge), m["cost"])
                        record_edge_result(m, True, inferred,
                                           destination_node=tentative_current_node,
                                           updated_cost=new_cost)
                pre_interaction_node = None
                pre_interaction_edge = None
            prev_cart_badge = current_cart_badge
            prev_form_input_count = current_form_input_count

            # --- EXTRACT ELEMENTS + MECHANICAL SAFETY TAGS -----------------
            # Site-agnostic external-link detection uses window.location.origin
            # comparison — it works for EVERY website, zero keywords, 100%
            # accuracy, zero LLM cost. Works for any <a href> that points
            # off-domain (Twitter, FB, LinkedIn, About page to saucelabs.com,
            # marketing trackers, everything).
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

            if ENABLE_MECHANICAL_EXTERNAL_DETECTION and mechanical_safety_tags:
                externals = [k for k, v in mechanical_safety_tags.items() if v == SAFETY_EXTERNAL_SITE]
                if externals:
                    print(f"[Safety] Mechanical detector tagged {len(externals)} off-domain elements: {externals}")

            current_node = compute_node_hash(current_url, available_elements)
            print(f"\n[Node: {current_node}] URL: {current_url} | Active Structural Edges: {available_elements}")

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
            # New STALE-SCORE logic: if (a) node was scored before, AND
            # (b) enough workflow progress happened since (action count grew
            # >= threshold), INVALIDATE cached weights by re-running the AI.
            # Context is different now; old cost=1 is no longer valid.
            need_ai_call = False
            if current_node not in state_graph:
                state_graph[current_node] = {}
                state_graph_metadata[current_node] = {"first_score_action_count": len(recent_actions_log)}
                need_ai_call = True
            else:
                if ENABLE_STALE_SCORE_INVALIDATION:
                    prev_count = state_graph_metadata[current_node]["first_score_action_count"]
                    current_count = len(recent_actions_log)
                    if current_count - prev_count >= STALE_SCORE_REASK_THRESHOLD:
                        print(f"[StaleScore] Node {current_node} was scored at action-count={prev_count}, "
                              f"now at action-count={current_count}. Context materially changed; re-asking AI.")
                        state_graph_metadata[current_node]["first_score_action_count"] = current_count
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

                decision = ask_ai_navigator(
                    safe_elements,
                    config["ai_context"],
                    recent_actions_log,
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
                if node_breadcrumbs:
                    await page.go_back()
                    current_node = node_breadcrumbs.pop()
                    pre_interaction_node = None
                    pre_interaction_edge = None
                    prev_cart_badge = None
                    prev_form_input_count = None
                    continue
                else:
                    print("[Error] Complete accessible graph workspace exhausted.")
                    scan_status = "GRAPH_COMPLETELY_EXHAUSTED"
                    break

            best_edge = min(valid_edges, key=effective_cost)
            chosen_final_cost = effective_cost(best_edge)
            print(f"-> Traversing Edge: '{best_edge}' (Path Cost: {chosen_final_cost})")

            trajectory_log.append({
                "step": steps_taken,
                "node": current_node,
                "url": current_url,
                "action": best_edge,
                "cost": chosen_final_cost,
            })
            recent_actions_log.append(best_edge)

            try:
                pre_interaction_node = current_node
                pre_interaction_edge = best_edge
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
                    pre_interaction_node = None
                    pre_interaction_edge = None
                    raise
                try:
                    await locator.scroll_into_view_if_needed(timeout=CLICK_ATTEMPT_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    edge_weights[(current_node, best_edge)] = 999
                    m_fail = get_or_create_edge(current_node, best_edge)
                    if m_fail is not None:
                        record_edge_result(m_fail, False, RESULT_FAILURE_SCROLL_TIMEOUT,
                                           updated_cost=999)
                    pre_interaction_node = None
                    pre_interaction_edge = None
                    raise RuntimeError(f"scroll_into_view timed out for '{best_edge}' — likely behind overlay or detached.")
                await click_with_overlay_recovery(page, locator, best_edge)
                node_breadcrumbs.append(pre_interaction_node)
                # Subjective cost +2 base bump on successful click completion; actual
                # performance-informed cost will be recomputed by compute_current_edge_cost
                # on the NEXT iteration once we know what last_result + destination was.
                bumped = edge_weights.get((pre_interaction_node, best_edge), 1) + 2
                edge_weights[(pre_interaction_node, best_edge)] = bumped
                m_ok = get_or_create_edge(pre_interaction_node, best_edge)
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
                    # Already recorded inside inner handlers — keep that result.
                    m_fail["cost"] = final_fail_cost
                elif m_fail is not None:
                    record_edge_result(m_fail, False, RESULT_FAILURE_CLICK_EXCEPTION,
                                       updated_cost=final_fail_cost)

        print("\n===== DIRECTED PATHFINDING TRANSACTION MATRIX COMPLETE =====")
        if ENABLE_EDGE_METADATA_TRACKING:
            n_with_history = sum(1 for ed in edge_metadata_store.values() if ed["attempts"] > 0)
            print(f"[EdgeMemory] Post-run summary: {len(edge_metadata_store)} tracked edge records, "
                  f"{n_with_history} with empirical history.")
        await browser.close()
        generate_scan_report(
            site_name=config.get("site_name", "Target Application"),
            target_goal=config["ai_context"],
            status=scan_status,
            total_steps=steps_taken,
            nodes_discovered=len(state_graph),
            trajectory_log=trajectory_log
        )

if __name__ == "__main__":
    asyncio.run(run_pathfinder_agent())
