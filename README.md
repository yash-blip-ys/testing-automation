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
- [Project Structure](#project-structure)
- [Understanding the Output](#understanding-the-output)
- [Troubleshooting](#troubleshooting)
- [Network Diagnostics](#network-diagnostics)

---

## Features

- **Directed State-Graph Exploration**: Maps web application states as nodes and clickable actions as edges
- **LLM-Assisted Heuristic Pathfinding**: Uses local LLM (Ollama + Llama 3.2) to prioritize navigation paths
- **A*-like Search Algorithm**: Combines AI-weighted edge costs with graph traversal for efficient navigation
- **Auto-Form Filling**: Automatically populates form fields based on keyword rules
- **Automatic Authentication**: Detects and fills login forms with provided credentials
- **Structured Markdown Reports**: Generates detailed execution reports with trajectory logs and metrics
- **Configurable Target Sites**: JSON-based configuration for testing different applications
- **Head/Headless Modes**: Visual browser interaction via Playwright (non-headless by default for debugging)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│            Autonomous Pathfinding Agent             │
├─────────────────────────────────────────────────────┤
│                                                     │
│  User Config (sites_config.json)                    │
│         │                                           │
│         ▼                                           │
│  [Playwright Browser]  <─── Web Interaction         │
│         │                                           │
│         ▼                                           │
│  State Graph Builder (Node Hashing)                 │
│         │                                           │
│         ▼                                           │
│  AI Edge Weighing (Ollama Llama 3.2)                │
│         │                                           │
│         ▼                                           │
│  Pathfinding Engine (Cost-Based Selection)          │
│         │                                           │
│         ▼                                           │
│  Report Generator (Markdown Output)                 │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### Core Components:

1. **State Node Hashing** (`automation_engine.py:10-16`) - Generates unique MD5-based signatures for each UI state using URL + clickable elements
2. **AI Heuristic Function** (`automation_engine.py:18-40`) - Queries local LLM to assign priority weights (1-5) to each available action
3. **Graph Traversal** (`automation_engine.py:145-265`) - Explores the state graph up to max depth (25 steps), backtracking on dead ends
4. **Victory Detection** (`automation_engine.py:159-165`) - Detects goal completion via text matches or URL substrings
5. **Report Engine** (`automation_engine.py:42-82`) - Compiles Markdown audit reports with trajectory tables and metrics

---

## Prerequisites

Before starting, ensure you have the following installed on your system:

| Component | Minimum Version |  Purpose |
|-----------|-----------------|----------|
| Python | 3.11.x |  Core runtime (tested on 3.11.9) |
| Ollama | Latest |  Local LLM inference server |
| Llama 3.2 Model | (via Ollama) |  AI heuristic decision-making |
| Playwright Browsers | Chromium (bundled) |  Browser automation engine |
| 4GB+ RAM | — |   LLM inference + browser overhead |
| Internet Connection | — |Target web app access |

---

## Cross-Platform Setup

### Windows Setup

#### Step 1: Install Python 3.11
1. Download Python 3.11.9 from [python.org/downloads/windows](https://www.python.org/downloads/windows/)
2. Run the installer **as Administrator**
3.  **CRITICAL**: Check "**Add Python 3.11 to PATH**" before clicking Install
4. After installation, verify by opening **PowerShell** and running:
   ```powershell
   python --version
   # Expected output: Python 3.11.9
   ```
5. If `python` doesn't work, try `py -3.11 --version` (Python Launcher for Windows)

#### Step 2: Install Ollama
1. Download Ollama for Windows from [ollama.com/download/windows](https://ollama.com/download/windows)
2. Run the installer (`OllamaSetup.exe`) and follow the setup wizard
3. Once installed, Ollama will start automatically as a background service
4. Pull the Llama 3.2 model (open a **new** PowerShell window):
   ```powershell
   ollama pull llama3.2
   ```
   Wait for the download to complete (~2GB for the 3B model variant)
5. Verify the model is available:
   ```powershell
   ollama list
   # You should see "llama3.2" in the list
   ```

#### Step 3: Clone / Copy the Project
```powershell
cd "C:\Users\YourUser\Projects"
git clone <your-repo-url> web-testing-tool
cd web-testing-tool
```
*Or simply copy the project folder to your desired location.*

#### Step 4: Create Virtual Environment & Install Dependencies
```powershell
cd "C:\path\to\web testing tool"

# Create virtual environment (Python 3.11)
python -m venv venv311

# Activate the virtual environment
.\venv311\Scripts\Activate.ps1

# If you get execution policy error, run this first (one-time only):
# Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# Install Python dependencies
pip install -r requirements.txt
```

#### Step 5: Install Playwright Browser Binaries
```powershell
# Inside the activated virtual environment
playwright install chromium
```
This downloads ~300MB of Chromium browser binaries. This is **required** even if Chrome is already installed on your system.

#### Step 6: Verify Installation
```powershell
# Verify Python packages
python -c "import ollama; import playwright; print('Core dependencies OK')"

# Quick connectivity test to Ollama
ollama ps
```

---

### macOS Setup

#### Step 1: Install Python 3.11
**Option A: Official Installer**
1. Download from [python.org/downloads/macos](https://www.python.org/downloads/macos/)
2. Install the `.pkg` file (macOS 64-bit universal2 installer)

**Option B: Homebrew (Recommended)**
```bash
brew install python@3.11
```

Verify installation:
```bash
python3.11 --version
# Expected: Python 3.11.9
```

#### Step 2: Install Ollama
```bash
# Download and install via Homebrew Cask
brew install --cask ollama

# OR download from: https://ollama.com/download/mac
# and drag Ollama.app to your Applications folder
```

Start Ollama service and pull the model:
```bash
# Start Ollama app (or it runs automatically after install)
ollama pull llama3.2
```

#### Step 3: Clone / Copy Project
```bash
cd ~/Projects
git clone <your-repo-url> web-testing-tool
cd web-testing-tool
```

#### Step 4: Set Up Virtual Environment
```bash
cd /path/to/web-testing-tool

# Create venv
python3.11 -m venv venv311

# Activate venv
source venv311/bin/activate

# Install dependencies
pip install -r requirements.txt
```

#### Step 5: Install Playwright Browsers
```bash
# With venv activated
playwright install chromium

# Optional: Install system dependencies for Playwright
# (may require sudo on first run)
playwright install-deps chromium
```

---

### Linux Setup (Ubuntu/Debian)

#### Step 1: Install Python 3.11
```bash
# Update packages
sudo apt update && sudo apt upgrade -y

# Install Python 3.11 and dev tools
sudo apt install -y software-properties-common
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev python3-pip
```

Verify:
```bash
python3.11 --version
```

#### Step 2: Install Ollama on Linux
```bash
# Install via official script (recommended)
curl -fsSL https://ollama.com/install.sh | sh

# The service should start automatically via systemd
# Verify service status
sudo systemctl status ollama
```

Pull the Llama model:
```bash
ollama pull llama3.2
```

**Note**: If running on a headless server without GPU, the 3B parameter Llama 3.2 variant works best.

#### Step 3: Project & Dependencies
```bash
cd /opt
sudo git clone <your-repo-url> web-testing-tool
sudo chown -R $USER:$USER web-testing-tool
cd web-testing-tool

# Create and activate venv
python3.11 -m venv venv311
source venv311/bin/activate

# Install Python packages
pip install -r requirements.txt
```

#### Step 4: Install Playwright + System Dependencies
```bash
# Install Chromium browser
playwright install chromium

# Install system-level dependencies (required for Linux)
# This installs libnss3, libatk, libgbm, fonts, etc.
sudo playwright install-deps chromium
```

**Headless Server Note**: The agent currently runs in **non-headless** mode by default (`automation_engine.py:109`). If running on a server without a display, you need to either:
- Install a virtual display: `sudo apt install xvfb` and prefix runs with `xvfb-run`
- Or modify the code: change `headless=False` to `headless=True` on line 109

---

## Configuration

All site-specific settings are stored in **`sites_config.json`**. This file **must** be present in the project's working directory when running the agent.

### Default Configuration Structure
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

### Configuration Fields Explained

| Field | Type | Description |
|-------|------|----------|
| `site_name` | string |  Human-readable name for reports |
| `portal_url` | string |  Starting URL (entry point) |
| `ai_context` | string |  Natural language goal description (passed to LLM heuristic) |
| `credentials.username` | string |  Login username |
| `credentials.password` | string |  Login password |
| `victory_conditions.text_matches` | string[] |  Stop & report SUCCESS if any of these strings appear in page body (case-insensitive) |
| `victory_conditions.url_substrings` | string[] |  Stop & report SUCCESS if URL contains any of these |
| `form_autofill` | object[] |  Rules for auto-filling form fields. Each rule has `keywords` (array of matchers) and `value` (text to fill) |
| `target_elements_query` | string |  CSS selector for clickable elements to include in state graph exploration |

### Example: Adding a New Target Site
Create a backup of your original config, then modify `sites_config.json`:
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
Before executing, ensure:
1.  Virtual environment is **activated**
2.  Ollama service is running (`ollama ps` should work)
3.  `llama3.2` model is pulled (`ollama list`)
4.  `sites_config.json` is in the **current working directory**
5.  Target application URL is reachable from your network
6.  Playwright Chromium is installed

### Windows (PowerShell)
```powershell
cd "C:\path\to\web testing tool"
.\venv311\Scripts\Activate.ps1

# Run the main automation engine
python automation_engine.py
```

### macOS / Linux (Bash/Zsh)
```bash
cd /path/to/web-testing-tool
source venv311/bin/activate

python automation_engine.py
```

### What Happens During Execution:
1. **Initialization** — Loads `sites_config.json`, starts Chromium browser window
2. **Authentication** — Auto-detects username/password fields, fills credentials, clicks login button
3. **Graph Exploration Phase** (up to 25 steps):
   - Hashes current page state (URL + visible buttons/links)
   - Queries Ollama/Llama 3.2 for AI priority weights on clickable elements
   - Applies hardcoded funnel heuristics (checkout/finish = cost 1; logout/cancel = cost 5)
   - Selects lowest-cost edge, clicks it, records trajectory
   - Checks victory conditions on every page load
   - Backtracks via browser history if dead-end node reached
4. **Report Generation** — Saves a timestamped Markdown report to the project directory

**Typical runtime**: 30 seconds to 3 minutes depending on site complexity and LLM response speed.

---

## Project Structure

```
web testing tool/
├── automation_engine.py       # Core agent: state graph, pathfinding, AI heuristics, reporting
├── sites_config.json          #  User configuration (target site, credentials, goals)
├── requirements.txt           # Python dependency manifest
├── netcheck.py                # Diagnostics: Test URL reachability
├── playwright_check.py        # Diagnostics: Verify Playwright install (empty placeholder)
├── venv311/                   # [Local] Python 3.11 virtual environment (not in Git)
├── __pycache__/               # [Local] Python bytecode cache
│
├── scan_report_*.md           #  Generated execution reports (auto-created)
├── report_*.txt               # Sample output logs from previous runs
└── .gitignore                 # Ignores venv, pycache, *.pyc
```

### Key Code Locations:

| Logic Area | File:Line |
|------------|-----------|
| Entry point / main async function | `automation_engine.py:85` |
| State node hashing algorithm | `automation_engine.py:10-16` |
| Ollama LLM call (edge weighting) | `automation_engine.py:18-40` |
| Browser launch & viewport setup | `automation_engine.py:108-111` |
| Authentication flow | `automation_engine.py:119-143` |
| Victory / success detection | `automation_engine.py:159-165` |
| Extraction of clickable elements | `automation_engine.py:167-179` |
| Form auto-fill engine | `automation_engine.py:184-197` |
| Core edge cost / heuristic logic | `automation_engine.py:199-220` |
| Action execution (clicking elements) | `automation_engine.py:246-264` |
| Max search depth limit | `automation_engine.py:148` (default: 25) |

---

## Understanding the Output

### Console Output Example
```
[Initialization] Initializing Directed State-Graph Pathfinder Agent for: SauceLabs E-Commerce Practice Sandbox
[*] Navigating to initial root node: https://www.saucedemo.com/
[+] Root node authenticated. Entering graph exploration phase.

[Node: cab39423af] URL: https://www.saucedemo.com/inventory.html | Active Structural Edges: ['Sauce Labs Backpack', 'Add to cart', ...]
-> Traversing Edge: 'Sauce Labs Backpack' (Path Cost: 1)

[SUCCESS] Targeted Destination Node Reached in 7 structural transitions!

===== DIRECTED PATHFINDING TRANSACTION MATRIX COMPLETE =====
[REPORT GENERATED] File saved successfully: C:\path\to\scan_report_2026-09-07_16-07-28.md
```

### Generated Markdown Report
Each run creates a `scan_report_YYYY-MM-DD_HH-MM-SS.md` file containing:

1. **Metadata Header** — Timestamp, target site, status (SUCCESS/FAILED)
2. **Objective** — The `ai_context` goal passed to the agent
3. **Summary Metrics Table** — Total transitions, unique nodes discovered
4. **Execution Trajectory Table** — Step-by-step log with columns:
   - Step number
   - Source node hash (unique state ID)
   - Current URL (clickable link)
   - Action / button clicked (edge traversed)
   - Assigned heuristic path cost

**Status Codes**:
| Status | Meaning |
|--------|---------|
| `SUCCESS_TARGET_REACHED` | Victory condition matched (text or URL) before max depth |
| `MAX_DEPTH_EXHAUSTED` | Completed 25 steps without hitting victory condition |
| `GRAPH_COMPLETELY_EXHAUSTED` | Visited all reachable nodes, no unvisited edges left |
| (Early exit) | Authentication failed or config error |

---

## Troubleshooting

### Common Issues & Solutions

---

####  `[Error] The file 'sites_config.json' was not found`
**Cause**: Agent is run from a different working directory.
**Fix**:
```powershell
# Windows: Ensure you CD to the project folder first
cd "C:\full\path\to\web testing tool"
python automation_engine.py
```
Or pass an absolute path by modifying line 87 in `automation_engine.py`.

---

####  Ollama Connection Errors (timeouts, model not found)
**Symptoms**:
- `ollama.chat()` exceptions silently swallowed (returns `{}` — all edges default to cost 2)
- Console shows repeated slow navigation without smart prioritization

**Diagnostics**:
```bash
# 1. Is Ollama service running?
ollama ps
# If error: start Ollama app (Win/Mac) or: sudo systemctl start ollama (Linux)

# 2. Is llama3.2 model downloaded?
ollama list
# If missing: ollama pull llama3.2

# 3. Quick test of model inference
ollama run llama3.2 "Hello, respond with just OK"
# Type /bye to exit
```

**Fix**: Restart Ollama service, or if running on WSL2/certain Linux distros, set:
```bash
export OLLAMA_HOST=127.0.0.1:11434
```

---

####  Playwright Errors: "Executable doesn't exist" / Browser Launch Failures
**Fix**: Reinstall Playwright browsers:
```bash
# Inside activated venv
playwright install chromium

# Linux extra step:
sudo playwright install-deps chromium
```

For **Windows** if that still fails:
```powershell
# Force download all browsers
playwright install
# Or repair existing install:
pip install --force-reinstall playwright
playwright install chromium
```

---

####  Authentication fails silently / "Root node authenticated" but logged out
**Cause**: The CSS selector login button detection fails on your target site.

**Fix**: Add the correct login button selectors to the `login_selectors` list on lines 123-124 of `automation_engine.py`. Example additions:
```python
login_selectors = [
    "#login-button", "input[type='submit']", "button[type='submit']",
    "button:has-text('Log In')", "button:has-text('Sign In')",
    ".submit-btn", "#btn-login", "button.login"  # ← Add your site's selectors
]
```

---

####  Victory condition never triggers
**Problem**: Agent reaches goal page but status still shows `MAX_DEPTH_EXHAUSTED`

**Check** & Fix in `sites_config.json`:
1. Ensure `text_matches` strings appear **exactly** (case-insensitive) in the page's `<body>` innerText
2. URL substrings: do they appear in the actual URL? Open dev tools → copy URL → verify substring
3. Escape special regex chars? No matching is plain substring, not regex

---

####  Form autofill not working
**Debug**:
1. Check that field placeholder/id/name attributes contain one of your `keywords` entries
2. Edit `sites_config.json` → add more keyword variations to each rule
3. The agent fills only inputs that match `input[type='text'], input[type='number'], textarea, input:not([type])` — password/email/hidden are excluded

---

####  Linux headless environment: "Browser closed" / Display errors
**Option 1** (Recommended) — Run headless by editing `automation_engine.py:109`:
```python
browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
```

**Option 2** — Use Xvfb virtual display:
```bash
sudo apt install -y xvfb
xvfb-run -a python automation_engine.py
```

---

### Slow Performance Tips
1. **Use smaller LLM**: If system has <8GB RAM, try `ollama pull llama3.2:1b` (1B params ~1.3GB) then edit line 35: `model='llama3.2:1b'`
2. **Reduce max depth**: Change `max_search_depth` on line 148 from 25 to 15
3. **Simplify element query**: Narrow `target_elements_query` in JSON (e.g., only primary buttons)
4. **Faster storage**: Move Ollama model cache to SSD (default locations:
   - Win: `C:\Users\<User>\.ollama\models`
   - Mac: `~/.ollama/models`
   - Linux: `/usr/share/ollama/.ollama/models`)

---

## Network Diagnostics

### Test Target Site Reachability (`netcheck.py`)
Edit `netcheck.py` line 3 to point to your target URL, then run:
```bash
python netcheck.py
# SUCCESS - Site is reachable. Status: 200
#  OR
# FAILED - Cannot reach site: <error details>
```

### Test All Prerequisites
Create a quick diagnostic script (save as `diag.py` and run) — or manually verify:
```bash
# Python + venv
python --version
which python   # (macOS/Linux)
# Get-Command python  (Windows PowerShell) → should point to venv311

# Core imports
python -c "
import ollama, playwright, json, asyncio, hashlib
from playwright.async_api import async_playwright
print(' All imports OK')
"

# Ollama
ollama list | Select-String llama3.2    # PowerShell
# ollama list | grep llama3.2           # macOS/Linux
```

---

## Uninstall / Cleanup

To fully remove the project from a device:
1. Stop any running Python/Ollama processes
2. Delete the project folder (removes venv and reports)
3. (Optional) Uninstall Ollama (control panel / `brew uninstall ollama` / `sudo systemctl disable ollama && rm -rf /usr/share/ollama`)
4. (Optional) Remove Playwright browser cache:
   - Win: `%LOCALAPPDATA%\ms-playwright`
   - Mac: `~/Library/Caches/ms-playwright`
   - Linux: `~/.cache/ms-playwright`

---

## Migration Between Devices

To move your working setup to a new machine:
1. **Copy these files only** (everything except `venv311/`, `__pycache__/`, old reports):
   - `automation_engine.py`
   - `sites_config.json`
   - `netcheck.py`
   - `playwright_check.py`
   - `requirements.txt`
   - `.gitignore`
2. On the new device, follow the platform-specific setup steps starting from **Step 3** (skip cloning)
3. Re-create venv and reinstall dependencies — **never copy the `venv311/` folder across different OS/architectures**

---

*Framework: Directed State-Graph Pathfinder Agent | Runtime: Python 3.11 + Playwright + Ollama Llama 3.2*
