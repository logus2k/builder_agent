"""The executor: drive opencode headless to build one task's artifact with local Gemma.

`opencode run --auto -m local-llama/gemma-4 --dir <workdir>` runs non-interactively,
auto-approves edits, and writes real files. Backend is llama.cpp on :8500 (opencode config).
No Claude, offline-capable — the whole build loop is local.
"""

from __future__ import annotations

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


def build_task(task: dict, workdir: str, attach: str | None = None,
               timeout: float = 420) -> tuple[list[str], str]:
    """Run opencode to produce the task's deliverable in workdir. Returns (new_files, log).
    new_files = files that appeared during this run (so a shared workspace is supported)."""
    os.makedirs(workdir, exist_ok=True)
    before = set(list_files(workdir))
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
    return list_files(workdir, since=before), log


def build_with_retry(task: dict, workdir: str, retries: int = 2,
                     attach: str | None = None) -> tuple[list[str], int]:
    """Retry on no-output (builder flakiness). Returns (new_files, attempts)."""
    for attempt in range(retries + 1):
        files, _ = build_task(task, workdir, attach=attach)
        if files:
            return files, attempt + 1
    return [], retries + 1
