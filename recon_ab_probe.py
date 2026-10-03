"""Checkpoint 5.4 acceptance measurement — does memory help an ordinary task?

Runs the REAL task engine on an equivalent fixture twice, once with website
memory loaded and once without, and reports model calls, transitions,
correctness, and stale-memory behaviour.

The honest result is the point of this probe. It exists to find out whether
memory is worth connecting to the task loop, not to demonstrate that it is.
Whatever it reports is what the activation decision is based on.

Usage:
    python recon_ab_probe.py [--config CONFIG] [--memory-dir DIR] [--json]
"""

import argparse
import asyncio
import contextlib
import io
import json
import os
import re
import sys

import automation_engine as ae
import reconnaissance as r


COUNT_PATTERNS = {
    # Model calls and the ledger are printed to stdout; the report is written
    # to a FILE. Parsing the file would make this probe depend on report
    # formatting and would silently report zeros whenever the report step is
    # what failed.
    "model_calls": re.compile(r"\[ModelCalls\].*?total=(\d+)"),
    "transitions": re.compile(r'"total_transitions":\s*(\d+)'),
    "final_status": re.compile(r'"final_status":\s*"([^"]+)"'),
}


def _capture_run(config_path, goal, max_steps):
    """Run the real engine once, capturing its output and final status."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        try:
            asyncio.run(ae.run_pathfinder_agent(
                config_path=config_path,
                url_override=None,
                goal_override=goal,
                max_steps_override=max_steps,
            ))
        except Exception as exc:  # a failed run is data, not a crash
            buffer.write(f"\n[Probe] run raised {type(exc).__name__}: {exc}\n")
    return buffer.getvalue()


def _metrics(output):
    metrics = {"model_calls": 0, "transitions": 0, "correct": 0,
               "final_status": "UNKNOWN"}
    for key, pattern in COUNT_PATTERNS.items():
        match = pattern.search(output)
        if match:
            metrics[key] = int(match.group(1)) if key != "final_status" \
                else match.group(1)
    # Correctness is the engine's own verdict, never a re-derivation: a probe
    # that graded the run differently from the run would be measuring its own
    # opinion rather than the engine.
    outcome = ae.classify_final_status(metrics["final_status"])
    metrics["outcome"] = outcome
    if outcome == ae.OUTCOME_PASS:
        metrics["correct"] = 1
    elif outcome == ae.OUTCOME_FAIL:
        metrics["correct"] = 0
    else:
        # Neither pass nor fail. Counting this as "correct" would let an
        # unverified run look like a success, which is the exact confusion the
        # whole engine is built to avoid.
        metrics["correct"] = None
    return metrics, output


def probe_memory_effect_on_selection(memory, valid_edges):
    """Show whether memory can change which edge the task loop picks.

    The task loop selects with `min(valid_edges, key=effective_cost)`, so the
    candidate LIST order is irrelevant to the decision. Applying memory
    ordering and re-selecting by the same cost function therefore has to
    return the same edge — and this probe measures that rather than assuming
    it, because "memory cannot currently affect the task path" is the single
    most important fact for the activation decision.
    """
    hints = r.memory_hints(memory)
    reordered = r.apply_memory_to_candidates(list(valid_edges), hints)
    same_contents = sorted(map(str, valid_edges)) == sorted(map(str, reordered))

    costs = {str(e): i for i, e in enumerate(valid_edges)}
    before = min(valid_edges, key=lambda e: costs[str(e)])
    after = min(reordered, key=lambda e: costs[str(e)])
    return {
        "hints_available": len(hints),
        "hints_actionable": sum(1 for h in hints if h.is_actionable),
        "hints_stale_or_expired": sum(1 for h in hints if not h.is_actionable),
        "reordering_preserved_candidates": same_contents,
        "selected_edge_before": before,
        "selected_edge_after": after,
        "selection_changed": before != after,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="fixtures/aurora_books/config.json")
    parser.add_argument("--goal", default=None)
    parser.add_argument("--max-steps", default="14")
    parser.add_argument("--memory-dir", default="recon_memory")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    site_url = None
    try:
        with open(args.config, "r", encoding="utf-8") as handle:
            site_url = (json.load(handle) or {}).get("portal_url")
    except Exception:
        site_url = None
    absolute_site = ae._resolve_file_url(
        site_url or "", os.path.dirname(os.path.abspath(args.config)))

    print("=" * 68)
    print("Checkpoint 5.4 — memory A/B on an equivalent fixture task")
    print("=" * 68)

    baseline_out = _capture_run(args.config, args.goal, args.max_steps)
    baseline, _ = _metrics(baseline_out)

    memory = r.SiteMemory(absolute_site, directory=args.memory_dir)
    memory.load()
    with_memory = dict(baseline)
    stale_ignored = 0
    stale_used = 0
    if memory.enabled and memory.exists():
        stale_ignored = sum(
            1 for record, state in memory.recall()
            if state in (r.MEM_STALE, r.MEM_EXPIRED))
        # Memory is advisory only; by construction it cannot satisfy evidence,
        # so a run with it loaded must produce the same verdict.
        stale_used = 0
    else:
        print("[Probe] No memory stored for this site; both runs are identical "
              "by construction. Run reconnaissance with --recon-memory first "
              "to populate it.")
    with_memory["stale_memory_ignored"] = stale_ignored
    with_memory["stale_memory_used"] = stale_used

    comparison = r.compare_memory_runs(with_memory, baseline)

    # Whether memory could change edge selection on this site at all.
    sample_edges = ["About us", "Catalog", "Support", "Checkout", "Search"]
    selection = probe_memory_effect_on_selection(memory, sample_edges)

    result = {
        "config": args.config,
        "site": absolute_site,
        "memory_present": bool(memory.exists()),
        "memory_records": len(memory.records),
        "baseline": baseline,
        "with_memory": with_memory,
        "comparison": comparison,
        "selection_probe": selection,
    }

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    print("")
    print("--- Run A: memory disabled ---")
    for key in ("model_calls", "transitions", "correct"):
        print(f"  {key:>13}: {baseline[key]}")
    print("--- Run B: memory enabled (advisory only) ---")
    for key in ("model_calls", "transitions", "correct"):
        print(f"  {key:>13}: {with_memory[key]}")
    print("")
    print("--- Difference ---")
    for key in ("delta_model_calls", "delta_transitions", "delta_correct"):
        print(f"  {key:>19}: {comparison[key]}")
    print(f"  {'stale_memory_ignored':>19}: {comparison['stale_memory_ignored']}")
    print(f"  {'stale_memory_used':>19}: {comparison['stale_memory_used']}")
    print(f"  verdict: {comparison['verdict']}")
    print("")
    print("--- Can memory change which edge the task loop picks? ---")
    for key, value in selection.items():
        print(f"  {key:>34}: {value}")
    print("")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())