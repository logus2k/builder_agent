"""Contract-driven scaffolding — the Builder IMPLEMENTS the Architect's code contract instead of
inventing structure. For each concept in the contract we deterministically write a module SKELETON:
its dependency imports (by the contract's exact names, from the contract's canonical module paths)
and a typed stub for each export (a class with its fields, a function with its signature). Structure
and the public interface are therefore correct BY CONSTRUCTION — collisions are impossible (one
canonical module per concept) and imports/exports line up (both sides read the same contract). The
model's job shrinks to filling bodies, not deciding names or layout.

The concept->path mapping is the ONE stack-specific step (the Architect's contract is stack-agnostic:
concepts, kinds, symbols). Here, for a Python/app layout: entity -> app/models/<key>.py, service ->
app/services/<key>.py. Other frames map differently; the contract is unchanged.
"""

from __future__ import annotations

import os


def _path_of(key: str, kind: str) -> str:
    sub = "models" if kind == "entity" else "services"
    return f"app/{sub}/{key}.py"


def module_paths(contract: dict) -> dict:
    """concept key -> its canonical module path (deterministic, one per concept)."""
    return {key: _path_of(key, v.get("kind", "service")) for key, v in contract.items()}


def _module_dotted(path: str) -> str:
    return path[:-3].replace("/", ".")


# ---- persistence seam ---------------------------------------------------------------------------
# The contract implies data-access (entities are stored/fetched) but names no layer, so model-filled
# service bodies invent `app.db.database` / repositories that don't exist and get stubbed. We provide
# a REAL, dependency-free in-memory repository the scaffold wires services to, so CRUD operations
# actually work by construction. Swap the backing store for sqlite/a driver without touching services.
_STORE_PY = '''"""Generic in-memory repository — real, working persistence for the scaffolded services.

Deterministic and dependency-free: a service calls repo("<name>").create/get/list/update/delete and
gets real data back, persisted for the process lifetime. This is the data-access seam the contract
implies; the backing dict can be swapped for sqlite or a driver without changing the service interface.
"""
from itertools import count
from threading import Lock


class Repo:
    def __init__(self):
        self._items = {}
        self._ids = count(1)
        self._lock = Lock()

    def _key(self, ident):
        try:
            return int(ident)
        except (TypeError, ValueError):
            return ident

    def create(self, data=None):
        with self._lock:
            i = next(self._ids)
        rec = {"id": i}
        if isinstance(data, dict):
            rec.update(data)
        elif data is not None:
            rec["value"] = data
        self._items[i] = rec
        return rec

    def get(self, ident):
        return self._items.get(self._key(ident))

    def list(self):
        return list(self._items.values())

    def update(self, ident, data=None):
        k = self._key(ident)
        if k in self._items:
            if isinstance(data, dict):
                self._items[k].update(data)
            elif data is not None:
                self._items[k]["value"] = data
        return self._items.get(k)

    def delete(self, ident):
        k = self._key(ident)
        return {"deleted": self._items.pop(k, None) is not None, "id": ident}


_REGISTRY = {}
_RLOCK = Lock()


def repo(name):
    """Return the process-wide singleton Repo for `name` (created on first use)."""
    with _RLOCK:
        return _REGISTRY.setdefault(name, Repo())
'''

_CREATE = ("create", "add", "register", "submit", "new", "insert", "upload", "save", "store",
           "post", "generate", "make", "build")
_GET = ("get", "fetch", "retrieve", "find", "show", "read", "lookup", "resolve")
_LIST = ("list", "browse", "all", "search", "query", "index", "view")
_UPDATE = ("update", "edit", "modify", "confirm", "cancel", "approve", "reject", "set", "change",
           "associate", "assign", "toggle", "switch", "activate", "deactivate", "publish")
_DELETE = ("delete", "remove", "discard", "clear", "unregister", "revoke")


def _verb(op_name: str) -> str | None:
    """Classify an operation into a CRUD verb by its leading token (deterministic, lexical)."""
    head = (op_name or "").split("_")[0].lower()
    for verbs, kind in ((_CREATE, "create"), (_GET, "get"), (_LIST, "list"),
                        (_UPDATE, "update"), (_DELETE, "delete")):
        if head in verbs:
            return kind
    return None


def _bare_type(t) -> str:
    """Strip container/optional wrappers to the base type name (`list[Menu]`/`Menu | None` -> `Menu`)."""
    if not isinstance(t, str):
        return ""
    return (t.replace("list[", "").replace("List[", "").replace("Optional[", "")
            .replace("]", "").replace(" | None", "").strip())


_NON_ENTITY_TYPES = {"", "void", "none", "bool", "boolean", "str", "string", "int", "integer",
                     "float", "dict", "any", "object", "list", "tuple"}


