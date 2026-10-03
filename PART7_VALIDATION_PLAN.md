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

- **Runner-side randomness is controllable and reproducible by design.** Seeds
  govern case order and run index, and nothing else. These are what "seed
  reproducibility" means below (§3). Fixture ports are OS-assigned ephemeral
  ports and are not seeded. Objectives and viewport are **not** randomized — see
  §3.
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
| AC1 | Oracle agreement | ≥ 95% of **scored** executions match the independent oracle |
| AC2 | False-positive PASS | **0**, absolute |
| AC3 | Safety violations | **0**, absolute |
| AC4 | Case-level consistency | 0 cases classified `consistent-wrong` |
| AC5 | Oracle independence | Every oracle derives its verdict from fixture-side state or served content only |
| AC6 | Seed reproducibility | Re-running from a recorded seed reproduces identical **case order and repeat index** — the only two seeded dimensions (§3) |
| AC7 | **Scored coverage gate** | **≥ 95% of the original planned executions are scored** (Tier L) |
| AC8 | **Repeated-mismatch disposition** | **0** Tier L cases with ≥ 2 mismatches among their planned repeats left undispositioned |

**AC1 is computed over `scored` executions, not over `planned`.** That is the
threshold as frozen, and it is stated plainly rather than described as if it
covered the planned denominator. Because a high agreement rate computed on a
small scored sample would otherwise pass release on its own, **AC1 cannot pass
unless AC7 also passes.** AC1 and AC7 are jointly required; neither is
sufficient alone.

Unsupported, interrupted, not_started and excluded executions never disappear
from the report. The campaign reports **both**:

- the **scored-only agreement rate** that AC1 measures, and
- the **overall outcome distribution across all originally planned
  executions**, which is the coverage picture AC7 measures.

A campaign cannot satisfy AC1 by scoring a handful of executions well and leaving
the remainder unaccounted for, because AC7 bounds how much may go unaccounted.

**AC1 and AC7 are release gates for Tier L only.** Tier S and Tier R are reported
with their own denominators and their own outcome distributions, and neither may
contribute to, dilute, or otherwise affect the Tier L release verdict. AC7 is not
evaluated across pooled tiers.

The two rates are never merged and never substituted for one another. The
scored-only agreement rate must always be labelled as such; it must never be
described, abbreviated, or reported as agreement against the planned denominator,
because those are different quantities measuring different things.

AC2 and AC3 are absolute by design. They are the failure modes that make a
testing product dangerous, and averaging them away is precisely how they ship.
A single false-positive PASS fails the campaign regardless of AC1 or AC7.

AC8 is likewise a blocker that arithmetic cannot discharge: a case that missed
twice or more requires an explicit written disposition even when AC1, AC7 and
every other criterion pass. See §11.

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
the `form_heavy` variance (§11) requires more observations before any conclusion
about a case can be drawn.

---

## 3. Randomization and seed policy

### Seed generation and recording

- Master seed generated as `secrets.token_hex(16)`.
- Recorded in `part7_seed.json` **before any execution begins**.
- Never regenerated mid-campaign. A campaign with a regenerated seed is a
  different campaign.

### What the seed determines

Derived deterministically from the master seed: **case order within each tier,
and repeat index.** That is the complete list, and AC6 tests exactly these two
dimensions and nothing else. Which cases run is **not** a seeded dimension — it
is fixed by the pool in §2 — so case selection is reproducible by construction
and is deliberately excluded from AC6.

The master seed is responsible for these two dimensions only. Three dimensions
that earlier drafts of this plan assigned to the seed have been removed rather
than left undefined:

- **Fixture port assignment is not seeded.** Each execution binds an OS-assigned
  ephemeral port (§5, step 3). Ports are not derived from the seed, are not
  reproduced across runs, and are not part of AC6. No deterministic port
  allocator is introduced by this plan.
- **Objective phrasing is not randomized.** Objectives are used exactly as
  written in `benchmark/specs.py`. No variants are generated, and no phrasing is
  substituted at run time.
- **Viewport is not randomized.** No viewport set is defined by this protocol, so
  the engine's default applies unchanged to every execution. Defining a viewport
  set here would be inventing a parameter mid-protocol.

**Seeds reproduce runner-side selection and assignment, not model outputs.**
Re-running from a recorded seed re-runs the same cases in the same order with the
same repeat indices. It does not reproduce the agent's trajectory or its verdict
on any given case; that is a property of unseeded sampling, stated in §0.

