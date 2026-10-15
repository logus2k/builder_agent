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


# --------------------------------------------------------------------------- #
# Route hygiene — a regression guard on the API scaffold                       #
# --------------------------------------------------------------------------- #
# A FastAPI handler parameter with NO annotation is bound from the QUERY STRING.
# For a scalar that is sloppy; for a structured payload it is wrong; for file
# bytes the request line overflows and the upload can NEVER work. The scaffold
# now annotates every parameter (contract_scaffold._param_binding), so this check
# exists to make sure it stays that way: it re-reads what the generator just
# wrote and fails loudly if the defect ever comes back. Deterministic AST, no
# regex, no LLM — same class as repo_seam's seam repair.

_HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_MUTATING = frozenset({"post", "put", "patch", "delete"})
_UPLOAD_TYPES = frozenset({"UploadFile", "bytes", "File", "Form", "Request"})
_BINARY_WORDS = frozenset({
    "file", "files", "upload", "image", "images", "photo", "picture",
    "avatar", "logo", "attachment", "document", "blob", "bytes",
})
#: a trailing reference word makes a file-shaped name a HANDLE, not the bytes
_REFERENCE_WORDS = frozenset({
    "id", "ids", "name", "names", "url", "urls", "uri", "path", "paths",
    "key", "keys", "ref", "type", "types", "count", "size",
})
_INJECTED = frozenset({"Depends", "Security", "Header", "Cookie", "Request", "State"})


def _binary_param(name: str) -> bool:
    parts = [x for x in (name or "").lower().split("_") if x]
    if not parts:
        return False
    if len(parts) > 1 and parts[-1] in _REFERENCE_WORDS:
        return False
    return bool(set(parts) & _BINARY_WORDS)


def _ann_name(node) -> str:
    """The base name of an annotation. Handles `str`, `mod.Type`, `list[X]` and
    the optional union `str | None` (a BinOp — miss it and every optional
    parameter reads as unannotated)."""
    if node is None:
        return ""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _ann_name(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        for side in (node.left, node.right):
            got = _ann_name(side)
            if got and got != "None":
                return got
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _client_params(fn):
    args = fn.args
    positional = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    pairs = list(zip(positional, defaults)) + list(zip(args.kwonlyargs, args.kw_defaults))
    out = []
    for arg, default in pairs:
        if arg.arg in ("self", "cls"):
            continue
        if isinstance(default, ast.Call) and _ann_name(default.func) in _INJECTED:
            continue
        out.append(arg)
    return out


def route_hygiene(path: str, rel: str = "") -> list:
    """Findings for one generated router module. Each is
    {rule, severity, message, file, line}. Empty list = conformant."""
    rel = rel or os.path.basename(path)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError) as e:
        return [{"rule": "R0-unparseable", "severity": "major", "file": rel,
                 "line": getattr(e, "lineno", 0) or 0,
                 "message": f"module does not parse: {e}"}]

    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            verb = dec.func.attr.lower()
            if verb not in _HTTP_VERBS:
                continue
            http_path = ""
            if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                http_path = dec.args[0].value
            where = f"{verb.upper()} {http_path or '?'}"
            for arg in _client_params(node):
                ann = _ann_name(arg.annotation)
                if _binary_param(arg.arg) and ann not in _UPLOAD_TYPES:
                    findings.append({
                        "rule": "R2-binary-in-query", "severity": "blocking", "file": rel,
                        "line": arg.lineno,
                        "message": (f"{where}: '{arg.arg}' carries a binary payload but is not typed "
                                    "as an upload — it is bound from the query string and the upload "
                                    "cannot work")})
                elif not ann:
                    findings.append({
                        "rule": "R1-unannotated-param",
                        "severity": "blocking" if verb in _MUTATING else "major",
                        "file": rel, "line": arg.lineno,
                        "message": (f"{where}: '{arg.arg}' has no type annotation — FastAPI binds it "
                                    "from the query string and applies no validation")})
    return findings


def frontend_payload_hygiene_text(text: str, upload_params, rel: str = "") -> list:
    """Same check against page SOURCE held in memory — used by the generation loop,
    which must reject a candidate before it is ever written to disk."""
    findings = []
    for param in sorted(set(upload_params or ())):
        needle = param + "="
        start = 0
        while True:
            i = text.find(needle, start)
            if i < 0:
                break
            start = i + len(needle)
            if text.rfind("?", max(0, i - 400), i) < 0 and text.rfind("&", max(0, i - 400), i) < 0:
                continue
            findings.append({
                "rule": "F1-payload-in-url", "severity": "blocking", "file": rel,
                "line": text.count("\n", 0, i) + 1,
                "message": (f"upload parameter '{param}' is sent in the URL query string; it must go "
                            "in the request body as multipart/form-data")})
    return findings


