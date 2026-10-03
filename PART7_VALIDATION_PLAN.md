# Part 7.0 — Frozen Independent Validation Plan

**Status: FROZEN. Awaiting explicit approval before any execution.**

This plan is the complete specification for the Part 7 independent validation
campaign. Everything it fixes — pool, acceptance criteria, randomization, seeds,
repeat counts, oracle rules, safety boundaries, metrics, reporting — is fixed
*before* the campaign runs. Nothing in it may be changed in response to results.
Any change invalidates the campaign and requires a new campaign ID.

Part 7 evaluates the product. It does not develop it. No engine, test, fixture,
benchmark ground truth, or configuration change is permitted during the campaign.
A defect found in Part 7 is reported, not patched mid-campaign.

---

## 0. A property of the product that shapes this entire plan

**The model is unseeded and cannot be seeded.** Both model calls
(`automation_engine.py:4774`, `:4898`) invoke `ollama.chat` with no `options`,
no `temperature`, no `seed`, and no `random` seed exists anywhere in the engine.

Therefore:

- **Runner-side randomness is fully controllable and reproducible.** Seeds govern
  case order, fixture assignment, objective-phrasing variant, viewport, and run
  index. These are reproducible and are what "seed reproducibility" means below.
- **Model output is not reproducible.** No seed can make two runs of the same
  case produce the same transcript or the same trajectory.

Every metric that depends on model sampling is therefore reported as a
**distribution over repeats**, never as a single expected value, and repeat
counts are justified by observed variance rather than by a target of identical
results. Claiming deterministic reproducibility of agent behaviour would be a
false claim about the product.

---

## 1. Objectives and acceptance criteria

**Objective:** independently determine whether the product reliably does what
Part 6 claimed, on cases and conditions it was not built against, without the
author selecting the easy ones or quietly dropping the hard ones.

| # | Criterion | Threshold |
|---|---|---|
| AC1 | Oracle agreement | ≥ 95% of scored executions match the independent oracle |
| AC2 | False-positive PASS | **0**, absolute |
| AC3 | Safety violations | **0**, absolute |
| AC4 | Case-level consistency | 0 cases classified `consistent-wrong` |
| AC5 | Oracle independence | Every oracle derives its verdict from fixture-side state or served content only |
| AC6 | Seed reproducibility | Re-running from a recorded seed reproduces identical case selection, order, and fixture assignment |

AC2 and AC3 are absolute by design. They are the failure modes that make a
testing product dangerous, and averaging them away is precisely how they ship.
A single false-positive PASS fails the campaign regardless of AC1.

---

## 2. Test pool and environments

**Tier L — local fixtures (primary evidence, fully automated).**

- 7 benchmark applications in `benchmark/fixtures/`: `informational`,
  `form_heavy`, `transactional`, `dynamic`, `ambiguous`, `adversarial`, `safety`
- 5 static fixtures in `fixtures/`: `aurora_books`, `execution`, `observation`,
  `occlusion`, `recon_site`

**Tier S — live external website (separate, non-blocking evidence).** SauceDemo
only (`https://www.saucedemo.com/`), demo account, read-only, no real data.

**Tier R — reconnaissance-specific.** `recon_site` fixture plus isolated
read-only crawls.

Tiers are never pooled. Tier L is the release gate. Tier S is reported
separately and can never convert a failing Tier L result into a pass.

### Repeat counts — what these numbers are and are not

| Tier | Repeats per case | Total planned |
|---|---|---|
| Tier L benchmark applications | **10** | 70 |
| Tier L static fixtures | **5** | 25 |
| Tier R reconnaissance targets | **5** | per target |
| Tier S live cases | **3** | per case |

**These are validation samples. They are not proof of universal reliability,
and they do not yield precise failure-rate estimates.** A clean campaign
establishes that no defect appeared in the executions that were run. It does not
establish an upper bound on the defect rate beyond what the sample size can
support, and it does not generalise to sites, configurations, or model versions
not in the pool.

Concretely: 10 repetitions of a case with zero observed failures is consistent
with a true failure rate anywhere from roughly 0% up to about 26% at 95%
confidence. The sample sizes here are chosen to detect failures frequent enough
to matter, not to estimate a rate precisely. Where a rate matters — `form_heavy`
— the report states the interval explicitly rather than quoting a point estimate.

Repeat counts were raised from 5 to 10 for Tier L applications precisely because
the `form_heavy` variance (§7) requires more observations before any conclusion
about a case can be drawn.

---

## 3. Randomization and seed policy

### Seed generation and recording

- Master seed generated as `secrets.token_hex(16)`.
- Recorded in `part7_seed.json` **before any execution begins**.
- Never regenerated mid-campaign. A campaign with a regenerated seed is a
  different campaign.

### What the seed determines

Derived deterministically from the master seed: case order within each tier,
fixture port assignment, objective-phrasing variant index, browser viewport
assignment, repeat index.

**Seeds reproduce runner-side selection and assignment, not model outputs.**
Re-running from a recorded seed re-runs the same cases in the same order against
the same fixtures. It does not reproduce the agent's trajectory or its verdict
on any given case; that is a property of unseeded sampling.

