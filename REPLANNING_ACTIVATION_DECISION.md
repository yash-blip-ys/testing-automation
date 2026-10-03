# Replanning Activation Decision — Part 4.5

**Status: production replanning stays DISABLED. Do not enable it.**

This report is the answer to Part 4.5's question. It was written from
measurements, not from the tests passing. 733 offline tests pass, which is not
evidence that replanning helps; the evidence below is what the model actually
proposed when asked.

Date of evidence: 2026-10-02. Model: `llama3.2` via Ollama. Site: Sauce Demo,
objective *"Add two items to the cart, then complete checkout."*

---

## The evidence

Shadow replanning was run live against a real page. Six proposals were requested
and evaluated; **none were adopted.**

| # | Step | Model proposed | Would adopt | Outstanding work lost | Filler |
|---|------|----------------|-------------|----------------------|--------|
| 1 | 2 | `Open Menu`, `Add to cart`, `Add to cart #2` | **yes** | `Add two items to the cart`, `complete checkout` | yes |
| 2 | 3 | `Open Menu`, `Add to cart`, `Open Menu`, `Add to cart` | no | — | yes |
| 3 | 4 | `Add to cart`, `Add to cart #2` | no | — | yes |
| 4 | 5 | `View details for Sauce Labs Backpack`, `Add to cart`, `View details for Sauce Labs Bike Light` | no | — | yes |
| 5 | 6 | *(identical to #4)* | no | — | yes |
| 6 | 7 | `View details…`, `Add to cart`, `View details…`, `Add to cart #2` | **yes** | `complete checkout` | yes |

**6 of 6 proposals were filler.** Every single one named controls that were
already visible on screen. Two would have been adopted, and both would have
discarded outstanding work.

This is not a near miss. It is a precise, live reproduction of the historical
regression that replanning was disabled for: the planner restates the present
page, the plan comes to *look* finished, and `complete checkout` — the real
remaining work — disappears.

> Note on the numbers: shadow mode fired every step here because the probe
> lowered the stall threshold to 3. In production the threshold is 4 and a run
> would propose at most 3 times (`RUNTIME_PLAN_MAX_REPLANS`). The *quality* of
> proposals is unchanged either way; only the count is smaller.

---

## Answers to the six questions

**Which failure classes does replanning actually improve?**
None that we can demonstrate. The proposals offered routes to pages already on
screen. The failure classes replanning was expected to fix are route staleness
and no-progress stalls; a proposal that restates visible controls addresses
neither.

**Does it preserve the objective?**
The objective string is never rewritten — that is structurally enforced and
tested. But *relevance* is lost. Both adoptable proposals would have removed
`complete checkout` from the plan. The user's words survive; their intent does
not.

**Does it increase filler steps?**
It is the primary source of them. 6/6 proposals carried filler. The filler guard
caught 4 of 6, but 2 still passed because each retained one entry whose derived
evidence the guard could not already satisfy.

**Does it increase model calls materially?**
Yes, with no benefit. Six extra model calls in an 8-step run — roughly one extra
call per step. Bounded by `RUNTIME_PLAN_MAX_REPLANS = 3` per plan in production,
but shadow mode fired at the stall threshold (3 steps here), and the default
threshold of 4 means roughly one planner call per stall in a real run.

**Does it cause loops or delays?**
Loops. Record 5 repeated record 4 verbatim. The run would have re-proposed the
same filler every 3 steps, each time consuming a model call and discarding the
result. Nothing in the current design breaks that cycle.

**What remains unsupported?**
- The route a replan proposes is unverifiable in principle. There is no generic
  observable signal that a proposed plan *would have worked*.
- Requirements are free text. Whether a replacement is about the requirement it
  replaces cannot be checked mechanically, and the obvious check — shared
  content words — also rejects legitimate reroutes. See
  `test_known_limitation_replace_keys_trusts_the_caller`.
- An unverifiable proposal is admitted rather than rejected, because rejecting
  it would discard user work. So the plan can be *replaced by nothing useful*.

---

## What was built, and is worth keeping regardless

The value of Part 4 is not the replan proposal. It is the validation and
measurement machinery, which is what produced the evidence above.

- **Failure classification** (`FAILURE_CLASSES`, 23 classes). Pure, deterministic,
  every class carries a documented policy. It is wired into the run loop and is
  working on live failures.
- **Bounded recovery controller** (`RecoveryController`). Attempt counts scoped
  to `(node, action, class, goal context)`. Penalties expire when the task
  advances, so there is no permanent blacklist. Global budgets bound the run.
  Wired into every failure path; a live run classified a real
  `target_ambiguous` failure and still reached PASS.
- **Proposal validation** (`validate_replan_proposal`). Bounds, safety markers,
  and quantity contradictions. This is what caught the two bad proposals rather
  than the four wholly-filler ones.
- **Shadow evaluation** (`ShadowEvaluator`, `RuntimePlan.would_adopt`). Provably
  inert: no page, no state manager, no mutation, bounded log, page data reduced
  to counts and shape. It is the reason this report has data instead of opinion.

A real defect was found and fixed while building this: any `open the X page`
route matched an `Open Menu` button on the shared verb "open" alone, producing
circular evidence. Fixed with a generic-word exclusion (`_GENERIC_MATCH_WORDS`).

---

## Recommendation

Keep `ENABLE_RUNTIME_PLAN_REPLANNING = False`.

Shadow mode (`ENABLE_SHADOW_REPLANNING`) is also `False`, and should stay that
way in normal runs: it doubles model calls to produce evidence about a feature
we are not using. It is a diagnostic to run deliberately via `shadow_probe.py`,
not a default.

If replanning is revisited, the precondition is not more tests — 733 already
pass. It is a planner that proposes routes rather than restating the page. Until
a proposal can be shown to describe a *future* state on a live run, enabling it
converts a stall into a silently truncated task.

### If you want to gather more evidence anyway

```powershell
$env:SHADOW_STALL_STEPS = "3"
.\venv311\Scripts\python.exe shadow_probe.py fixtures\_demo_unverifiable.json 8
```

Nothing is adopted and no source file is modified. Records are written to
`%TEMP%\shadow_probe.json` with page data reduced to counts and shape — no
element attributes, no page text, no query strings.