"""Deterministic repair of persistence-seam misuse.

The scaffold exposes `repo` as a FACTORY: `repo("entity").get(id)`. Model-filled CUSTOM (non-CRUD) bodies
routinely call the CRUD method on the bare function — `repo.get(id)` — which raises at runtime
`AttributeError: 'function' object has no attribute 'get'` (HTTP 500). The scaffold's own CRUD bodies are
correct; only model-written custom operations drift, so this is a systemic, deterministically detectable
defect — not something to hope a prompt prevents.

We fix it deterministically and WITHOUT regex: locate each `repo.<crud>(` via the AST (a Call whose func
is an Attribute on the bare Name `repo`), then do a POSITION-PRECISE text edit at the `repo` token's
recorded offsets — so comments and formatting are preserved. Each bare call is bound to a concrete store:
the service's single depended-on entity when the contract makes it unambiguous (fully correct), else the
service's PRIMARY entity as a crash-stop (a multi-entity service's exact store per call is left to the
tester->builder fix loop, which has the runtime error context). Store names come from the contract's
`depends_on`, never guessed.
"""

from __future__ import annotations

import ast
import os

#: repo methods the store exposes; a bare `repo.<one-of-these>(` is always misuse (repo is a factory fn).
_CRUD = {"get", "create", "list", "update", "delete"}


def repair_module(path: str, candidates: list[str], primary: str, log) -> tuple[int, int]:
    """Rewrite every bare `repo.<crud>(` in one module to `repo("<entity>").<crud>(`. Returns
    (sites_fixed, ambiguous_count)."""
    try:
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError):
        return 0, 0
    edits: dict[int, list[tuple[int, int, str]]] = {}   # lineno -> [(col, end_col, replacement)]
    fixed = ambiguous = 0
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in _CRUD and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "repo"):
            token = n.func.value                          # the bare `repo` Name node
            if token.end_col_offset is None:
                continue
            ambiguous_here = len(candidates) > 1
            entity = candidates[0] if not ambiguous_here else primary
            edits.setdefault(token.lineno, []).append(
                (token.col_offset, token.end_col_offset, f'repo("{entity}")'))
            fixed += 1
            ambiguous += 1 if ambiguous_here else 0
    if not edits:
        return 0, 0
    lines = src.splitlines(keepends=True)
    for lineno, spans in edits.items():
        line = lines[lineno - 1]
        # apply right-to-left so earlier edits don't shift later offsets on the same line
        for col, end_col, repl in sorted(spans, key=lambda s: s[0], reverse=True):
            line = line[:col] + repl + line[end_col:]
        lines[lineno - 1] = line
    new_src = "".join(lines)
    try:                                                  # never write a version that won't parse
        ast.parse(new_src)
    except SyntaxError:
        return 0, 0
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_src)
    log(f"  [repo-seam] {os.path.basename(path)}: bound {fixed} bare repo.<crud>( call(s)"
        + (f" ({ambiguous} ambiguous -> primary '{primary}', fix-loop refines the exact store)"
           if ambiguous else " (single store — exact)"))
    return fixed, ambiguous


def repair(workspace: str, contract: dict, log=print) -> dict:
    """Fix bare `repo.<crud>(` in every contract service module. Store candidates for a concept are the
    ENTITY concepts it depends on (plus itself when it is an entity). Deterministic; a no-op when a module
    has no candidate store or no misuse."""
    entity_keys = {k for k, v in contract.items() if v.get("kind") == "entity"}
    total = ambiguous = modules = 0
    sub = "services"
    for key, v in contract.items():
        cands = list(v.get("depends_on") or [])
        if v.get("kind") == "entity":
            cands = [key] + cands
        cands = [c for c in cands if c in entity_keys]    # only real stores
        if not cands:
            # CRASH-STOP, never skip. A bare `repo.<crud>(` is ALWAYS a runtime 500 (repo is a factory),
            # independent of whether the contract lets us name the right entity. A service with no entity
            # dependency (e.g. a compliance/validation service that looks records up by an external key)
            # would otherwise keep the crash. Bind such calls to the concept's OWN store: a valid, empty
            # store whose get()/list() return None/[] so the model's existing not-found branches produce a
            # graceful 200 instead of a 500. The exact store, if it should be a real entity, is left to the
            # tester->builder fix-loop, which has the runtime error context. Deterministic, no guessing.
            cands = [key]
        # entity concepts live under models/, services under services/ (mirror contract_scaffold._path_of)
        subdir = "models" if v.get("kind") == "entity" else "services"
        path = os.path.join(workspace, "app", subdir, f"{key}.py")
        if not os.path.isfile(path):
            continue
        f, a = repair_module(path, cands, cands[0], log)
        # Second seam defect, same module: the store returns DICTS, so `record.id`
        # on a CRUD result is always a runtime AttributeError. Repair it here too.
        f += repair_record_attrs(path, log)
        if f:
            total += f
            ambiguous += a
            modules += 1
    if total:
        log(f"repo-seam repair: bound {total} misuse site(s) across {modules} module(s) "
            f"({ambiguous} ambiguous deferred to the fix-loop)")
    return {"sites": total, "ambiguous": ambiguous, "modules": modules}


