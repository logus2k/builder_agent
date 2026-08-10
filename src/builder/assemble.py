"""Assemble the built files into a runnable project and verify — honestly — that it runs.

Per-task building yields real files at real paths (structure.py + opencode). But an application
also needs a dependency manifest and an entrypoint that wires the pieces, and the honest question
is not "does each file parse?" but "does the thing actually start?". This stage:

  1. ensure_manifest()  — if the frame's manifest is missing, have opencode generate it from the
     code that was actually produced (real imports, not a guess).
  2. ensure_entrypoint() — if the entrypoint is missing, have opencode create it wiring the app.
  3. run_verify()       — bounded, language-aware smoke: compile everything, install deps into a
     throwaway venv, import the entrypoint. Reports what it OBSERVED — never claims "runs" on
     anything it did not actually execute.

Python-focused (this pipeline's apps are Python/FastAPI); other languages get the structural +
syntax checks and an explicit "boot smoke not implemented for <lang>" note rather than a false pass.
"""

from __future__ import annotations

import ast
import json
import math
import os
import subprocess
import sys
import urllib.request
from collections import defaultdict

from . import opencode

#: House reranker (sigmoid-scaled) — the approved tool for "are these two the same?" (never string
#: matching). Used to decide, for a demanded-but-undefined symbol, whether the module already has an
#: equivalent under a different name (alias) or the symbol is genuinely absent (implement).
EMBEDDINGS_URL = os.environ.get("EMBEDDINGS_URL", "http://localhost:8601").rstrip("/")
RERANK_MODEL = os.environ.get("RERANK_MODEL_NAME", "bge-reranker")
#: Alias only on a VERY strong match. True name-drift (word-form differences: `X_config` vs
#: `X_configuration`, `get_db` vs `get_db_session`) scores ~0.92-0.999; a verb mismatch that shares
#: context (`update_X` vs `get_X`) scores ~0.88 and must NOT alias — it is a genuinely missing
#: function. Erring toward IMPLEMENT on uncertainty is safe (at worst a mild duplication); a wrong
#: alias silently binds the wrong behaviour. 0.9 cleanly separates the two on measured data.
_ALIAS_THRESHOLD = 0.9


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x)) if x >= 0 else math.exp(x) / (1.0 + math.exp(x))


