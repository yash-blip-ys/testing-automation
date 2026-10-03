"""Evidence driver: run one live pass with SHADOW replanning enabled.

Part 4.5 asks whether enabling production replanning is justified. The
fixture replay answers that for the mechanical parts of adoption, but not for
the part that actually decides it: what the model proposes when asked for a
revised route on a real page.

This driver answers that without touching production. It sets
ENABLE_SHADOW_REPLANNING for the duration of one process, runs the normal agent
unchanged, and writes the shadow records to a JSON file. It does not modify
automation_engine.py and does not change any persisted flag.

Usage:
    .\\venv311\\Scripts\\python.exe shadow_probe.py <config.json> [max_steps]

Writes: <TEMP>\\shadow_probe.json
"""

import asyncio
import json
import os
import sys
import tempfile

import automation_engine as ae


async def main(config_path, max_steps=None):
    # Enable shadow mode for THIS PROCESS ONLY. Nothing is written back to the
    # source file, so the repository keeps production replanning off.
    ae.ENABLE_SHADOW_REPLANNING = True
    ae.RUNTIME_PLAN_STALL_STEPS = int(
        os.environ.get("SHADOW_STALL_STEPS", ae.RUNTIME_PLAN_STALL_STEPS))

    _orig = ae.ShadowEvaluator.__init__

    def _capturing_init(self, *a, **kw):
        _orig(self, *a, **kw)
        globals()["_EVALUATOR"] = self

    ae.ShadowEvaluator.__init__ = _capturing_init

    await ae.run_pathfinder_agent(config_path=config_path,
                                  max_steps_override=max_steps)
    evaluator = globals().get("_EVALUATOR")
    payload = {
        "config": os.path.basename(config_path),
        "stall_steps": ae.RUNTIME_PLAN_STALL_STEPS,
        "summary": evaluator.summary() if evaluator else "no evaluator ran",
        "records": evaluator.records if evaluator else [],
    }
    out = os.path.join(tempfile.gettempdir(), "shadow_probe.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\n[SHADOW PROBE] {len(payload['records'])} record(s) -> {out}")
    print(f"[SHADOW PROBE] {payload['summary']}")


if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else "sites_config.json"
    steps = int(sys.argv[2]) if len(sys.argv) > 2 else None
    asyncio.run(main(cfg, steps))