"""Generic, guarded run-verify -> repair loop. PROJECT-AGNOSTIC.

After the tasks are built, the app is booted (install the manifest into a throwaway venv, then
`import <entrypoint_module>`); on failure the real error is fed back into the SAME opencode
session to fix, then re-booted — until it runs or the guards stop it. Guards (all cheap): targeted
single-file repair (offender = deepest PROJECT frame; vendored dirs excluded), a syntax gate
(revert any round that introduces a SyntaxError), revert-on-regression (whole-tree snapshot +
importable-module count), and a no-progress bound. No regex.

The entrypoint + manifest come from the build frame (skeleton) — nothing here is project-specific.
Python only for now (other languages report runnable="unknown" honestly, like assemble.run_verify).
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import context, opencode

#: Cached venvs (keyed by manifest content hash) live here, OUTSIDE the workspace, so they persist
#: across builds and are never committed to the repo. Creating a venv + full reinstall every build
#: is the run-verify tail; a warm cache turns it into a fast pip "already satisfied" check.
_VENV_CACHE = os.path.join(
    os.environ.get("BUILDER_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "data")),
    "venvs")

SKIP = {"__pycache__", ".venv_build", ".venv", "venv", ".git", "node_modules",
        "site-packages", ".opencode"}


def _src_files(ws: str) -> list[str]:
    out = []
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(root, f), ws))
    return sorted(out)


def _as_module(rel: str) -> str:
    m = rel[:-3].replace("/", ".")
    return m[:-9] if m.endswith(".__init__") else m


def _count_importable(py: str, ws: str) -> int:
    """How many source modules import cleanly — the regression health signal. One subprocess
    (not one per file) to keep the loop cheap over many files × rounds."""
    mods = [_as_module(r) for r in _src_files(ws)]
    if not mods:
        return 0
    script = ("import importlib\nok=0\nfor m in %r:\n"
              "    try:\n        importlib.import_module(m); ok+=1\n"
              "    except Exception:\n        pass\nprint(ok)" % mods)
    r = subprocess.run([py, "-c", script], cwd=ws, capture_output=True, text=True)
    try:
        return int((r.stdout or "0").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def _all_compile(py: str, ws: str) -> bool:
    """Syntax gate — compile every source file in ONE call."""
    files = _src_files(ws)
    if not files:
        return True
    return subprocess.run([py, "-m", "py_compile", *files], cwd=ws, capture_output=True).returncode == 0


def _snapshot(ws: str) -> dict:
    return {r: open(os.path.join(ws, r)).read() for r in _src_files(ws)}


def _restore(ws: str, snap: dict) -> None:
    for r in _src_files(ws):
        if r not in snap:
            try:
                os.remove(os.path.join(ws, r))
            except OSError:
                pass
    for r, c in snap.items():
        try:
            open(os.path.join(ws, r), "w").write(c)
        except OSError:
            pass


def _boot(py: str, ws: str, entry: str) -> tuple[bool, str]:
    r = subprocess.run([py, "-c", f"import {entry}\nprint('BOOT OK')"], cwd=ws,
                       capture_output=True, text=True)
    return (r.returncode == 0 and "BOOT OK" in r.stdout), r.stderr


def _offender(ws: str, stderr: str) -> str | None:
    off = None
    for line in stderr.splitlines():
        s = line.strip()
        if s.startswith('File "'):
            path = s.split('"')[1]
            if not path.startswith(ws) or not path.endswith(".py"):
                continue
            rel = os.path.relpath(path, ws)
            if any(part in SKIP for part in rel.split(os.sep)):
                continue
            off = rel
    return off


def _signature(stderr: str) -> str:
    lines = [l for l in stderr.strip().splitlines() if l.strip()]
    return lines[-1][:160] if lines else ""


def _repair(ws: str, rel: str | None, err: str, entry: str) -> None:
    if rel:
        instr = (f"The file '{rel}' has a bug. Running the app (`import {entry}`) fails with:\n\n"
                 f"{err[-1000:]}\n\nFix '{rel}' (and only if strictly necessary, the file that owns "
                 f"the missing symbol) so the whole app imports and runs. Follow AGENTS.md. No placeholders.")
    else:
        instr = (f"The app does not run (`import {entry}`) — error:\n\n{err[-1000:]}\n\n"
                 f"Fix the necessary file(s) so the whole app imports and runs. Follow AGENTS.md. No placeholders.")
    opencode.run_opencode(instr, ws, first=False)     # continue the build session


def _make_venv(ws: str, manifest: str | None, log) -> str | None:
    """A venv with the project's deps — CACHED by the manifest's content hash and reused across
    builds. Creating a venv + reinstalling on every build is the run-verify tail; here it's a
    one-time cost per distinct manifest, then a fast pip 'already satisfied' verify."""
    mpath = os.path.join(ws, manifest) if manifest else None
    try:
        mtext = open(mpath).read() if (mpath and os.path.isfile(mpath)) else ""
    except OSError:
        mtext = ""
    key = hashlib.md5(mtext.encode()).hexdigest()[:12]
    venv = os.path.join(_VENV_CACHE, key)
    py = os.path.join(venv, "bin", "python")
    if not os.path.exists(py):
        os.makedirs(_VENV_CACHE, exist_ok=True)
        try:
            subprocess.run([sys.executable, "-m", "venv", venv], capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"  run-verify: venv create failed ({type(e).__name__})"); return None
        log(f"  run-verify: new venv (manifest {key})")
    else:
        log(f"  run-verify: reusing cached venv ({key})")
    if mpath and os.path.isfile(mpath):
        r = subprocess.run([os.path.join(venv, "bin", "pip"), "install", "-q", "-r", mpath],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            log(f"  run-verify: pip install had errors: {(r.stderr or '')[-200:]}")
    return py


def run_verify_and_repair(ws: str, skeleton: dict, max_rounds: int = 5, log=print) -> dict:
    """Boot the built app; guardedly repair until it runs. Returns a report dict."""
    lang = (skeleton.get("language") or "python").lower()
    entry = context.entrypoint_module(skeleton)
    if lang != "python" or not entry:
        return {"language": lang, "entry": entry, "runnable": "unknown",
                "note": f"run-verify not implemented for language={lang!r}, entry={entry!r}"}

    py = _make_venv(ws, skeleton.get("manifest"), log)
    if not py:
        return {"language": lang, "entry": entry, "runnable": "unknown", "note": "no venv"}

    manifest = skeleton.get("manifest") or "requirements.txt"
    mpath = os.path.join(ws, manifest)

    def _manifest_text():
        try:
            return open(mpath).read()
        except OSError:
            return ""

    last_manifest = _manifest_text()

    rounds, reverts, stuck = 0, [], {}
    final_err = ""
    for rnd in range(1, max_rounds + 1):
        ok, err = _boot(py, ws, entry)
        if ok:
            log(f"  run-verify: BOOT OK (entry {entry}, {rounds} repairs)")
            smoke = server_smoke(py, ws, skeleton, log=log)
            _cleanup(ws)
            return {"language": "python", "entry": entry, "runnable": "yes",
                    "repairs": rounds, "reverts": reverts, "server_smoke": smoke}
        final_err = err
        rel, sig = _offender(ws, err), _signature(err)
        log(f"  run-verify round {rnd}: offender={rel} :: {sig}")
        if stuck.get(sig, 0) >= 3:
            log("  run-verify: no progress; stopping"); break
        snap, before = _snapshot(ws), _count_importable(py, ws)
        _repair(ws, rel, err, entry); rounds += 1
        # A repair may fix a MISSING DEPENDENCY by adding it to the manifest — that can only take
        # effect if we reinstall (editing .py files never installs a package). Reinstall on change.
        if _manifest_text() != last_manifest:
            last_manifest = _manifest_text()
            subprocess.run([os.path.join(os.path.dirname(py), "pip"), "install", "-q", "-r", mpath],
                           capture_output=True, timeout=600)
            log("  reinstalled deps (manifest changed by repair)")
        if not _all_compile(py, ws):
            _restore(ws, snap); reverts.append({"round": rnd, "why": "syntax"}); stuck[sig] = stuck.get(sig, 0) + 1
            log("  reverted: introduced a SyntaxError"); continue
        after = _count_importable(py, ws)
        if after < before:
            _restore(ws, snap); reverts.append({"round": rnd, "why": "regression"}); stuck[sig] = stuck.get(sig, 0) + 1
            log(f"  reverted: regression (importable {before}->{after})"); continue
        _, err2 = _boot(py, ws, entry)
        if _signature(err2) == sig:
            stuck[sig] = stuck.get(sig, 0) + 1

    _cleanup(ws)
    return {"language": "python", "entry": entry, "runnable": "no", "repairs": rounds,
            "reverts": reverts, "final_error": _signature(final_err)}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def server_smoke(py: str, ws: str, skeleton: dict, wait: float = 8.0, log=print) -> dict:
    """Actually START the app (if it's a server) and confirm it comes up, then tear it down.
    Non-fatal — a richer signal than 'imports'. Generic: uses the frame's run_cmd; only handles
    uvicorn-style servers, otherwise reports attempted=False honestly."""
    run_cmd = skeleton.get("run_cmd") or ""
    if "uvicorn" not in run_cmd:
        return {"attempted": False, "reason": f"run_cmd not a uvicorn server: {run_cmd!r}"}
    target = next((t for t in run_cmd.split() if ":" in t and "/" not in t and "//" not in t), None)
    if not target:
        return {"attempted": False, "reason": "no app target (module:app) in run_cmd"}
    uvicorn_bin = os.path.join(os.path.dirname(py), "uvicorn")
    base = [uvicorn_bin] if os.path.exists(uvicorn_bin) else [py, "-m", "uvicorn"]
    port = _free_port()
    proc = subprocess.Popen(base + [target, "--host", "127.0.0.1", "--port", str(port)],
                            cwd=ws, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    started, status, t0 = False, None, time.time()
    while time.time() - t0 < wait:
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as r:
                status, started = r.status, True
                break
        except urllib.error.HTTPError as e:      # server answered (even 404) = it's up
            status, started = e.code, True
            break
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    if proc.poll() is None and not started:      # process alive, port slow — count as up
        started = True
    err = ""
    if proc.poll() is not None and not started:
        err = (proc.stdout.read() or "")[-400:]
    try:
        proc.terminate(); proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        proc.kill()
    log(f"  server-smoke: started={started} http_status={status}")
    return {"attempted": True, "started": started, "http_status": status,
            "error": err or None}


def _cleanup(ws: str) -> None:
    subprocess.run(["rm", "-rf", os.path.join(ws, ".venv_build")], capture_output=True)
