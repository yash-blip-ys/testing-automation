import asyncio
import ollama
import json
import re
import hashlib
import os
from datetime import datetime
from playwright.async_api import async_playwright

def compute_node_hash(url, elements):
    """
    Generates a unique cryptographic signature for the current visual layout state.
    This serves as our unique identifier for vertices (nodes) in the state graph.
    """
    state_string = f"{url}|{','.join(sorted(elements))}"
    return hashlib.md5(state_string.encode('utf-8')).hexdigest()[:10]

def ask_ai_to_weight_edges(available_elements, goal):
    """
    Acts as our A* search heuristic function. Uses a lightweight LLM to evaluate 
    and assign priority weight costs to outgoing edges based on semantic relevance.
    """
    prompt = (
        f"USER GOAL: {goal}\n\n"
        f"AVAILABLE CLICKABLE OPTIONS ON SCREEN:\n{available_elements}\n\n"
        "INSTRUCTIONS:\n"
        "Analyze the options. For each option, assign a priority weight from 1 to 5:\n"
        "1 = Direct match or highly logical next step toward the goal.\n"
        "3 = Neutral navigation / alternative option.\n"
        "5 = Highly unlikely to help / potential dead end / backtracking.\n\n"
        "Output ONLY a raw JSON object mapping elements to weights, exactly like this:\n"
        '{"Shopping Cart": 1, "Add to cart": 2}'
    )
    try:
        response = ollama.chat(model='llama3.2', messages=[{'role': 'user', 'content': prompt}])
        content = response['message']['content'].strip()
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        return json.loads(json_match.group(0)) if json_match else {}
    except Exception:
        return {}

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


async def run_pathfinder_agent():
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
        await page.goto(config["portal_url"], wait_until="networkidle")

        
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
                except: 
                    continue
            
            if not authenticated:
                await page.keyboard.press("Enter")
                
            await page.wait_for_load_state("networkidle")
            print("[+] Root node authenticated. Entering graph exploration phase.")
        except Exception as e:
            print(f"[Error] Authentication Node Failure: {e}")
            await browser.close()
            return

        state_graph = {}       
        edge_weights = {}      
        node_breadcrumbs = []  
        max_search_depth = 25

        for step in range(max_search_depth):
            steps_taken = step + 1
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1.5)  
            
            current_url = page.url
            current_ui_text = await page.evaluate("() => document.body.innerText")
            
            
            matched_text = any(msg.lower() in current_ui_text.lower() for msg in victory_text_matches)
            matched_url = any(sub.lower() in current_url.lower() for sub in victory_url_subs)
            
            if matched_text or matched_url:
                print(f"\n[SUCCESS] Targeted Destination Node Reached in {step} structural transitions!")
                scan_status = "SUCCESS_TARGET_REACHED"
                break

            available_elements = await page.evaluate("""(selectorQuery) => {
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

            # ye hai imp logic isko chnage karne se pehle informkar dena kya change kar rahe aur better logic kya hai 
            if current_node not in state_graph:
                state_graph[current_node] = {}
                ai_weights = ask_ai_to_weight_edges(available_elements, config["ai_context"])
                
                lowered_elements = [e.lower() for e in available_elements]
                has_funnel_driver = "checkout" in lowered_elements or "finish" in lowered_elements or ("continue" in lowered_elements and "continue shopping" not in lowered_elements)

                for edge in available_elements:
                    edge_lower = edge.lower()
                    
                    if edge_lower in ["finish", "checkout", "save", "submit"] or (edge_lower == "continue"):
                        edge_weights[(current_node, edge)] = 1
                    elif has_funnel_driver:
                        edge_weights[(current_node, edge)] = 5
                    elif any(k in edge_lower for k in ["open menu", "logout", "about", "cancel", "all items", "reset app state", "twitter", "facebook", "linkedin", "continue shopping"]):
                        edge_weights[(current_node, edge)] = 5
                    elif edge in ["Shopping Cart", "Back to products"]:
                        edge_weights[(current_node, edge)] = 4
                    else:
                        edge_weights[(current_node, edge)] = ai_weights.get(edge, 2)

            valid_edges = [edge for edge in available_elements if edge_weights.get((current_node, edge), 0) < 999]
            
            if not valid_edges:
                print(f"[Dead End] Node {current_node} fully exhausted. Backtracking...")
                if node_breadcrumbs:
                    await page.go_back()
                    current_node = node_breadcrumbs.pop()
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

            # execution wala part hai toh ye bhi dekh lena ek bar 
            try:
                pre_interaction_node = current_node
                
                if best_edge == "Shopping Cart":
                    locator = page.locator(".shopping_cart_link").first
                else:
                    locator = page.locator(f"text=\"{best_edge}\"").first
                    if not await locator.is_visible(timeout=500):
                        locator = page.locator(f"input[value=\"{best_edge}\"]").first

                await locator.scroll_into_view_if_needed()
                await locator.click()
                
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