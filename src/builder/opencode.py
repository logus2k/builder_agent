"""The executor: drive opencode headless to build one task's artifact with local Gemma.

`opencode run --auto -m local-llama/gemma-4 --dir <workdir>` runs non-interactively,
auto-approves edits, and writes real files. Backend is llama.cpp on :8500 (opencode config).
No Claude, offline-capable — the whole build loop is local.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time

OPENCODE = os.path.expanduser("~/.opencode/bin/opencode")
MODEL = os.environ.get("BUILDER_MODEL", "local-llama/gemma-4")


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
                 timeout: float = 420) -> tuple[list[str], dict]:
    """Run one opencode step in the project's CONTINUED session (so it accumulates context of what
    it already built) as the custom `builder` agent. `first` starts the session; later steps use
    --continue. Returns (changed_files, diag): files created/modified, plus a diagnosis dict
    (exit code, timeout flag, duration, stderr/stdout tails) so a 'no output' is never a black box."""
    os.makedirs(workdir, exist_ok=True)
    before = _snapshot(workdir)
    cmd = [OPENCODE, "run", instr, "--auto", "--agent", AGENT, "--dir", workdir]
    if not first:
        cmd.append("--continue")           # continue THIS project's session (shared context)
    if attach:
        cmd += ["--attach", attach]
    env = {**os.environ, "PATH": os.path.dirname(OPENCODE) + ":" + os.environ.get("PATH", "")}
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


def _file_instruction(path: str, tasks: list[dict], skeleton: dict | None) -> str:
    """One instruction that writes an ENTIRE file from ALL the tasks assigned to it — so a file
    with many tasks is generated in ONE coherent pass, not N fragile incremental extends (which
    corrupt large files). Same names, shared imports, no duplication across the tasks."""
    sk = skeleton or {}
    frame = (f" This is part of a {sk.get('stack') or sk.get('language','')} project; follow its "
             f"conventions ({sk.get('conventions','')}).") if sk else ""
    specs = "\n".join(f"  - {t.get('title','')}: {(t.get('instructions','') or '').strip()}"
                      for t in tasks)
    return (f"Create the file at path '{path}' (create any directories it needs) as ONE coherent, "
            f"complete module that implements ALL of the following together — shared imports, one "
            f"consistent set of names, no duplicated definitions:\n{specs}\n{frame} "
            f"If any task mentions mocking or stubbing an external service, implement a real, "
            f"configurable client/adapter instead. Write complete, working code — no placeholders, "
            f"TODOs, mocks, or simulated logic.")


def build_file(path: str, tasks: list[dict], workdir: str, skeleton: dict | None = None,
               first: bool = False, attach: str | None = None,
               timeout: float = 600) -> tuple[list[str], str]:
    """Generate the whole file `path` in one opencode pass from all `tasks` assigned to it."""
    return run_opencode(_file_instruction(path, tasks, skeleton), workdir,
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
        if files:
            return files, attempt + 1, last_diag
    return [], retries + 1, last_diag
