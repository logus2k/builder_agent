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

from . import assemble, context, heal, opencode, release, repair, structure, verify

_SKIP_DIRS = {"__pycache__", ".venv_build", ".venv", ".git", ".opencode", "node_modules"}


def _list_code_files(ws: str) -> list[str]:
    """Real source files under the code area (build tooling + release.json excluded)."""
    out = []
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if f == "release.json" or f.endswith(".pyc"):
                continue
            out.append(os.path.relpath(os.path.join(root, f), ws))
    return sorted(out)


def _normalize_collisions(groups: dict, log=print) -> dict:
    """DETERMINISTIC structural contract: a concept is a module OR a package, never both. Placement
    is a per-task model decision, so one task can land at `app/crud.py` while another lands at
    `app/crud/user.py` — Python then loads the package and the module's symbols are unreachable (the
    dominant coherence defect, measured ~74% of export mismatches). Here, before any code is
    generated, any `X.py` whose stem `X` is also used as a package directory is folded into
    `X/__init__.py`. One canonical location per concept, decided once, so the collision never occurs."""
    dirs = {os.path.dirname(p) for p in groups if "/" in p}
    fixed: dict[str, list] = {}
    for path, tasks in groups.items():
        if path.endswith(".py") and not path.endswith("__init__.py") and path[:-3] in dirs:
            newpath = path[:-3] + "/__init__.py"
            log(f"  collision-prevent: {path} -> {newpath} (stem is also a package)")
            fixed.setdefault(newpath, []).extend(tasks)
        else:
            fixed.setdefault(path, []).extend(tasks)
    return fixed


def _internal_tops(ws: str) -> set:
    """Top-level workspace packages/modules — imports of these are INTERNAL, never a third-party
    dependency, so the conformance check must not flag them."""
    tops = set()
    for e in os.listdir(ws):
        if e.startswith(".") or e in _SKIP_DIRS:
            continue
        if os.path.isdir(os.path.join(ws, e)):
            tops.add(e)
        elif e.endswith(".py"):
            tops.add(e[:-3])
    return tops


#: A sanctioned framework directly re-exports / pairs with packages a file may import by name; admit
#: them for the import-conformance check so we don't false-reject (they install transitively).
_IMPLIED_DEPS = {"fastapi": ["starlette", "pydantic"]}

#: The FRAME owns the web-framework deps — the proportionality model decides persistence + extras but
#: is unreliable about the framework (it omitted fastapi, and leaked JS libs). So we GUARANTEE the
#: stack's Python packages in code, detected from the skeleton by SUBSTRING (the frame's `stack`
#: string is free-form — "FastAPI", "Python/FastAPI", "FastAPI (Python 3)" — so an exact-key lookup
#: is fragile; we scan stack + run_cmd + language + entrypoint for a known framework signature).
_STACK_SIGNATURES = [
    # pydantic-settings is a SEPARATE PyPI package from pydantic (not pulled in by it), and the
    # AGENTS.md contract tells the model to read config via `pydantic_settings.BaseSettings` — so a
    # FastAPI app here needs it in the manifest or it fails to boot on `import pydantic_settings`.
    ("fastapi", ["fastapi", "pydantic", "pydantic-settings", "uvicorn", "python-multipart", "starlette"]),
    ("flask", ["flask"]), ("django", ["django", "djangorestframework"]),
    ("starlette", ["starlette", "uvicorn"]), ("aiohttp", ["aiohttp"]),
]

#: Frontend / JS / CSS libraries that a persistence-focused model sometimes lists but that are NOT
#: PyPI packages — they must never reach the Python manifest (pip install would fail). Stripped from
#: the manifest; harmless if they linger in the allow-list (no Python file imports them).
_FRONTEND_DENY = {"leaflet", "mapbox-gl", "mapbox", "react", "react-dom", "vue", "angular", "jquery",
                  "bootstrap", "tailwindcss", "d3", "openlayers", "chart.js", "chartjs", "htmx"}


def _frame_deps(skeleton: dict) -> list:
    """The framework's Python deps, detected by signature from the (free-form) frame fields. Falls
    back to a FastAPI/ASGI set when the run command uses uvicorn but no explicit signature matched."""
    hay = " ".join(str(skeleton.get(k, "")) for k in ("stack", "run_cmd", "language", "entrypoint")).lower()
    for sig, deps in _STACK_SIGNATURES:
        if sig in hay:
            return deps
    if "uvicorn" in hay:      # ASGI runner with an unrecognised stack name -> assume FastAPI/Starlette
        return ["fastapi", "pydantic", "uvicorn", "python-multipart", "starlette"]
    return []


def _policy_python_deps(policy: dict) -> list:
    """The policy's sanctioned deps minus known frontend/JS libs (which are not pip-installable)."""
    return [d for d in (policy.get("sanctioned_dependencies") or [])
            if isinstance(d, str) and d.strip().lower() not in _FRONTEND_DENY]


