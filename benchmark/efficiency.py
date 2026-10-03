"""Checkpoint 6.4 — efficiency measurement.

Efficiency here means work per unit of outcome, not raw speed. A run that
takes twelve model calls to place an order is not worse than one that takes
six if the extra calls were the ones that made it correct; a run that reaches
PASS by brute force is a different product from one that reaches it directly,
and only the numbers say which this is.

Reported per case:
  model calls     — the cost driver; the only step that leaves the machine
  transitions     — distinct page states visited
  actions executed — the floor; 1 is a task that needs exactly one act
  calls per action — the efficiency ratio that actually matters
  wall clock      — reported, but never used as a pass criterion

A case is only called efficient if it matched the oracle. Efficiency of a
wrong answer is not a thing.
"""

import json
import os
import statistics
import sys


def load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def normalise(data):
    """Flatten either results shape into one list of per-case rows.

    `run_benchmark` writes {"cases": [...]}, `repeat` writes
    {"summaries": [...], "repetitions": {...}}. Accepting only one of them
    would mean the tool silently fails on the other, so both are handled here
    and the source is reported.
    """
    if isinstance(data.get("cases"), list):
        return "run_benchmark", analyse(data)
    if isinstance(data.get("summaries"), list):
        return "repeat", analyse_repeat(data)
    raise SystemExit(
        "unrecognised results file: expected a 'cases' list (run_benchmark) "
        "or a 'summaries' list (repeat)")


def analyse_repeat(data):
    """Per-case rows from a repeatability campaign, using the median values."""
    rows = []
    repetitions = data.get("repetitions") or {}
    for summary in data.get("summaries") or []:
        key = summary["case"]
        runs = repetitions.get(key) or []
        actions = [r.get("actions_executed", 0) for r in runs if not r.get("error")]
        median_actions = statistics.median(actions) if actions else 0
        calls = (summary.get("model_calls") or {}).get("median") or 0
        rows.append({
            "case": key,
            "matched": summary.get("accuracy", 0) == 1.0,
            "outcome": (summary.get("classification") or ""),
            "model_calls": calls,
            "transitions": (summary.get("transitions") or {}).get("median") or 0,
            "actions": median_actions,
            "calls_per_action": (
                round(calls / median_actions, 2)
                if median_actions and calls else None),
            "unique_nodes": None,
            "grounding_rejections": None,
            "safety_stops": None,
            "seconds": (summary.get("duration") or {}).get("median") or 0.0,
            "runs": summary.get("runs"),
        })
    return rows


def analyse(data):
    rows = []
    for case in data["cases"]:
        result = case["result"]
        metrics = result.get("metrics") or {}
        calls = metrics.get("model_calls") or 0
        actions = metrics.get("actions_executed") or 0
        transitions = metrics.get("transitions") or 0
        rows.append({
            "case": case["key"],
            "matched": bool(case["verdict"]["matched"]),
            "outcome": result.get("agent_outcome"),
            "model_calls": calls,
            "model_calls_failed": metrics.get("model_calls_failed") or 0,
            "transitions": transitions,
            "actions": actions,
            "unique_nodes": metrics.get("unique_nodes") or 0,
            "grounding_rejections": metrics.get("grounding_rejections") or 0,
            "safety_stops": metrics.get("safety_stops") or 0,
            "calls_per_action": round(calls / actions, 2) if actions else None,
            "calls_per_transition": (round(calls / transitions, 2)
                                     if transitions else None),
            "seconds": round(result.get("duration_s") or 0, 1),
        })
    return rows


def report(rows, source="run_benchmark"):
    print("=" * 100)
    print(f"EFFICIENCY (Checkpoint 6.4) — correct runs only [{source}]")
    print("=" * 100)
    print(f"{'case':15} {'ok':5} {'calls':6} {'acts':5} {'trans':6} {'nodes':6} "
          f"{'rej':4} {'stop':5} {'calls/act':10} {'secs':7}")
    print("-" * 100)
    for row in rows:
        cpa = row["calls_per_action"]
        print(f"{row['case']:15} {str(row['matched']):5} {row['model_calls']:<6} "
              f"{row['actions']:<5} {row['transitions']:<6} "
              f"{str(row['unique_nodes']):6} "
              f"{str(row['grounding_rejections']):4} "
              f"{str(row['safety_stops']):5} {str(cpa):10} {row['seconds']:<7.1f}")

    correct = [r for r in rows if r["matched"]]
    wrong = [r for r in rows if not r["matched"]]
    total_calls = sum(r["model_calls"] or 0 for r in rows)
    total_actions = sum(r["actions"] or 0 for r in rows)
    total_seconds = sum(r["seconds"] or 0 for r in rows)

    print("-" * 100)
    print(f"total model calls        : {total_calls}")
    print(f"total actions executed   : {total_actions}")
    print(f"total transitions        : {sum(r['transitions'] or 0 for r in rows)}")
    rejections = sum((r['grounding_rejections'] or 0) for r in rows)
    print(f"total grounding rejections: {rejections}")
    print(f"total wall clock         : {total_seconds:.1f}s")
    if total_actions:
        print(f"calls per action overall : "
              f"{round(total_calls / total_actions, 2)}")
    ratios = [r["calls_per_action"] for r in correct if r["calls_per_action"]]
    if ratios:
        print(f"calls per action (correct runs only): "
              f"{round(statistics.mean(ratios), 2)}")
    print()

    print("OBSERVATIONS")
    if wrong:
        names = ", ".join(r["case"] for r in wrong)
        print(f"  - not matched (efficiency not assessed): {names}")
    for row in correct:
        if row["actions"] == 1:
            print(f"  - {row['case']}: resolved in a single action "
                  f"({row['model_calls']} model call(s)) — no wasted traversal.")
    safety = next((r for r in rows if r["case"] == "safety"), None)
    if safety:
        print(f"  - safety: stopped after {safety['actions']} action(s); the stop "
              f"is the result, not wasted work.")
    ambiguous = next((r for r in rows if r["case"] == "ambiguous"), None)
    if ambiguous:
        print(f"  - ambiguous: {ambiguous['actions']} action(s) executed. "
              f"Zero actions is the correct outcome — asking the user is the "
              f"work, and guessing would have been a violation.")
    print("=" * 100)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    path = argv[0] if argv else os.path.join(os.environ.get("TEMP", "."),
                                             "bench_v3.json")
    source, rows = normalise(load(path))
    report(rows, source)
    return 0


if __name__ == "__main__":
    sys.exit(main())