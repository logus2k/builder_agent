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
            continue
        # entity concepts live under models/, services under services/ (mirror contract_scaffold._path_of)
        subdir = "models" if v.get("kind") == "entity" else "services"
        path = os.path.join(workspace, "app", subdir, f"{key}.py")
        if not os.path.isfile(path):
            continue
        f, a = repair_module(path, cands, cands[0], log)
        if f:
            total += f
            ambiguous += a
            modules += 1
    if total:
        log(f"repo-seam repair: bound {total} misuse site(s) across {modules} module(s) "
            f"({ambiguous} ambiguous deferred to the fix-loop)")
    return {"sites": total, "ambiguous": ambiguous, "modules": modules}
