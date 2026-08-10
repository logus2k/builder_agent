"""Integrated heal loop — DETECT all issues, then FIX each (guarded, one by one), then RE-DETECT,
until the codebase is clean or the loop is bounded out.

Detection is deliberately SEPARATE from fixing: detectors only SURFACE problems (with context, never
a fix), and re-detection each round MEASURES the real effect of the previous round instead of
assuming it. Fixers resolve issues — deterministically where the fix is mechanical (add an import,
alias a name-drift), with the model where it needs judgment (implement a missing symbol, fix a boot
error). Every fix is guarded (revert if it introduces a NEW parse error or drops importable modules)
and every revert LOGS the concrete measured reason — nothing is asserted about "the model".

This subsumes the earlier one-off passes (reconcile_exports, resolve_missing_imports) and the two
boot-repair loops into ONE bounded loop. Stack-agnostic within Python: detectors work on the import
graph and the AST, not on any framework's constructs.

This module is DETECTION-ONLY for now (fixers + loop land next); it is import-light so the detectors
can be unit-tested without the model or a venv.
"""

from __future__ import annotations

import ast
import collections.abc
import dataclasses
import enum
import os
import typing
from collections import defaultdict

from . import assemble, opencode, repair, verify

#: Stdlib modules whose MEMBERS are commonly used bare without importing them (the classic being
#: `Dict`/`Optional`/`List` from typing). For an otherwise-unresolved name we check these so it is
#: classified as a missing `from <mod> import <name>`, not as "defined nowhere".
_MEMBER_MODS = {"typing": typing, "collections.abc": collections.abc,
                "dataclasses": dataclasses, "enum": enum}


def _parse_all(ws: str, py: list[str]) -> tuple[dict, list[dict]]:
    """Parse every source file. Returns ({rel: AST} for the ones that parse, [parse_error issues])."""
    trees, issues = {}, []
    for rel in py:
        try:
            trees[rel] = ast.parse(open(os.path.join(ws, rel), encoding="utf-8").read())
        except SyntaxError as e:
            issues.append({"kind": "parse_error", "file": rel,
                           "detail": f"SyntaxError L{e.lineno}: {e.msg}",
                           "context": {"lineno": e.lineno, "msg": e.msg}})
        except (OSError, ValueError) as e:
            issues.append({"kind": "parse_error", "file": rel, "detail": f"{type(e).__name__}: {e}",
                           "context": {}})
    return trees, issues


def _top_packages(ws: str) -> set:
    tops = {e for e in os.listdir(ws)
            if os.path.isdir(os.path.join(ws, e)) and not e.startswith(".")
            and e not in ("__pycache__", "tests")}
    tops |= {f[:-3] for f in os.listdir(ws) if f.endswith(".py")}
    return tops


def _module_exists(ws: str, mod: str) -> bool:
    base = mod.replace(".", "/")
    return os.path.isfile(os.path.join(ws, base + ".py")) \
        or os.path.isfile(os.path.join(ws, base, "__init__.py"))


