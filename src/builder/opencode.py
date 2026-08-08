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


def build_task(task: dict, workdir: str, attach: str | None = None,
               timeout: float = 420) -> tuple[list[str], str]:
    """Run opencode to produce the task's deliverable in workdir. Returns (changed_files, log).
    changed_files = files CREATED or MODIFIED during this run (so a shared workspace and tasks
    that edit an existing file are both supported)."""
    os.makedirs(workdir, exist_ok=True)
    before = _snapshot(workdir)
    instr = (f"{task['title']}. {task.get('instructions','')} "
             f"Produce the deliverable file named '{task['deliverable']}' with the complete, "
             f"working implementation — no placeholders, TODOs, or mock/simulated logic.")
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


def build_with_retry(task: dict, workdir: str, retries: int = 2,
                     attach: str | None = None) -> tuple[list[str], int]:
    """Retry on no-output (builder flakiness). Returns (new_files, attempts)."""
    for attempt in range(retries + 1):
        files, _ = build_task(task, workdir, attach=attach)
        if files:
            return files, attempt + 1
    return [], retries + 1
