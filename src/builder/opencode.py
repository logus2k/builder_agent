"""The executor: drive opencode headless to build one task's artifact with local Gemma.

`opencode run --auto -m local-llama/gemma-4 --dir <workdir>` runs non-interactively,
auto-approves edits, and writes real files. Backend is llama.cpp on :8500 (opencode config).
No Claude, offline-capable — the whole build loop is local.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.request

OPENCODE = os.path.expanduser("~/.opencode/bin/opencode")
#: The llama.cpp server opencode talks to (same host as the opencode provider baseURL).
MODEL_URL = os.environ.get("BUILDER_LLM_URL", "http://127.0.0.1:8500").rstrip("/")


def _resolve_model() -> str:
    """Use whatever CHAT model is ACTIVE on the server — never pin a specific id (a hardcoded name
    just relocates the 'model not found' failure the next time the loaded model changes). BUILDER_LLM_MODEL
    overrides; otherwise pick the primary chat model from /v1/models: exclude embedders/rerankers, then
    take the largest loaded model (the main chat model is invariably larger than any draft/embedder).
    Swapping the loaded model on the server changes the model here with no code change."""
    override = os.environ.get("BUILDER_LLM_MODEL")
    if override:
        return override
    try:
        data = json.load(urllib.request.urlopen(f"{MODEL_URL}/v1/models", timeout=5)).get("data", [])
        cands = []
        for m in data:
            mid = m.get("id", "")
            low = mid.lower()
            if any(x in low for x in ("bge", "embed", "rerank")):
                continue
            args = " ".join((m.get("status") or {}).get("args") or [])
            if "--embeddings" in args:                    # an embedding backend, not a chat model
                continue
            loaded = ((m.get("status") or {}).get("value") == "loaded")
            nparams = ((m.get("meta") or {}).get("n_params")) or 0
            cands.append((loaded, nparams, mid))
        if cands:
            cands.sort(key=lambda t: (t[0], t[1]), reverse=True)   # prefer loaded, then largest
            return cands[0][2]
    except Exception:
        pass
    return "gemma-4-12b"


#: Cached generated-config path (per build session); refreshed when a session starts (first=True).
_CFG_PATH: str | None = None


def _opencode_config_path(refresh: bool = False) -> str:
    """Write a self-contained opencode config PINNED TO THE ACTIVE MODEL and return its path, so the
    build uses whatever model is loaded — independent of any static/mounted config. Set via
    OPENCODE_CONFIG on the subprocess env. Cached across a session; refreshed when a session starts."""
    global _CFG_PATH
    if _CFG_PATH and not refresh:
        return _CFG_PATH
    model = _resolve_model()
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        # LSP OFF: pyright analysis costs ~50s/task (GPU idle) while our correctness comes from
        # run-verify + guarded repair, not the editor. Off => the build is inference-bound, not
        # blocked on a language server (which also isn't installed for every stack anyway).
        "lsp": False,
        "provider": {"local-llama": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Local llama.cpp Server",
            "options": {"baseURL": f"{MODEL_URL}/v1", "apiKey": "not-needed"},
            "models": {model: {"name": model, "reasoning": True, "tool_call": True,
                               "temperature": True, "limit": {"context": 32768, "output": 8192}}},
        }},
        "model": f"local-llama/{model}",
        "agent": {"build": {"temperature": 0}},
    }
    path = os.path.join(tempfile.gettempdir(), "builder_opencode.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    _CFG_PATH = path
    return path


def list_files(workdir: str, since: set | None = None) -> list[str]:
    """Non-empty, non-hidden files under workdir (optionally only those not in `since`)."""
    hits = []
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for f in files:
            if f.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(root, f), workdir)
            if since is not None and rel in since:
                continue
            if os.path.getsize(os.path.join(root, f)) > 0:
                hits.append(rel)
    return sorted(hits)


def _snapshot(workdir: str) -> dict[str, str]:
    """Map each non-empty, non-hidden file (relative path) to a content hash. Comparing two
    snapshots detects files that were CREATED *or* EDITED — so a task that appends to an
    existing deliverable (several tasks can share one file) is not mistaken for 'no output'."""
    snap: dict[str, str] = {}
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for f in files:
            if f.startswith("."):
                continue
            p = os.path.join(root, f)
            try:
                if os.path.getsize(p) == 0:
                    continue
                with open(p, "rb") as fh:
                    snap[os.path.relpath(p, workdir)] = hashlib.md5(fh.read()).hexdigest()
            except OSError:
                continue
    return snap


def _instruction(task: dict, target_path: str, action: str, skeleton: dict | None) -> str:
    """Build opencode's instruction: write/extend a SPECIFIC file at an agent-decided path
    within the project frame — not a flat basename. Structure is decided upstream (structure.py)."""
    sk = skeleton or {}
    frame = ""
    if sk:
        frame = (f" This is part of a {sk.get('stack') or sk.get('language','')} project; "
                 f"follow its conventions ({sk.get('conventions','')}).")
    title = task.get("title", "")
    instr = task.get("instructions", "")
    # Override any "mock/stub this" wording in the task: an external service must become a REAL,
    # configurable client/adapter (endpoint + credentials from config/env), never inline fake data.
    # This keeps the code runnable and free of stub fingerprints even when the plan says "mocked".
    no_mock = ("If the task mentions mocking, stubbing, or simulating an external service or data, "
               "do NOT inline a fake — implement a real, configurable client/adapter that reads its "
               "endpoint/credentials from config or environment and performs the actual call. "
               "Write real, working code — no placeholders, TODOs, mocks, or simulated logic.")
    if action == "extend":
        return (f"The file '{target_path}' already exists in this project. EXTEND it so it also "
                f"satisfies: {title}. {instr} Preserve the existing code and integrate cleanly "
                f"(add to it, do not rewrite unrelated parts).{frame} {no_mock}")
    return (f"Create the file at path '{target_path}' (create any directories it needs) with the "
            f"complete, working implementation for: {title}. {instr}{frame} {no_mock}")


#: The custom opencode agent (temperature 0, build-focused prompt) written into the workspace by
#: context.write_project_context. Every task runs as this agent so the build is one coherent persona.
AGENT = os.environ.get("BUILDER_AGENT_NAME", "builder")


def run_opencode(instr: str, workdir: str, first: bool = False, attach: str | None = None,
                 timeout: float = 420, agent: str | None = None) -> tuple[list[str], dict]:
    """Run one opencode step in the project's CONTINUED session (so it accumulates context of what
    it already built) as the custom `builder` agent. `first` starts the session; later steps use
    --continue. Returns (changed_files, diag): files created/modified, plus a diagnosis dict
    (exit code, timeout flag, duration, stderr/stdout tails) so a 'no output' is never a black box."""
    os.makedirs(workdir, exist_ok=True)
    before = _snapshot(workdir)
    cmd = [OPENCODE, "run", instr, "--auto", "--agent", agent or AGENT, "--dir", workdir]
    if not first:
        cmd.append("--continue")           # continue THIS project's session (shared context)
    if attach:
        cmd += ["--attach", attach]
    # OPENCODE_CONFIG points opencode at a generated config pinned to the ACTIVE model (model-agnostic):
    # refresh it when a session starts (first) so a mid-session model swap is not required, and reuse the
    # cached one for the continued steps.
    env = {**os.environ, "PATH": os.path.dirname(OPENCODE) + ":" + os.environ.get("PATH", ""),
           "OPENCODE_CONFIG": _opencode_config_path(refresh=first)}
    diag = {"exit": None, "timeout": False, "dur": 0.0, "stderr": "", "stdout": ""}
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        diag["exit"] = r.returncode
        diag["stdout"] = (r.stdout or "")[-1500:]
        diag["stderr"] = (r.stderr or "")[-1500:]
    except subprocess.TimeoutExpired as e:
        diag["timeout"] = True
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        diag["stdout"], diag["stderr"] = out[-1500:], err[-1500:]
    diag["dur"] = round(time.time() - t0, 1)
    after = _snapshot(workdir)
    changed = sorted(rel for rel, sig in after.items() if before.get(rel) != sig)
    return changed, diag


def build_task(task: dict, workdir: str, target_path: str, action: str = "create",
               skeleton: dict | None = None, first: bool = False, attach: str | None = None,
               timeout: float = 420) -> tuple[list[str], str]:
    """Produce/extend the task's file at `target_path` in the continued session."""
    return run_opencode(_instruction(task, target_path, action, skeleton), workdir,
                        first=first, attach=attach, timeout=timeout)


