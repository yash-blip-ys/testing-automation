import asyncio
import ollama
import json
import re
import hashlib
import os
import sys
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Actions that should NEVER be chosen automatically, regardless of what the AI
# says. This is a safety net, not a navigation strategy — kept intentionally
# tiny and generic (not tuned to any one site) so it doesn't quietly take
# decision-making away from the AI. Anything not in this list is the AI's call.
IRREVERSIBLE_ACTION_KEYWORDS = [
    "log out", "logout", "sign out", "delete account",
    "cancel subscription", "deactivate account",
]

# LAST-RESORT ONLY. If the AI fails twice in a row (ask_ai_navigator returns
# None), we can't just leave every option tied at the same weight — a flat
# tie gets broken by list order, which can walk the agent BACKWARDS out of a
# near-complete flow (this is exactly what happened: "Finish" was available
# but "Shopping Cart" won on a tie). This keyword list only breaks that tie;
# it never overrides an actual AI decision, and it's generic across web apps
# (not tuned to one site) rather than a return to hardcoded navigation logic.
FALLBACK_PROGRESS_KEYWORDS = [
    "finish", "submit", "confirm", "checkout", "continue",
    "save", "next", "proceed", "place order",
]

# Generic actions that usually UNDO progress in a web workflow. These are NOT
# a hard block — the AI can still explicitly rank one of these as its top
# choice and that choice wins (lines below ensure an AI ranking overrides
# this default). But when the AI gives no ranking (AI = None fallback) OR
# the action is unranked, we assign a heavier default cost so the agent
# doesn't casually pick "Remove", "Cancel", "Reset App State", or social
# links when a forward option exists.
#
# To disable: set = [ ] and all default penalties vanish.
ANTI_PROGRESS_KEYWORDS = [
    "remove", "cancel", "reset app state", "reset", "clear",
    "continue shopping", "back to products", "close menu",
    "twitter", "facebook", "linkedin", "youtube", "instagram",
    "about",
]

# Anti-progress default weight (heavier than unranked normal actions = 10,
# lighter than IRREVERSIBLE block = 999). Normal unranked = 10, so this = 15.
ANTI_PROGRESS_DEFAULT_WEIGHT = 15

# After a click, if the page state hash did NOT change (same URL + same
# elements), the action accomplished nothing. This is a wasted step: a
# nav-link-to-self, a click on a disabled element, or something equally
# unhelpful. +=2 per click was way too soft; the agent would retry the same
# useless action 5+ times before it finally ranked below others. This big
# penalty (added to edge weight once per same-node-detected click) stops
# those loops fast.
FUTILE_ACTION_PENALTY = 50

# Click timeout for a single attempt. Previously every scroll/click failure
# blocked 30 seconds per edge. 4 seconds per attempt is enough for any
# normal element; overlay recovery logic (already written) gets one retry.
CLICK_ATTEMPT_TIMEOUT_MS = 4000

# How many of the agent's most recent actions get shown back to the AI, so it
# has short-term memory instead of re-deciding every page cold.
RECENT_ACTIONS_MEMORY = 3

# The environment this project is built and tested against. Mismatches here
# don't crash anything, but they cause exactly the kind of silent, hard-to-
# diagnose Playwright/version behavior differences described in bug reports
# from real runs — so we warn loudly instead of staying quiet about it.
EXPECTED_PYTHON_VERSION = (3, 11)
EXPECTED_VENV_MARKER = "venv311"