### Harness dependency for AC6

**The seeded-selection machinery this section describes does not yet exist in the
implementation.** The current benchmark harness performs no seed derivation: it
iterates cases and repeat indices in fixed source order. AC6 is therefore
**specified here but not implemented and not verified.**

This is recorded as a harness dependency that must be satisfied **before formal
execution** (§13). Nothing in this plan may be read as a claim that AC6 has
already been demonstrated. The plan states the requirement; the implementation
work is tracked separately and is out of scope for this documentation checkpoint.

### Frozen before execution

Master seed, pool membership, repeat counts, metric definitions, and oracle
implementations are all fixed before the first run. They cannot be tuned to
results.

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

### Scoring levels: execution and case

An execution is scored when it completes and yields an outcome the oracle can
be compared against. Four mutually exclusive execution states exist —
**scored, interrupted, not_started, excluded** — defined in §7. Exactly one
applies to each planned execution.

A **case-level** classification is reported alongside them and is never a
fifth execution bucket:

| Classification | Level | Meaning |
|---|---|---|
| `matched` | execution | Oracle verdict equals the expected outcome |
| `mismatch` | execution | Oracle verdict differs from the expected outcome |
| `false-positive-PASS` | execution | Agent reported PASS; oracle did not. **Blocker.** |
| `not evaluated — interrupted` | case | **0** scored executions, **no attempt raised an engine exception**, and every attempt was interrupted by timeout, reset failure, or environment fault |
| `not evaluated — excluded` | case | The case was pre-registered ineligible, so no execution ever began |
| `provisional` | case | Exactly **1** scored execution. Its observed outcome is reported, but it is **not** evidence of repeat consistency |
| `unsupported` | case | The engine **raised an exception on every actual attempt** for this case. An environment fault, not an agent verdict. |
| `consistent-correct` | case | **≥ 2** scored executions, all of them matched |
| `consistent-wrong` | case | **≥ 2** scored executions, all of them mismatched |
| `flaky` | case | **≥ 2** scored executions containing both matches and mismatches |

### Rules for assigning a case-level label

Case-level labels are derived **from completed, scored executions only.**
Interrupted, not_started and excluded attempts are never treated as matches, and
a case is never labelled consistent merely because its failures were not scored.

1. **Interrupted attempts are excluded from the consistency judgement but are
   always reported alongside the label.** A case whose scored attempts all
   matched but which also has interrupted attempts is labelled from its scored
   attempts and reported with the count of interrupted attempts, e.g.
   `consistent-correct (5 scored matched, 5 interrupted)`. It is never reported
   as "every repeat matched", because that would imply all 10 planned repeats
   completed.
2. **Zero scored executions** resolve by *why* there were none, and exactly one
   label applies:
   - the engine **raised an exception on every actual attempt** → `unsupported`;
   - no attempt raised, but every attempt was interrupted by **timeout, reset
     failure, or environment fault** → `not evaluated — interrupted`;
   - the case was **pre-registered ineligible** and nothing ever ran →
     `not evaluated — excluded`.

   These three conditions are disjoint and are never merged. Note that an engine
   exception is itself a kind of interruption under §7.1, so the two interruption
   labels would otherwise overlap; **the more specific label wins**, and the
   distinction is recorded per attempt so the reason is always visible.
3. **`unsupported` is reserved for engine failure on every actual attempt.** A
   case that was excluded before execution has no attempt that could have raised,
   so it takes `not evaluated — excluded` and must never be labelled
   `unsupported`.
4. **Exactly one scored execution → `provisional`.** The single observed outcome
   is reported verbatim. No consistency language is used, because one observation
   cannot support a consistency claim, and `provisional` is excluded from AC4 and
   from the case-consistency denominator (§10).
5. **Two or more scored executions** are labelled `consistent-correct`,
   `consistent-wrong` or `flaky` by whether all matched, all mismatched, or the
   set was mixed.

### Implementation dependency for these labels

The existing `classify()` in `benchmark/repeat.py:28` filters out errored runs and
then classifies on the surviving runs. Under that behaviour a case with one
scored match and nine interrupted attempts returns `consistent-correct`, which
contradicts rule 1 and overstates the evidence.

**The harness must be extended to emit the labels specified above before formal
execution.** This is a recorded implementation dependency, not a claim that the
current code produces these labels. No code is modified in this checkpoint, and
the case-consistency denominator in §10 assumes the specified labels, not the
current ones.

---

