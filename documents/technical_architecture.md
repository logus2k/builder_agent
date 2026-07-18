# Builder Agent — Technical Architecture

Status: MVP (initial implementation)
Scope: the **execution** stage of the reqoach → planner_agent → **builder_agent** pipeline.
Consumes a planner_agent `plan.json`, builds each feasible task's deliverable with a **local
model (Gemma 4 via opencode)**, and verifies the result with **deterministic code** — no LLM
judge, no Claude, offline-capable.

Upstream: `~/env/assets/planner_agent` (produces `plan.json`). Grandparent: `~/env/labs/requirements`
(reqoach — requirements + coverage).

---

## 1. Why this exists / the boundary

planner_agent stops at a plan: it decides *what to build* and guarantees every task it marks
**feasible** passed the feasibility gate (buildable without fabricating product decisions).
builder_agent does the **doing**: it takes those feasible tasks and produces real artifacts.

Hard constraints (inherited from the ecosystem):
- **Local-only operation.** Execution uses opencode driving Gemma on llama.cpp (`:8500`);
  verification is regex/parse code. **No Claude in the runtime**, works offline.
- **Deterministic verification, not an LLM judge.** A local LLM judging its own build output
  saturates (~92% rubber-stamp — measured in planner's validation). So the acceptance oracle is
  code: does the deliverable exist, parse/compile, and contain no stub fingerprints?

---

## 2. Input — planner's `plan.json`

The handoff contract (produced by `planner_agent/scripts/produce_plan.py`):
```
tasks[]:            # feasible only — every one passed the gate
  task_id, title, kind, deliverable, instructions,
  acceptance{ kind, check }, depends_on[], traces_to[], feasibility{ verdict, reasoning }
questions[]         # genuine unknowns escalated to the requirements author (NOT built)
flagged[]           # tasks that couldn't converge to feasible (NOT built)
coverage_gaps[]     # missing-requirement escalations (NOT built)
graph{ nodes, edges }   # DAG; edges [a,b] = a depends on b
```
builder builds **only `tasks[]`** in dependency order. questions/flagged/coverage_gaps are the
requirements author's to resolve, not builder's to guess.

---

## 3. Pipeline

```
plan.json ──► toposort(tasks, graph) ──► for each task, in dependency order:
                                            opencode build (retry on no-output)
                                            ──► deterministic verify against acceptance
                                            ──► outcome: built | failed | no_output
                                          ──► build report + workspace
```

- **Executor** (`src/builder/opencode.py`) — `opencode run --auto -m local-llama/gemma-4 --dir <ws>`.
  Headless, auto-approves edits, writes real files. Detects files produced *this* run so a
  **shared workspace** works: prerequisites build first, dependents can read their artifacts.
  Retries on no-output (opencode/Gemma occasionally globs-then-stops — a builder flakiness, not a
  plan defect).
- **Verifier** (`src/builder/verify.py`) — deterministic, per-**deliverable**:
  - *produced?* the declared deliverable file exists.
  - *parses?* `py_compile` for `.py`, `json.load` for `.json`, n/a otherwise (not a failure).
  - *stub?* HARD fingerprints (TODO/NotImplemented/placeholder) always disqualify; SOFT
    (mock/simulate/dummy/setTimeout) disqualify only an **implementation** — a *definitional*
    deliverable (interface/schema/contract, by title/kind/ext) may ship a mock example beside it.
  - `clean = parses AND stub_hits < 2` → **built**; else **failed**.
- **Orchestrator** (`src/builder/build.py`) — toposort (dependencies first; cycle-guarded),
  build+verify each, categorize, summarize (`build_success_rate = built / produced`).

Run: `python scripts/build_plan.py <plan.json> [--workspace DIR] [--cap N] [--attach URL]`.

---

## 4. Outcomes & the calibration loop

Per task: **built** (deliverable real & clean), **failed** (produced but stub/doesn't parse),
**no_output** (builder flakiness after retries — excluded from the success rate, a builder issue).

`build_success_rate = built / (built + failed)` is the concrete quality signal. Because builder's
outcomes are **deterministic and real**, they are the ground truth that can calibrate planner's
feasibility gate (a "feasible" task that repeatedly **fails** to build is a gate false-positive to
tune). This closes the cross-agent loop planner validated against by proxy.

---

## 5. Reuse / stack

- **opencode** 1.18.3 (`~/.opencode/bin/opencode`), model `local-llama/gemma-4` → llama.cpp `:8500`.
- Verifier ported from planner's validated build-probe (`stub_signals` HARD/SOFT, definitional-aware).
- Pure stdlib in `src/builder/` (no external deps); `data/` gitignored.

---

## 6. MVP limits / next

- **Acceptance depth:** parse + stub check only. `kind:"test"` should eventually *run* the test;
  `kind:"run"` acceptance is not yet executed. Real execution/test-running is the main upgrade.
- **Shared workspace** is naive (all tasks one dir); no per-feature isolation or git commits yet.
- **No retry-on-failure**: a `failed` task is reported, not re-attempted or fed back to planner.
- **No dependency data flow beyond file presence** (a dependent sees prerequisite files, but we
  don't inject them into the prompt).
- **Feedback to planner** (failed feasible → gate calibration) is designed (§4) but not wired.
- **Not yet a service** — CLI batch only; a socket.io/streaming service like reqoach/reqoach is later.