# --------------------------------------------------------------------------- #
# Store results are DICTS, not ORM objects                                      #
# --------------------------------------------------------------------------- #
# Repo.create/get/update/list return plain dicts (see the persistence template).
# Model-written bodies routinely treat the result as an ORM row and read
# `record.id`, which raises at runtime `AttributeError: 'dict' object has no
# attribute 'id'` (HTTP 500). Same shape as the bare-`repo.get(` defect above:
# systemic, deterministically detectable, not something to hope a prompt
# prevents. Detected via the AST and repaired with a position-precise edit —
# no regex — so comments and formatting survive.

#: attributes that are REAL on a dict; never rewrite these
_DICT_ATTRS = {
    "get", "keys", "values", "items", "copy", "pop", "popitem", "setdefault",
    "update", "clear", "fromkeys",
}


def _store_bound_names(tree: "ast.AST") -> set:
    """Names bound to the result of a store CRUD call, in either form the models
    write: `x = repo("y").create(...)` and `r = repo("y")` then `x = r.create(...)`."""
    handles, bound = set(), set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        # r = repo("y")
        if isinstance(func, ast.Name) and func.id == "repo":
            for t in node.targets:
                if isinstance(t, ast.Name):
                    handles.add(t.id)
            continue
        if not isinstance(func, ast.Attribute) or func.attr not in _CRUD:
            continue
        base = func.value
        is_store = (
            (isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "repo")
            or (isinstance(base, ast.Name) and base.id in handles)
        )
        if is_store:
            for t in node.targets:
                if isinstance(t, ast.Name):
                    bound.add(t.id)
    return bound


def repair_record_attrs(path: str, log) -> int:
    """Rewrite `record.field` -> `record["field"]` for every name bound from a
    store CRUD call. Returns the number of sites fixed."""
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return 0

    bound = _store_bound_names(tree)
    if not bound:
        return 0

    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(id(node.func))

    edits, hasattr_edits = [], []
    # `hasattr(record, "x")` on a dict is ALWAYS False, so the guarded branch never
    # runs and the field silently reads as absent — worse than a crash, because the
    # endpoint returns 200 with quietly wrong data. Rewrite to a membership test.
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "hasattr" and len(node.args) == 2
                and isinstance(node.args[0], ast.Name) and node.args[0].id in bound
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            hasattr_edits.append(node)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            continue
        if node.value.id not in bound or node.attr in _DICT_ATTRS:
            continue
        if id(node) in called:
            continue          # a CALL on a non-dict attribute is a different bug
        if not isinstance(node.ctx, ast.Load):
            continue          # only reads; an assignment target needs a real fix
        edits.append(node)

    if not edits and not hasattr_edits:
        return 0

    lines = src.splitlines(keepends=True)
    offsets, total = [], 0
    for line in lines:
        offsets.append(total)
        total += len(line)

    def pos(lineno, col):
        return offsets[lineno - 1] + col

    # apply back-to-front so earlier offsets stay valid
    replacements = [
        (n.value.lineno, n.value.col_offset, n.end_lineno, n.end_col_offset,
         f'{n.value.id}["{n.attr}"]') for n in edits
    ] + [
        (n.lineno, n.col_offset, n.end_lineno, n.end_col_offset,
         f'"{n.args[1].value}" in {n.args[0].id}') for n in hasattr_edits
    ]
    out = src
    for lineno, col, end_lineno, end_col, text in sorted(replacements, reverse=True):
        out = out[:pos(lineno, col)] + text + out[pos(end_lineno, end_col):]

    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    parts = []
    if edits:
        parts.append(f"{len(edits)} attribute access(es) -> dict access")
    if hasattr_edits:
        parts.append(f"{len(hasattr_edits)} always-False hasattr() -> membership test")
    log(f"  [repo-seam] {os.path.basename(path)}: " + "; ".join(parts))
    return len(edits) + len(hasattr_edits)