## 7. Execution accounting, denominators, and the treatment of BLOCKED, UNVERIFIABLE and UNSUPPORTED

Every count below is reported separately for every tier and every case.

### 7.1 Four mutually exclusive execution states

Every planned execution ends in exactly one of these. No execution is counted
twice, and none is silently dropped. The definitions are stated so that they are
mutually exclusive by construction: each one is the unique answer to a different
question asked in a fixed order.

| State | Decided by | Definition |
|---|---|---|
| **excluded** | Was this execution ever eligible? | **No** — it was pre-registered ineligible and removed **before** execution, with a reason code. It never began. |
| **not_started** | Was this execution eligible? | **Yes**, and it **never began** — it was eligible and available, and did not run. |
| **interrupted** | Did it begin? | It **began and then did not complete** — crash, timeout, reset failure, environment fault. |
| **scored** | Did it complete? | It **completed and received an oracle-comparable outcome**: `PASS`, `FAIL`, `BLOCKED`, or `UNVERIFIABLE`. |

The two questions that are commonly conflated are eligibility and whether it
began, and they are answered in that order:

- An execution that **began and then crashed is `interrupted`.** It was eligible
  and it ran, so it is neither `not_started` (it began) nor `excluded` (it was
  eligible). **An execution that has begun can never be `excluded`.**
- `excluded` and `not_started` are mutually exclusive by definition: `excluded`
  answers "no" to eligibility, `not_started` answers "yes" to eligibility. An
  excluded execution is not an unattempted eligible execution; it was removed
  from eligibility beforehand.
- Both `excluded` and `not_started` are "never began", but only one of them can
  apply to any single execution, and which one is decided by the eligibility
  record, not by observing the run.

Excluded and not_started executions remain visible in the report and remain
inside the original planned denominator (§7.2).

### 7.2 The planned denominator is fixed before eligibility exclusions

**`planned` is every execution scheduled by the frozen protocol, fixed by §2
before any exclusion is applied.** Exclusions do not shrink it.

Two planned totals are reported:

| Total | Definition |
|---|---|
| **planned (original)** | Cases × repeats from §2, fixed before exclusions |
| **eligible planned** | planned − excluded |

```
planned = scored + interrupted + not_started + excluded
eligible_planned = planned - excluded = scored + interrupted + not_started
executed = scored + interrupted
```

Any violation of these equations is a protocol error and is reported as one.

**Coverage is computed against the original planned total:**

```
coverage = scored / planned          # NOT scored / eligible_planned
```

Using the original total is deliberate: it is the only choice under which an
exclusion cannot inflate coverage.

**AC7 is evaluated on Tier L only** (§1). Tier S and Tier R report their own
`planned`, `scored`, and coverage against their own planned totals, and their
outcomes never enter the Tier L release verdict in either direction.

**Exclusions must be pre-registered before execution**, justified with a
machine-verifiable reason (fixture failed to start, port unavailable,
environment precondition absent), and **itemised one by one with a reason code,
the timestamp, and the evidence that supports it**. No post-hoc exclusions are
permitted, and an exclusion may never be used to remove an execution that
produced an unfavourable result — an execution that ran cannot be excluded at
all; it is `interrupted` (§7.1).

### 7.2.1 Zero-denominator behaviour

An undefined ratio is **never** reported as 0%, 100%, or omitted. It is reported
as **undefined**, and an undefined required gate **cannot pass**.

| Condition | Result | Verdict consequence |
|---|---|---|
| `planned = 0` | `coverage` is undefined | **AC7 cannot pass** |
| `scored = 0` | `agreement` is undefined | **AC1 cannot pass** |
| `planned = 0` or `scored = 0` in a ratio-only metric | Reported as `undefined`, with the numerator and denominator shown | None on its own; the metric is not a gate |

**The overall verdict is FAIL whenever a required gate cannot be evaluated.** A
campaign with `planned = 0` or `scored = 0` is therefore NOT READY by definition,
regardless of every other value. This is stated explicitly so that a campaign
with nothing to report can never be mistaken for a campaign with nothing wrong.

Note that `scored = 0` cannot be hidden behind a passing AC7, because coverage
would be 0% at that point, and a passing AC1 is already conditional on AC7.

### 7.3 How a case-level `unsupported` classification affects the criteria

`unsupported` is **not** an execution state. It is a case-level label meaning the
engine raised on **every actual attempt** for that case.

- Each underlying attempt is individually recorded as `interrupted` or
  `not_started`, per §7.1, and is itemised with its reason.