def _rerank(query: str, documents: list[str], timeout: float = 30.0) -> list[float]:
    """0-1 relevance per document, input order. Empty on any failure — the caller then treats every
    missing symbol as genuinely-missing (implement) rather than risk a wrong alias."""
    if not documents:
        return []
    try:
        body = json.dumps({"model": RERANK_MODEL, "query": query, "documents": documents}).encode()
        req = urllib.request.Request(f"{EMBEDDINGS_URL}/v1/rerank", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except Exception:  # noqa: BLE001 — reranker down -> no aliasing, still implement
        return []
    results = data.get("results") or data.get("data") or []
    scored = [0.0] * len(documents)
    for it in results:
        try:
            i = int(it.get("index", 0))
            raw = it.get("relevance_score", it.get("score", 0.0))
            if 0 <= i < len(scored):
                scored[i] = _sigmoid(float(raw))
        except (TypeError, ValueError):
            continue
    return scored


def ensure_internal_modules(workspace: str, skeleton: dict, log=print) -> dict:
    """Create INTERNAL modules that the code imports but that no task produced (e.g.
    `app.core.config` — every file reads `settings` from it, but nothing built it). Generic: parse
    all files, find `from <pkg>… import …` / `import <pkg>…` where <pkg> is a workspace package but
    the module has no file, and generate each missing module (in one pass) from the symbols it must
    provide. This is the foundational-module analogue of ensure_manifest/ensure_entrypoint."""
    py = _py_files(workspace)
    top = {e for e in os.listdir(workspace)
           if os.path.isdir(os.path.join(workspace, e)) and not e.startswith(".")
           and e not in ("__pycache__", "tests")}
    top |= {f[:-3] for f in os.listdir(workspace) if f.endswith(".py")}

    needed: dict[str, set] = {}
    for rel in py:
        try:
            tree = ast.parse(open(os.path.join(workspace, rel), encoding="utf-8").read())
        except (SyntaxError, OSError, ValueError):
            continue
        pkg_parts = os.path.dirname(rel).split("/") if os.path.dirname(rel) else []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module and node.module.split(".")[0] in top:
                    needed.setdefault(node.module, set()).update(a.name for a in node.names)
                elif node.level > 0:
                    # relative import (from .x import y): resolve to an absolute module using the
                    # importing file's package + the level (was previously skipped -> missing module).
                    base = pkg_parts[:len(pkg_parts) - (node.level - 1)]
                    parts = [p for p in base if p] + (node.module.split(".") if node.module else [])
                    if parts and parts[0] in top:
                        needed.setdefault(".".join(parts), set()).update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in top:
                        needed.setdefault(a.name, set())

    created = []
    for mod, syms in sorted(needed.items()):
        base = mod.replace(".", "/")
        if os.path.isfile(os.path.join(workspace, base + ".py")) \
                or os.path.isfile(os.path.join(workspace, base, "__init__.py")):
            continue
        path = base + ".py"
        symlist = ", ".join(sorted(s for s in syms if s and s != "*")) or "the symbols it should provide"
        task = {"title": f"missing internal module {mod}", "instructions":
                (f"Create '{path}' — the module '{mod}' the app imports but which is missing. It "
                 f"MUST define: {symlist}. Implement it correctly for the app: e.g. a config/"
                 f"settings module exposes a `settings` object (pydantic Settings) with the "
                 f"project's configuration; a base/db module exposes the shared engine/Base/session.")}
        files, _ = opencode.build_task(task, workspace, path, action="create", skeleton=skeleton)
        got = _exists(workspace, path)
        created.append({"module": mod, "created": got})
        log(f"  [foundation] {mod} -> {path} ({'created' if got else 'MISSING'})")
    return {"missing_found": len(created), "created": created}


def _exists(workdir: str, rel: str) -> bool:
    return bool(rel) and os.path.isfile(os.path.join(workdir, rel))


def _module_name(rel: str) -> str:
    m = rel[:-3].replace("/", ".")
    return m[:-9] if m.endswith(".__init__") else m


def _defined_names(tree: ast.AST) -> set:
    """The names in a module's public NAMESPACE — what `from mod import X` can resolve: top-level
    functions, classes, assignments, AND imported names (a re-export IS part of the namespace, e.g.
    a package `__init__` that does `from .impl import foo` exposes `foo`). Excludes star imports,
    which are not statically resolvable."""
    names = set()
    for n in getattr(tree, "body", []):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(n, ast.ImportFrom):
            names.update(a.asname or a.name for a in n.names if a.name != "*")
        elif isinstance(n, ast.Import):
            names.update((a.asname or a.name).split(".")[0] for a in n.names)
    return names


def _usage_snippets(workspace: str, pyfiles: list[str], mod: str, symbols: list[str],
                    cap: int = 12) -> str:
    """Lines from OTHER files that use these symbols — the contract the implementation must match
    (call signatures, attribute access). Plain token containment, bounded. No regex."""
    out, symset = [], set(symbols)
    for rel in pyfiles:
        if _module_name(rel) == mod:
            continue
        try:
            lines = open(os.path.join(workspace, rel), encoding="utf-8").read().splitlines()
        except OSError:
            continue
        if f"from {mod} import" not in "\n".join(lines):
            continue
        for ln in lines:
            s = ln.strip()
            if not s or s.startswith(("from ", "import ")) or len(s) > 180:
                continue
            if any((sym + "(") in ln or (sym + ".") in ln or (sym + ")") in ln for sym in symset):
                if s not in out:
                    out.append(s)
            if len(out) >= cap:
                return "\n".join(f"    {x}" for x in out)
    return "\n".join(f"    {x}" for x in out)


def reconcile_exports(workspace: str, skeleton: dict, log=print) -> dict:
    """Complete EXISTING shared modules so they satisfy their import contract. `ensure_internal_modules`
    creates MISSING modules; this closes the symmetric gap that breaks the boot far more often — a
    module that EXISTS but lacks symbols other files import from it (name-drift like `get_db` vs
    `get_db_session`, or a genuinely absent `ItemCreate`). Generic + graph-driven: the demanded
    export set comes from the actual import graph, and the reranker decides alias-vs-implement per
    symbol (never string matching). One opencode pass per gapped module."""
    py = _py_files(workspace)
    tops = {e for e in os.listdir(workspace)
            if os.path.isdir(os.path.join(workspace, e)) and not e.startswith(".")
            and e not in ("__pycache__", "tests")}
    tops |= {f[:-3] for f in os.listdir(workspace) if f.endswith(".py")}

    demanded: dict[str, set] = defaultdict(set)
    defines: dict[str, set] = {}
    for rel in py:
        try:
            tree = ast.parse(open(os.path.join(workspace, rel), encoding="utf-8").read())
        except (SyntaxError, OSError, ValueError):
            continue
        defines[_module_name(rel)] = _defined_names(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                    and node.module.split(".")[0] in tops):
                for a in node.names:
                    if a.name != "*":
                        demanded[node.module].add(a.name)

    reconciled = []
    for mod in sorted(demanded):
        base = mod.replace(".", "/")
        path = (base + ".py" if os.path.isfile(os.path.join(workspace, base + ".py"))
                else base + "/__init__.py" if os.path.isfile(os.path.join(workspace, base, "__init__.py"))
                else None)
        if not path:                       # missing MODULE -> ensure_internal_modules' job, not this
            continue
        have = defines.get(mod, set())
        missing = sorted(demanded[mod] - have - {"*"})
        if not missing:
            continue
        havelist = sorted(have)
        aliases, to_impl = {}, []
        for s in missing:
            scores = _rerank(s, havelist) if havelist else []
            if scores:
                bi = max(range(len(scores)), key=lambda i: scores[i])
                if scores[bi] >= _ALIAS_THRESHOLD:
                    aliases[s] = havelist[bi]
                    continue
            to_impl.append(s)

        abspath = os.path.join(workspace, path)

        # 1. ALIASES — apply DETERMINISTICALLY in code (append `name = existing`). Trivial one-liners
        # that never need the executor; doing them by hand means opencode can't drop existing defs.
        if aliases:
            try:
                content = open(abspath, encoding="utf-8").read()
                add = [f"{s} = {tgt}" for s, tgt in aliases.items()
                       if f"\n{s} " not in content and f"\n{s}=" not in content
                       and not content.startswith(f"{s} ") and not content.startswith(f"{s}=")]
                if add:
                    with open(abspath, "a", encoding="utf-8") as f:
                        f.write("\n\n# --- reconciled export aliases ---\n" + "\n".join(add) + "\n")
            except OSError:
                pass

        # 2. IMPLEMENTATIONS — opencode, append-only. Revert the edit if it DROPS an existing symbol
        # or breaks parsing (the executor sometimes rewrites and loses content); a dropped export is
        # a regression, so we keep the aliases and leave the impl to the run-verify repair loop.
        if to_impl:
            usage = _usage_snippets(workspace, py, mod, to_impl)
            try:
                pre = open(abspath, encoding="utf-8").read()
                before = _defined_names(ast.parse(pre))
            except (SyntaxError, OSError, ValueError):
                pre, before = None, set()
            # Work WITH opencode's tendency to rewrite the whole file: give it the FULL contract —
            # every existing name it MUST keep, plus the missing names to add — and the current
            # content. Then verify the union survived; revert if any existing name was still lost
            # (keeps the aliases, leaves the impl to the run-verify repair loop).
            keep = ", ".join(sorted(before)) or "(none)"
            instr = (
                f"Rewrite the module file '{path}' so it defines ALL of the required top-level names, "
                f"losing NONE. It MUST keep every one of these existing names, preserving their "
                f"current behaviour: {keep}. It MUST additionally define these names that other files "
                f"import from '{mod}' but which are missing: {', '.join(to_impl)} — implement each for "
                f"real (function or class as the usage implies), consistent with the module's style. "
                f"Follow AGENTS.md. No placeholders."
                + (f"\nHow other files use the missing names (match their signatures/attributes):\n{usage}"
                   if usage else "")
                + (f"\n\nCurrent content of '{path}' to preserve and extend:\n---\n{pre[:6000]}\n---"
                   if pre else ""))
            task = {"title": f"complete exports of {mod}", "instructions": instr}
            opencode.build_task(task, workspace, path, action="edit", skeleton=skeleton)
            try:
                after = _defined_names(ast.parse(open(abspath, encoding="utf-8").read()))
            except (SyntaxError, OSError, ValueError):
                after = set()
            if pre is not None and (before - after):      # still dropped an existing symbol / broke parse
                open(abspath, "w", encoding="utf-8").write(pre)
                log(f"  [reconcile] {mod}: reverted impl edit (dropped {sorted(before - after)})")

        try:
            now = _defined_names(ast.parse(open(abspath, encoding="utf-8").read()))
        except (SyntaxError, OSError, ValueError):
            now = set()
        closed = [s for s in missing if s in now]
        reconciled.append({"module": mod, "missing": missing, "aliased": list(aliases),
                           "implemented": to_impl, "closed": closed})
        log(f"  [reconcile] {mod}: {len(closed)}/{len(missing)} names now defined "
            f"(alias={list(aliases)} impl={to_impl})")
    return {"modules_reconciled": len(reconciled), "detail": reconciled}


import builtins as _builtins

#: Names that are always in scope — never treated as "missing" (builtins + module dunders + the
#: conventional self/cls). Over-approximating what is bound keeps us from adding a bogus import.
_ALWAYS_BOUND = set(dir(_builtins)) | {
    "self", "cls", "__name__", "__file__", "__doc__", "__package__", "__loader__", "__spec__",
    "__builtins__", "__annotations__", "__dict__", "__class__", "__module__", "__qualname__"}

#: Standard-library top-level module names — an undefined name matching one of these (used as
#: `sqlite3.Connection`, `json.dumps`, ...) is a missing `import <mod>`, not an internal symbol.
_STDLIB_MODS = getattr(sys, "stdlib_module_names", frozenset())


def _bound_and_used(tree: ast.AST) -> tuple[set, set]:
    """(names bound ANYWHERE in the module, names used in Load context). Bound is intentionally
    over-approximated across all scopes so we never invent an import for something that is actually
    defined locally somewhere — we'd rather miss a real missing-import than add a wrong one."""
    bound, used = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Name):
            (bound if isinstance(node.ctx, ast.Store) else used).add(node.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    return bound, used


def _insert_imports(src: str, tree: ast.AST, imports: list[str]) -> str:
    """Insert import lines after the module docstring + any `from __future__` imports (which must
    stay first), leaving the rest of the file byte-for-byte unchanged."""
    lines = src.splitlines(keepends=True)
    at, body, i = 0, list(getattr(tree, "body", [])), 0
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        at, i = body[0].end_lineno, 1
    while i < len(body) and isinstance(body[i], ast.ImportFrom) and body[i].module == "__future__":
        at, i = body[i].end_lineno, i + 1
    block = "".join(imp + "\n" for imp in imports)
    return "".join(lines[:at]) + block + "".join(lines[at:])


def resolve_missing_imports(workspace: str, skeleton: dict, log=print) -> dict:
    """Add the missing IMPORT for a name a file USES but neither defines nor imports, when that name
    is defined at module level somewhere else in the project (the NameError class — e.g. a FastAPI
    dependency `get_menu_service` used in one router but defined in another). Deterministic: adding
    an import statement can't drop code, so no executor and no reranker — the name either resolves to
    an internal module or it doesn't. Complements reconcile_exports (imported-but-undefined)."""
    py = _py_files(workspace)
    index: dict[str, set] = defaultdict(set)          # symbol -> modules defining it (top level)
    pop: dict[tuple, int] = defaultdict(int)          # (symbol, module) -> #files importing it thus
    parsed = {}
    for rel in py:
        try:
            tree = ast.parse(open(os.path.join(workspace, rel), encoding="utf-8").read())
        except (SyntaxError, OSError, ValueError):
            continue
        parsed[rel] = tree
        for n in _defined_names(tree):
            index[n].add(_module_name(rel))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                for a in node.names:
                    pop[(a.name, node.module)] += 1

    fixed = []
    for rel, tree in parsed.items():
        m = _module_name(rel)
        bound, used = _bound_and_used(tree)
        undefined = used - bound - _ALWAYS_BOUND
        internal: dict[str, str] = {}
        stdlib: list[str] = []
        for name in sorted(undefined):
            mods = [mm for mm in index.get(name, ()) if mm != m]
            if mods:
                # Prefer the module others already import this name from; tie-break shortest path.
                internal[name] = max(mods, key=lambda mm: (pop.get((name, mm), 0), -len(mm)))
            elif name in _STDLIB_MODS:
                stdlib.append(name)                   # e.g. `sqlite3.Connection` -> `import sqlite3`
        if not internal and not stdlib:
            continue
        bymod: dict[str, list] = defaultdict(list)
        for name, mod in internal.items():
            bymod[mod].append(name)
        new_imports = [f"import {n}" for n in sorted(stdlib)] + \
                      [f"from {mod} import {', '.join(sorted(names))}" for mod, names in sorted(bymod.items())]
        try:
            src = open(os.path.join(workspace, rel), encoding="utf-8").read()
            newsrc = _insert_imports(src, tree, new_imports)
            ast.parse(newsrc)                         # must still parse (adding imports is safe, but verify)
        except (SyntaxError, OSError, ValueError):
            continue
        open(os.path.join(workspace, rel), "w", encoding="utf-8").write(newsrc)
        fixed.append({"file": rel, "added": new_imports})
        log(f"  [missing-import] {rel}: {new_imports}")
    return {"files_fixed": len(fixed), "detail": fixed}


def ensure_manifest(workdir: str, skeleton: dict, log=print) -> dict:
    manifest = skeleton.get("manifest") or "requirements.txt"
    if _exists(workdir, manifest):
        return {"manifest": manifest, "action": "present"}
    task = {"title": f"dependency manifest ({manifest})", "instructions":
            (f"Scan every source file already in this project and create '{manifest}' listing "
             f"exactly the THIRD-PARTY packages they import (no standard-library modules, no app "
             f"modules). Pin nothing unless a version is required.")}
    files, _ = opencode.build_task(task, workdir, manifest, action="create", skeleton=skeleton)
    got = _exists(workdir, manifest)
    log(f"  [manifest ] {manifest} ({'created' if got else 'MISSING'})")
    return {"manifest": manifest, "action": "created" if got else "failed"}


def ensure_entrypoint(workdir: str, skeleton: dict, log=print) -> dict:
    entry = skeleton.get("entrypoint")
    if not entry or _exists(workdir, entry):
        return {"entrypoint": entry, "action": "present" if entry else "none"}
    task = {"title": f"application entrypoint ({entry})", "instructions":
            (f"Create '{entry}': the entrypoint that wires this project's modules into a runnable "
             f"{skeleton.get('stack','app')} application. Import the existing modules (routers, "
             f"models, services already in the tree) and expose the app object / main() so that "
             f"`{skeleton.get('run_cmd','')}` starts it. Real wiring, no placeholders.")}
    files, _ = opencode.build_task(task, workdir, entry, action="create", skeleton=skeleton)
    got = _exists(workdir, entry)
    log(f"  [entry    ] {entry} ({'created' if got else 'MISSING'})")
    return {"entrypoint": entry, "action": "created" if got else "failed"}


def _py_files(workdir: str) -> list[str]:
    out = []
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "venv", ".venv")]
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(root, f), workdir))
    return sorted(out)


