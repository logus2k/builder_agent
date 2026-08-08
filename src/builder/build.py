"""Build a planner_agent plan.json: execute each feasible task in dependency order,
verify deterministically, and report. Local-only (opencode + Gemma + code), no Claude.

A task's outcome is one of:
  - built   : deliverable produced, parses, no stub fingerprints (acceptance met)
  - failed  : deliverable produced but doesn't parse or is a stub (quality issue)
  - no_output: opencode produced nothing after retries (builder flakiness)
Prerequisites build before dependents (shared workspace) so a dependent can use them.
"""

from __future__ import annotations

import json
import os
import urllib.request

from . import assemble, opencode, structure, verify

#: The Builder delivers into the project repo's `code/` area — the single-owner layout it shares
#: with the Analyst (requirements/), Architect (architecture/) and Planner (plans/). reqoach owns
#: git; the Builder writes files there and asks reqoach to commit (same pattern as the others).
REPOS_ROOT = os.environ.get("PROJECT_REPOS_ROOT", os.path.expanduser("~/env/project-repos"))
REQOACH_URL = os.environ.get("REQOACH_URL", "http://localhost:7802").rstrip("/")


def repo_code_workspace(pid: str) -> str:
    """The project repo's `code/` area for project `pid`."""
    return os.path.join(REPOS_ROOT, pid, "code")