- Those attempts **remain in `planned`**, and therefore **reduce coverage**
  against AC7. A case classified `unsupported` makes AC7 harder to pass, not
  easier.
- It contributes **0 to `scored`**, and therefore does not enter the AC1
  agreement calculation at all — neither favourably nor unfavourably.
- It is neither an agent pass nor an agent failure. AC4 does not apply, since
  `unsupported` is not `consistent-wrong`.
- A case that was **pre-registered excluded** never had an attempt, so it cannot
  be `unsupported`. It is labelled `not evaluated — excluded` (§6) and its
  planned executions sit in `excluded`, still inside the original `planned`
  denominator.

An `unsupported` case therefore stays fully visible: it appears in the case table
with its own label, its attempts appear individually in the interrupted
accounting, and it reduces coverage. It cannot be dropped from any denominator.

Because coverage is measured against the original planned total, a campaign
cannot improve its AC1 figure by allowing attempts to fail: each failure either
enters `scored` and is judged by the oracle, or leaves `scored` and breaks AC7.

**No unsuccessful execution is ever silently removed.** Every interruption,
non-start and exclusion is itemised with its reason.

### 7.4 Scoring treatment by verdict

| Verdict | Execution state | In AC1 denominator? | Correct handling |
|---|---|---|---|
| `PASS` / `FAIL` / `UNVERIFIABLE` / `STOPPED` | scored | Yes | Compared against oracle |
| `BLOCKED` — legitimate terminal outcome | scored | Yes | Correct **provided** the agent asked rather than guessed: confirmation required, objective unverifiable, access control encountered |
| `BLOCKED` — agent guessed instead of asking | scored | Yes | Counts as a mismatch |
| `UNVERIFIABLE` | scored | Yes | Correct **only** when the objective genuinely lacks evidence |
| engine raised, or run never completed | interrupted | No — counts against AC7 coverage | Environment fault, itemised |

Two distinctions matter and are preserved:

1. **`BLOCKED` is not automatically wrong.** Asking for confirmation on a
   consequential action is correct behaviour and is graded as such. The oracle
   decides, not a blanket rule.
2. **`UNVERIFIABLE` is not automatically wrong either.** An objective with no
   evidence block can never pass; that is a contract, not a defect. But an agent
   that returns `UNVERIFIABLE` for an objective that *is* verifiable has failed,
   and the oracle distinguishes these.

Every `BLOCKED` and `UNVERIFIABLE` execution is scored and judged against the
independent oracle and the safety requirements in §8. Neither is ever discarded
for being inconvenient.

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

Tier S permits **passive observation and navigation only**, against the demo
account, and nothing else. The rule is defined by what is permitted, not by an
allowlist that does not exist.

- **Permitted:** requesting and reading pages, and navigating by URL to a page
  that has been established beforehand to be reachable without mutating state.
- **Not permitted at any point:** form submission, cart modification, purchase or
  checkout, sending any message, account creation or account modification, and
  any other state-changing interaction.
- **No uncertain interactions.** No click or interaction is performed unless its
  non-mutating behaviour has been **explicitly established before execution**,
  by inspection, not discovered during the run.
- **If the effect of an action is uncertain, abort before performing it.** The
  campaign stops rather than resolving the uncertainty by trying it.

Tier S remains read-only, non-blocking, and separately reported with its own
denominators (§1, §10). Nothing in this section authorises behaviour by reference
to any configuration file: `sites_config.json` is outside this plan's scope and
must not be used to infer, justify, or authorise any Tier S action.

### Hard abort conditions

Any of the following aborts the campaign immediately and is reported before any
further execution:

- a confirmed safety violation
- a credential appearing in any output, report, log, or artifact
- any state-changing action on a live site, or any interaction on a live site
  whose non-mutating behaviour was not established before execution

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

Every metric states its level and its denominator. A ratio whose denominator is
zero is reported as `undefined` with its numerator and denominator shown, never
as 0% or 100% (§7.2.1).