def run_verify(workdir: str, skeleton: dict, timeout: float = 420, log=print) -> dict:
    """Bounded, honest run-smoke. Reports only what it actually executed."""
    lang = (skeleton.get("language") or "").lower()
    if lang and lang != "python":
        return {"language": lang, "runnable": "unknown",
                "note": f"boot smoke not implemented for {lang}; structure + per-file checks only"}

    pyfiles = _py_files(workdir)
    # 1. compile everything
    syntax_ok, syntax_fail = 0, []
    import py_compile
    for rel in pyfiles:
        try:
            py_compile.compile(os.path.join(workdir, rel), doraise=True)
            syntax_ok += 1
        except py_compile.PyCompileError as e:
            syntax_fail.append({"file": rel, "error": str(e)[:120]})

    result = {"language": "python", "py_files": len(pyfiles), "syntax_ok": syntax_ok,
              "syntax_fail": syntax_fail}

    manifest = skeleton.get("manifest") or "requirements.txt"
    mpath = os.path.join(workdir, manifest)
    if not os.path.isfile(mpath):
        result.update({"deps_installed": False, "boot": "skipped (no manifest)",
                       "runnable": "no" if syntax_fail else "unknown"})
        return result

    # 2. install deps into a throwaway venv
    venv = os.path.join(workdir, ".venv_smoke")
    try:
        subprocess.run([sys.executable, "-m", "venv", venv], capture_output=True, timeout=120)
        pip = os.path.join(venv, "bin", "pip")
        r = subprocess.run([pip, "install", "-q", "-r", mpath],
                           capture_output=True, text=True, timeout=timeout)
        deps_ok = r.returncode == 0
        result["deps_installed"] = deps_ok
        if not deps_ok:
            result["deps_error"] = (r.stderr or "")[-400:]
    except (subprocess.TimeoutExpired, OSError) as e:
        result.update({"deps_installed": False, "deps_error": f"{type(e).__name__}: {e}"})
        deps_ok = False

    # 3. import the entrypoint's module in the venv (bounded boot smoke)
    entry = skeleton.get("entrypoint")
    if deps_ok and entry and entry.endswith(".py"):
        mod = entry[:-3].replace("/", ".")
        py = os.path.join(venv, "bin", "python")
        try:
            r = subprocess.run([py, "-c", f"import importlib; importlib.import_module('{mod}')"],
                               capture_output=True, text=True, timeout=90, cwd=workdir)
            if r.returncode == 0:
                result["boot"] = "entrypoint imported OK"
                result["runnable"] = "likely"
            else:
                result["boot"] = "entrypoint import FAILED"
                result["boot_error"] = (r.stderr or "")[-400:]
                result["runnable"] = "no"
        except (subprocess.TimeoutExpired, OSError) as e:
            result.update({"boot": f"import smoke error: {type(e).__name__}", "runnable": "unknown"})
    else:
        result.setdefault("boot", "skipped (deps not installed)")
        result.setdefault("runnable", "no" if (syntax_fail or not deps_ok) else "unknown")

    # cleanup the throwaway venv so it isn't committed into code/
    try:
        subprocess.run(["rm", "-rf", venv], capture_output=True, timeout=30)
    except OSError:
        pass
    return result
