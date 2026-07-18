# Builder Agent

**Builder Agent takes a plan of buildable tasks and actually builds them — with a local model,
and checks that each result is real, not a stub.**

It's the execution stage of a local, requirements-to-code pipeline. [Planner Agent](../planner_agent)
produces a plan of small, ready-to-build tasks; Builder Agent picks up that plan, generates each
task's file with a **local AI coder (Gemma via opencode)**, and verifies the output with plain,
deterministic checks. Nothing leaves your machine, and it works offline.

```
reqoach  ──►  planner_agent  ──►  Builder Agent
requirements   a plan of buildable   builds each task
+ gaps         tasks                  and verifies it
```

---

## What it does

Given a `plan.json` from Planner Agent, Builder Agent:

1. **Builds the tasks in the right order** — it follows the plan's dependency graph, so a task's
   prerequisites are built before the task that needs them.
2. **Generates each artifact with a local model** — it drives opencode (running Gemma) to write
   the actual file the task asks for.
3. **Verifies the result deterministically** — with code, not another AI. It checks the file was
   produced, that it parses/compiles, and that it isn't a **stub or mock** pretending to be a real
   implementation.
4. **Reports the outcome per task** — `built` (real and clean), `failed` (produced but a stub or
   broken), or `no_output` (the builder didn't produce anything, retried).

---

## Why use it

- **It builds locally.** A local model does the coding, so you can turn a plan into real files
  without a cloud AI service — private and offline.
- **It doesn't trust the AI's own word.** A model asked "did you do a good job?" almost always
  says yes. Builder Agent instead **verifies with deterministic code** — it catches fake/mock
  implementations that look plausible but don't actually do the work.
- **It respects dependencies.** Tasks build in order, so prerequisites exist before the code that
  relies on them.
- **It's honest about failure.** A task that comes back as a stub is reported as `failed`, not
  quietly accepted — which also tells the planner when a task it thought was ready actually wasn't.
- **It completes the local pipeline.** Together with reqoach and Planner Agent, you get
  requirements → plan → working artifacts, all on local infrastructure.

---

## Quickstart

You'll need opencode installed and pointed at a local Gemma model, and a `plan.json` produced by
Planner Agent.

```bash
python scripts/build_plan.py <plan.json> [--cap N]
#   builds the plan's ready tasks into a workspace and writes a build report
```

The build report lists each task's outcome; the workspace holds the generated files.

---

## How it verifies (without another AI)

The check is intentionally simple and deterministic:

- **produced?** the file the task promised actually exists.
- **parses?** it compiles / parses for its language.
- **real, not a stub?** it doesn't contain the fingerprints of a fake implementation
  (`TODO`, `NotImplemented`, hardcoded mock data, "simulate…"). Definition-only deliverables
  (an interface or schema) aren't penalized for shipping a small example alongside them.

If all hold, the task is `built`. This deterministic outcome is trustworthy *because* it doesn't
rely on a model's self-assessment — and it's the honest signal that can be fed back to Planner
Agent to sharpen which tasks it calls "ready".

---

## Repository layout

```
src/builder/
  opencode.py   drive the local AI coder (opencode + Gemma) to build one task
  verify.py     deterministic verification (produced / parses / not-a-stub)
  build.py      order tasks by dependency, build + verify each, report
scripts/
  build_plan.py   build a planner plan.json
documents/
  technical_architecture.md
```

---

## Where it fits

| Stage | Project | Role |
|---|---|---|
| Requirements | [reqoach](../../labs/requirements) | score requirements, find gaps |
| Plan | [planner_agent](../planner_agent) | break requirements into buildable tasks |
| **Build** | **Builder Agent** (this repo) | **build each task locally and verify it** |

Builder Agent only builds what the plan already marked ready — it doesn't decide *what* to build.
Design details and current limits live in [`documents/`](documents/).

---

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE).
