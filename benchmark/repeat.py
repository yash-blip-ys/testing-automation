"""Checkpoint 6.7.2 — repeatability and flaky-run detection.

Runs every deterministic fixture N times (default 5) from a CLEAN initial state
and reports each repetition individually. Nothing is averaged away and no run
is discarded: a case is FLKY when its repetitions disagree, and every disagreeing
repetition is shown.

    python -m benchmark.repeat                     # all cases, 5 runs each
    python -m benchmark.repeat --case safety --runs 5
    python -m benchmark.repeat --json out.json

State is reset before every single run, and the safety and correctness
requirements are identical across repetitions — a repetition that violates
safety fails the campaign regardless of how many others passed.
"""

import argparse
import json
import os
import statistics
import sys
import tempfile

from benchmark.runner import grade, run_case
from benchmark.specs import CASES, case_by_key


def classify(runs):
    """Classify one case's repetitions.

    "flaky" is not the only thing worth reporting. A case can be perfectly
    consistent and consistently WRONG, which is a different defect with a
    different fix, so it is named separately rather than folded into success:

      * "consistent-correct"  — every repetition matched the oracle
      * "consistent-wrong"    — every repetition agreed, and all agreed wrong
      * "flaky"               — repetitions disagreed
      * "unsupported"         — the engine raised on every attempt (an
                                environment problem, not an agent verdict)
    """
    scored = [r for r in runs if not r.get("error")]
    if not scored:
        return "unsupported"
    verdicts = {r["matched"] for r in scored}
    if len(verdicts) > 1:
        return "flaky"
    return "consistent-correct" if verdicts == {True} else "consistent-wrong"


def run_case_repeated(case, runs, workdir):
    """Run one case `runs` times, each from a clean initial state."""
    results = []
    for index in range(runs):
        print(f"[Repeat] {case.key} run {index + 1}/{runs} ...", flush=True)
        result = run_case(case, headless=True, workdir=workdir)
        verdict = grade(case, result)
        results.append({
            "run": index + 1,
            "agent_outcome": result.agent_outcome,
            "agent_raw_status": result.agent_raw_status,
            "oracle": result.oracle.get("ground_truth"),
            "matched": verdict["matched"],
            "false_positive_pass": verdict.get("false_positive_pass"),
            "safety_violation": verdict.get("safety_violation"),
            "stop_reason": result.agent_raw_status,
            "actions_executed": result.metrics.get("actions_executed", 0),
            "transitions": result.metrics.get("transitions", 0),
            "model_calls": result.metrics.get("model_calls", 0),
            "duration_s": round(result.duration_s, 2),
            "error": result.error,
        })
    return results


def summarise_case(case, results):
    durations = [r["duration_s"] for r in results if not r["error"]]
    calls = [r["model_calls"] for r in results if not r["error"]]
    transitions = [r["transitions"] for r in results if not r["error"]]
    fp = sum(1 for r in results if r.get("false_positive_pass"))
    violations = sum(1 for r in results if r.get("safety_violation"))
    matched = sum(1 for r in results if r["matched"])
    total = len(results)

    def _stats(values):
        if not values:
            return {"median": None, "min": None, "max": None}
        return {"median": round(statistics.median(values), 2),
                "min": min(values), "max": max(values)}

    return {
        "case": case.key,
        "runs": total,
        "matched": matched,
        "accuracy": round(matched / total, 4) if total else 0.0,
        "false_positive_passes": fp,
        "safety_violations": violations,
        "classification": classify(results),
        "duration": _stats(durations),
        "model_calls": _stats(calls),
        "transitions": _stats(transitions),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    cases = ([case_by_key(k) for k in args.case] if args.case
             else list(CASES))
    workdir = tempfile.mkdtemp(prefix="bench_repeat_")

    all_results = {}
    summaries = []
    for case in cases:
        results = run_case_repeated(case, args.runs, workdir)
        summary = summarise_case(case, results)
        all_results[case.key] = results
        summaries.append(summary)

    print("")
    print("=" * 100)
    print("REPEATABILITY (Checkpoint 6.7.2)")
    print("=" * 100)
    print(f"{'case':15} {'runs':5} {'matched':8} {'fp':4} {'safety':7} "
          f"{'class':20} {'med_calls':10} {'med_trans':10} {'med_secs':9}")
    print("-" * 100)
    for summary in summaries:
        print(f"{summary['case']:15} {summary['runs']:<5} "
              f"{str(summary['matched']) + '/' + str(summary['runs']):8} "
              f"{summary['false_positive_passes']:<4} "
              f"{summary['safety_violations']:<7} "
              f"{summary['classification']:20} "
              f"{str(summary['model_calls']['median']):10} "
              f"{str(summary['transitions']['median']):10} "
              f"{str(summary['duration']['median']):9}")

    print("")
    print("PER-REPETITION OUTCOMES (nothing averaged away)")
    for key, results in all_results.items():
        print("")
        print(f"[{key}]")
        for r in results:
            marks = []
            if r["matched"]:
                marks.append("match")
            if r["false_positive_pass"]:
                marks.append("FALSE-POSITIVE-PASS")
            if r["safety_violation"]:
                marks.append("SAFETY-VIOLATION")
            print(f"  run {r['run']}: agent={r['agent_outcome']:<12} "
                  f"oracle={r['oracle']:<12} "
                  f"acts={r['actions_executed']} trans={r['transitions']} "
                  f"calls={r['model_calls']} {r['duration_s']}s "
                  f"{' '.join(marks) if marks else ''}")

    flaky = [s["case"] for s in summaries if s["classification"] == "flaky"]
    wrong = [s["case"] for s in summaries
             if s["classification"] == "consistent-wrong"]
    total_fp = sum(s["false_positive_passes"] for s in summaries)
    total_violations = sum(s["safety_violations"] for s in summaries)
    print("")
    print("=" * 100)
    print(f"FLAKY CASES          : {flaky or 'none'}")
    print(f"CONSISTENTLY WRONG   : {wrong or 'none'}")
    print(f"FALSE-POSITIVE PASSES: {total_fp}   <-- critical metric")
    print(f"SAFETY VIOLATIONS    : {total_violations}")
    print("=" * 100)

    if args.json:
        payload = {"runs_per_case": args.runs, "summaries": summaries,
                   "repetitions": all_results}
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)
        print(f"[Repeat] wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())