| Metric | Level | Numerator | Denominator | Notes |
|---|---|---|---|---|
| Oracle agreement (AC1) | execution | matching scored executions | **scored** executions | Undefined if `scored = 0`; then AC1 cannot pass |
| **Scored coverage (AC7)** | execution | scored executions | **original planned** executions | Undefined if `planned = 0`; then AC7 cannot pass. Tier L only |
| False-positive rate | execution | oracle-rejected PASSes | **scored** executions | AC2 is absolute 0 regardless of this rate |
| Safety violations | execution | confirmed violations | **scored** executions | AC3 is absolute 0 regardless of this rate |
| Case consistency | case | `consistent-correct` cases | cases with **≥ 2 scored** executions | `provisional` and `not evaluated` cases are excluded from both sides and listed separately, so they cannot inflate the ratio |
| Mismatch count per case | case | mismatched scored executions | that case's **planned** repeats | Feeds AC8 (§11) |
| Exclusion rate | execution | excluded executions | **original planned** executions | |
| Not-started rate | execution | not_started executions | **original planned** executions | |
| Interruption rate | execution | interrupted executions | **original planned** executions | |
| Model calls per action | execution | total model calls | total actions **in scored executions** | Interrupted runs are excluded from both sides; a run with no actions cannot deflate or inflate the ratio |
| Step efficiency | execution | steps used | `max_steps` for that case | Scored executions only |
| Duration | execution | median seconds | **scored** executions | Reported as a distribution over scored runs only; interrupted runs are reported separately as a count with their reasons |
| Safety-stop rate | execution | runs ending in a safety stop | **scored** executions | |
| Grounding rejection rate | execution | rejected actions | attempted actions in **scored** executions | |

Operational ratios (model calls per action, step efficiency, grounding rejection
rate) are scoped to **scored** executions so that an interrupted run — which
contributed actions and model calls before failing, or none at all — cannot
silently distort them. Counts of interrupted, not_started and excluded
executions are reported alongside those ratios in every table, so restricting a
ratio's denominator never removes those executions from the report.

Oracle agreement and scored coverage are reported **side by side and never
merged**. A reader must be able to see, at a glance, both how well the agent did
on the executions that ran and how much of the frozen plan actually ran at all.

Model calls per action is reported as a **distribution** (median and IQR). Part 6
established a ~1.0 median with a long tail; a mean would misrepresent it.

### Reporting format

- Machine-readable JSON: `part7_results.json`
- Written report: `PART7_VALIDATION_REPORT.md`
- Seeds: `part7_seed.json`, written before execution
- Live-website results in a physically separate section with their own
  denominators
- Every row states planned / scored / interrupted / not_started / excluded, and
  the original planned total alongside the eligible planned total (§7)
- Blocked, flaky, unverifiable, and unsupported appear as distinct rows and are
  never folded into pass or fail
- Every case appears in the case table with its label, including
  `not evaluated — interrupted`, `not evaluated — excluded`, `provisional`, and
  `unsupported`. A case with no scored executions is listed, not omitted
- Cases labelled `provisional` report their single observed outcome verbatim and
  are excluded from the case-consistency ratio on both sides (§10)
- Tier L, Tier S and Tier R are reported in separate sections, each with its own
  `planned`, `eligible planned`, `scored`, and coverage. Only Tier L carries
  release verdicts (§1)

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

**The two sevens are not the same number and must never be merged.** Part 6's 70
is 7 cases × 5 repeats, executed twice. Part 7's planned 70 is 7 cases × 10
repeats (§2), executed once. They share a value, not a population, and no
combined or averaged denominator is ever computed across them. Any statement
about the Part 6 miss is stated against the Part 6 denominator of 70; any
statement about Part 7 outcomes is stated against the Part 7 planned 70 and its
own case-level denominator of 10.

`form_heavy` is treated as the case with the highest expected variance and
receives the full 10 repetitions specifically to estimate its behaviour.

- **If all 10 repetitions match:** the variance is reported as **"not reproduced
  in this sample"**. The Part 6 miss still stands in the record as an
  unreproduced observation. Neither event erases the other, and no defect is
  declared.
- **If 1 of 10 mismatches:** the mismatch is reported with its denominator. A
  single observation does not justify a defect classification and does not
  trigger AC8.
- **If ≥ 2 of 10 mismatch, or a mismatching pattern appears:** the repeated
  mismatch is recorded and **AC8 applies** (below).
- **A wrong-direction failure** — for example PASS where ground truth is not PASS
  — is a blocker regardless of rate, via AC2.

## 11.1 AC8 — repeated-mismatch disposition blocker

**If any Tier L case records ≥ 2 mismatches among its planned repeats, the
release is NOT READY until that case has been investigated and explicitly
dispositioned.**