def detect(ws: str, py_exe: str | None = None, skeleton: dict | None = None,
           sanctioned=None, internal_tops: set | None = None) -> list[dict]:
    """Run every detector over the current workspace and return a flat list of Issues. An Issue is
    {kind, file, detail, context}. Static detectors need no venv; the boot detector needs `py_exe`.
    Order is roughly dependency-first (parse errors before analysis that depends on parsing)."""
    py = repair._src_files(ws)
    trees, issues = _parse_all(ws, py)
    tops = _top_packages(ws)
    internal_tops = internal_tops if internal_tops is not None else tops

    # Symbol index (from the files that PARSE): symbol -> {modules defining it}, and per-module defs.
    index: dict[str, set] = defaultdict(set)
    defines: dict[str, set] = {}
    for rel, tree in trees.items():
        d = assemble._defined_names(tree)
        defines[assemble._module_name(rel)] = d
        for n in d:
            index[n].add(assemble._module_name(rel))

    # (0) module/package COLLISION: both `X.py` and `X/__init__.py` exist. Python imports the
    #     package, so every symbol defined only in the shadowed `X.py` is unreachable (and shows up
    #     as a flood of missing-exports). Detect once per colliding module; the fixer folds the
    #     module into the package. This is a STRUCTURAL defect from independent generation.
    for rel in py:
        if rel.endswith(".py") and not rel.endswith("__init__.py"):
            base = rel[:-3]
            if os.path.isfile(os.path.join(ws, base, "__init__.py")):
                issues.append({"kind": "collision", "file": rel,
                               "detail": f"module '{rel}' is shadowed by package '{base}/' (its "
                                         f"symbols are unreachable)",
                               "context": {"module_file": rel, "package_base": base}})

    for rel, tree in trees.items():
        mod_self = assemble._module_name(rel)

        # (a) internal imports: missing MODULE, or imported-but-not-EXPORTED symbol.
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                    and node.module.split(".")[0] in tops):
                continue
            mod = node.module
            exists = _module_exists(ws, mod)
            for a in node.names:
                if a.name == "*":
                    continue
                if not exists:
                    issues.append({"kind": "missing_module", "file": rel,
                                   "detail": f"imports '{a.name}' from missing internal module '{mod}'",
                                   "context": {"module": mod, "symbol": a.name}})
                elif a.name not in defines.get(mod, set()):
                    issues.append({"kind": "missing_export", "file": rel,
                                   "detail": f"imports '{a.name}' from '{mod}', which does not define it",
                                   "context": {"module": mod, "symbol": a.name}})

        # (b) used-but-undefined names: resolvable to another module (missing import), a stdlib module
        #     (missing stdlib import), or resolvable NOWHERE (unresolved — a real gap or a bug).
        bound, used = assemble._bound_and_used(tree)
        for name in sorted(used - bound - assemble._ALWAYS_BOUND):
            mods = [m for m in index.get(name, ()) if m != mod_self]
            if mods:
                issues.append({"kind": "missing_import", "file": rel,
                               "detail": f"uses '{name}', defined in '{mods[0]}' but not imported here",
                               "context": {"name": name, "defined_in": sorted(mods), "stdlib": False}})
            elif name in assemble._STDLIB_MODS:
                issues.append({"kind": "missing_import", "file": rel,
                               "detail": f"uses stdlib module '{name}' without importing it",
                               "context": {"name": name, "defined_in": [], "stdlib": True}})
            elif (member_mod := next((mn for mn, mo in _MEMBER_MODS.items()
                                      if hasattr(mo, name)), None)):
                issues.append({"kind": "missing_import", "file": rel,
                               "detail": f"uses '{name}' without importing it from '{member_mod}'",
                               "context": {"name": name, "defined_in": [], "stdlib": True,
                                           "from_module": member_mod}})
            else:
                issues.append({"kind": "undefined_unresolved", "file": rel,
                               "detail": f"uses '{name}', which is defined nowhere in the project",
                               "context": {"name": name}})

        # (c) stub / incomplete markers.
        hard, _ = verify.stub_hits(os.path.join(ws, rel), "", "code", rel)
        if hard >= verify.STUB_THRESHOLD:
            issues.append({"kind": "stub", "file": rel,
                           "detail": f"{hard} incompleteness markers (TODO/NotImplemented/placeholder)",
                           "context": {"count": hard}})

        # (d) unsanctioned dependency (only when a policy is in force).
        if sanctioned is not None:
            uns = verify.unsanctioned_deps(os.path.join(ws, rel), sanctioned, internal_tops)
            if uns:
                issues.append({"kind": "unsanctioned", "file": rel,
                               "detail": f"imports unsanctioned dependency: {', '.join(uns)}",
                               "context": {"deps": uns}})

    # (e) boot error — the catch-all for what static analysis cannot see (circular imports, runtime
    #     errors, missing third-party deps). One per run: the first thing that breaks the import.
    if py_exe and skeleton is not None:
        import builder.context as _ctx  # local import to keep detectors import-light
        entry = _ctx.entrypoint_module(skeleton)
        if entry:
            ok, err = repair._boot(py_exe, ws, entry)
            if not ok:
                issues.append({"kind": "boot_error", "file": repair._offender(ws, err),
                               "detail": repair._signature(err),
                               "context": {"traceback": err[-1500:], "entry": entry}})

    return issues