def publish_to_repo(pid: str, message: str = "Builder: build code from plan") -> dict:
    """Ask reqoach to commit the project repo's `code/` area (reqoach owns git)."""
    body = json.dumps({"area": "code", "agent": "builder", "message": message}).encode()
    req = urllib.request.Request(f"{REQOACH_URL}/repos/{pid}/commit", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def toposort(tasks: list[dict], edges: list[list[str]]) -> list[dict]:
    """Order tasks so that dependencies come first. edges = [a, b] meaning a depends on b."""
    by_id = {t["task_id"]: t for t in tasks}
    deps = {tid: set() for tid in by_id}
    for a, b in edges:
        if a in by_id and b in by_id:
            deps[a].add(b)
    ordered, seen = [], set()

    def visit(tid, stack):
        if tid in seen or tid not in by_id:
            return
        if tid in stack:            # cycle guard — break it, order is best-effort
            return
        stack.add(tid)
        for d in sorted(deps[tid]):
            visit(d, stack)
        stack.discard(tid)
        seen.add(tid)
        ordered.append(by_id[tid])

    for t in tasks:               # preserve plan order among independents
        visit(t["task_id"], set())
    return ordered


def load_handover(pid: str | None) -> dict:
    """Read the Architect handover (planner_handover.json) from the project repo — the design the
    Builder organizes the code around. Empty dict if absent (Builder still runs, less grounded)."""
    if not pid:
        return {}
    path = os.path.join(REPOS_ROOT, pid, "architecture", "planner_handover.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _req_to_aspect(handover: dict) -> dict:
    """Index req_id -> aspect name, from the handover, so a task's traces_to locate its design."""
    idx = {}
    for aspect, a in (handover.get("by_aspect") or {}).items():
        for rid in a.get("req_ids", []):
            idx[rid] = aspect
    return idx


def _design_hint(task: dict, handover: dict, req_aspect: dict) -> str:
    """The design context for a task (reference, not prescription): the aspect(s) it realizes and
    that aspect's full components (responsibility + attributes) and interfaces (purpose + ops), so
    placement is design-aware. No truncation — the 32K slot holds it."""
    aspects = {req_aspect.get(rid) for rid in task.get("traces_to", [])} - {None}
    if not aspects:
        return ""
    out = []
    for name in aspects:
        a = (handover.get("by_aspect") or {}).get(name) or {}
        out.append(f"  aspect '{name}': {a.get('scope','')}")
        for c in (a.get("components") or []):
            attrs = structure._attrs_str(c.get("attributes"))
            out.append(f"    - component {c.get('name','')}: {c.get('responsibility','')}"
                       + (f" [attrs: {attrs}]" if attrs else ""))
        for i in (a.get("interfaces") or []):
            ops = structure._ops_str(i.get("operations"))
            out.append(f"    - interface {i.get('name','')}: {i.get('purpose','')}"
                       + (f" [ops: {ops}]" if ops else ""))
    return "\n".join(out)


def build_plan(plan: dict, workspace: str, cap: int | None = None, retries: int = 2,
               attach: str | None = None, handover: dict | None = None, log=print) -> dict:
    tasks = plan.get("tasks", [])
    order = toposort(tasks, plan.get("graph", {}).get("edges", []))
    if cap:
        order = order[:cap]
    os.makedirs(workspace, exist_ok=True)

    # The design the code is organized around — from plan.source if the caller didn't pass it.
    if handover is None:
        handover = load_handover((plan.get("source") or {}).get("project_id"))
    req_aspect = _req_to_aspect(handover)

    # 1. AGENTIC STRUCTURE: decide the project frame, then place each task within it.
    skeleton = structure.seed_skeleton(plan, handover)
    log(f"frame: {skeleton.get('stack')} | entrypoint {skeleton.get('entrypoint')} | "
        f"manifest {skeleton.get('manifest')}{' (fallback)' if skeleton.get('_fallback') else ''}")
    cmap = structure.new_map(skeleton)
    log(f"building {len(order)} feasible tasks into {workspace}")

    results = []
    for t in order:
        placement = structure.place_task(t, cmap, design_hint=_design_hint(t, handover, req_aspect))
        target = placement["path"]
        files, tries = opencode.build_with_retry(
            t, workspace, target, action=placement["action"], skeleton=skeleton,
            retries=retries, attach=attach)
        if not files:
            v = {"produced": False, "clean": False, "reason": "no output after retries"}
            outcome = "no_output"
        else:
            # Verify the file the agent was asked to write (real path), not the plan's flat name.
            v = verify.verify({**t, "deliverable": target}, files, workspace)
            outcome = "built" if v["clean"] else "failed"
        results.append({"task_id": t["task_id"], "title": t["title"], "kind": t["kind"],
                        "deliverable": t["deliverable"], "path": target,
                        "action": placement["action"], "traces_to": t.get("traces_to", []),
                        "outcome": outcome, "tries": tries if files else retries + 1, **v})
        log(f"  [{outcome:9}] {t['task_id']} -> {target}  ({v.get('reason','')})")

    # 2. ASSEMBLE + RUN-VERIFY: manifest + entrypoint, then an honest boot smoke.
    log("assembling: manifest + entrypoint")
    manifest = assemble.ensure_manifest(workspace, skeleton, log=log)
    entry = assemble.ensure_entrypoint(workspace, skeleton, log=log)
    log("run-verify: compile + install + boot smoke")
    run = assemble.run_verify(workspace, skeleton, log=log)
    log(f"run-verify: runnable={run.get('runnable')} "
        f"(syntax_ok={run.get('syntax_ok')}, deps_installed={run.get('deps_installed')}, "
        f"boot={run.get('boot')})")

    built = sum(1 for r in results if r["outcome"] == "built")
    failed = sum(1 for r in results if r["outcome"] == "failed")
    noout = sum(1 for r in results if r["outcome"] == "no_output")
    judged = built + failed
    return {
        "workspace": workspace,
        "skeleton": skeleton,
        "summary": {"total": len(results), "built": built, "failed": failed,
                    "no_output": noout,
                    "build_success_rate": round(built / judged, 3) if judged else None,
                    "files": len(cmap.get("files", [])),
                    "runnable": run.get("runnable")},
        "assembly": {"manifest": manifest, "entrypoint": entry, "run_verify": run},
        "results": results,
    }


def build_plan_file(plan_path: str, workspace: str, **kw) -> dict:
    plan = json.load(open(plan_path))
    return build_plan(plan, workspace, **kw)
