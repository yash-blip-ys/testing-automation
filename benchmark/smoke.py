"""Smoke check: every fixture boots and its oracle reports an INITIAL state.

Run before the full benchmark to catch a broken fixture cheaply, rather than
discovering it as a mysterious agent failure twenty minutes later.
"""

import sys

from benchmark.server import FixtureServer
from benchmark.specs import CASES, FAIL, PASS, STOPPED, UNVERIFIABLE


def main():
    problems = []
    for case in CASES:
        app = case.make_app()
        server = FixtureServer(app).start()
        try:
            state = server.snapshot()
            verdict = case.oracle(state)
            truth = verdict["ground_truth"]
            # Before the agent runs, no task has been achieved, so every
            # task fixture must read FAIL. The two exceptions are fixtures
            # whose ground truth is not "did the task get done":
            #   * ambiguous — has no completable outcome at all;
            #   * safety     — "nothing irreversible happened" is already
            #                 true at the start, and stays true only if the
            #                 tool behaves.
            acceptable = {FAIL, UNVERIFIABLE, STOPPED}
            if case.key == "safety":
                acceptable.add(PASS)
            ok = truth in acceptable
            print(f"{case.key:15} initial_oracle={truth:13} "
                  f"{'OK' if ok else 'UNEXPECTED'}")
            if not ok:
                problems.append(case.key)
            checks = verdict.get("checks") or {}
            failed = [k for k, v in checks.items()
                      if k == "account_intact" and v is not True]
            if failed:
                problems.append(f"{case.key}:{failed}")
        finally:
            server.stop()
    if problems:
        print(f"\nProblems: {problems}")
        return 1
    print("\nAll fixtures boot and report a clean initial state.")
    return 0


if __name__ == "__main__":
    sys.exit(main())