def summarize(issues: list[dict]) -> dict:
    """Counts by kind — for logging the detection round."""
    by_kind: dict[str, int] = defaultdict(int)
    for i in issues:
        by_kind[i["kind"]] += 1
    return dict(by_kind)


# --------------------------------------------------------------------------------------------- #
# FIXERS — resolve one issue. DETERMINISTIC where the fix is mechanical (imports, name-drift      #
# aliases); the MODEL where it needs judgment (implement a symbol/module, fix a parse/boot error, #
# complete a stub). Each returns (applied: bool, how: str). Guards live in the loop, not here.    #
# --------------------------------------------------------------------------------------------- #
_ALIAS_THRESHOLD = 0.9        # same conservative bar reconcile used: alias only on a very strong match

#: Fix order — parse errors first (they block analysis of the file), then structural gaps, then
#: within-file gaps, then the catch-all boot error last.
_ORDER = {"parse_error": 0, "missing_module": 1, "missing_export": 2, "missing_import": 3,
          "stub": 4, "undefined_unresolved": 5, "unsanctioned": 6, "boot_error": 7}


def _order(issues: list[dict]) -> list[dict]:
    return sorted(issues, key=lambda i: (_ORDER.get(i["kind"], 9), i.get("file") or ""))


def _fix_missing_import(issue: dict, ws: str) -> tuple[bool, str]:
    """Deterministic: add the one import line. No model — adding an import can't drop code."""
    rel, ctx = issue["file"], issue["context"]
    name = ctx["name"]
    if ctx.get("from_module"):
        imp = f"from {ctx['from_module']} import {name}"
    elif ctx.get("stdlib"):
        imp = f"import {name}"
    elif ctx.get("defined_in"):
        imp = f"from {ctx['defined_in'][0]} import {name}"
    else:
        return False, "no source"
    try:
        src = open(os.path.join(ws, rel), encoding="utf-8").read()
        newsrc = assemble._insert_imports(src, ast.parse(src), [imp])
        ast.parse(newsrc)
        open(os.path.join(ws, rel), "w", encoding="utf-8").write(newsrc)
        return True, f"import: {imp}"
    except (SyntaxError, OSError, ValueError) as e:
        return False, f"{type(e).__name__}"


def _module_path(ws: str, mod: str) -> str:
    base = mod.replace(".", "/")
    return base + ".py" if os.path.isfile(os.path.join(ws, base + ".py")) else base + "/__init__.py"


def _try_alias_export(ws: str, mod: str, sym: str) -> str | None:
    """DETERMINISTIC name-drift fix: if the module already defines a strong equivalent of `sym` (per
    the reranker), append `sym = equivalent` and return the target; else None. Never calls the model,
    never drops code — so it's safe to run to a fixpoint."""
    path = _module_path(ws, mod)
    try:
        content = open(os.path.join(ws, path), encoding="utf-8").read()
        have = sorted(assemble._defined_names(ast.parse(content)))
    except (SyntaxError, OSError, ValueError):
        return None
    if not have:
        return None
    if sym in have:
        return None            # already defined here (e.g. shadowed by a package) — NOT a name-drift
    candidates = [h for h in have if h != sym]
    scores = assemble._rerank(sym, candidates) if candidates else []
    if not scores:
        return None
    bi = max(range(len(scores)), key=lambda i: scores[i])
    if scores[bi] < _ALIAS_THRESHOLD:
        return None
    have = candidates          # index into the filtered list
    if f"\n{sym} " not in content and f"\n{sym}=" not in content:
        with open(os.path.join(ws, path), "a", encoding="utf-8") as f:
            f.write(f"\n\n# --- reconciled export alias ---\n{sym} = {have[bi]}\n")
    return have[bi]


