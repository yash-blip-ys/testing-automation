"""Run the full Part 6 benchmark and print per-case and aggregate results.

    python -m benchmark.run_benchmark            # all seven cases
    python -m benchmark.run_benchmark --case safety
    python -m benchmark.run_benchmark --json out.json

Every case goes through the ordinary agent entry point with a real browser and
a real model. The ground truth is never adjusted to match what the agent did.
"""

import argparse
import json
import os
import sys
import tempfile

from benchmark.runner import grade, run_benchmark, run_case, summarise
from benchmark.specs import CASES, case_by_key


def _fmt(value, width=10):
    return str(value).ljust(width)


def print_results(graded, aggregate):
    print("")
    print("=" * 96)
    print("PER-CASE RESULTS")
    print("=" * 96)
    header = (f"{'case':15} {'agent':13} {'oracle':13} {'ok':5} "
              f"{'fp_pass':8} {'safety':7} {'calls':6} {'trans':6} {'secs':7}")
    print(header)
    print("-" * 96)
    for item in graded:
        result = item["result"]
        verdict = item["verdict"]
        print(
            f"{item['case'].key:15} "
            f"{_fmt(result.agent_outcome)} "
            f"{_fmt(result.oracle.get('ground_truth'))} "
            f"{('yes' if verdict['matched'] else 'NO'):5} "
            f"{('YES' if verdict.get('false_positive_pass') else 'no'):8} "
            f"{('VIOL' if verdict.get('safety_violation') else 'ok'):7} "
            f"{result.metrics.get('model_calls', 0):<6} "
            f"{result.metrics.get('transitions', 0):<6} "
            f"{result.duration_s:<7.1f}")
    print("-" * 96)

    print("")
    print("PER-CASE DETAIL")
    for item in graded:
        result = item["result"]
        verdict = item["verdict"]
        print("")
        print(f"[{item['case'].key}]")
        print(f"  objective      : {item['case'].objective}")
        print(f"  agent outcome  : {result.agent_outcome} "
              f"(raw: {result.agent_raw_status or 'n/a'})")
        print(f"  agent evidence : {result.agent_evidence or '(none reported)'}")
        print(f"  oracle         : {result.oracle.get('ground_truth')} — "
              f"{result.oracle.get('detail')}")
        print(f"  verdict        : {verdict['reason']}")
        print(f"  matched        : {verdict['matched']}")
        if verdict.get("false_positive_pass"):
            print("  *** FALSE-POSITIVE PASS: agent claimed success the "
                  "application cannot confirm ***")
        if verdict.get("safety_violation"):
            print("  *** SAFETY VIOLATION ***")
        print(f"  metrics        : {json.dumps(result.metrics, default=str)}")
        if result.error:
            print(f"  run error      : {result.error}")

    print("")
    print("=" * 96)
    print("AGGREGATE")
    print("=" * 96)
    print(f"  cases                        : {aggregate['cases']}")
    print(f"  matched oracle               : {aggregate['matched']}"
          f"  (accuracy {aggregate['accuracy']:.0%})")
    print(f"  FALSE-POSITIVE PASSES        : "
          f"{aggregate['false_positive_passes']}   <-- critical metric")
    print(f"  SAFETY VIOLATIONS            : {aggregate['safety_violations']}")
    print(f"  agent reported PASS          : {aggregate['reported_pass']}")
    print(f"  agent reported UNVERIFIABLE  : {aggregate['unverifiable']}")
    print(f"  agent reported STOPPED       : {aggregate['stopped']}")
    print(f"  model calls (total)          : {aggregate['model_calls']}")
    print(f"  transitions (total)          : {aggregate['transitions']}")
    print(f"  actions executed (total)     : {aggregate['actions_executed']}")
    print(f"  grounding rejections (total) : "
          f"{aggregate['grounding_rejections']}")
    print(f"  wall clock (total)           : {aggregate['total_duration_s']}s")
    print("")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--json", default=None)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)

    cases = ([case_by_key(k) for k in args.case] if args.case
             else list(CASES))
    workdir = tempfile.mkdtemp(prefix="bench_run_")
    graded = []
    for case in cases:
        print(f"[Benchmark] running '{case.key}' ...", flush=True)
        result = run_case(case, headless=not args.headed, workdir=workdir)
        graded.append({"case": case, "result": result,
                       "verdict": grade(case, result)})

    aggregate = summarise(graded)
    print_results(graded, aggregate)

    if args.json:
        payload = {
            "aggregate": aggregate,
            "cases": [
                {
                    "key": g["case"].key,
                    "objective": g["case"].objective,
                    "expected_outcome": g["case"].expected_outcome,
                    "ground_truth": g["case"].ground_truth,
                    "forbidden": g["case"].forbidden,
                    "result": g["result"].to_dict(),
                    "verdict": g["verdict"],
                }
                for g in graded
            ],
        }
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)
        print(f"[Benchmark] wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())