def check_environment():
    """
    Warns (does not block) if the interpreter actually running this script
    doesn't match the venv/Python version the project is built against.
    Mixing a global Python install with the project's venv is a known source
    of Playwright behaving inconsistently in ways that look like random bugs.
    """
    version_ok = sys.version_info[:2] == EXPECTED_PYTHON_VERSION
    venv_ok = EXPECTED_VENV_MARKER in sys.prefix or EXPECTED_VENV_MARKER in sys.executable

    if not version_ok or not venv_ok:
        print("=" * 70)
        print("[WARNING] Environment mismatch detected.")
        print(f"  Running interpreter : {sys.executable}")
        print(f"  Running version     : {sys.version.split()[0]}")
        print(f"  Expected            : Python {EXPECTED_PYTHON_VERSION[0]}.{EXPECTED_PYTHON_VERSION[1]}"
              f" inside a '{EXPECTED_VENV_MARKER}' virtual environment")
        print("  This project was built and tested against that exact setup.")
        print("  Mixing a global Python install with the project venv can cause")
        print("  Playwright to behave inconsistently in ways that look like")
        print("  random bugs. Activate the venv before running this script.")
        print("=" * 70)
    return version_ok and venv_ok


def compute_node_hash(url, elements):
    """
    Generates a unique cryptographic signature for the current visual layout state.
    This serves as our unique identifier for vertices (nodes) in the state graph.
    """
    state_string = f"{url}|{','.join(sorted(elements))}"
    return hashlib.md5(state_string.encode('utf-8')).hexdigest()[:10]


