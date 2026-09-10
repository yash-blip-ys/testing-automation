# Autonomous Web Testing Agent

An AI-powered autonomous web testing and pathfinding agent that uses directed state-graph exploration combined with LLM-based heuristic weighting to intelligently navigate web applications, complete workflows, and generate structured audit reports.

## Table of Contents
- [Features](#features)
- [Architecture Overview](#architecture-overview)
- [Prerequisites](#prerequisites)
- [Cross-Platform Setup](#cross-platform-setup)
  - [Windows Setup](#windows-setup)
  - [macOS Setup](#macos-setup)
  - [Linux Setup (Ubuntu/Debian)](#linux-setup-ubuntudebian)
- [Configuration](#configuration)
- [Running the Agent](#running-the-agent)
- [Agent Behavior Tuning (Kill Switches)](#agent-behavior-tuning-kill-switches)
- [Safety Tag System](#safety-tag-system)
- [Stale Score Invalidation](#stale-score-invalidation)
- [Project Structure](#project-structure)
- [Understanding the Output](#understanding-the-output)
- [Troubleshooting](#troubleshooting)
- [Network Diagnostics](#network-diagnostics)
- [Reverting Changes (Backup Files)](#reverting-changes-backup-files)

---

## Features

- **Directed State-Graph Exploration**: Maps web application states as nodes (URL + visible element list, MD5-hashed) and clickable actions as weighted edges
- **Full-Page-Context LLM Navigator**: Passes current URL, page title, body text preview, and run depth to Ollama/Llama 3.2 so the AI knows exactly what page it is on before ranking buttons
- **A*-like Cost-Based Pathfinding**: Combines AI-assigned priority (1 = best) with mechanical safety penalties and same-node loop guard
- **Mechanical External-Link Detector**: 100% accurate, zero-LLM-cost detection of off-domain `<a>` tags via `window.location.origin` comparison (no keyword lists)
- **LLM-Originated Semantic Safety Tags**: AI must classify every available element as `0` (safe), `-1` (external site), or `-2` (destructive / undo-progress)
- **Stale Score Invalidation**: When returning to a previously visited page after ≥4 new workflow actions, cached AI scoring is discarded and the AI is re-prompted with the updated context
- **Environment Preflight Check**: Emits a loud banner warning if executed outside the `venv311` virtual environment or with a Python version other than 3.11.x (never blocks execution)
- **Overlay-Aware Click Recovery**: Before failing a blocked click, auto-closes open slide-out menus, role=dialog modals, or presses Escape to dismiss overlays
- **Locator Fallback Chain with Source Hints**: Each element's label is tagged at extraction time with its source (text / value / placeholder / id / special) so the correct CSS selector family is tried first (fixes id-labeled elements like `item_4_img_link`)
- **Futile-Action Penalty**: Any click that results in the same node hash (no page change, no DOM structure change) costs +50 on the next try instead of the slow +2
- **Auto-Form Filling**: Automatically populates form fields via placeholder/id/name keyword matching rules
- **Automatic Authentication**: Auto-detects username/password fields and cycles through common login button selectors
- **Structured Markdown Reports**: Timestamped execution reports with trajectory log, metrics tables, and SUCCESS/FAILED status
- **Configurable Target Sites**: JSON-based `sites_config.json` for swapping between applications
- **Non-Headless Default**: Visual browser interaction via Playwright Chromium (for debugging / observability)

---

## Architecture Overview

```
┌───────────────────────────────────────────────────────────────────────┐
│            Autonomous Pathfinding Agent                               │
├───────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  User Config (sites_config.json)                                      │
│    │    site_name, portal_url, credentials, victory, autofill,        │
│    │    ai_context (goal), target_elements_query                      │
│    ▼                                                                  │
│  Environment Preflight ─────► banner if Python != 3.11 or !venv311    │
│    │                                                                  │
│    ▼                                                                  │
│  [Playwright Chromium Browser]                                        │
│    │   1280×720 viewport, anti-detection args, dialog auto-accept     │
│    ▼                                                                  │
│  Authentication Flow ───► fills user/pass, clicks login submitters    │
│    │                                                                  │
│    ▼                                                                  │
│  Main Loop (up to 25 steps):                                          │
│    │                                                                  │
│    ├─ safe_wait_for_load()  networkidle w/ domcontentloaded fallback  │
│    ├─ Victory Check (URL substrings + body text matches)              │
│    ├─ Futile-action penalty (if previous click caused 0 state change) │
│    ├─ Extract Elements + Mechanical Safety Tags (origin mismatch -1)  │
│    ├─ Hash Node (URL + sorted elements → MD5 10-char signature)       │
│    ├─ Autofill Forms (placeholder/id/name keyword rules)              │
│    ├─ Stale-Score Check: re-ask AI if workflow progressed ≥4 actions  │
│    ├─ ask_ai_navigator() → {best_choice, ranked_backup, safety_tags,  │
│    │                         reasoning} + optional FULL PAGE CONTEXT   │
│    ├─ Apply Costs: AI ranking → mechanical → AI safety → default=10   │
│    ├─ build_locator_for_edge() (hints first, 4-strategy fallback)     │
│    ├─ click_with_overlay_recovery() (auto-close bm-menu/dialog/Esc)   │
│    ├─ On error: edge weight = 999 (never retried)                     │
│    └─ On success: append trajectory, +2 base cost for future retries  │
│                                                                       │
│    ▼                                                                  │
│  Final Status → generate_scan_report()                                │
│    │   Markdown report: metadata, objective, metrics, trajectory tbl  │
│    ▼                                                                  │
│  scan_report_YYYY-MM-DD_HH-MM-SS.md saved in cwd                      │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
```

### Core Components & Entry Points:

| Logic Area | File:Line | Function / Constant |
|---|---|---|
| Entry point | `automation_engine.py:765` | `asyncio.run(run_pathfinder_agent())` |
| All kill-switches / tuning constants | `automation_engine.py:12-74` | Top of file, one section |
| Environment preflight warning | `automation_engine.py:78-91` | `check_environment()` |
| Node hashing | `automation_engine.py:94-96` | `compute_node_hash(url, elements)` |
| Safe load-state (anti-30s-freeze) | `automation_engine.py:99-107` | `safe_wait_for_load(page, timeout_ms=8000)` |
| Navigator response validation | `automation_engine.py:109-130` | `_validate_navigator_response()` |
| **AI Navigator** (full-page-context + safety tags) | `automation_engine.py:132-253` | `ask_ai_navigator()` |
| Markdown report writer | `automation_engine.py:255-288` | `generate_scan_report()` |
| Auto-close open menus / modals | `automation_engine.py:290-316` | `close_blocking_overlays()` |
| Click retried once with overlay recovery | `automation_engine.py:318-326` | `click_with_overlay_recovery()` |
| Hint-aware locator + fallback chain | `automation_engine.py:328-372` | `build_locator_for_edge()` |
| Main loop orchestration | `automation_engine.py:374-764` | `run_pathfinder_agent()` |
| Max search depth limit | `automation_engine.py:454` (`for step in range(max_search_depth)`) | default = `25` |

---

## Prerequisites

Before starting, ensure you have the following installed on your system:

| Component | Exact Version | Purpose |
|---|---|---|
| Python | 3.11.x (tested: 3.11.9) | Core runtime; the agent emits a WARNING banner if any other version is used |
| Ollama | Latest | Local LLM inference server (Windows service / macOS app / Linux systemd unit) |
| Llama 3.2 model | pulled via Ollama | AI heuristic decision-making (default model name used in code: `llama3.2` on line 167) |
| Playwright Chromium | Bundled with `playwright==1.60.0` | Browser automation engine — required even if Chrome is already installed |
| 4 GB+ RAM | — | LLM inference + browser + OS overhead |
| Internet connection | — | Target web app access + Ollama model download |

---

## Cross-Platform Setup

### Windows Setup

#### Step 1: Install Python 3.11
1. Download Python 3.11.9 from [python.org/downloads/windows](https://www.python.org/downloads/windows/)
2. Run the installer **as Administrator**
3. **CRITICAL**: Check "**Add Python 3.11 to PATH**" before clicking Install
4. After installation, verify by opening **PowerShell**:
   ```powershell
   python --version
   # Expected output: Python 3.11.9
   ```
5. If `python` doesn't resolve, try `py -3.11 --version` (Python Launcher for Windows).

#### Step 2: Install Ollama
1. Download Ollama for Windows from [ollama.com/download/windows](https://ollama.com/download/windows)
2. Run the installer (`OllamaSetup.exe`) and follow the wizard
3. Once installed, Ollama runs automatically as a background service
4. Pull the Llama 3.2 model (open a **new** PowerShell window):
   ```powershell
   ollama pull llama3.2
   ```
   Download size: ~2 GB for the default 3B variant.
5. Verify the model is available:
   ```powershell
   ollama list
   # You should see "llama3.2" in the output list
   ```

#### Step 3: Clone / Copy the Project
```powershell
cd "C:\Users\YourUser\Projects"
git clone <your-repo-url> web-testing-tool
cd web-testing-tool
```
*Or just copy the project folder to your desired location.*

#### Step 4: Create Virtual Environment & Install Dependencies
```powershell
cd "C:\path\to\web testing tool"

# Create virtual environment (Python 3.11)
python -m venv venv311

# Activate the virtual environment
.\venv311\Scripts\Activate.ps1

# If you get an "execution policy" error, run this ONCE (per-user):
# Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# Install pinned dependency manifest
pip install -r requirements.txt
```

#### Step 5: Install Playwright Browser Binaries
```powershell
# Inside the activated venv only
playwright install chromium
```
Downloads ~300 MB of Chromium binaries specific to the pinned Playwright version 1.60.0. Required even if Chrome is already installed on the system.

#### Step 6: Verify Installation
```powershell
# Core imports
python -c "import ollama, playwright, asyncio, json, hashlib, re; print('Core dependencies OK')"

# Ollama service connectivity
ollama ps
```

---

### macOS Setup

#### Step 1: Install Python 3.11
**Option A — Official Installer (simplest):**
1. Download from [python.org/downloads/macos](https://www.python.org/downloads/macos/)
2. Run the universal2 `.pkg` installer

**Option B — Homebrew:**
```bash
brew install python@3.11
```

Verify:
```bash
python3.11 --version
# Expected: Python 3.11.9
```

#### Step 2: Install Ollama
```bash
# Homebrew Cask (recommended):
brew install --cask ollama

# OR download the dmg from: https://ollama.com/download/mac
#    then drag Ollama.app to /Applications
```

Start the app and pull the model:
```bash
ollama pull llama3.2
```

#### Step 3: Clone / Copy the Project
```bash
cd ~/Projects
git clone <your-repo-url> web-testing-tool
cd web-testing-tool
```

#### Step 4: Set Up Virtual Environment
```bash
cd /path/to/web-testing-tool

python3.11 -m venv venv311
source venv311/bin/activate

pip install -r requirements.txt
```

#### Step 5: Install Playwright Browsers
```bash
# Still inside activated venv
playwright install chromium

# Optional but recommended: install system libs Playwright needs
playwright install-deps chromium
```

---

### Linux Setup (Ubuntu/Debian)

#### Step 1: Install Python 3.11
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y software-properties-common
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev python3-pip
```

Verify:
```bash
python3.11 --version
```

#### Step 2: Install Ollama
```bash
# Official install script (creates a systemd service):
curl -fsSL https://ollama.com/install.sh | sh

# Verify service started:
sudo systemctl status ollama
```

Pull the Llama model:
```bash
ollama pull llama3.2
```

Headless / no-GPU servers work best with the 1B or 3B Llama 3.2 variant.

#### Step 3: Project & Dependencies
```bash
cd /opt
sudo git clone <your-repo-url> web-testing-tool
sudo chown -R $USER:$USER web-testing-tool
cd web-testing-tool

python3.11 -m venv venv311
source venv311/bin/activate

pip install -r requirements.txt
```

#### Step 4: Install Playwright + Required System Libraries
```bash
playwright install chromium

# Installs libnss3, libatk, libgbm, fontconfig, etc. REQUIRED on Linux.
sudo playwright install-deps chromium
```

**Headless Server Display Workaround**: The agent runs with `headless=False` (line 410 of `run_pathfinder_agent()`) so a browser window is visible during runs. For servers without a physical display either:
1. Edit `automation_engine.py` line 410 → `headless=True`, or
2. Install a virtual framebuffer: `sudo apt install -y xvfb` and prefix runs with `xvfb-run -a python automation_engine.py`

---

## Configuration

All site-specific settings live in **`sites_config.json`**, which **must** be present in the working directory at script launch.

### Default Configuration (current working file)
```json
{
  "site_name": "SauceLabs E-Commerce Practice Sandbox",
  "portal_url": "https://www.saucedemo.com/",
  "ai_context": "Select a product, add it to the shopping cart, navigate to the checkout page, fill out the required shipping details, and successfully complete the order confirmation.",
  "credentials": {
    "username": "standard_user",
    "password": "secret_sauce"
  },
  "victory_conditions": {
    "text_matches": ["thank you for your order", "checkout: complete", "order complete"],
    "url_substrings": ["checkout-complete"]
  },
  "form_autofill": [
    { "keywords": ["first", "name"], "value": "Yuvraj" },
    { "keywords": ["last"], "value": "Singh" },
    { "keywords": ["zip", "postal", "code"], "value": "462026" }
  ],
  "target_elements_query": "button, a, .shopping_cart_link, [role='button'], input[type='submit']"
}
```

### Configuration Field Reference

| Field | Type | Description |
|---|---|---|
| `site_name` | string | Human-readable label used in the generated report header |
| `portal_url` | string | Starting URL the browser navigates to (login page / portal entry) |
| `ai_context` | string | Full natural-language goal passed to the LLM navigator at every decision step |
| `credentials.username` | string | Used to fill the first matched username/login email field |
| `credentials.password` | string | Used to fill the first matched password field |
| `victory_conditions.text_matches` | string[] | Agent halts with status `SUCCESS_TARGET_REACHED` if any string appears (case-insensitive) in `document.body.innerText` |
| `victory_conditions.url_substrings` | string[] | Agent halts with SUCCESS if `current_url.lower()` contains any substring |
| `form_autofill[].keywords` | string[] | Each input is tested by combining `placeholder + id + name` (lowercase); if ANY keyword substring matches, the field is filled |
| `form_autofill[].value` | string | Text inserted into matched fields |
| `target_elements_query` | string | CSS selector passed to `document.querySelectorAll()` to enumerate clickable candidate elements |

### Adding a New Target Site (Example)
```json
{
  "site_name": "Your Internal Admin Portal",
  "portal_url": "http://192.168.1.100:8080/login",
  "ai_context": "Log in, navigate to Users section, create a new test user with valid data, and confirm the user appears in the list.",
  "credentials": {
    "username": "admin",
    "password": "your_password_here"
  },
  "victory_conditions": {
    "text_matches": ["user created successfully", "new user added"],
    "url_substrings": ["/users/list", "/users/view"]
  },
  "form_autofill": [
    { "keywords": ["username", "user", "login"], "value": "testuser01" },
    { "keywords": ["email"], "value": "test@example.com" },
    { "keywords": ["first"], "value": "Test" },
    { "keywords": ["last"], "value": "User" }
  ],
  "target_elements_query": "button, a, [role='button'], input[type='submit'], .btn"
}
```

---

## Running the Agent

### Prerun Checklist
1. Virtual environment is **activated**
2. Ollama service running (`ollama ps` returns OK)
3. `llama3.2` model downloaded (`ollama list`)
4. `sites_config.json` exists in the **current working directory**
5. Target application URL is reachable
6. Playwright Chromium installed inside the venv

### Windows (PowerShell)
```powershell
cd "C:\path\to\web testing tool"
.\venv311\Scripts\Activate.ps1

python automation_engine.py
```

### macOS / Linux (Bash / Zsh)
```bash
cd /path/to/web-testing-tool
source venv311/bin/activate

python automation_engine.py
```

### What Happens During Execution
1. **Preflight banner** — If Python ≠ 3.11.x or `sys.executable` not inside `venv311`, a 70-column warning banner prints immediately (execution still proceeds)
2. **Initialization** — `sites_config.json` loaded, Chromium browser launched at 1280×720 with anti-automation args
3. **Navigation & Authentication** — Goes to `portal_url`, fills user/pass fields, cycles through 5 common login button selectors, falls back to pressing Enter if none match
4. **Graph Exploration Phase** (up to 25 steps, `max_search_depth = 25`):
   - `safe_wait_for_load()` — waits up to 8 s for `networkidle`, then falls back gracefully to `domcontentloaded` (never 30 s freeze)
   - Victory condition check on page text + URL
   - Futile-action penalty: if previous click produced identical node hash, +50 applied
   - Element extraction: visible elements matching `target_elements_query`, labels produced by `innerText → value → placeholder → id` fallback chain, each labeled with its source hint
   - Mechanical external-link detection: any absolute-href `<a>` whose URL origin differs from `window.location.origin` gets `safety_tag = -1`
   - Node hashing: URL + sorted visible element list → 10-char MD5 signature
   - Autofill pass over `input[type=text/number], textarea, input:not([type])` using `form_autofill` rules
   - Stale-score decision: re-ask AI if ≥ `STALE_SCORE_REASK_THRESHOLD` new actions since node was first scored
   - Navigator call: passes goal, recent actions, optional FULL PAGE CONTEXT (URL + title + preview + step depth), mechanically proven tags; expects `{best_choice, ranked_backup, reasoning, safety_tags}`
   - Edge costs: AI ranking 1..N → mechanical override (-1→50, -2→998) → AI safety tag default → fallback 10 (progress keywords bias to 8 when AI fully fails)
   - `build_locator_for_edge()` tries source-hint first, then 4 strategies (text / input[value] / [id] / a[id])
   - Fail-fast visibility check before scroll; 4-second cap on `scroll_into_view_if_needed`
   - `click_with_overlay_recovery()` — on timeout, auto-closes bm-menu / role=dialog / presses Esc then retries once
   - Dead-end (all edges = 999) → `page.go_back()` or `GRAPH_COMPLETELY_EXHAUSTED`
5. **Report Generation** — Timestamped Markdown file written to cwd; stdout prints full absolute path

Typical runtime: 30 s – 3 min depending on site complexity and Llama response speed.

---

## Agent Behavior Tuning (Kill Switches)

Every non-trivial behavior is controlled by a module-level constant at the **top of `automation_engine.py` (lines 19–74)**. No need to restore a backup file just to disable one feature — flip the constant.

| Constant | Default | Meaning / Effect |
|---|---|---|
| `ENABLE_FULL_PAGE_CONTEXT_FOR_AI` | `True` | When `False`, AI navigator receives only the original blind inputs: goal + recent actions + button list. No URL/title/page-preview/step-depth context. Use to reproduce the old class of hallucinations ("Continue" button on checkout-step-2) for comparison. |
| `ENABLE_STALE_SCORE_INVALIDATION` | `True` | When `False`, AI scores are **permanently cached** on the first visit to a node, exactly like the original code path (regressive "cost=1 at step 1 still costs 1 at step 14" bug). |
| `STALE_SCORE_REASK_THRESHOLD` | `4` | How many new workflow actions must occur between visits for a node's cached score to be invalidated and the AI re-queried with fresh context. Lower = more re-asks / less cache reuse. |
| `ENABLE_MECHANICAL_EXTERNAL_DETECTION` | `True` | When `False`, skips origin-based off-domain detection entirely; no automatic `-1` tag for links to Twitter/Facebook/marketing sites. |
| `ENABLE_AI_SEMANTIC_SAFETY_TAGS` | `True` | When `False`, AI navigator prompt does NOT ask for per-element safety classification; `safety_tags` are ignored in validation and only mechanical tags apply. |
| `SAFETY_EXTERNAL_SITE` | `-1` | Tag numeric constant for "leaves current website". Matches the specification. |
| `SAFETY_DESTRUCTIVE` | `-2` | Tag numeric constant for "destructive / undo-progress". Matches the specification. |
| `COST_FOR_SAFETY_EXTERNAL` | `50` | Edge weight applied when element has `-1` safety tag and AI did NOT rank it #1 explicitly. Kept < 999 so an explicit AI choice can still override. |
| `COST_FOR_SAFETY_DESTRUCTIVE` | `998` | Edge weight applied when element has `-2` safety tag and AI did NOT rank it #1 explicitly. Near-blocked (only AI's #1 choice overrides). |
| `FUTILE_ACTION_PENALTY` | `50` | Cost bump applied to edges where a click produced the **identical node hash** on the next iteration. Replaces the original slow `+2` bump for same-noop retries. |
| `CLICK_ATTEMPT_TIMEOUT_MS` | `4000` | Millisecond cap for a single `scroll_into_view_if_needed` or `click` attempt. The original Playwright default was 30000 (30 s freezes). |
| `RECENT_ACTIONS_MEMORY` | `5` | How many of the agent's last actions are fed back into the navigator prompt as context. |
| `EXPECTED_PYTHON_VERSION` | `(3, 11)` | Tuple compared to `sys.version_info[:2]` for the preflight banner warning only. Never blocks execution. |
| `EXPECTED_VENV_MARKER` | `"venv311"` | Substring searched in `sys.prefix` and `sys.executable` for the preflight banner warning only. |

---

## Safety Tag System

Every clickable element can receive a numeric safety classification that drives edge-cost penalties. Two independent layers produce these tags:

### Layer 1 — Mechanical Detector (`ENABLE_MECHANICAL_EXTERNAL_DETECTION = True`)
100% accurate, site-agnostic, zero LLM cost. Runs inside the element extraction `evaluate()` call before any AI invocation:
- For each labeled element that is or wraps an `<a>` tag with an absolute `href`:
  - `new URL(href, location).origin !== location.origin` ⇒ `safety_tag = -1`
- Applies to all off-domain navigation: social networks, marketing/about pages, documentation, partner sites.

When the detector fires you see this in stdout:
```
[Safety] Mechanical detector tagged 4 off-domain elements: ['Twitter', 'Facebook', 'LinkedIn', 'About']
```

Provenance is explicitly passed **back to the LLM in its prompt** as the `MECHANICALLY PROVEN SAFETY TAGS` block, and the validator instructions forbid the AI from downgrading a mechanically proven tag to `0`. During edge-cost application, mechanical tags **always take precedence over AI-suggested tags**.

### Layer 2 — LLM Semantic Classification (`ENABLE_AI_SEMANTIC_SAFETY_TAGS = True`)
The navigator prompt explicitly requires every element to receive a tag:
- `0` = safe in-app navigation
- `-1` = leads off-domain (AI still marks these independently as a cross-check)
- `-2` = destructive / undo-progress action (Remove item, Cancel form/order, Reset app state, Delete account, Sign out, Cancel subscription, Close without saving)

The JSON schema is validated by `_validate_navigator_response()` (`automation_engine.py:109-130`) which:
- Ensures `safety_tags` is a JSON object with string keys and integer values only
- Ensures every value is in `{0, -1, -2}` (no other integers allowed)

### Priority Order When Applying Costs
1. AI's explicit `best_choice` (#1 pick) always stays at its AI-assigned rank cost (1). No tag can demote the AI's explicit #1 pick — it chose deliberately.
2. Mechanical proven tags override everything else (Layer 1):
   - `-1` → `COST_FOR_SAFETY_EXTERNAL` = 50
   - `-2` → `COST_FOR_SAFETY_DESTRUCTIVE` = 998
3. AI-assigned tags for items not ranked in top-N:
   - `-1` → 50, `-2` → 998
4. Untagged, AI-unranked elements → fallback 10 (or 8 when AI fully failed and element matches generic progress-keyword list like finish/submit/checkout)

The intent: safety penalties never block an intentional choice, but they do strongly deprioritize unconsidered side-actions.

---

## Stale Score Invalidation

### Why It Exists
The original code path cached AI decisions permanently on first visit to a node: if Inventory page was scored at step 1, returning to that same Inventory page at step 14 after starting and abandoning checkout would still use the step-1 priorities — even though "Shopping Cart" now makes more sense than "Add to cart" and the AI would say so if asked with the updated trajectory context.

### How It Works Now (Enabled)
- A companion dict `state_graph_metadata` stores, per node hash:
  ```python
  {"first_score_action_count": len(recent_actions_log)}
  ```
- On every visit to an already-seen node, the length of `recent_actions_log` is compared to the stored snapshot.
- If `current_count - previous_count >= STALE_SCORE_REASK_THRESHOLD` (default ≥ 4 new actions), cached scores are intentionally skipped: the AI is re-asked with all new recent-actions context and updated page context.
- `state_graph_metadata` is updated to the new `action_count` on re-ask so a further progress measurement is possible.

Stdout shows this log line when a re-ask fires:
```
[StaleScore] Node cab39423af was scored at action-count=2,
now at action-count=10. Context materially changed; re-asking AI.
```

---

## Project Structure

```
web testing tool/
├── automation_engine.py                    # Core agent (state graph, full-context AI navigator, safety tags, overlay recovery, stale invalidation, reporting)
├── sites_config.json                       # User configuration (target site, credentials, goal, victory, autofill)
├── requirements.txt                        # Pinned Python 3.11 dependency manifest (84 packages, playwright==1.60.0, ollama==0.6.2, etc.)
├── netcheck.py                             # Diagnostics: HTTP GET to target URL, report reachability
├── playwright_check.py                     # Diagnostics: placeholder Playwright smoke check
├── agent_knowledge.json                    # (if ever created) reserved for persistent memories across runs
├── venv311/                                # [Local] Python 3.11 virtual environment — NEVER copy between devices/OS
├── __pycache__/                            # [Local] Python bytecode cache
├── automation_engine_BACKUP_BEFORE_CONTEXT_NEG_SCORES_20260910.py   # Snapshot before navigator context + stale-score + safety-tag refactor
├── automation_engine_BACKUP_BEFORE_FIXES_20260910.py                # Snapshot before the first bug-fix round (original baseline)
├── scan_report_*.md                        # Generated execution reports (timestamped, auto-created, gitignored)
├── report_*.txt                            # Sample stdout capture logs from previous runs
├── .gitignore                              # Ignores venv311/, __pycache__/, scan_report_*.md, *.pyc
└── README.md                               # This document
```

---

## Understanding the Output

### Console Output
```
======================================================================
[WARNING] Environment mismatch detected.
  Running interpreter : C:\Python313\python.exe
  Running version     : 3.13.0
  Expected            : Python 3.11 inside a 'venv311' virtual environment
  Activate the venv before running this script.
======================================================================

[Initialization] Initializing Directed State-Graph Pathfinder Agent for: SauceLabs E-Commerce Practice Sandbox
[*] Navigating to initial root node: https://www.saucedemo.com/
[+] Root node authenticated. Entering graph exploration phase.

[Node: cab39423af] URL: https://www.saucedemo.com/inventory.html | Active Structural Edges: ['Open Menu', 'Shopping Cart', ...]
[Safety] Mechanical detector tagged 4 off-domain elements: ['Twitter', 'Facebook', 'LinkedIn', 'About']
-> Traversing Edge: 'Add to cart' (Path Cost: 1)

[Node: b8b4d85ab4] URL: https://www.saucedemo.com/inventory.html | ...
-> Traversing Edge: 'Shopping Cart' (Path Cost: 1)

[SUCCESS] Targeted Destination Node Reached in 6 structural transitions!

===== DIRECTED PATHFINDING TRANSACTION MATRIX COMPLETE =====
[REPORT GENERATED] File saved successfully: C:\path\to\scan_report_2026-09-10_20-19-25.md
```

Expected diagnostic log tags you may see:
- `[WARNING] Environment mismatch` (top of run) — not inside venv311 or Python isn't 3.11. Run still proceeds.
- `[Safety] Mechanical detector tagged N off-domain elements` — origin comparison fired normally.
- `[Navigator] Invalid response on attempt N: ...` — AI replied with invalid JSON or wrong-page hallucination; auto-retry in progress.
- `[Fallback] AI decision unavailable — applying mechanical tags + progress tiebreak.` — AI failed after 2 retries; mechanical tags apply plus finish/submit/checkout-like keywords biased to weight 8.
- `[StaleScore] ... re-asking AI.` — cached score expired due to workflow progress; fresh call made.
- `[Locator] No selector matched '<X>' before attempt...` — element's labeled source hint couldn't be resolved before the attempt; generic fallbacks tried; expect possible failure.
- `[Overlay] Click on '<X>' was blocked — attempting recovery.` — Playwright timeout on first click; overlay cleanup ran, then one retry.
- `[LoopGuard] Last action '<X>' produced no state change. Penalizing: old -> new.` — same node after click, heavy penalty applied.
- `[Error] Traversal Boundary Blocked: ...` — click/scroll failed, edge weight set to 999 so it is never retried.

### Markdown Report Contents
Each run creates a `scan_report_YYYY-MM-DD_HH-MM-SS.md` file with:
1. **Header** — timestamp, site name, SUCCESS/FAILED status banner
2. **Objective** — raw `ai_context` goal string used
3. **Summary Metrics Table** — total transitions executed, unique nodes discovered
4. **Execution Trajectory Table** with columns:
   - Step number
   - Source node hash (unique 10-char state signature)
   - Current URL (hyperlink)
   - Action/element clicked
   - Assigned path cost at the time of traversal

### Final Status Codes
| Status | Meaning |
|---|---|
| `SUCCESS_TARGET_REACHED` | A victory condition (text match OR URL substring) fired before `max_search_depth` was exhausted. |
| `MAX_DEPTH_EXHAUSTED` | All 25 search steps executed without hitting any victory condition. Report still generated. |
| `GRAPH_COMPLETELY_EXHAUSTED` | No unvisited / non-failed edges remained AND breadcrumb back-history was empty. No way to continue exploring. |
| *(Early exit)* | `sites_config.json` missing / malformed, or Playwright authentication crashed before entering the exploration loop. |

---

## Troubleshooting

### Missing `sites_config.json`
**Symptom**: `[Error] The file 'sites_config.json' was not found in the working directory.`

**Fix**: Run the script from the directory the file lives in. The code uses relative paths.
```powershell
# Windows PowerShell
cd "C:\full\path\to\web testing tool"
python automation_engine.py
```
If you need to run from another directory, edit the `open("sites_config.json")` call near the top of `run_pathfinder_agent()` (`automation_engine.py:376`) to an absolute path.

### Ollama / Model Connectivity
**Symptoms**: repeated silent `[Fallback]` log messages, slow navigation, no smart prioritization.

**Diagnostics (run in order)**:
```bash
# 1. Ollama service running?
ollama ps
# Windows: restart Ollama app. macOS: restart /Applications/Ollama.app.
# Linux: sudo systemctl restart ollama.

# 2. llama3.2 downloaded?
ollama list
# If missing:
ollama pull llama3.2

# 3. Smoke-test model inference:
ollama run llama3.2 "Respond with only the word OK"
# Type /bye to exit the interactive shell.
```

If remote / WSL2 / custom-port Ollama: set env var `OLLAMA_HOST=127.0.0.1:11434` before launching.

### Playwright Browser Launch Failures
**Fix (inside activated venv)**:
```bash
playwright install chromium

# Linux required:
sudo playwright install-deps chromium
```

If Windows still fails:
```powershell
pip install --force-reinstall playwright
playwright install chromium
```

### Authentication Failures ("Root node authenticated" printed but still on login screen)
**Cause**: the 5-item `login_selectors` list didn't match the site's button. Edit `automation_engine.py` inside the login block (search for `login_selectors` in `run_pathfinder_agent()`) to append the correct CSS selectors for your site. Example:
```python
login_selectors = [
    "#login-button", "input[type='submit']", "button[type='submit']",
    "button:has-text('Log In')", "button:has-text('Sign In')",
    ".submit-btn", "#btn-login", "button.primary-login",     # <-- append yours
]
```

### Victory Condition Never Fires
Run still ends with `MAX_DEPTH_EXHAUSTED` even though the agent visibly reached the goal page. Fix:
1. Open devtools on the goal page → copy actual `<body>` innerText → verify your `text_matches` entry exists verbatim (match is case-insensitive substring)
2. Match URLs against actual full URL, not the pretty path. URL matching is plain substring (not regex).

### Form Autofill Not Working
1. Field must match one of the matched input types: `input[type='text']`, `input[type='number']`, `textarea`, `input:not([type])`. Password/email/hidden fields are intentionally skipped.
2. Keywords in `sites_config.json` must appear (as lowercase substring) in the concatenation of the field's `placeholder + id + name` attributes. Add more keyword variants.
3. If fields have no placeholder and use `<label>` elements only, you need to extend the autofill engine; the current code doesn't crawl for sibling/for-attribute labels.

### Linux Headless / Display-Related Crashes
Two options:
1. Edit `run_pathfinder_agent()` → change `headless=False` to `headless=True` on the `p.chromium.launch()` call.
2. Install xvfb virtual buffer and prefix the command:
   ```bash
   sudo apt install -y xvfb
   xvfb-run -a python automation_engine.py
   ```

### Slow Performance Tips
1. Smaller LLM variant: if <8 GB RAM, use `ollama pull llama3.2:1b`, then edit `ask_ai_navigator()` (line 167) → `model='llama3.2:1b'`.
2. Reduce `max_search_depth`: edit the `range(25)` to a smaller number in `run_pathfinder_agent()`.
3. Narrow `target_elements_query` in `sites_config.json` (e.g., primary buttons only). Fewer elements = cheaper AI prompt, smaller state graph.
4. Move Ollama model cache to SSD. Default locations: Win `%USERPROFILE%\.ollama\models`, Mac `~/.ollama/models`, Linux `/usr/share/ollama/.ollama/models`.

### 30-Second Frozen Clicks / Scrolls (Should Not Happen Anymore)
If you still see a >4 s click hang:
1. Confirm `CLICK_ATTEMPT_TIMEOUT_MS = 4000` constant is still set.
2. Confirm the freeze isn't actually in `safe_wait_for_load` after navigating to an external marketing site with never-ending ad network activity — external links should be heavily deprioritized by mechanical tags and rarely clicked in the first place.

---

## Network Diagnostics

### `netcheck.py`
Edit the target URL inside `netcheck.py` (`url = "https://..."` on line 3), then:
```bash
python netcheck.py
# SUCCESS - Site is reachable. Status: 200
#  — or —
# FAILED - Cannot reach site: <exception details>
```

### Manual Full Prerequisite Verification
```bash
# Python / venv
python --version
# Windows PowerShell:
Get-Command python    # Source column should point into venv311\Scripts
# macOS / Linux:
which python          # Path should end with venv311/bin/python

# Core imports
python -c "
import ollama, playwright, json, asyncio, hashlib, re, os, sys
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError
print('All imports OK')
"

# Ollama model present
ollama list | findstr llama3.2     # Windows
# ollama list | grep llama3.2      # macOS / Linux
```

---

## Reverting Changes (Backup Files)

Two full byte-identical snapshots of `automation_engine.py` are included, taken before each round of code changes. You can fully restore any prior state with one file copy (no git required).

| Backup File | Captured Before | Behavior Restored |
|---|---|---|
| `automation_engine_BACKUP_BEFORE_CONTEXT_NEG_SCORES_20260910.py` | The navigator refactor that added: full-page context injection, mechanical external detection, AI semantic `-1/-2` safety tags, and stale-score invalidation. | Reverts to the agent that still had click-locator fixes, overlay recovery, and futile-action penalties, but used the original blind button-list prompt with no safety tags and permanently cached scores. |
| `automation_engine_BACKUP_BEFORE_FIXES_20260910.py` | The very first round of bug fixes (original baseline). | Original pre-bug-fix behavior: 30 s click timeouts, no overlay recovery, no locator hint chain, no idle anti-freeze on external sites, permanent AI score caching, zero safety tag system, and original short prompt. |

**Revert Commands** (Windows PowerShell):
```powershell
cd "C:\path\to\web testing tool"

# Option 1: revert to the version before context/safety-tags/stale-score refactor
Copy-Item .\automation_engine_BACKUP_BEFORE_CONTEXT_NEG_SCORES_20260910.py .\automation_engine.py -Force

# Option 2: fully revert to original baseline before any bug fixes
Copy-Item .\automation_engine_BACKUP_BEFORE_FIXES_20260910.py .\automation_engine.py -Force
```

**Revert Commands** (macOS / Linux bash):
```bash
cd /path/to/web-testing-tool

cp automation_engine_BACKUP_BEFORE_CONTEXT_NEG_SCORES_20260910.py automation_engine.py
# or
cp automation_engine_BACKUP_BEFORE_FIXES_20260910.py automation_engine.py
```

For *feature-level* reverts (disable one behavior without restoring the whole file) instead, edit the kill-switch constants documented in [Agent Behavior Tuning (Kill Switches)](#agent-behavior-tuning-kill-switches).

---

*Framework: Directed State-Graph Pathfinder Agent | Runtime: Python 3.11 + Playwright 1.60.0 + Ollama Llama 3.2 navigator*
