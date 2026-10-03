"""Print the per-case benchmark table from a saved results JSON."""

import json
import os
import sys

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    path = argv[0] if argv else os.path.join(os.environ.get("TEMP", "."),
                                             "bench_v3.json")
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    print(f"{'case':15} {'agent':13} {'oracle':13} {'ok':6} {'fp':6} "
          f"{'calls':6} {'trans':6} {'acts':5} {'secs':7}")
    print("-" * 92)
    for case in data["cases"]:
        result = case["result"]
        verdict = case["verdict"]
        metrics = result["metrics"]
        print(f"{case['key']:15} {result['agent_outcome']:13} "
              f"{result['oracle']['ground_truth']:13} "
              f"{str(verdict['matched']):6} "
              f"{str(verdict.get('false_positive_pass')):6} "
              f"{metrics.get('model_calls', 0):<6} "
              f"{metrics.get('transitions', 0):<6} "
              f"{metrics.get('actions_executed', 0):<5} "
              f"{result['duration_s']:<7.1f}")
    print("-" * 92)
    agg = data["aggregate"]
    print(f"accuracy {agg['accuracy']:.0%} | false-positive PASSES "
          f"{agg['false_positive_passes']} | safety violations "
          f"{agg['safety_violations']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())