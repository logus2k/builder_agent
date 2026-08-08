"""Assemble the built files into a runnable project and verify — honestly — that it runs.

Per-task building yields real files at real paths (structure.py + opencode). But an application
also needs a dependency manifest and an entrypoint that wires the pieces, and the honest question
is not "does each file parse?" but "does the thing actually start?". This stage:

  1. ensure_manifest()  — if the frame's manifest is missing, have opencode generate it from the
     code that was actually produced (real imports, not a guess).
  2. ensure_entrypoint() — if the entrypoint is missing, have opencode create it wiring the app.
  3. run_verify()       — bounded, language-aware smoke: compile everything, install deps into a
     throwaway venv, import the entrypoint. Reports what it OBSERVED — never claims "runs" on
     anything it did not actually execute.

Python-focused (this pipeline's apps are Python/FastAPI); other languages get the structural +
syntax checks and an explicit "boot smoke not implemented for <lang>" note rather than a false pass.
"""

from __future__ import annotations

import os
import subprocess
import sys

from . import opencode


def _exists(workdir: str, rel: str) -> bool:
    return bool(rel) and os.path.isfile(os.path.join(workdir, rel))


def ensure_manifest(workdir: str, skeleton: dict, log=print) -> dict:
    manifest = skeleton.get("manifest") or "requirements.txt"
    if _exists(workdir, manifest):
        return {"manifest": manifest, "action": "present"}
    task = {"title": f"dependency manifest ({manifest})", "instructions":
            (f"Scan every source file already in this project and create '{manifest}' listing "
             f"exactly the THIRD-PARTY packages they import (no standard-library modules, no app "
             f"modules). Pin nothing unless a version is required.")}
    files, _ = opencode.build_task(task, workdir, manifest, action="create", skeleton=skeleton)
    got = _exists(workdir, manifest)
    log(f"  [manifest ] {manifest} ({'created' if got else 'MISSING'})")
    return {"manifest": manifest, "action": "created" if got else "failed"}


def ensure_entrypoint(workdir: str, skeleton: dict, log=print) -> dict:
    entry = skeleton.get("entrypoint")
    if not entry or _exists(workdir, entry):
        return {"entrypoint": entry, "action": "present" if entry else "none"}
    task = {"title": f"application entrypoint ({entry})", "instructions":
            (f"Create '{entry}': the entrypoint that wires this project's modules into a runnable "
             f"{skeleton.get('stack','app')} application. Import the existing modules (routers, "
             f"models, services already in the tree) and expose the app object / main() so that "
             f"`{skeleton.get('run_cmd','')}` starts it. Real wiring, no placeholders.")}
    files, _ = opencode.build_task(task, workdir, entry, action="create", skeleton=skeleton)
    got = _exists(workdir, entry)
    log(f"  [entry    ] {entry} ({'created' if got else 'MISSING'})")
    return {"entrypoint": entry, "action": "created" if got else "failed"}


def _py_files(workdir: str) -> list[str]:
    out = []
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "venv", ".venv")]
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(root, f), workdir))
    return sorted(out)


def run_verify(workdir: str, skeleton: dict, timeout: float = 420, log=print) -> dict:
    """Bounded, honest run-smoke. Reports only what it actually executed."""
    lang = (skeleton.get("language") or "").lower()
    if lang and lang != "python":
        return {"language": lang, "runnable": "unknown",
                "note": f"boot smoke not implemented for {lang}; structure + per-file checks only"}

    pyfiles = _py_files(workdir)
    # 1. compile everything
    syntax_ok, syntax_fail = 0, []
    import py_compile
    for rel in pyfiles:
        try:
            py_compile.compile(os.path.join(workdir, rel), doraise=True)
            syntax_ok += 1
        except py_compile.PyCompileError as e:
            syntax_fail.append({"file": rel, "error": str(e)[:120]})

    result = {"language": "python", "py_files": len(pyfiles), "syntax_ok": syntax_ok,
              "syntax_fail": syntax_fail}

    manifest = skeleton.get("manifest") or "requirements.txt"
    mpath = os.path.join(workdir, manifest)
    if not os.path.isfile(mpath):
        result.update({"deps_installed": False, "boot": "skipped (no manifest)",
                       "runnable": "no" if syntax_fail else "unknown"})
        return result

    # 2. install deps into a throwaway venv
    venv = os.path.join(workdir, ".venv_smoke")
    try:
        subprocess.run([sys.executable, "-m", "venv", venv], capture_output=True, timeout=120)
        pip = os.path.join(venv, "bin", "pip")
        r = subprocess.run([pip, "install", "-q", "-r", mpath],
                           capture_output=True, text=True, timeout=timeout)
        deps_ok = r.returncode == 0
        result["deps_installed"] = deps_ok
        if not deps_ok:
            result["deps_error"] = (r.stderr or "")[-400:]
    except (subprocess.TimeoutExpired, OSError) as e:
        result.update({"deps_installed": False, "deps_error": f"{type(e).__name__}: {e}"})
        deps_ok = False

    # 3. import the entrypoint's module in the venv (bounded boot smoke)
    entry = skeleton.get("entrypoint")
    if deps_ok and entry and entry.endswith(".py"):
        mod = entry[:-3].replace("/", ".")
        py = os.path.join(venv, "bin", "python")
        try:
            r = subprocess.run([py, "-c", f"import importlib; importlib.import_module('{mod}')"],
                               capture_output=True, text=True, timeout=90, cwd=workdir)
            if r.returncode == 0:
                result["boot"] = "entrypoint imported OK"
                result["runnable"] = "likely"
            else:
                result["boot"] = "entrypoint import FAILED"
                result["boot_error"] = (r.stderr or "")[-400:]
                result["runnable"] = "no"
        except (subprocess.TimeoutExpired, OSError) as e:
            result.update({"boot": f"import smoke error: {type(e).__name__}", "runnable": "unknown"})
    else:
        result.setdefault("boot", "skipped (deps not installed)")
        result.setdefault("runnable", "no" if (syntax_fail or not deps_ok) else "unknown")

    # cleanup the throwaway venv so it isn't committed into code/
    try:
        subprocess.run(["rm", "-rf", venv], capture_output=True, timeout=30)
    except OSError:
        pass
    return result