def _repo_name(sym: str, returns, key: str) -> str:
    """The store an operation reads/writes. Prefer the entity in its return type; when that isn't an
    entity (void/bool/…), fall back to the noun in the operation name (`delete_menu` -> `menu`), so
    create/get/update/delete on the same entity share one store. Deterministic."""
    t = _bare_type(returns).lower().replace(" ", "_")
    if t and t not in _NON_ENTITY_TYPES:
        return t
    parts = (sym or "").split("_")
    if len(parts) > 1:
        return "_".join(parts[1:]).lower()
    return key


def _crud_body(verb: str | None, params: list[str], repo_name: str) -> str | None:
    """Real body delegating to the repository, or None for a non-CRUD op (left as a stub)."""
    if not verb:
        return None
    r = f'repo("{repo_name}")'
    if verb == "create":
        return f"return {r}.create({params[0] if params else '{}'})"
    if verb == "get":
        return f"return {r}.get({params[0] if params else 'None'})"
    if verb == "list":
        return f"return {r}.list()"
    if verb == "update":
        ident = params[0] if params else "None"
        data = params[1] if len(params) > 1 else (params[0] if params else "{}")
        return f"return {r}.update({ident}, {data})"
    if verb == "delete":
        return f"return {r}.delete({params[0] if params else 'None'})"
    return None