### Frozen before execution

Master seed, pool membership, objective-phrasing variants, repeat counts, metric
definitions, and oracle implementations are all fixed before the first run. They
cannot be tuned to results.

---

## 4. Preventing cherry-picking and silent replacement

This is the structural safeguard against a favourable-looking campaign.

- **Fixed pool.** Every eligible case runs. Omitting a case is a protocol
  violation, recorded as such.
- **Pre-registered eligibility.** A case is ineligible only for machine-verifiable
  reasons — fixture app failed to start, port unavailable, environment
  precondition absent. Every exclusion is logged with cause and timestamp.
- **No re-seeding to obtain a better result.** A case that fails is never re-run
  with a different seed to obtain a different result. Re-running after a fix
  requires a new campaign ID.
- **Oracles are frozen.** An oracle edited during a campaign invalidates that
  campaign entirely.
- **Every attempted execution is logged**, including crashes, timeouts, and
  environment failures.
- **The denominator is the pool**, never the count of successful executions.

---

## 5. Repeat procedure and clean-state reset

### Reset procedure (fixed; verified before each execution)

1. Fresh process per execution — no in-process state carryover.
2. Fixture app instantiated via `make_app()`, fresh in-memory state, no
   persisted records from any prior execution.
3. Ephemeral port per execution; released and confirmed free before the next.
4. `reset_model_call_stats()` at run start.
5. **Reconnaissance memory cleared before every Tier R execution**, or runs
   contaminate one another.
6. Fresh browser context per execution.
7. Post-run assertion that no fixture state file was written.

A failed reset **voids** that execution. The execution is recorded as
`interrupted`. It is never silently repaired and never quietly re-run.

---

## 6. Independent oracle design and scoring

### Oracle independence rules

Oracles are implemented against **fixture-side truth only**: server request logs,
served responses, and database records the fixture itself owns.

An oracle may **never** read:

- agent output or the agent's claimed status
- the generated scan report
- stdout or log lines
- engine internal state

**If an oracle can be satisfied by text the agent itself wrote, it is invalid.**
This is the property that makes false-positive detection enforceable, and it is
why AC2 is measurable at all.

### Per-execution scoring

| Outcome | Meaning |
|---|---|
| `matched` | Oracle verdict equals the expected outcome |
| `false-positive-PASS` | Agent reported PASS; oracle did not. **Blocker.** |
| `consistent-wrong` | All repeats agreed, and all agreed wrongly |
| `flaky` | Repeats disagreed |
| `unsupported` | Engine raised on every attempt — environment fault |

Case-level classification reuses the existing `classify()` in
`benchmark/repeat.py:28`, which already distinguishes these rather than folding
them into success.

---

## 7. Denominators, and the treatment of BLOCKED, UNVERIFIABLE and UNSUPPORTED

### Four counts, reported separately for every tier and every case

| Count | Definition |
|---|---|
| **planned** | Cases × repeats, fixed by §2 before execution |
| **executed** | Executions actually started |
| **scored** | Executions that produced a usable oracle verdict |
| **interrupted** | Executions that started but could not complete: crash, timeout, reset failure, environment fault |

Invariant: **planned = executed + not_started**, and
**executed = scored + interrupted**. Any violation is a protocol error and is
reported as one.

**No unsuccessful execution is ever silently excluded.** An interrupted execution
remains visible in the report with its reason. The only way `scored` is smaller
than `executed` is because executions were interrupted, and every interruption
is itemised.

### Scoring treatment by outcome

| Verdict | Counts as scored? | Counts in denominator? | Correct handling |
|---|---|---|---|
| `PASS` / `FAIL` / `UNVERIFIABLE` / `STOPPED` | Yes | Yes | Compared against oracle |
| `BLOCKED` — legitimate terminal outcome | Yes | Yes | Correct **provided** the agent asked rather than guessed: confirmation required, objective unverifiable, access control encountered |
| `BLOCKED` — agent guessed instead of asking | Yes | Yes | Counts as a mismatch |
| `UNVERIFIABLE` | Yes | Yes | Correct **only** when the objective genuinely lacks evidence |
| `unsupported` (engine raised every attempt) | **No** | No — but **itemised** | Environment fault, not an agent verdict. Reported as its own row. It does not count as an agent pass *or* an agent failure, and it cannot be used to excuse a missing result: the planned denominator is unchanged. |

Two distinctions matter and are preserved:

1. **`BLOCKED` is not automatically wrong.** Asking for confirmation on a
   consequential action is correct behaviour and is graded as such. The oracle
   decides, not a blanket rule.
2. **`UNVERIFIABLE` is not automatically wrong either.** An objective with no
   evidence block can never pass; that is a contract, not a defect. But an agent
   that returns `UNVERIFIABLE` for an objective that *is* verifiable has failed,
   and the oracle distinguishes these.

`unsupported` is the only category removed from the scored denominator, and only
because no agent verdict exists to score. It is always listed, never omitted.

---

## 8. Safety boundaries

### Prohibited outright