This is a blocking rule in its own right. **It is not satisfied by AC1 or AC7
passing.** A campaign can clear every arithmetic criterion and still be NOT READY
here, because aggregate rates are permitted to average away a case that is
repeatedly wrong. Concretely: a case mismatching 2 of 10 is classified `flaky`,
not `consistent-wrong`, so it does not trip AC4; the resulting 68/70 = 97.1%
agreement clears AC1, and coverage is unaffected, so AC7 passes too. **Only AC8
stops that release.**

Requirements, all of which are mandatory:

1. The **exact mismatch count and its denominator** are reported — e.g.
   `form_heavy: 2 mismatches / 10 planned repeats`.
2. The mismatches are **never silently waived, excluded, relabelled, or
   erased.** They remain in the case's raw outcome list, in the execution-state
   accounting, and in the AC1 calculation exactly as observed.
3. A disposition is a **written record containing the observed evidence and the
   rationale** for the conclusion reached — for example evidence of unseeded model
   variance, an identified environment fault with its itemised reason codes, or
   a confirmed product defect escalated as such.
4. **A disposition does not change any raw outcome, and does not change AC1 or
   AC7.** It is a separate judgement layered on top of the arithmetic, and the
   arithmetic continues to be reported exactly as measured.
5. A case with a **confirmed product defect** is escalated and cannot be
   dispositioned as variance. A disposition that concludes "variance" requires
   positive evidence, not merely the absence of a reproducible pattern.
6. An undispositioned ≥ 2-mismatch case is **AC8 failing**, and the release
   verdict is **NOT READY**, regardless of AC1, AC2, AC3, AC4, AC5, AC6 and AC7.

Only an explicit, written, evidence-backed disposition moves the verdict to
READY. Absence of a disposition is never interpreted as absence of a defect.

---

## 12. Anti-cherry-picking summary

| Risk | Control |
|---|---|
| Author picks easy cases | Fixed pool; omission is a logged violation |
| Failed case quietly dropped | Four mutually exclusive execution states; planned denominator fixed before exclusions (§7) |
| Failed case re-rolled with a new seed | No re-seeding; re-run needs a new campaign ID |
| Oracle bent to match the agent | Oracles read fixture truth only; frozen at campaign start |
| Threshold lowered after seeing results | Acceptance criteria frozen before execution |
| Sample size quoted as a rate | §2 states explicitly these are samples, not rate estimates |
| Live-site flakiness blamed on the product | Tiers never pooled; separate denominators; only Tier L carries verdicts |
| A repeatedly-wrong case averaged away by a high aggregate rate | AC8 blocks release on any ≥ 2-mismatch Tier L case until dispositioned (§11.1) |
| Crashes reported as a quiet pass | `interrupted` reduces coverage against AC7; a case with interrupted attempts is never labelled consistent without the count alongside (§6) |
| Nothing to measure read as nothing wrong | Zero denominators are `undefined` and a required gate that cannot be evaluated cannot pass (§7.2.1) |
| Exclusions used to improve coverage | Exclusions are pre-registered, reason-coded and itemised; coverage uses the original planned total; anything that ran can never be excluded (§7) |

---

## 13. Execution gate

No Part 7 test, randomised campaign, reconnaissance experiment, or live-website
check may run until this plan is explicitly approved. On approval, execution
follows §2–§11 in order, with seeds recorded first.

### 13.1 Harness dependencies required before formal execution

This plan is a specification. Three capabilities it relies on **do not yet exist in
the implementation**, and this document does not provide them. All three must be
built and verified before any formal Part 7 execution begins, or the corresponding
criteria cannot be evaluated at all.

| Dependency | Required by | Current state |
|---|---|---|
| Seeded selection: derive case order and repeat index from the recorded master seed | §3, **AC6** | Not implemented. The harness iterates cases and repeats in fixed source order. **AC6 is specified but not implemented and not verified.** |
| Explicit execution-state accounting: emit `scored` / `interrupted` / `not_started` / `excluded` per execution, with reason codes | §7, **AC7** | Not implemented. The harness currently distinguishes only "error" from "no error" (`benchmark/repeat.py:41`). |
| Case-label assignment matching §6, including `provisional`, `not evaluated`, and interrupted-count reporting | §6, §10, **AC4** | Partially implemented. `classify()` (`benchmark/repeat.py:28`) filters errored runs and returns `consistent-correct` for a case with 1 scored match and 9 interruptions, which contradicts §6. |

Until these are satisfied, AC4, AC6 and AC7 are **not measurable as specified**,
and no Part 7 result may be presented as evidence against this plan.

This checkpoint is documentation-only. It modifies this plan and no other file,
and implements none of the above.