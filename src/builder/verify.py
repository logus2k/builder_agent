"""Deterministic verification — NO LLM, NO Claude, offline. Checks a task's produced
DELIVERABLE against its acceptance: does it exist, parse/compile, and contain no stub
fingerprints? A definitional deliverable (interface/schema) is not a stub just because a
mock ships beside it (HARD vs SOFT signals). Ported from the validated planner probe.
"""

from __future__ import annotations

import json
import os
import py_compile
import re

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


def stub_hits(path: str, title: str, kind: str, deliverable: str) -> int:
    try:
        body = open(path).read()
    except Exception:  # noqa: BLE001
        return 0
    n = len(_HARD.findall(body))
    if not _is_definitional(title, kind, deliverable):
        n += len(_SOFT.findall(body))
    return n


def verify(task: dict, files: list[str], workdir: str) -> dict:
    """Verify the task's deliverable. Returns a structured verdict."""
    deliverable = task.get("deliverable", "")
    dfile = find_deliverable(files, workdir, deliverable)
    if dfile is None:
        return {"produced": False, "parses": False, "stub_hits": 0, "clean": False,
                "reason": "deliverable not produced", "deliverable_file": None}
    path = os.path.join(workdir, dfile)
    parses, pdetail = _parses(path)
    stubs = stub_hits(path, task.get("title", ""), task.get("kind", ""), deliverable)
    clean = bool(parses and stubs < STUB_THRESHOLD)
    reason = "clean" if clean else (
        "does not parse" if not parses else f"stub fingerprints ({stubs})")
    return {"produced": True, "parses": parses, "parse_detail": pdetail,
            "stub_hits": stubs, "clean": clean, "reason": reason, "deliverable_file": dfile}
