"""Build a planner_agent plan.json: execute each feasible task in dependency order,
verify deterministically, and report. Local-only (opencode + Gemma + code), no Claude.

A task's outcome is one of:
  - built   : deliverable produced, parses, no stub fingerprints (acceptance met)
  - failed  : deliverable produced but doesn't parse or is a stub (quality issue)
  - no_output: opencode produced nothing after retries (builder flakiness)
Prerequisites build before dependents (shared workspace) so a dependent can use them.
"""

from __future__ import annotations

import ast
import json
import os
import urllib.request

from . import (assemble, context, contract_scaffold, frontend, frontend_gate, heal, opencode, release,
               repair, structure, verify)

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


def _finalize_contract_app(workspace: str, contract: dict, skeleton: dict, log=print) -> dict:
    """Guarantee the acceptance-green STRUCTURE regardless of what the model produced. The model
    reliably degrades scaffolds (drops exports, un-mounts routers, injects an ORM, imports files it
    never created); rather than chase that, we re-assert the deterministic contract structure as the
    final step and drop the drift:

      1. re-assert routers + entrypoint (pure wiring; endpoints delegate to the services);
      2. prune every non-contract .py (the task-layer drift that causes the residual);
      3. re-scaffold any contract module the model left broken, missing an export, or ORM-tainted;
      4. write a clean, ORM-free manifest.

    Result: models + services + routers + main are coherent, import-clean, ORM-free, and boot — the
    proven-green scaffold — keeping the model's bodies only where they survived intact."""
    import ast
    persist = contract_scaffold.scaffold_persistence(workspace, log=log)  # (0) real data-access seam
    contract_scaffold.scaffold_api(workspace, contract, log=log)          # (1) routers + main
    paths = contract_scaffold.module_paths(contract)
    keep = set(paths.values()) | {f"app/api/{k}.py" for k, v in contract.items()
                                  if v.get("kind") == "service"} | {"main.py", persist}
    pruned = 0
    for root, dirs, files in os.walk(workspace, topdown=False):          # (2) prune drift
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if not f.endswith(".py"):
                continue
            abspath = os.path.join(root, f)
            # __init__.py: reset to an EMPTY package marker. The model writes stale package
            # re-exports into these (e.g. `from .menu_management import router` for a module that
            # no longer exists) and they break the boot; the scaffold imports by full module path,
            # so empty markers are all that's needed. Never prune them (packages must stay importable).
            if f == "__init__.py":
                try:
                    if os.path.getsize(abspath) > 0:
                        open(abspath, "w", encoding="utf-8").close()
                except OSError:
                    pass
                continue
            rel = os.path.relpath(abspath, workspace)
            if rel not in keep:
                try:
                    os.remove(abspath); pruned += 1
                except OSError:
                    pass
    # (3) re-scaffold any contract MODULE the model left broken: unparseable, ORM-tainted, missing a
    # contract export, OR importing something that doesn't exist. The last case is the common one —
    # the model keeps the right function names (exports present) but bolts on imports to modules it
    # never created (`app.core.config`, `app.db.database`, ...), so the module passes a shape check
    # yet fails at import time. Re-scaffolding replaces the body with a stub that imports ONLY its
    # contract dependencies. Driven by the detector and looped to a fixpoint so a re-scaffold that
    # clears one module can't leave a dependent broken.
    file_to_key = {paths[k]: k for k in contract}

    def _shape_bad(key, v):
        p = os.path.join(workspace, paths[key])
        if not os.path.isfile(p):
            return True
        try:
            src = open(p, encoding="utf-8").read()
            have = assemble._defined_names(ast.parse(src))
        except (SyntaxError, OSError, ValueError):
            return True
        if "sqlalchemy" in src or "declarative_base" in src:
            return True
        return any(e.get("symbol") and e["symbol"] not in have for e in v.get("exports", []))

    rescaffolded = 0
    # Regenerate ALL service bodies deterministically (working CRUD delegating to the repository, or a
    # stub for a genuinely custom op) — model service bodies are the unreliable part and this makes the
    # endpoints actually WORK. Entities keep their model-filled bodies unless the model broke them.
    bad = {k for k, v in contract.items() if v.get("kind") == "service" or _shape_bad(k, v)}
    for _ in range(6):                      # bounded fixpoint
        # add contract modules the detector flags (unresolved imports / missing exports)
        for issue in heal.detect(workspace, py_exe=None, skeleton=skeleton, sanctioned=None):
            k = file_to_key.get(issue.get("file"))
            if k:
                bad.add(k)
        if not bad:
            break
        for k in bad:
            contract_scaffold.scaffold(workspace, {k: contract[k]}, log=lambda m: None)
            rescaffolded += 1
        bad = set()
    contract_scaffold.scaffold_api(workspace, contract, log=lambda m: None)   # routers depend on services
    # (4) clean, ORM-free manifest.
    mani = skeleton.get("manifest") or "requirements.txt"
    deps = ["fastapi", "pydantic", "pydantic-settings", "python-multipart", "starlette", "uvicorn"]
    try:
        open(os.path.join(workspace, mani), "w", encoding="utf-8").write("\n".join(deps) + "\n")
    except OSError:
        pass
    log(f"finalize: re-asserted contract structure (pruned {pruned} drift, re-scaffolded "
        f"{rescaffolded} module(s), clean manifest)")
    return {"pruned": pruned, "rescaffolded": rescaffolded, "manifest": deps}