def _fix_missing_export(issue: dict, ws: str, skeleton: dict) -> tuple[bool, str]:
    """Name-drift -> deterministic alias (reranker decides); genuinely-missing -> model implements
    it with the FULL module contract (so the rewrite can't drop existing names)."""
    ctx = issue["context"]
    mod, sym = ctx["module"], ctx["symbol"]
    alias = _try_alias_export(ws, mod, sym)
    if alias:
        return True, f"alias {sym}={alias}"
    path = _module_path(ws, mod)
    try:
        pre = open(os.path.join(ws, path), encoding="utf-8").read()
        have = sorted(assemble._defined_names(ast.parse(pre)))
    except (SyntaxError, OSError, ValueError):
        pre, have = None, []
    # implement (model), full-contract so nothing is lost
    keep = ", ".join(have) or "(none)"
    instr = (f"Rewrite '{path}' so it defines ALL of these existing names, preserving behaviour: "
             f"{keep}. And ADD this missing name that other files import from '{mod}': {sym} — "
             f"implement it for real. Follow AGENTS.md. No placeholders."
             + (f"\n\nCurrent content:\n---\n{pre[:6000]}\n---" if pre else ""))
    opencode.build_task({"title": f"add {sym} to {mod}", "instructions": instr}, ws, path,
                        action="edit", skeleton=skeleton)
    now = _defs_of(ws, path)
    return (sym in now), f"impl {sym}"


def _fix_missing_module(issue: dict, ws: str, skeleton: dict) -> tuple[bool, str]:
    """Create the missing internal module (model), defining the imported symbol."""
    ctx = issue["context"]
    mod, sym = ctx["module"], ctx["symbol"]
    path = mod.replace(".", "/") + ".py"
    instr = (f"Create '{path}' — the internal module '{mod}' the app imports but which does not "
             f"exist. It MUST define '{sym}' (and any siblings other files import from it), "
             f"implemented for real for this project. Follow AGENTS.md. No placeholders.")
    opencode.build_task({"title": f"create module {mod}", "instructions": instr}, ws, path,
                        action="create", skeleton=skeleton)
    return (sym in _defs_of(ws, path)), f"create {mod}"


def _fix_with_model(issue: dict, ws: str, skeleton: dict) -> tuple[bool, str]:
    """Generic model fix for issues that need judgment on a SINGLE file: parse error, stub, boot
    error, unsanctioned dep, unresolved name. Full current content inline; guarded by the loop."""
    rel = issue["file"]
    if not rel or not os.path.isfile(os.path.join(ws, rel)):
        return False, "no file"
    try:
        pre = open(os.path.join(ws, rel), encoding="utf-8").read()
    except OSError:
        return False, "read"
    kind = issue["kind"]
    ask = {
        "parse_error": f"Fix the SyntaxError in '{rel}' ({issue['detail']}) — make it valid Python, "
                       f"preserving intent.",
        "stub": f"Complete '{rel}' for real — replace every TODO/placeholder/NotImplemented with a "
                f"working implementation.",
        "boot_error": f"The app fails to import with:\n{issue['context'].get('traceback','')[-1000:]}\n"
                      f"Fix '{rel}' so the whole app imports and runs.",
        "unsanctioned": f"'{rel}' imports an unsanctioned dependency ({issue['detail']}). Rewrite it "
                        f"to use ONLY the allowed dependencies in AGENTS.md (e.g. stdlib sqlite3, not "
                        f"an ORM), keeping behaviour.",
        "undefined_unresolved": f"'{rel}' uses '{issue['context'].get('name')}' which is defined "
                                f"nowhere. Define it or fix the reference so the file is correct.",
    }.get(kind, f"Fix the issue in '{rel}': {issue['detail']}")
    instr = (f"{ask} Change ONLY what is needed; keep the rest of the file intact. Follow AGENTS.md. "
             f"No placeholders.\n\nCurrent content of '{rel}':\n---\n{pre[:6000]}\n---")
    opencode.build_task({"title": f"fix {kind} in {rel}", "instructions": instr}, ws, rel,
                        action="edit", skeleton=skeleton)
    return True, f"model:{kind}"


