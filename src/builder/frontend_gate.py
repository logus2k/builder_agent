"""In-pipeline frontend gate — the browser-based validator the Builder passes to `frontend.generate`.

Same principle as the backend acceptance harness: RUN the artifact, don't trust it. Here that means
rendering each generated page in a headless browser against the RUNNING app and rejecting it on JS
console errors (undefined functions, `Failed to construct 'URL'`, blocked resources, fetches to a
wrong host, ...). A page that throws is regenerated.

Requires a browser in the image (chromium). If none is present it returns None and generation falls
back to the structural check only — so the build never hard-fails for lack of a browser, it just
loses the runtime guard (logged clearly).
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.request

from . import repair

_JS_ERR = ("Uncaught", "is not defined", "SyntaxError", "ReferenceError", "TypeError",
           "Invalid URL", "Invalid LngLat", "ERR_NAME_NOT_RESOLVED", "Failed to fetch",
           "Refused to", "net::ERR")


def _browser() -> str | None:
    for b in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        p = shutil.which(b)
        if p:
            return p
    return None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_validator(workspace: str, skeleton: dict, log=print):
    """Boot the app once and return a validate(slug, html) that renders the candidate SERVED and
    returns its JS console errors. Returns None (gate disabled) if no browser or the app won't boot."""
    browser = _browser()
    if not browser:
        log("frontend gate: no browser in image — skipping the render gate (structural check only)")
        return None
    os.makedirs(os.path.join(workspace, "frontend"), exist_ok=True)   # so StaticFiles can mount "/"
    py = repair._make_venv(workspace, skeleton.get("manifest"), log)
    if not py:
        log("frontend gate: no venv — skipping the render gate")
        return None
    port = _free_port()
    proc = subprocess.Popen([py, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=workspace, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    up = False
    for _ in range(40):
        try:
            urllib.request.urlopen(base + "/docs", timeout=2)
            up = True
            break
        except Exception:
            time.sleep(0.5)
    if not up:
        proc.send_signal(signal.SIGKILL)
        log("frontend gate: app did not boot — skipping the render gate")
        return None
    log(f"frontend gate: browser + app up on :{port} — pages will be rendered and JS-checked")
    val_rel = os.path.join(workspace, "frontend", "_val.html")

    def validate(slug: str, html: str) -> list[str]:
        try:
            with open(val_rel, "w", encoding="utf-8") as f:
                f.write(html)
            clog = os.path.join(workspace, "frontend", "_val.log")
            subprocess.run([browser, "--headless", "--disable-gpu", "--no-sandbox",
                            "--enable-logging=stderr", "--v=0", "--virtual-time-budget=4000",
                            base + "/_val.html"],
                           stderr=open(clog, "w"), stdout=subprocess.DEVNULL, timeout=45)
            errs = [l.split("CONSOLE", 1)[-1].strip()[:80]
                    for l in open(clog, encoding="utf-8", errors="ignore")
                    if "CONSOLE" in l and any(t in l for t in _JS_ERR)]
            return errs
        except Exception as ex:
            return [f"gate-error:{type(ex).__name__}"]

    validate._proc = proc          # keep a handle so the caller can shut it down
    return validate


def shutdown(validate) -> None:
    proc = getattr(validate, "_proc", None)
    if proc is not None:
        try:
            proc.send_signal(signal.SIGKILL)
        except Exception:
            pass