def _enforce_contract_exports(workspace: str, contract: dict, log=print) -> int:
    """Builder CONTRACT-CONFORMANCE review: after generation, every contract module must still export
    its contract symbols. If the model dropped/renamed one while filling bodies, re-add a stub so the
    interface the rest of the app imports stays intact (heal then fills it). Deterministic."""
    paths = contract_scaffold.module_paths(contract)
    fixed = 0
    for key, v in contract.items():
        rel = paths[key]
        abspath = os.path.join(workspace, rel)
        if not os.path.isfile(abspath):
            contract_scaffold.scaffold(workspace, {key: v}, log=lambda m: None)
            fixed += 1
            log(f"  [contract] re-scaffolded missing module {rel}")
            continue
        try:
            have = assemble._defined_names(ast.parse(open(abspath, encoding="utf-8").read()))
        except (SyntaxError, ValueError):
            contract_scaffold.scaffold(workspace, {key: v}, log=lambda m: None)  # model broke it -> restore scaffold
            fixed += 1
            log(f"  [contract] re-scaffolded unparseable module {rel}")
            continue
        except OSError:
            continue
        missing = [e for e in v.get("exports", []) if e.get("symbol") and e["symbol"] not in have]
        if not missing:
            continue
        try:
            with open(abspath, "a", encoding="utf-8") as f:
                f.write("\n\n# --- contract exports restored (must not be dropped) ---\n")
                for e in missing:
                    if e.get("kind") == "class":
                        f.write(f"class {e['symbol']}:\n    pass\n\n")
                    else:
                        params = ", ".join(p["name"] for p in e.get("inputs", []) if p.get("name"))
                        f.write(f"def {e['symbol']}({params}):\n    ...\n\n")
            fixed += 1
            log(f"  [contract] restored dropped exports in {rel}: {[e['symbol'] for e in missing]}")
        except OSError:
            pass
    return fixed


