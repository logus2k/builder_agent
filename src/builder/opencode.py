"""The executor: drive opencode headless to build one task's artifact with local Gemma.

`opencode run --auto -m local-llama/gemma-4 --dir <workdir>` runs non-interactively,
auto-approves edits, and writes real files. Backend is llama.cpp on :8500 (opencode config).
No Claude, offline-capable — the whole build loop is local.
"""

from __future__ import annotations

import hashlib
import os
import subprocess

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
    if action == "extend":
        return (f"The file '{target_path}' already exists in this project. EXTEND it so it also "
                f"satisfies: {title}. {instr} Preserve the existing code and integrate cleanly "
                f"(add to it, do not rewrite unrelated parts).{frame} "
                f"Write real, working code — no placeholders, TODOs, or mock/simulated logic.")
    return (f"Create the file at path '{target_path}' (create any directories it needs) with the "
            f"complete, working implementation for: {title}. {instr}{frame} "
            f"No placeholders, TODOs, or mock/simulated logic.")


def build_task(task: dict, workdir: str, target_path: str, action: str = "create",
               skeleton: dict | None = None, attach: str | None = None,
               timeout: float = 420) -> tuple[list[str], str]:
    """Run opencode to produce/extend the task's file at `target_path` (relative to workdir).
    Returns (changed_files, log) — files CREATED or MODIFIED during this run (so a shared
    workspace, subdirectories, and tasks that extend an existing file are all supported)."""
    os.makedirs(workdir, exist_ok=True)
    before = _snapshot(workdir)
    instr = _instruction(task, target_path, action, skeleton)
    cmd = [OPENCODE, "run", instr, "--auto", "-m", MODEL, "--dir", workdir]
    if attach:
        cmd += ["--attach", attach]
    env = {**os.environ, "PATH": os.path.dirname(OPENCODE) + ":" + os.environ.get("PATH", "")}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        log = (r.stdout or "")[-1500:]
    except subprocess.TimeoutExpired:
        log = "TIMEOUT"
    after = _snapshot(workdir)
    changed = sorted(rel for rel, sig in after.items() if before.get(rel) != sig)
    return changed, log


def build_with_retry(task: dict, workdir: str, target_path: str, action: str = "create",
                     skeleton: dict | None = None, retries: int = 2,
                     attach: str | None = None) -> tuple[list[str], int]:
    """Retry on no-output (builder flakiness). Returns (changed_files, attempts)."""
    for attempt in range(retries + 1):
        files, _ = build_task(task, workdir, target_path, action=action,
                              skeleton=skeleton, attach=attach)
        if files:
            return files, attempt + 1
    return [], retries + 1