def frontend_payload_hygiene(path: str, upload_params, rel: str = "") -> list:
    """Findings for one generated PAGE: does it send a file payload in the URL?

    The router scaffold now binds an upload parameter as multipart (UploadFile),
    but that only fixes the SERVER half. A page that still calls

        fetch(`/x/upload_image?file_data=${base64}`)

    overflows the request line and the upload fails exactly as before — the
    defect the file-upload skill and the generation prompt both warn about, and
    which the model nonetheless emits. Prompt guidance is not a guarantee, so
    this asserts it deterministically.

    `upload_params` are the parameter names the contract binds as uploads, so the
    check is driven by the contract rather than by guessing. Plain substring
    scanning — no regex.
    """
    rel = rel or os.path.basename(path)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return [{"rule": "F0-unreadable", "severity": "major", "file": rel, "line": 0,
                 "message": f"page could not be read: {e}"}]

    findings = []
    for param in sorted(set(upload_params or ())):
        needle = param + "="
        start = 0
        while True:
            i = text.find(needle, start)
            if i < 0:
                break
            start = i + len(needle)
            # only a QUERY position matters: the parameter must sit after a ? or &
            before = text.rfind("?", max(0, i - 400), i)
            amp = text.rfind("&", max(0, i - 400), i)
            if before < 0 and amp < 0:
                continue
            line = text.count("\n", 0, i) + 1
            findings.append({
                "rule": "F1-payload-in-url", "severity": "blocking", "file": rel, "line": line,
                "message": (f"upload parameter '{param}' is sent in the URL query string; it must go "
                            "in the request body as multipart/form-data (FormData + fetch body), "
                            "or the request line overflows and the upload fails")})
    return findings


def upload_endpoints(contract: dict) -> dict:
    """{endpoint_path: {"upload": [param, ...], "query": [param, ...]}} for every
    operation that takes a file. The page must append the file under the EXACT
    declared parameter name and still supply the scalar params, or FastAPI
    answers 422 — a body-not-URL fix alone does not make the upload work."""
    from . import contract_scaffold as _cs

    out = {}
    for key, v in (contract or {}).items():
        for e in v.get("exports", []) or []:
            sym = e.get("symbol")
            if not sym or e.get("kind") != "function":
                continue
            inputs = [p for p in (e.get("inputs") or []) if p.get("name")]
            up = [p["name"] for p in inputs if _cs._is_binary_param(p["name"])]
            if not up:
                continue
            out[f"/{key}/{sym}"] = {
                "upload": up,
                "query": [p["name"] for p in inputs if p["name"] not in up],
            }
    return out


def frontend_upload_fields(text: str, endpoints: dict, rel: str = "") -> list:
    """Findings for a page that posts a file under the WRONG field name.

    The payload check (F1) only proves the bytes left the URL. This proves they
    are addressed correctly: FastAPI matches a multipart part to a parameter BY
    NAME, so `form.append('file', ...)` against `upload_image(file_data: UploadFile)`
    is a 422 no matter how correctly the body is built.
    """
    findings = []
    if "FormData" not in text:
        return findings
    for path, spec in (endpoints or {}).items():
        if path not in text:
            continue
        for param in spec.get("upload", []):
            if f"append('{param}'" in text or f'append("{param}"' in text or f"append(`{param}`" in text:
                continue
            findings.append({
                "rule": "F2-upload-field-mismatch", "severity": "blocking", "file": rel,
                "line": text.count("\n", 0, max(text.find(path), 0)) + 1,
                "message": (f"posts to {path} but never appends the file under '{param}' — FastAPI "
                            f"matches a multipart part to a parameter BY NAME, so this returns 422")})
        for param in spec.get("query", []):
            if param in text:
                continue
            findings.append({
                "rule": "F3-missing-required-param", "severity": "blocking", "file": rel,
                "line": text.count("\n", 0, max(text.find(path), 0)) + 1,
                "message": (f"posts to {path} but never sends required parameter '{param}' — "
                            "the request is rejected with 422")})
    return findings


def upload_param_names(contract: dict) -> set:
    """Parameter names the API scaffold binds as uploads — the contract-driven
    input to frontend_payload_hygiene."""
    from . import contract_scaffold as _cs

    names = set()
    for v in (contract or {}).values():
        for e in v.get("exports", []) or []:
            for p in e.get("inputs", []) or []:
                if p.get("name") and _cs._is_binary_param(p["name"]):
                    names.add(p["name"])
    return names


def upload_contract_for(workspace: str) -> str:
    """Upload field-name guidance resolved from the project's handover on disk —
    for callers (the page fix-loop) that hold a workspace but no handover."""
    import json as _json

    repo = os.path.dirname(os.path.abspath(workspace))
    path = os.path.join(repo, "architecture", "planner_handover.json")
    try:
        with open(path, encoding="utf-8") as f:
            contract = (_json.load(f) or {}).get("code_contract") or {}
    except (OSError, ValueError):
        return ""
    from . import frontend as _fe

    return _fe._upload_contract_text(upload_endpoints(contract))