_INDEX_HTML = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Application</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; margin: 0; background: #0e1116; color: #e6e6e6; }
  header { background: #161b22; padding: 1rem 1.5rem; border-bottom: 1px solid #30363d; }
  header h1 { margin: 0; font-size: 1.2rem; }
  header a { color: #58a6ff; font-size: .85rem; }
  main { max-width: 920px; margin: 1.5rem auto; padding: 0 1rem; }
  .grp { margin: 1.5rem 0 .5rem; font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; color: #8b949e; }
  .ep { border: 1px solid #30363d; border-radius: 8px; padding: .9rem 1rem; margin: .6rem 0; background: #161b22; }
  .ep h3 { margin: 0 0 .5rem; font-size: .95rem; font-family: ui-monospace, monospace; }
  .m { font-size: .7rem; padding: .1rem .4rem; border-radius: 4px; background: #238636; color: #fff; margin-right: .4rem; }
  input { margin: .2rem .3rem .2rem 0; padding: .35rem .5rem; background: #0d1117; color: #e6e6e6;
          border: 1px solid #30363d; border-radius: 5px; }
  button { padding: .4rem .9rem; cursor: pointer; background: #238636; color: #fff; border: 0; border-radius: 5px; }
  pre { background: #0d1117; padding: .5rem .7rem; border-radius: 5px; overflow: auto; margin: .5rem 0 0;
        font-size: .8rem; white-space: pre-wrap; word-break: break-all; }
</style>
</head>
<body>
<header><h1 id="title">Application</h1><a href="/docs">API docs →</a></header>
<main id="app">loading…</main>
<script>
async function load() {
  const spec = await (await fetch("/openapi.json")).json();
  document.getElementById("title").textContent = (spec.info && spec.info.title) || "Application";
  const app = document.getElementById("app"); app.innerHTML = "";
  const groups = {};
  for (const [path, item] of Object.entries(spec.paths)) {
    const g = path.split("/").filter(Boolean)[0] || "api";
    (groups[g] = groups[g] || []).push([path, item]);
  }
  for (const [g, eps] of Object.entries(groups)) {
    const h = document.createElement("div"); h.className = "grp"; h.textContent = g; app.appendChild(h);
    for (const [path, item] of eps) {
      for (const [method, op] of Object.entries(item)) {
        if (!["get", "post", "put", "delete"].includes(method)) continue;
        const params = (op.parameters || []).filter(p => p.in === "query");
        const div = document.createElement("div"); div.className = "ep";
        div.innerHTML = `<h3><span class="m">${method.toUpperCase()}</span>${path}</h3>`;
        const inputs = {};
        params.forEach(p => {
          const i = document.createElement("input"); i.placeholder = p.name; div.appendChild(i); inputs[p.name] = i;
        });
        const btn = document.createElement("button"); btn.textContent = "Call";
        const out = document.createElement("pre"); out.style.display = "none";
        btn.onclick = async () => {
          const q = Object.entries(inputs).filter(([k, i]) => i.value !== "")
            .map(([k, i]) => `${encodeURIComponent(k)}=${encodeURIComponent(i.value)}`).join("&");
          const url = path + (q ? `?${q}` : "");
          out.style.display = "block"; out.textContent = "…";
          try {
            const r = await fetch(url, { method: method.toUpperCase() });
            out.textContent = `HTTP ${r.status}\\n` + await r.text();
          } catch (e) { out.textContent = "ERROR " + e; }
        };
        div.appendChild(btn); div.appendChild(out); app.appendChild(div);
      }
    }
  }
}
load().catch(e => { document.getElementById("app").textContent = "Failed to load API: " + e; });
</script>
</body>
</html>
'''


def scaffold_frontend(workspace: str, log=print) -> str:
    """Write a generic, self-contained web UI driven by the app's own /openapi.json: a form per
    endpoint that calls the API and shows the response. Works for ANY backend the pipeline builds —
    the deliverable is a clickable application, not just API docs. Returns its path."""
    fe = os.path.join(workspace, "frontend")
    os.makedirs(fe, exist_ok=True)
    rel = "frontend/index.html"
    with open(os.path.join(workspace, rel), "w", encoding="utf-8") as f:
        f.write(_INDEX_HTML)
    log(f"  [scaffold] {rel} (generic openapi-driven web UI)")
    return rel


def scaffold_persistence(workspace: str, log=print) -> str:
    """Write the real in-memory repository module (once). Returns its module path."""
    pkg = os.path.join(workspace, "app", "repositories")
    os.makedirs(pkg, exist_ok=True)
    open(os.path.join(pkg, "__init__.py"), "a", encoding="utf-8").close()
    rel = "app/repositories/store.py"
    with open(os.path.join(workspace, rel), "w", encoding="utf-8") as f:
        f.write(_STORE_PY)
    log(f"  [scaffold] {rel} (generic in-memory repository — real data-access seam)")
    return rel


def scaffold(workspace: str, contract: dict, log=print, bodies: str = "working") -> dict:
    """Write a skeleton module for every contract concept. Returns what was written.

    `bodies` controls how function bodies are seeded:
      - "working"     — a repo()-delegating CRUD body (green by construction). Used as the finalize
                        FALLBACK to backfill any operation the model left unimplemented.
      - "placeholder" — `raise NotImplementedError(...)`, so the model SEES the operation needs real
                        logic and fills it (a working stub reads as 'already done' and gets skipped).
                        Used for the INITIAL scaffold the model builds on."""
    paths = module_paths(contract)
    written = []
    # ensure package __init__.py files exist along the way
    pkgs = set()
    for rel in paths.values():
        d = os.path.dirname(rel)
        while d and d != ".":
            pkgs.add(d)
            d = os.path.dirname(d)
    for pkg in sorted(pkgs):
        os.makedirs(os.path.join(workspace, pkg), exist_ok=True)
        init = os.path.join(workspace, pkg, "__init__.py")
        if not os.path.isfile(init):
            open(init, "w", encoding="utf-8").close()

    for key, v in contract.items():
        rel = paths[key]
        header = [f'"""{v.get("concept", key)} ({v.get("kind")}) — generated from the code contract."""']
        # dependency imports: each dependency's exported symbols by their exact contract names. These are
        # referenced only in type positions (field-type comments / annotations, or heal-filled hints), NEVER
        # in executable scaffold code, and concepts routinely reference each other (tenant <-> tenant_config),
        # so importing them eagerly at module load forms circular-import chains that crash the boot the moment
        # a model module is imported at runtime (e.g. a router that mounts an entity-hosted operation). Guard
        # them under TYPE_CHECKING: available to type-checkers and as heal context, inert at runtime.
        imports = []
        type_imports = []
        for dep in v.get("depends_on", []):
            dv = contract.get(dep)
            if not dv or dep == key:
                continue
            syms = [e["symbol"] for e in dv.get("exports", []) if e.get("symbol")]
            if syms:
                type_imports.append(f"    from {_module_dotted(paths[dep])} import {', '.join(sorted(set(syms)))}")
        # exports: a filled body per contract export — CRUD operations delegate to the real repository
        # (working by construction); classes keep their fields; non-CRUD ops stay stubs for the model.
        body_lines, needs_repo = [], False
        for e in v.get("exports", []):
            sym = e.get("symbol")
            if not sym:
                continue
            if e.get("kind") == "class":
                body_lines.append(f"class {sym}:")
                fields = [f for f in e.get("fields", []) if f.get("name")]
                if fields:
                    for f in fields:
                        body_lines.append(f"    {f['name']} = None  # contract field: {f.get('type','')}")
                else:
                    body_lines.append("    pass")
                body_lines.append("")
            elif e.get("kind") == "function":
                params = [p["name"] for p in e.get("inputs", []) if p.get("name")]
                body_lines.append(f"def {sym}({', '.join(params)}):")
                if bodies == "placeholder":
                    # An explicit not-yet-implemented body: the model recognizes it must supply the real
                    # logic (a working stub reads as done and is skipped). finalize backfills any left.
                    body_lines.append(
                        f'    raise NotImplementedError("{sym}: implement per the assigned task(s)")')
                    needs_repo = True                    # keep the seam imported and ready to use
                else:
                    verb = _verb(sym)
                    crud = _crud_body(verb, params, _repo_name(sym, e.get("returns"), key))
                    if crud:
                        body_lines.append(f"    {crud}")
                        needs_repo = True
                    else:
                        body_lines.append(f"    ...  # custom operation (no CRUD mapping) -> {e.get('returns')}")
                body_lines.append("")
        if needs_repo:
            imports.append("from app.repositories.store import repo")
        type_block = (["from typing import TYPE_CHECKING", "", "if TYPE_CHECKING:"]
                      + sorted(type_imports)) if type_imports else []
        lines = header + sorted(imports) + type_block + [""] + body_lines
        content = "\n".join(lines) + "\n"
        abspath = os.path.join(workspace, rel)
        os.makedirs(os.path.dirname(abspath), exist_ok=True)
        with open(abspath, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(rel)
        log(f"  [scaffold] {rel} exports={[e.get('symbol') for e in v.get('exports',[])]} "
            f"deps={v.get('depends_on')}")
    return {"modules": len(written), "paths": written}


def scaffold_api(workspace: str, contract: dict, log=print) -> dict:
    """Extend the scaffold to the DELIVERY layer so the WHOLE structure is coherent by construction,
    not just models/services: for each service, a router module that imports the service functions by
    their exact contract names and exposes an endpoint per operation; and the entrypoint (main.py)
    that imports every router and mounts it. Routers therefore stop inventing imports (the cause of
    the residual `missing_module` drift), and every service is reachable (routes wired by
    construction). FastAPI-specific — the one stack-aware step, kept small."""
    # Routability follows CONTENT, not the concept's `kind` label: any concept that exports at least
    # one FUNCTION owns operations and gets a router; the operations are exactly its function exports
    # (class exports are data models, never routed). The Architect sometimes co-locates operations on
    # an entity (e.g. `reservation` owns `submit_reservation`), so gating on kind=="service" silently
    # drops those endpoints. `kind` still drives file placement (module_paths), only not routing.
    def _ops(v):
        return [e for e in v.get("exports", []) if e.get("kind") == "function" and e.get("symbol")]
    routable = {k: v for k, v in contract.items() if _ops(v)}
    paths = module_paths(contract)
    os.makedirs(os.path.join(workspace, "app/api"), exist_ok=True)
    open(os.path.join(workspace, "app/api/__init__.py"), "a", encoding="utf-8").close()
    routers = []
    for key, v in routable.items():
        rel = f"app/api/{key}.py"
        ops = _ops(v)
        syms = [e["symbol"] for e in ops]
        lines = [f'"""{v.get("concept", key)} API router — generated from the code contract."""',
                 "from fastapi import APIRouter"]
        if syms:
            lines.append(f"from {_module_dotted(paths[key])} import {', '.join(sorted(set(syms)))}")
        lines += ["", f'router = APIRouter(prefix="/{key}", tags=["{key}"])', ""]
        for e in ops:
            op = e["symbol"]
            params = ", ".join(p["name"] for p in e.get("inputs", []) if p.get("name"))
            lines.append(f'@router.post("/{op}")')
            lines.append(f"def {op}_endpoint({params}):")
            lines.append(f"    return {op}({params})")
            lines.append("")
        with open(os.path.join(workspace, rel), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        routers.append(key)
        log(f"  [scaffold-api] {rel} mounts {len(v.get('exports', []))} operation(s)")

    # entrypoint: import every router and mount it -> all services reachable by construction; then
    # serve the generated frontend/ (the real UI pages) as static files at "/", so the API routes
    # (added first) still win and the pages are served for everything else.
    m = ['"""Application entrypoint — generated from the code contract."""',
         "import os", "from fastapi import FastAPI", "from fastapi.staticfiles import StaticFiles"]
    for key in routers:
        m.append(f"from app.api.{key} import router as {key}_router")
    m += ["", "app = FastAPI()"]
    for key in routers:
        m.append(f"app.include_router({key}_router)")
    m += ["",
          '_FE = os.path.join(os.path.dirname(__file__), "frontend")',
          "if os.path.isdir(_FE):",
          '    app.mount("/", StaticFiles(directory=_FE, html=True), name="site")',
          ""]
    with open(os.path.join(workspace, "main.py"), "w", encoding="utf-8") as f:
        f.write("\n".join(m) + "\n")
    log(f"  [scaffold-api] main.py mounts {len(routers)} router(s) + serves frontend/ at /")
    return {"routers": len(routers), "entrypoint": "main.py"}