def _seed_contract_modules(cmap: dict, contract: dict, log=print) -> None:
    """Register the scaffolded contract modules in the placement map so the tasks that realize each
    concept EXTEND the scaffold (filling bodies) instead of a model-invented rival file. Uses the
    same (concept, layer) registry the placer already consults, so a same-concept task in that layer
    routes to the contract module."""
    paths = contract_scaffold.module_paths(contract)
    registry = cmap.setdefault("concepts", {})
    for key, v in contract.items():
        path = paths[key]
        layer = "/".join(path.split("/")[:-1])
        registry.setdefault(f"{key}::{layer}", path)
        if not any(f["path"] == path for f in cmap["files"]):
            cmap["files"].append({"path": path, "purpose": f"contract {v.get('kind')} {v.get('concept')}",
                                  "concept": key, "task_ids": []})
        if v.get("kind") == "service":     # its scaffolded router — route API tasks here to FILL it
            rpath = f"app/api/{key}.py"
            registry.setdefault(f"{key}::app/api", rpath)
            if not any(f["path"] == rpath for f in cmap["files"]):
                cmap["files"].append({"path": rpath, "purpose": f"contract router {v.get('concept')}",
                                      "concept": key, "task_ids": []})


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

    # CONTRACT-DRIVEN SCAFFOLDING: the Architect's code_contract defines the modules (entities +
    # services) with exact exports + dependencies. Scaffold them deterministically — coherent by
    # construction (verified: collisions impossible, imports/exports line up) — and register them so
    # the tasks realizing each concept FILL the scaffold instead of inventing a rival file.
    contract = (handover or {}).get("code_contract") or {}
    if contract:
        contract_scaffold.scaffold_persistence(workspace, log=log)          # real data-access seam
        scaf = contract_scaffold.scaffold(workspace, contract, log=log)
        api = contract_scaffold.scaffold_api(workspace, contract, log=log)   # routers + entrypoint
        _seed_contract_modules(cmap, contract, log)
        log(f"contract: scaffolded {scaf['modules']} modules + {api['routers']} routers + main "
            f"(full structure coherent by construction) from {len(contract)} concepts")

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
    # CONTRACT CONFORMANCE (P4b): body-fill must not have dropped any contract export.
    if contract:
        n = _enforce_contract_exports(workspace, contract, log)
        log(f"contract-conformance: {'restored ' + str(n) + ' module(s)' if n else 'all exports intact'}")

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
    # DETERMINISTIC SWEEP ONLY (max_rounds=0): mechanical import/alias/collision fixes to a fixpoint,
    # NO model-repair rounds. The model rounds used to create a missing module per issue, and the
    # created module imported yet more nonexistent modules -> an unbounded foundation cascade (~50min
    # for this project). It is redundant now: the finalize step below re-scaffolds any broken contract
    # module and prunes drift deterministically, so correctness no longer depends on model repair.
    log("deterministic heal sweep (no model rounds; finalize guarantees the contract structure)")
    run = heal.heal(workspace, skeleton, sanctioned=sanctioned,
                    internal_tops=_internal_tops(workspace), max_rounds=0, log=log)
    log(f"heal: runnable={run.get('runnable')} fixes={run.get('fixes_applied')} "
        f"reverts={len(run.get('reverts', []))}"
        + (f" final_error={run.get('final_error')}" if run.get("runnable") == "no" else ""))

    # FINAL contract conformance: the model reliably BREAKS the scaffold (drops exports, un-mounts
    # routers, injects an ORM, imports files it never wrote). Rather than chase that, re-assert the
    # deterministic contract structure as the last step and drop the drift — guaranteeing a coherent,
    # booting, route-complete, ORM-free app regardless of what the model did. Model bodies survive
    # only where they stayed intact (parse + keep exports + no ORM + imports resolve post-prune).
    finalize = _finalize_contract_app(workspace, contract, skeleton, log) if contract else None

    # FRONTEND STAGE — after the backend, build the UI from the frontend requirements + the real
    # endpoints the backend now exposes (derived from the contract). Real pages (menu/admin/
    # reservation/contact), model-generated, not the API console.
    frontend_report = None
    if contract:
        try:
            reqs_path = os.path.join(os.path.dirname(workspace.rstrip("/")), "requirements", "package.json")
            reqs = json.load(open(reqs_path)).get("requirements", []) if os.path.isfile(reqs_path) else []
            log(f"frontend stage: {len(reqs)} requirement(s) available; generating UI from the API")
            gate = frontend_gate.make_validator(workspace, skeleton, log)   # browser gate (or None)
            try:
                frontend_report = frontend.generate(workspace, handover or {}, reqs, log=log, validate=gate)
            finally:
                frontend_gate.shutdown(gate)
        except Exception as ex:                          # never let UI generation break the build
            log(f"frontend stage error ({type(ex).__name__}: {ex})")

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
                    "runnable": run.get("runnable"), "finalize": finalize,
                    "frontend": frontend_report},
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