def build_with_retry(task: dict, workdir: str, target_path: str, action: str = "create",
                     skeleton: dict | None = None, first: bool = False, retries: int = 2,
                     attach: str | None = None) -> tuple[list[str], int]:
    """Retry on no-output. Returns (changed_files, attempts). Retries continue the same session."""
    for attempt in range(retries + 1):
        files, _ = build_task(task, workdir, target_path, action=action, skeleton=skeleton,
                              first=first and attempt == 0, attach=attach)
        if files:
            return files, attempt + 1
    return [], retries + 1


def _file_instruction(path: str, tasks: list[dict], skeleton: dict | None, exists: bool = False) -> str:
    """One instruction that writes an ENTIRE file from ALL the tasks assigned to it — so a file
    with many tasks is generated in ONE coherent pass, not N fragile incremental extends (which
    corrupt large files). Same names, shared imports, no duplication across the tasks. When the file
    already exists as a CONTRACT SCAFFOLD, the instruction becomes fill-the-bodies-preserving-the-
    interface, so the imports/exports the rest of the app relies on are not altered."""
    sk = skeleton or {}
    frame = (f" This is part of a {sk.get('stack') or sk.get('language','')} project; follow its "
             f"conventions ({sk.get('conventions','')}).") if sk else ""
    specs = "\n".join(f"  - {t.get('title','')}: {(t.get('instructions','') or '').strip()}"
                      for t in tasks)
    no_mock = ("If any task mentions mocking or stubbing an external service, implement a real, "
               "configurable client/adapter instead. Write complete, working code — no placeholders, "
               "TODOs, mocks, or simulated logic. This is a BACKEND Python module: write ONLY server-"
               "side Python. If a task describes a frontend/UI artifact (a React/Vue/SPA component, a "
               "map widget, HTML/CSS/JS, or a browser API client), implement ONLY its backend counterpart"
               " — the endpoint/service/data the UI needs — and put NO HTML, CSS, JS, JSX or TypeScript "
               "in this file (the UI is generated separately). For data access use the in-memory "
               "repository `repo(name)` from app.repositories.store; do NOT introduce SQLAlchemy or any "
               "ORM. Preserve the module's existing imports and public names exactly.")
    if exists:
        return (f"IMPLEMENT the stub functions in the single file '{path}'. Each body is currently "
                f"`raise NotImplementedError(...)`; use the `edit` tool to REPLACE every one with the real "
                f"implementation the tasks below require (validation, the repo() queries/filtering/nesting, "
                f"orchestration, error handling). Work ONLY inside this file.\n{specs}\n{frame}\n"
                f"EFFICIENCY (important — keep the context small): do NOT `read` whole dependency files. To "
                f"check a symbol, signature or field, use `grep`/`glob` for that specific name only. You "
                f"already know the data seam: `repo(name).create(dict)/get(id)/list()/update(id,dict)/"
                f"delete(id)` from app.repositories.store.\nCRITICAL: keep every existing function/class "
                f"NAME, parameter list and import EXACTLY as written (other modules import them by these "
                f"names); rewrite only the BODIES and add internal helpers as needed. Implement a task's "
                f"operation inside the existing function it maps to — never add a divergently-named "
                f"duplicate. {no_mock}")
    return (f"Create the file at path '{path}' (create any directories it needs) as ONE coherent, "
            f"complete module that implements ALL of the following together — shared imports, one "
            f"consistent set of names, no duplicated definitions:\n{specs}\n{frame} {no_mock}")