def _effective_sanctioned(policy: dict, skeleton: dict) -> list | None:
    """The import allow-list to enforce: the frame's guaranteed framework deps + the Architect's
    sanctioned deps + the runner + framework-implied packages. Returns None only when there is no
    policy at all (errored/absent) — then conformance no-ops rather than false-reject everything."""
    if policy.get("error"):
        return None
    frame = _frame_deps(skeleton)
    base = [d for d in (policy.get("sanctioned_dependencies") or []) if isinstance(d, str)]
    if not base and not frame:
        return None
    eff = set(base) | set(frame)
    for d in list(eff):
        eff.update(_IMPLIED_DEPS.get(d.strip().lower(), []))
    if "uvicorn" in (skeleton.get("run_cmd") or ""):
        eff.add("uvicorn")
    return sorted(eff)


def _manifest_deps(policy: dict, skeleton: dict) -> list:
    """The top-down manifest: guaranteed framework deps + justified Python extras (persistence deps
    are already in the policy's sanctioned list) + the runner. Frontend/JS libs stripped."""
    deps = set(_frame_deps(skeleton)) | set(_policy_python_deps(policy))
    if "uvicorn" in (skeleton.get("run_cmd") or ""):
        deps.add("uvicorn")
    return sorted(deps)


#: Build files lowest-layer first so dependents see their prerequisites (the plan graph is thin).
_LAYER_RANK = [("config", 0), ("setting", 0), ("core", 1), ("base", 1), ("db", 2), ("database", 2),
               ("session", 2), ("model", 3), ("schema", 3), ("entit", 3), ("repositor", 4),
               ("client", 4), ("dao", 4), ("service", 5), ("processor", 5), ("router", 6),
               ("api", 6), ("endpoint", 6), ("view", 6), ("controller", 6), ("test", 8), ("main", 9)]


def _concept_vocab(handover: dict) -> list[str]:
    """Canonical concept keys from the architecture's components + interfaces — a stable vocabulary
    so the placer names concepts consistently across the whole plan (same-concept tasks then land
    in one file instead of scattering under paraphrased names)."""
    vocab = set()
    for a in (handover.get("by_aspect") or {}).values():
        for c in a.get("components", []):
            if c.get("name"):
                vocab.add(structure._concept_key(c["name"]))
        for i in a.get("interfaces", []):
            n = i.get("name") or ""
            if n.lower().endswith("interface"):
                n = n[:-len("interface")]
            if n:
                vocab.add(structure._concept_key(n))
    return sorted(v for v in vocab if v)


