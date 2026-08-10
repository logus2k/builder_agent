"""Deterministic verification — NO LLM, NO Claude, offline. Checks a task's produced
DELIVERABLE against its acceptance: does it exist, parse/compile, and contain no stub
fingerprints? A definitional deliverable (interface/schema) is not a stub just because a
mock ships beside it (HARD vs SOFT signals). Ported from the validated planner probe.
"""

from __future__ import annotations

import ast
import json
import os
import py_compile
import re
import sys

#: Python's own standard-library top-level module names — anything imported that is NOT here, NOT
#: an internal package, and NOT a relative import is a THIRD-PARTY dependency the design must have
#: sanctioned. (3.10+; empty on older runtimes -> conformance simply no-ops, never false-positives.)
_STDLIB = getattr(sys, "stdlib_module_names", frozenset())

# HARD = incomplete deliverable (always disqualifies). SOFT = fake implementation
# (disqualifies an implementation, harmless for a definitional deliverable).
_HARD = re.compile("|".join([
    r"\bTODO\b", r"\bFIXME\b", r"NotImplemented", r"raise\s+NotImplementedError",
    r"\bplaceholder\b", r"pass\s*#\s*implement", r"return\s+None\s*#",
]), re.IGNORECASE)
_SOFT = re.compile("|".join([
    r"console\.log\(['\"][^'\"]*API", r"setTimeout\(", r"\bsimulat", r"\bmock", r"\bdummy\b",
]), re.IGNORECASE)
_DEFN_TITLE = re.compile(r"\b(interface|schema|contract|protocol|data model|definition|structure)\b",
                         re.IGNORECASE)
_DEFN_EXT = (".json", ".yaml", ".yml", ".md", ".proto", ".graphql")

STUB_THRESHOLD = 2


def _is_definitional(title: str, kind: str, deliverable: str) -> bool:
    if (kind or "").lower() in ("schema", "config", "docs"):
        return True
    if title and _DEFN_TITLE.search(title):
        return True
    return deliverable.lower().endswith(_DEFN_EXT)


def find_deliverable(files: list[str], workdir: str, deliverable: str) -> str | None:
    """The produced file matching the task's declared deliverable (by basename)."""
    want = os.path.basename(deliverable).strip().split()[0] if deliverable else ""
    for f in files:
        if os.path.basename(f) == want or f == deliverable:
            return f
    # fallback: a single produced file is the deliverable
    return files[0] if len(files) == 1 else None


def _parses(path: str) -> tuple[bool, str]:
    if path.endswith(".py"):
        try:
            py_compile.compile(path, doraise=True)
            return True, "py:ok"
        except py_compile.PyCompileError as e:
            return False, f"py:FAIL {str(e)[:50]}"
    if path.endswith(".json"):
        try:
            json.load(open(path))
            return True, "json:ok"
        except Exception as e:  # noqa: BLE001
            return False, f"json:FAIL {str(e)[:40]}"
    return True, "n/a"  # no parser for this type -> not a failure


def stub_hits(path: str, title: str, kind: str, deliverable: str) -> tuple[int, int]:
    """Return (hard, soft). HARD = genuine incompleteness (TODO / NotImplementedError /
    placeholder) — a real defect. SOFT = words like mock/simulate/dummy that ALSO occur in
    perfectly real code (a configurable adapter's 'simulated mode' fallback message, a test
    double, a docstring). SOFT is advisory only — it must NOT fail a task, or every real
    external-service client gets punished for mentioning the word."""
    try:
        body = open(path).read()
    except Exception:  # noqa: BLE001
        return 0, 0
    hard = len(_HARD.findall(body))
    soft = 0 if _is_definitional(title, kind, deliverable) else len(_SOFT.findall(body))
    return hard, soft


def _third_party_tops(path: str, internal_tops: set) -> set:
    """Top-level THIRD-PARTY modules a Python file imports: not stdlib, not an internal workspace
    package, not a relative import. AST-based (lexical fact, no regex). Empty on any parse error."""
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (SyntaxError, OSError, ValueError):
        return set()
    tops = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            tops.add(node.module.split(".")[0])
    return {t for t in tops if t and t not in _STDLIB and t not in (internal_tops or set())}


def _norm_pkg(n: str) -> str:
    """Normalize a package/module name for matching: lowercase, '-'->'_', drop a 'python_' prefix.
    (PyPI distribution names and import names differ — 'python-jose' imports as 'jose',
    'python-multipart' as 'multipart' — so we compare on a normalized form, not raw strings.)"""
    n = (n or "").strip().lower().replace("-", "_")
    return n[7:] if n.startswith("python_") else n


def unsanctioned_deps(path: str, sanctioned, internal_tops: set) -> list[str]:
    """Third-party imports NOT in the design-sanctioned dependency set. This is the CONFORMANCE
    check: the design decides the stack (top-down); code that reaches for a library nobody
    sanctioned — an ORM in a store-some-rows app — is a review failure, not an accepted build.
    `sanctioned=None` means 'no policy declared' -> the check no-ops (never false-positives).

    Matching tolerates the PyPI-name vs import-name gap: a sanctioned dist contributes both its
    normalized name AND its first namespace segment (so 'google-auth-oauthlib' admits `google`),
    and an import is allowed if its normalized name or first segment is sanctioned."""
    if sanctioned is None or not path.endswith(".py"):
        return []
    allowed = set()
    for s in sanctioned:
        n = _norm_pkg(s)
        if n:
            allowed.add(n)
            allowed.add(n.split("_")[0])
    out = []
    for t in _third_party_tops(path, internal_tops):
        n = _norm_pkg(t)
        if n not in allowed and n.split("_")[0] not in allowed:
            out.append(t)
    return sorted(out)


def verify(task: dict, files: list[str], workdir: str, sanctioned=None,
           internal_tops: set | None = None) -> dict:
    """Verify the task's deliverable. A task is clean if it PARSES, has no genuine-incompleteness
    (HARD) markers, AND (when a dependency policy is declared) imports ONLY design-sanctioned
    third-party libraries. SOFT fingerprints are advisory. The authoritative 'it actually works'
    signal is still run-verify; this adds the missing 'does it even BELONG' signal."""
    deliverable = task.get("deliverable", "")
    dfile = find_deliverable(files, workdir, deliverable)
    if dfile is None:
        return {"produced": False, "parses": False, "stub_hits": 0, "soft_flags": 0, "clean": False,
                "reason": "deliverable not produced", "deliverable_file": None}
    path = os.path.join(workdir, dfile)
    parses, pdetail = _parses(path)
    hard, soft = stub_hits(path, task.get("title", ""), task.get("kind", ""), deliverable)
    unsanctioned = unsanctioned_deps(path, sanctioned, internal_tops or set())
    clean = bool(parses and hard < STUB_THRESHOLD and not unsanctioned)
    reason = "clean" if clean else (
        "does not parse" if not parses else
        f"unsanctioned dependency: {', '.join(unsanctioned)}" if unsanctioned else
        f"incomplete: {hard} stub markers")
    return {"produced": True, "parses": parses, "parse_detail": pdetail,
            "stub_hits": hard, "soft_flags": soft, "unsanctioned": unsanctioned,
            "clean": clean, "reason": reason, "deliverable_file": dfile}