def _stub_instruction(path: str, tasks: list[dict]) -> str:
    """A SHORT, self-contained stub-completion prompt. Deliberately does NOT dump the task titles:
    MEASURED, the Planner's task names ('getMenuStructure') often do not match the contract function
    names ('get_menu_details'), so listing them makes the model hunt for functions that aren't there,
    read the whole tree, and never edit. The function SIGNATURES in the file are self-describing for the
    CRUD the repo() seam supports, which is what actually gets kept."""
    return (f"Implement every stub (a `raise NotImplementedError` body) in the file: {path}. "
            f"The function NAME and its parameters tell you what it does. Replace each stub with real, "
            f"working logic using the data seam `repo(name)` from app.repositories.store — "
            f"`.create(dict)` (store a FULL dict of the params), `.get(id)`, `.list()` (filter/nest in "
            f"Python), `.update(id, dict)`, `.delete(id)`. Use `grep`/`glob` ONLY to confirm a specific "
            f"symbol; do NOT read whole files. Apply changes with `edit`. Keep every existing function "
            f"name, parameter list and import EXACTLY; just fill the bodies.")


def _fill_stubs(path: str, workdir: str) -> tuple[list[str], dict]:
    """Fill a scaffold's `raise NotImplementedError` stubs via a DIRECT model completion — the frontend's
    reliable pattern — instead of opencode's agentic edit loop, which (MEASURED) reads models, greps, and
    then never edits a non-trivial service. The model returns the COMPLETE module; we validate it (parses,
    keeps every original def/class name, no leftover stub) and write it, else keep the scaffold untouched.
    Returns (changed_files, diag)."""
    import ast as _ast
    full = os.path.join(workdir, path)
    try:
        before = open(full, encoding="utf-8").read()
    except OSError:
        return [], {"exit": 1, "reason": "unreadable"}
    if "raise NotImplementedError" not in before:
        return [], {"skipped": True, "reason": "data-only scaffold (no stubs to implement)"}
    orig_names = {n.name for n in _ast.walk(_ast.parse(before))
                  if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef))}
    prompt = ("Implement this Python module: replace every `raise NotImplementedError(...)` body with "
              "real, working logic. Use the data seam `repo(name)` from app.repositories.store — "
              "`.create(dict)` (store a full dict built from the parameters), `.get(id)`, `.list()` "
              "(filter/nest in Python), `.update(id, dict)`, `.delete(id)`. Keep EVERY function name, "
              "parameter list, class and import EXACTLY as given; fill only the bodies, add small internal "
              "helpers only if needed. Output ONLY the complete Python file — no markdown fences, no prose.\n\n"
              + before)
    body = json.dumps({"model": _resolve_model(), "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0.2, "max_tokens": 8192,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    t0 = time.time()
    try:
        req = urllib.request.Request(f"{MODEL_URL}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        code = json.load(urllib.request.urlopen(req, timeout=180))["choices"][0]["message"]["content"]
    except Exception as e:
        return [], {"exit": 1, "dur": round(time.time() - t0, 1), "reason": f"model error: {type(e).__name__}"}
    if "</think>" in code:
        code = code.rsplit("</think>", 1)[1]
    if "```" in code:                                    # strip a markdown fence if present
        parts = code.split("```")
        code = parts[1] if len(parts) > 1 else code
        if code.lower().lstrip().startswith("python"):
            code = code.lstrip()[6:]
    code = code.strip()
    dur = round(time.time() - t0, 1)
    try:
        new_names = {n.name for n in _ast.walk(_ast.parse(code))
                     if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef))}
    except SyntaxError:
        return [], {"exit": 1, "dur": dur, "reason": "model output does not parse"}
    if "raise NotImplementedError" in code:
        return [], {"exit": 1, "dur": dur, "reason": "stubs still unimplemented"}
    if not orig_names.issubset(new_names):               # must preserve the interface others import
        return [], {"exit": 1, "dur": dur, "reason": f"dropped symbols: {sorted(orig_names - new_names)}"}
    with open(full, "w", encoding="utf-8") as f:
        f.write(code + "\n")
    return [path], {"exit": 0, "dur": dur}


def build_file(path: str, tasks: list[dict], workdir: str, skeleton: dict | None = None,
               first: bool = False, attach: str | None = None,
               timeout: float = 600) -> tuple[list[str], str]:
    """Generate the whole file `path` in one opencode pass from all `tasks` assigned to it. If the
    file already exists as a contract scaffold, implement its stub bodies with the focused `stubs`
    SUBAGENT (a short prompt, its own fresh context) — never the primary agent + a huge merged spec,
    which reads the whole tree, compacts, and edits nothing. New files use the primary builder agent."""
    full = os.path.join(workdir, path)
    exists = os.path.isfile(full) and os.path.getsize(full) > 0
    if exists:
        # A contract scaffold: fill its stub bodies with a DIRECT model completion (reliable) rather than
        # opencode's agentic edit loop (which explores the tree and often never edits). _fill_stubs also
        # returns a fast skip for a data-only module (no stubs), so entities cost ~0s.
        return _fill_stubs(path, workdir)
    return run_opencode(_file_instruction(path, tasks, skeleton, exists=exists), workdir,
                        first=first, attach=attach, timeout=timeout)


def build_file_with_retry(path: str, tasks: list[dict], workdir: str, skeleton: dict | None = None,
                          first: bool = False, retries: int = 2,
                          attach: str | None = None) -> tuple[list[str], int, dict]:
    """Returns (changed_files, attempts, last_diag). last_diag carries the final attempt's opencode
    diagnosis so a no-output file is explainable (exit/timeout/stderr), not a silent gap."""
    last_diag: dict = {}
    for attempt in range(retries + 1):
        files, last_diag = build_file(path, tasks, workdir, skeleton=skeleton,
                                      first=first and attempt == 0, attach=attach)
        if files or last_diag.get("skipped"):     # a deliberate skip is not a failure — do not retry
            return files, attempt + 1, last_diag
    return [], retries + 1, last_diag