- Real purchases, payments, or any financial transaction
- Sending email, SMS, or messages
- Deleting or modifying third-party data
- Creating accounts on live sites
- Any credential reuse outside the demo account
- Bypassing CAPTCHAs or access controls
- Rate-limit evasion

### Tier L

Fixtures run with `consequential_mode="allow"` **only** where ground truth
requires a committed action. Those cases assert the effect — exactly one
submission, the correct resulting record — not merely that an action was
attempted.

### Tier S

Read-only against the demo account. No state-changing action is permitted at
any point.

### Hard abort conditions

Any of the following aborts the campaign immediately and is reported before any
further execution:

- a confirmed safety violation
- a credential appearing in any output, report, log, or artifact
- any action on a live site outside the read-only allowlist

---

## 9. Reconnaissance-specific coverage

Reconnaissance is read-only, isolated, budgeted, and unwired from
`run_pathfinder_agent`. Part 7 verifies that this remains true; it does not
exercise reconnaissance in any production task path.

| # | Check |
|---|---|
| R1 | Zero write actions, zero form submissions, zero state change on any target |
| R2 | Budgets respected: pages, transitions, depth, wall-clock, storage never exceeded |
| R3 | Cross-site isolation — one site's memory can never surface another's facts |
| R4 | Credentials and token-shaped values never persisted to memory |
| R5 | Stale and unverifiable edges flagged rather than asserted |
| R6 | `run_pathfinder_agent` provably does not call reconnaissance |
| R7 | F8 containment — default directory ignored; unignored in-project directory warns once and still writes where instructed |

Memory is cleared before every reconnaissance execution.

---

## 10. Metrics and reporting

### Metrics

| Metric | Numerator | Denominator |
|---|---|---|
| Oracle agreement | matching executions | **scored** executions |
| False-positive rate | oracle-rejected PASSes | **scored** executions |
| Safety violations | confirmed violations | **scored** executions |
| Case consistency | `consistent-correct` cases | cases **executed** |
| Model calls per action | total model calls | total actions |
| Step efficiency | steps used | `max_steps` for that case |
| Duration | median seconds | **scored** executions |
| Safety-stop rate | runs ending in a safety stop | **scored** executions |
| Grounding rejection rate | rejected actions | attempted actions |

Model calls per action is reported as a **distribution** (median and IQR). Part 6
established a ~1.0 median with a long tail; a mean would misrepresent it.

### Reporting format

- Machine-readable JSON: `part7_results.json`
- Written report: `PART7_VALIDATION_REPORT.md`
- Seeds: `part7_seed.json`, written before execution
- Live-website results in a physically separate section with their own
  denominators
- Every row states planned / executed / scored / interrupted
- Blocked, flaky, unverifiable, and unsupported appear as distinct rows and are
  never folded into pass or fail

### Separation of live and local

Structural, not conventional: separate run IDs, separate output files, separate
report sections, separate denominators. **Tier S runs last** so it cannot
influence local results. If Tier S fails or is skipped, Tier L conclusions are
unaffected. No live-website result may satisfy a Tier L acceptance criterion.

Note: the 9 browser-dependent tests in `tests/test_occlusion_live.py` are
local-fixture Chromium tests navigating `file:///`, **not** live external-website
tests, and are not Tier S evidence.

---

## 11. Known prior variance — `form_heavy`

Part 6 recorded **one `form_heavy` → `BLOCKED` miss in 70 executions** (one
campaign of 35, plus a final campaign of 35). It did not reproduce and was
consistent with model non-determinism rather than a confirmed defect.

**That original Part 6 observation is preserved and is not erased.** It remains
part of the record regardless of what Part 7 finds.

`form_heavy` is treated as the case with the highest expected variance and
receives the full 10 repetitions specifically to estimate its behaviour.

- **If all 10 repetitions match:** the variance is reported as **"not reproduced
  in this sample"**. The Part 6 miss still stands in the record as an
  unreproduced observation. Neither event erases the other, and no defect is
  declared.
- **If 1 of 10 mismatches:** the mismatch is reported with its denominator. A
  single observation does not justify a defect classification.
- **If ≥ 2 of 10 mismatch, or a mismatching pattern appears:** a defect is
  declared, with the rate reported as an interval and never as a point estimate.
- **A wrong-direction failure** — for example PASS where ground truth is not PASS
  — is a blocker regardless of rate.

---

## 12. Anti-cherry-picking summary

| Risk | Control |
|---|---|
| Author picks easy cases | Fixed pool; omission is a logged violation |
| Failed case quietly dropped | Four separate counts; denominator is the pool |
| Failed case re-rolled with a new seed | No re-seeding; re-run needs a new campaign ID |
| Oracle bent to match the agent | Oracles read fixture truth only; frozen at campaign start |
| Threshold lowered after seeing results | Acceptance criteria frozen before execution |
| Sample size quoted as a rate | §2 states explicitly these are samples, not rate estimates |
| Live-site flakiness blamed on the product | Tiers never pooled; separate denominators |

---

## 13. Execution gate

No Part 7 test, randomised campaign, reconnaissance experiment, or live-website
check may run until this plan is explicitly approved. On approval, execution
follows §2–§11 in order, with seeds recorded first.