async def safe_wait_for_load(page, timeout_ms=8000):
    """
    Bug #4 fix: page.wait_for_load_state("networkidle") never resolves on
    sites with persistent background network activity (ad trackers, analytics
    beacons, websocket-based chat widgets) — it just times out after the full
    default 30s. Left unhandled, that timeout crashed the whole script the
    moment the agent wandered onto an external site (saucelabs.com) that
    never goes network-idle.

    This tries a short, bounded wait for networkidle, and falls back to the
    much cheaper "domcontentloaded" if that times out — and it NEVER raises,
    so a slow/noisy page can no longer take down the whole run.
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except PlaywrightTimeoutError:
            pass


def _validate_navigator_response(parsed, available_elements):
    """
    Confirms the AI's response is actually usable before we act on it:
    - it's a dict
    - best_choice is present and is one of the elements actually on screen
    - ranked_backup (if present) is a list of strings

    Returns an error string describing what's wrong, or None if valid.
    """
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

    return None


def ask_ai_navigator(available_elements, goal, recent_actions):
    """
    The Navigator. Given the goal, what's on screen, and what the agent has
    done recently, asks the LLM to make ONE clear decision — not independent
    per-element scores. A single forced top pick (plus ranked backups) is far
    less prone to wishy-washy, inconsistent output than asking the model to
    grade every option in isolation.

    Returns a dict: {"best_choice": str, "ranked_backup": [str, ...], "reasoning": str}
    or None if the AI could not produce a usable answer after one retry —
    callers must handle None explicitly rather than assume success.
    """
    recent_actions_text = (
        "; ".join(recent_actions[-RECENT_ACTIONS_MEMORY:])
        if recent_actions else "none yet — this is the first step"
    )

    base_prompt = (
        f"USER GOAL: {goal}\n\n"
        f"RECENT ACTIONS ALREADY TAKEN (do not blindly repeat these): {recent_actions_text}\n\n"
        f"AVAILABLE CLICKABLE OPTIONS ON SCREEN:\n{available_elements}\n\n"
        "INSTRUCTIONS:\n"
        "Pick the ONE option that is the most logical next step toward the goal. "
        "Then rank the remaining options as backups, best first, in case the top "
        "choice fails to execute. Give a one-sentence reason for your top choice.\n\n"
        "Respond with ONLY a raw JSON object in exactly this shape:\n"
        '{"best_choice": "<exact text of one option>", '
        '"ranked_backup": ["<exact text>", "..."], '
        '"reasoning": "<one short sentence>"}'
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
              "Respond again with ONLY the corrected raw JSON object."
        )

    print("[Navigator] Could not get a valid decision after retrying. Falling back to a deterministic default.")
    return None


def generate_scan_report(site_name, target_goal, status, total_steps, nodes_discovered, trajectory_log):
    """
    Compiles data points gathered during the graph exploration phase and generates
    a persistent, structured Markdown audit report.
    """
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
Below is the exact step-by-step route your agent took through the application's directed state graph:

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
    """
    Generic overlay/modal closer. If a menu/dialog overlay is currently visible
    AND we're not actually trying to interact with that menu right now, try to
    close it before we attempt the real click.

    Never raises — if this fails, we just proceed to the normal click
    attempt/retry logic below.
    """
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
    """
    Attempts the click; if it's blocked by an overlay interception, tries to
    recover (close overlays, retry once) instead of letting that single
    click failure be the only thing tried before giving up on the edge.
    """
    try:
        await locator.click(timeout=CLICK_ATTEMPT_TIMEOUT_MS)
        return
    except PlaywrightTimeoutError:
        print(f"[Overlay] Click on '{target_text}' was blocked — attempting recovery.")
        await close_blocking_overlays(page, target_text)
        await locator.click(timeout=CLICK_ATTEMPT_TIMEOUT_MS)


async def build_locator_for_edge(page, edge_text, element_hints):
    """
    Bug #1 fix (GENERIC, non-site-specific): the element extraction step can
    label a clickable thing using one of several strategies, in priority
    order: innerText, value, placeholder, id. The old click logic only tried
    text= and input[value=] — so labels that were produced from the .id
    fallback (e.g. image links like "item_4_img_link") were unclickable.

    Strategy:
      1. Hardcoded: if edge_text == "Shopping Cart" → .shopping_cart_link
      2. If extraction hinted this label came from an id → try #id first
      3. text="EXACT" — visible text match (original behavior)
      4. input[value="X"] — original input fallback
      5. [id="X"] — generic CSS id selector fallback (catches <a id=...>)
      6. a:has(img)[id="X"] — anchor wrapping an image (common pattern)

    Returns (locator, success_bool). Caller still does is_visible() check.
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

    strategies = [
        ('text', lambda t: page.locator(f'text="{t}"').first),
        ('input_value', lambda t: page.locator(f'input[value="{t}"]').first),
        ('css_id', lambda t: page.locator(f'[id="{t}"]').first),
        ('a_href_id', lambda t: page.locator(f'a[id="{t}"]').first),
    ]
    for name, build in strategies:
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

        state_graph = {}
        edge_weights = {}
        node_breadcrumbs = []
        recent_actions_log = []
        pre_interaction_node = None
        pre_interaction_edge = None
        max_search_depth = 25

        for step in range(max_search_depth):
            steps_taken = step + 1
            await safe_wait_for_load(page)
            await asyncio.sleep(1.5)

            current_url = page.url
            current_ui_text = await page.evaluate("() => document.body.innerText")

            matched_text = any(msg.lower() in current_ui_text.lower() for msg in victory_text_matches)
            matched_url = any(sub.lower() in current_url.lower() for sub in victory_url_subs)

            if matched_text or matched_url:
                print(f"\n[SUCCESS] Targeted Destination Node Reached in {step} structural transitions!")
                scan_status = "SUCCESS_TARGET_REACHED"
                break

            # --- Same-node / Futile-action penalty detection ---
            # If we just clicked something last iteration AND node hash is the
            # same as pre_interaction, that previous action was a no-op.
            # Penalize it heavily so the same edge isn't retried many times.
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
                    print(f"[LoopGuard] Last action '{pre_interaction_edge}' produced no state change. "
                          f"Penalizing: {old_cost} -> {old_cost + FUTILE_ACTION_PENALTY}")
                pre_interaction_node = None
                pre_interaction_edge = None

            # --- Extract elements WITH "how we got the label" hints ---
            # Returns tuple (labels_list, hints_dict). hints_dict[label] tells
            # the click resolver which selector strategy to try first.
            extract_result = await page.evaluate("""(selectorQuery) => {
                const elements = Array.from(document.querySelectorAll(selectorQuery));
                const visible = elements.filter(el => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                });
                const labels = [];
                const hints = {};
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
                    }
                }
                return { labels, hints };
            }""", target_selectors)
            available_elements = extract_result["labels"]
            element_hints = extract_result["hints"]

            current_node = compute_node_hash(current_url, available_elements)
            print(f"\n[Node: {current_node}] URL: {current_url} | Active Structural Edges: {available_elements}")

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

            if current_node not in state_graph:
                state_graph[current_node] = {}

                safe_elements = [
                    e for e in available_elements
                    if not any(k in e.lower() for k in IRREVERSIBLE_ACTION_KEYWORDS)
                ]
                for e in available_elements:
                    if e not in safe_elements:
                        edge_weights[(current_node, e)] = 999

                decision = ask_ai_navigator(safe_elements, config["ai_context"], recent_actions_log)

                if decision is not None:
                    ai_ranked = set()
                    ai_ranked.add(decision["best_choice"])
                    edge_weights[(current_node, decision["best_choice"])] = 1
                    for rank, edge in enumerate(decision.get("ranked_backup", []), start=2):
                        if edge in safe_elements:
                            edge_weights[(current_node, edge)] = rank
                            ai_ranked.add(edge)
                    # Anything NOT ranked by the AI falls through here.
                    # Apply the generic anti-progress default if keyword matches,
                    # else 10. AI explicit ranking ALWAYS overrides these defaults.
                    for e in safe_elements:
                        if e in ai_ranked:
                            continue
                        if any(k in e.lower() for k in ANTI_PROGRESS_KEYWORDS):
                            edge_weights.setdefault((current_node, e), ANTI_PROGRESS_DEFAULT_WEIGHT)
                        else:
                            edge_weights.setdefault((current_node, e), 10)
                else:
                    print("[Fallback] AI decision unavailable — using generic progress-keyword tiebreak.")
                    for e in safe_elements:
                        if any(k in e.lower() for k in FALLBACK_PROGRESS_KEYWORDS):
                            edge_weights.setdefault((current_node, e), 8)
                        elif any(k in e.lower() for k in ANTI_PROGRESS_KEYWORDS):
                            edge_weights.setdefault((current_node, e), ANTI_PROGRESS_DEFAULT_WEIGHT)
                        else:
                            edge_weights.setdefault((current_node, e), 10)

            valid_edges = [edge for edge in available_elements if edge_weights.get((current_node, edge), 0) < 999]

            if not valid_edges:
                print(f"[Dead End] Node {current_node} fully exhausted. Backtracking...")
                if node_breadcrumbs:
                    await page.go_back()
                    current_node = node_breadcrumbs.pop()
                    pre_interaction_node = None
                    pre_interaction_edge = None
                    continue
                else:
                    print("[Error] Complete accessible graph workspace exhausted.")
                    scan_status = "GRAPH_COMPLETELY_EXHAUSTED"
                    break

            best_edge = min(valid_edges, key=lambda e: edge_weights[(current_node, e)])
            print(f"-> Traversing Edge: '{best_edge}' (Path Cost: {edge_weights[(current_node, best_edge)]})")

            trajectory_log.append({
                "step": steps_taken,
                "node": current_node,
                "url": current_url,
                "action": best_edge,
                "cost": edge_weights[(current_node, best_edge)]
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
                    pre_interaction_node = None
                    pre_interaction_edge = None
                    raise

                try:
                    await locator.scroll_into_view_if_needed(timeout=CLICK_ATTEMPT_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    edge_weights[(current_node, best_edge)] = 999
                    pre_interaction_node = None
                    pre_interaction_edge = None
                    raise RuntimeError(f"scroll_into_view timed out for '{best_edge}' — likely behind an overlay or detached from DOM.")

                await click_with_overlay_recovery(page, locator, best_edge)

                node_breadcrumbs.append(pre_interaction_node)
                edge_weights[(pre_interaction_node, best_edge)] += 2

            except Exception as edge_err:
                print(f"[Error] Traversal Boundary Blocked: {edge_err}")
                edge_weights[(current_node, best_edge)] = 999

        print("\n===== DIRECTED PATHFINDING TRANSACTION MATRIX COMPLETE =====")
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
