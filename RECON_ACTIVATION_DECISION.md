# Part 5 — Reconnaissance and Persistent Memory: Activation Decision

Status: **RECONNAISSANCE ENABLED. TASK-ASSIST MEMORY NOT CONNECTED.**
Measured, not assumed. Every number below comes from a real run.

---

## 1. What was built

| Checkpoint | Delivered | Where |
| :--- | :--- | :--- |
| 5.1 Reconnaissance mode contract | Separate `--recon` mode, six explicit budgets, thirteen named stop reasons | `reconnaissance.py`, `ReconBudget`, `RECON_DONE_*` |
| 5.2 Website graph | Nodes keyed by structure+state (never URL), edges split walked / suggested / blocked, facts separated from inferences | `ReconGraph`, `ReconNode`, `ReconEdge` |
| 5.3 Persistent memory | One JSON file per site, optional, freshness-graded, secret-refusing, isolated | `SiteMemory`, `MemoryRecord` |
| 5.4 Memory-assisted execution | Advisory hints + ordering + A/B harness — **measured, not wired into the task loop** | `memory_hints`, `apply_memory_to_candidates`, `recon_ab_probe.py` |
| 5.5 Reconnaissance report | Structured markdown that disclaims coverage whenever a budget or barrier stopped the run | `build_recon_report` |

Reconnaissance shares no mutable state with the task engine and is dispatched
before any task setup, so it cannot influence a task run in either direction.

## 2. Read-only posture

Reconnaissance navigates and observes. It never submits a form, fills a field,
signs in, accepts consent, or walks a checkout. Traversal is restricted to
same-origin hyperlink navigation, which has no side effect. Consequential,
off-site, and non-page links are recorded as unexplored areas with a reason
rather than followed.

Verified by `tests/test_recon_runner.py::test_never_fills_clicks_or_submits`,
which runs the real runner against a page that raises on any interaction
beyond `goto`.

## 3. The Checkpoint 5.4 measurement

Command:

```
python recon_ab_probe.py --config fixtures/execution/run_config.json --max-steps 6
```

Result on an equivalent fixture task, with website memory populated by a prior
reconnaissance run:

| Metric | Memory disabled | Memory enabled | Delta |
| :--- | ---: | ---: | ---: |
| Model calls | 1 | 1 | **0** |
| Transitions | 2 | 2 | **0** |
| Correct (PASS/FAIL) | not gradeable | not gradeable | **0** |
| Stale memory ignored | — | 0 | — |
| Stale memory used | — | **0** | — |

Verdict: **no measurable difference.**

### Why memory cannot help yet — the structural finding

The probe measured edge selection directly rather than assuming it:

```
hints_available                : 2
hints_actionable               : 2
reordering_preserved_candidates: True
selected_edge_before           : About us
selected_edge_after            : About us
selection_changed              : False
```

The task loop chooses its next action with
`min(valid_edges, key=effective_cost)` — a minimum over a **cost**, not the
first element of a list. Reordering the candidate list therefore cannot change
the choice, no matter how good the memory is.

The only way memory could steer the task loop is by adjusting `edge_weights`.
That is exactly the machinery that carries evidence-based loop penalties,
recovery withdrawals, and mechanical safety costs. Letting memory write there
would risk overriding a penalty the engine earned from a failure the memory
knows nothing about. **That trade was not made without a demonstrated win.**

## 4. Decision

- **Reconnaissance is available and recommended.** It is separate, read-only,
  budgeted, and honestly reported. It costs an existing task run nothing.
- **Memory is built, tested, and safe, but is not consulted by task runs.**
  It can be enabled for a task run later with one hook, and the measurement
  above is the baseline that change would have to beat.
- **No production flag changed.** As with the replanning decision in Part 4,
  a capability is not enabled on the strength of having been built.

## 5. Known limitations, stated plainly

- **Not exhaustive.** Every recon run is bounded by six budgets; the report
  says which one stopped it and says the map is incomplete.
- **Structural identity only.** Without configured `semantic_signals`, a page
  whose application state changed but whose controls did not is one node, not
  two. Reconnaissance never invents signals to fill this gap.
- **Model calls: zero.** The budget exists and is enforced, but the crawler is
  entirely mechanical. There is no model in the recon loop today.
- **No workflow inference yet.** Page *roles* are inferred from observed
  structure and labelled as inferences. Multi-step workflow suggestions are
  not generated; doing so honestly would need a confidence model that has not
  been validated.
- **Blind to JavaScript-only navigation.** Links whose target is produced at
  runtime without an `href` are not discovered, because following them would
  require clicking — and clicking is outside the read-only posture.
- **Correctness was not gradeable** in the fixture measured (the run ended
  neither PASS nor FAIL), so the correctness column is null rather than
  optimistically counted as a success.

## 6. What would justify enabling memory on tasks

A change that moves `edge_weights` from memory, measured on a fixture where
the run *does* reach PASS, showing a reduction in model calls or transitions
**with unchanged correctness and zero stale-memory use**. Anything less is not
evidence, and the current numbers are not.