def _order_files(paths: list[str], skeleton: dict) -> list[str]:
    entry = (skeleton or {}).get("entrypoint")

    def rank(p: str) -> int:
        if p == entry:
            return 10
        pl = p.lower()
        for kw, r in _LAYER_RANK:
            if kw in pl:
                return r
        return 5
    return sorted(paths, key=lambda p: (rank(p), p))

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

    # Write the shared BUILD CONTRACTS (AGENTS.md, auto-loaded by opencode) + the custom builder
    # agent, so every file builds into ONE coherent app in a single continued session.
    context.write_project_context(workspace, skeleton, handover)

    # STACK POLICY (Architect proportionality review, carried in the handover): the sanctioned
    # third-party allow-list the build must CONFORM to. Enforced at acceptance (unsanctioned import
    # -> not 'built') and used to write the manifest top-down. None when no usable policy is present.
    policy = (handover or {}).get("stack_policy") or {}
    sanctioned = _effective_sanctioned(policy, skeleton)
    if sanctioned:
        log(f"stack policy: {policy.get('recommended_persistence','?')} | sanctioned deps: {sanctioned}")

    # PHASE 1 — PLACEMENT: assign every task to a file (concept+layer consolidation). Cheap persona
    # calls, no opencode. Result: a file -> [tasks] grouping. A canonical concept vocabulary (from
    # the architecture) keeps concept keys stable so same-concept tasks collapse into one file.
    concept_vocab = _concept_vocab(handover)
    groups: dict[str, list[dict]] = {}
    for t in order:
        p = structure.place_task(t, cmap, design_hint=_design_hint(t, handover, req_aspect),
                                 concept_vocab=concept_vocab)
        groups.setdefault(p["path"], []).append(t)
    groups = _normalize_collisions(groups, log)      # one canonical location per concept (no module/package collision)
    log(f"placed {len(order)} tasks into {len(groups)} files")

    # PHASE 2 — GENERATION: write each file ONCE from ALL its tasks' merged specs (one opencode
    # pass per file — not N incremental extends, which corrupt large files). Base layer first.
    ordered = _order_files(list(groups), skeleton)
    results = []
    for i, path in enumerate(ordered):
        gtasks = groups[path]
        files, tries, diag = opencode.build_file_with_retry(
            path, gtasks, workspace, skeleton=skeleton, first=(i == 0),
            retries=retries, attach=attach)
        if not files:
            v = {"produced": False, "clean": False, "reason": "no output after retries",
                 "no_output_diag": {"exit": diag.get("exit"), "timeout": diag.get("timeout"),
                                    "dur": diag.get("dur"),
                                    "stderr": (diag.get("stderr") or "")[-500:],
                                    "stdout": (diag.get("stdout") or "")[-300:]}}
            outcome = "no_output"
            log(f"  [no_output] {path} exit={diag.get('exit')} timeout={diag.get('timeout')} "
                f"dur={diag.get('dur')}s :: {((diag.get('stderr') or diag.get('stdout') or '').strip()[-200:])}")
        else:
            v = verify.verify({"deliverable": path, "kind": gtasks[0].get("kind", "code"),
                               "title": "merged"}, files, workspace,
                              sanctioned=sanctioned, internal_tops=_internal_tops(workspace))
            outcome = "built" if v["clean"] else "failed"
        for t in gtasks:      # every task on the file inherits the file's outcome
            results.append({"task_id": t["task_id"], "title": t["title"], "kind": t["kind"],
                            "deliverable": t["deliverable"], "path": path,
                            "traces_to": t.get("traces_to", []), "outcome": outcome,
                            "tries": tries, **v})
        n = len(gtasks)
        log(f"  [{outcome:9}] {path}  ({n} task{'' if n == 1 else 's'}) ({v.get('reason','')})")

    # 2. ASSEMBLE + RUN-VERIFY + GUARDED REPAIR: ensure a manifest + entrypoint, then boot the app
    # and repair (guarded) until it runs. All in the same continued session as the build.
    log("assembling: entrypoint + foundational modules + manifest")
    entry = assemble.ensure_entrypoint(workspace, skeleton, log=log)
    foundations = assemble.ensure_internal_modules(workspace, skeleton, log=log)
    if sanctioned:
        # TOP-DOWN manifest, written LAST (after all opencode generation) so it is authoritative —
        # opencode otherwise appends whatever it imported (it is told to keep deps in the manifest),
        # which re-pollutes it. The design decides the deps; the manifest IS the sanctioned set (PyPI
        # names). Stdlib (sqlite3, uuid) is not a dependency and correctly never appears here.
        mani = skeleton.get("manifest") or "requirements.txt"
        deps = _manifest_deps(policy, skeleton)
        try:
            with open(os.path.join(workspace, mani), "w", encoding="utf-8") as f:
                f.write("\n".join(deps) + "\n")
            manifest = {"manifest": mani, "action": "written top-down from stack policy", "deps": deps}
            log(f"  [manifest ] {mani} <- stack policy (authoritative): {deps}")
        except OSError as e:
            manifest = {"manifest": mani, "action": f"failed: {e}"}
    else:
        manifest = assemble.ensure_manifest(workspace, skeleton, log=log)

    # Inspectable record of what the stack policy actually did this run (the job log is not retained).
    try:
        with open(os.path.join(workspace, ".build_policy.json"), "w", encoding="utf-8") as f:
            json.dump({"stack": skeleton.get("stack"), "run_cmd": skeleton.get("run_cmd"),
                       "frame_deps": _frame_deps(skeleton), "sanctioned": sanctioned,
                       "policy_persistence": policy.get("recommended_persistence"),
                       "manifest": manifest}, f, indent=1)
    except OSError:
        pass
    # INTEGRATED HEAL LOOP: detect ALL issues -> fix each (deterministic where mechanical, model
    # where judgment) guarded -> re-detect -> until clean or bounded. Subsumes export reconciliation,
    # missing-import resolution, and boot repair into one loop with generic detectors.
    log("integrated heal loop (detect -> fix -> re-detect)")
    run = heal.heal(workspace, skeleton, sanctioned=sanctioned,
                    internal_tops=_internal_tops(workspace), log=log)
    log(f"heal: runnable={run.get('runnable')} fixes={run.get('fixes_applied')} "
        f"reverts={len(run.get('reverts', []))}"
        + (f" final_error={run.get('final_error')}" if run.get("runnable") == "no" else ""))

    built = sum(1 for r in results if r["outcome"] == "built")
    failed = sum(1 for r in results if r["outcome"] == "failed")
    noout = sum(1 for r in results if r["outcome"] == "no_output")
    judged = built + failed
    report = {
        "workspace": workspace,
        "skeleton": skeleton,
        "source": plan.get("source", {}),
        "summary": {"total": len(results), "built": built, "failed": failed,
                    "no_output": noout,
                    "build_success_rate": round(built / judged, 3) if judged else None,
                    "files": len(_list_code_files(workspace)),
                    "runnable": run.get("runnable")},
        "assembly": {"manifest": manifest, "entrypoint": entry, "foundations": foundations,
                     "run_verify": run},
        "results": results,
    }

    # 3. RELEASE PACKAGE — the deploy manifest a Deployment Agent consumes; written into code/.
    code_files = _list_code_files(workspace)
    env = release.discover_env(workspace, code_files)
    rel = release.build_release(report, skeleton, code_files, plan.get("source"), env=env)
    try:
        with open(os.path.join(workspace, "release.json"), "w", encoding="utf-8") as f:
            json.dump(rel, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    report["release"] = rel
    log(f"release package: runnable={rel.get('runnable')} run='{rel.get('deploy_cmd')}' "
        f"port={rel.get('port')} files={len(code_files)}")
    return report


def build_plan_file(plan_path: str, workspace: str, **kw) -> dict:
    plan = json.load(open(plan_path))
    return build_plan(plan, workspace, **kw)