def _fix_collision(issue: dict, ws: str) -> tuple[bool, str]:
    """Fold a module shadowed by a same-named package INTO that package (deterministic, no model):
    move `X.py` to `X/_<name>_impl.py` and re-export it from `X/__init__.py`, so its symbols become
    reachable via the package. Safety net for collisions that slip past placement normalization."""
    rel = issue["context"]["module_file"]
    base = issue["context"]["package_base"]
    name = os.path.basename(base)
    sub_rel = f"{base}/_{name}_impl.py"
    try:
        content = open(os.path.join(ws, rel), encoding="utf-8").read()
        # Re-export the moved module's OWN top-level definitions EXPLICITLY (not `import *`, which is
        # invisible to static analysis) so both Python and the detector see the symbols.
        tree = ast.parse(content)
        own = sorted({n.name for n in tree.body
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
                     | {t.id for n in tree.body if isinstance(n, ast.Assign)
                        for t in n.targets if isinstance(t, ast.Name)}) or None
        with open(os.path.join(ws, sub_rel), "w", encoding="utf-8") as f:
            f.write(content)
        os.remove(os.path.join(ws, rel))
        init = os.path.join(ws, base, "__init__.py")
        line = (f"from ._{name}_impl import {', '.join(n for n in own if not n.startswith('_'))}"
                if own else f"from ._{name}_impl import *")
        existing = open(init, encoding="utf-8").read() if os.path.isfile(init) else ""
        if line not in existing:
            with open(init, "a", encoding="utf-8") as f:
                f.write(f"\n{line}\n")
        return True, f"folded {rel} into {base}/"
    except (SyntaxError, OSError, ValueError) as e:
        return False, str(e)


def _defs_of(ws: str, rel: str) -> set:
    try:
        return assemble._defined_names(ast.parse(open(os.path.join(ws, rel), encoding="utf-8").read()))
    except (SyntaxError, OSError, ValueError):
        return set()


def fix_one(issue: dict, ws: str, skeleton: dict) -> tuple[bool, str]:
    kind = issue["kind"]
    if kind == "collision":
        return _fix_collision(issue, ws)
    if kind == "missing_import":
        return _fix_missing_import(issue, ws)
    if kind == "missing_export":
        return _fix_missing_export(issue, ws, skeleton)
    if kind == "missing_module":
        return _fix_missing_module(issue, ws, skeleton)
    return _fix_with_model(issue, ws, skeleton)


def _deterministic_sweep(ws: str, skeleton: dict, sanctioned, internal_tops, log,
                         max_passes: int = 6) -> int:
    """Apply ONLY the mechanical fixes (add missing import, alias name-drift) repeatedly until no
    more apply (a fixpoint). These fixes can only REDUCE issues — they never call the model and
    never drop code — so running them to convergence after each model round mops up the imports a
    model fix just introduced, instead of letting them oscillate across rounds. Static detection
    only (no boot), so it's cheap."""
    total = 0
    for _ in range(max_passes):
        did = 0
        for i in detect(ws, None, skeleton, sanctioned, internal_tops):
            if i["kind"] == "collision":
                ok, _ = _fix_collision(i, ws)
                did += 1 if ok else 0
            elif i["kind"] == "missing_import":
                ok, _ = _fix_missing_import(i, ws)
                did += 1 if ok else 0
            elif i["kind"] == "missing_export":
                if _try_alias_export(ws, i["context"]["module"], i["context"]["symbol"]):
                    did += 1
        total += did
        if not did:
            break
    if total:
        log(f"    deterministic sweep: resolved {total} mechanical issue(s)")
    return total


# --------------------------------------------------------------------------------------------- #
# THE LOOP — detect all -> fix each (guarded) -> re-detect -> until clean or bounded.             #
# --------------------------------------------------------------------------------------------- #
def heal(ws: str, skeleton: dict, sanctioned=None, internal_tops: set | None = None,
         max_rounds: int = 6, log=print) -> dict:
    """Run the integrated heal loop. Each round: detect ALL issues, fix them one by one (each with a
    snapshot+guard so a bad fix is reverted with a MEASURED reason), then re-detect. Stops when clean,
    when the issue set stops shrinking, or at max_rounds. Returns a report of what happened."""
    lang = (skeleton.get("language") or "python").lower()
    import builder.context as _ctx
    entry = _ctx.entrypoint_module(skeleton)
    if lang != "python" or not entry:
        return {"language": lang, "runnable": "unknown", "note": f"heal not implemented for {lang!r}"}
    py_exe = repair._make_venv(ws, skeleton.get("manifest"), log)
    if not py_exe:
        return {"runnable": "unknown", "note": "no venv"}

    # Mop up all mechanical issues up front so the rounds only spend the model on real judgment.
    applied_total = _deterministic_sweep(ws, skeleton, sanctioned, internal_tops, log)
    rounds, reverts, last_sig, stuck = [], [], None, 0
    for rnd in range(1, max_rounds + 1):
        issues = detect(ws, py_exe, skeleton, sanctioned, internal_tops)
        counts = summarize(issues)
        log(f"  heal round {rnd}: {sum(counts.values())} issues {counts}")
        if not issues:
            break
        sig = frozenset((i["kind"], i.get("file"), i["detail"]) for i in issues)
        if sig == last_sig:
            stuck += 1
            if stuck >= 2:
                log("  heal: no progress (issue set unchanged); stopping")
                break
        else:
            stuck, last_sig = 0, sig
        n_fixed = 0
        bad_before = repair._nonparsing(py_exe, ws)
        for issue in _order(issues):
            snap = repair._snapshot(ws)
            ok, how = fix_one(issue, ws, skeleton)
            if not ok:
                continue
            # Cheap per-fix guard: a fix must not introduce a NEW parse error (one py_compile call,
            # not a full import of every module). Import-level regressions are caught by re-detection
            # next round and by the final boot — so we don't pay the expensive per-fix import check.
            new_bad = repair._nonparsing(py_exe, ws) - bad_before
            if new_bad:
                repair._restore(ws, snap)
                reverts.append({"round": rnd, "issue": issue["kind"], "file": issue.get("file"),
                                "reason": f"new parse error in {sorted(new_bad)}"})
                log(f"    reverted {issue['kind']} on {issue.get('file')}: new parse error")
            else:
                n_fixed += 1
                applied_total += 1
                bad_before = repair._nonparsing(py_exe, ws)
        # Fixpoint the mechanical issues the model fixes just introduced, before the next detect.
        applied_total += _deterministic_sweep(ws, skeleton, sanctioned, internal_tops, log)
        rounds.append({"round": rnd, "counts": counts, "fixed": n_fixed})

    ok, err = repair._boot(py_exe, ws, entry)
    result = {"language": "python", "entry": entry, "runnable": "yes" if ok else "no",
              "rounds": rounds, "fixes_applied": applied_total, "reverts": reverts}
    if ok:
        result["server_smoke"] = repair.server_smoke(py_exe, ws, skeleton, log=log)
    else:
        result["final_error"] = repair._signature(err)
    repair._cleanup(ws)
    return result
