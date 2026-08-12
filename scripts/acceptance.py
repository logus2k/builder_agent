"""Factory acceptance harness — the single, reproducible definition of "done" for a built project.

Usage (inside the builder container):  python /app/data/acceptance.py <project_id>

Reports GREEN/RED for: coherence, no-ORM, boots, serves, routes>=services, and (when the handover
carries per-element req_ids) requirement traceability. Prints a scoreboard and exits 0 iff all green.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, "/app/src")
from builder import heal, repair  # noqa: E402


def _skeleton():
    return {"language": "python", "entrypoint": "main.py", "manifest": "requirements.txt",
            "run_cmd": "uvicorn main:app --reload", "stack": "fastapi"}


def _handover(pid):
    try:
        return json.load(open(f"/app/project-repos/{pid}/architecture/planner_handover.json"))
    except Exception:
        return {}


def _traceability(pid, ho):
    """A requirement is traceable when it is realized by a contract concept OR is a legitimate
    non-backend deferral. The Architect computes this deterministically and writes the authoritative
    `traceability` block (covered_by_concept / deferred / real_gaps). Green iff no real gaps.
    Returns (ok, detail, applicable)."""
    tr = ho.get("traceability")
    if isinstance(tr, dict) and "traceable" in tr:
        gaps = tr.get("real_gaps") or []
        detail = (f"{tr.get('covered_by_concept')} by concept + {tr.get('deferred')} deferred "
                  f"of {tr.get('total')}; {len(gaps)} real gap(s) {gaps[:6]}")
        return (bool(tr.get("traceable")), detail, True)
    return (None, "no traceability block in handover (pre-Phase-1 architect run)", False)


def _functional(C, py, log=print):
    """Boot the app and REALLY exercise it — not a sampled slice:
      - call EVERY endpoint, classify each response as real-data / null / 5xx-error;
      - do a create->read ROUND-TRIP (create a record, read it back by its id);
      - confirm the web UI is served at /.
    Green requires: every create endpoint returns data, the round-trip works, the UI serves, and no
    5xx. This is the check a hollow/stubbed app fails. Returns (ok, detail, applicable)."""
    import socket
    import time
    import urllib.error

    def hit(base, path, params=None, method="POST"):
        q = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        url = f"{base}{path}" + (f"?{q}" if q else "")
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, data=b"", method=method), timeout=6)
            return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()[:100]
        except Exception as e:
            return -1, str(e)

    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([py, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=C, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        spec = None
        for _ in range(40):
            try:
                spec = json.load(urllib.request.urlopen(f"{base}/openapi.json", timeout=2)); break
            except Exception:
                time.sleep(0.5)
        if not spec:
            return (False, "app did not start", True)
        paths = spec.get("paths", {})
        eps = [(p, m, d[m]) for p, d in paths.items() for m in d
               if m in ("get", "post", "put", "delete")]

        def verb(p):
            return p.rsplit("/", 1)[-1].split("_")[0].lower()

        # create phase — call every create-style endpoint, capture the ids it mints
        created_ids, creates_ok, creates_total = [], 0, 0
        for p, m, op in eps:
            if verb(p) not in ("create", "add", "submit", "register", "new"):
                continue
            creates_total += 1
            params = {pp["name"]: "seed" for pp in op.get("parameters", []) if pp.get("in") == "query"}
            st, body = hit(base, p, params, m.upper())
            try:
                rec = json.loads(body)
            except Exception:
                rec = None
            if st == 200 and isinstance(rec, dict) and rec.get("id") is not None:
                creates_ok += 1
                created_ids.append(rec["id"])

        # round-trip — read back a created id via any get-style endpoint
        roundtrip = False
        if created_ids:
            for p, m, op in eps:
                if verb(p) not in ("get", "fetch", "retrieve", "find", "show", "read"):
                    continue
                qparams = [pp["name"] for pp in op.get("parameters", []) if pp.get("in") == "query"]
                for cid in created_ids:
                    # fill EVERY required param with the created id (missing one -> 422, not a read)
                    st, body = hit(base, p, {name: cid for name in qparams}, m.upper())
                    if st == 200 and body.strip() not in ("null", "") and str(cid) in body:
                        roundtrip = True
                        break
                if roundtrip:
                    break

        # full census — how much of the app returns real data vs null vs errors
        data = nulls = errs = 0
        for p, m, op in eps:
            params = {pp["name"]: "1" for pp in op.get("parameters", []) if pp.get("in") == "query"}
            st, body = hit(base, p, params, m.upper())
            if st >= 500 or st == -1:
                errs += 1
            elif body.strip() in ("null", ""):
                nulls += 1
            else:
                data += 1

        # web UI served at /
        st, body = hit(base, "/", method="GET")
        ui_ok = st == 200 and "openapi.json" in body

        ok = (creates_total > 0 and creates_ok == creates_total and roundtrip and ui_ok and errs == 0)
        detail = (f"creates {creates_ok}/{creates_total} | round-trip {'PASS' if roundtrip else 'FAIL'} "
                  f"| UI {'served' if ui_ok else 'MISSING'} | census: {data} data / {nulls} null / "
                  f"{errs} err of {len(eps)} endpoints")
        return (ok, detail, True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def acceptance(pid, log=print):
    C = f"/app/project-repos/{pid}/code"
    sk = _skeleton()
    ho = _handover(pid)
    r = {}

    issues = heal.detect(C, py_exe=None, skeleton=sk, sanctioned=None)
    r["coherence"] = (len(issues) == 0, f"{len(issues)} issues {heal.summarize(issues)}")

    orm = subprocess.run(["bash", "-c", f"grep -rl 'sqlalchemy\\|declarative_base' {C} --include='*.py' | wc -l"],
                         capture_output=True, text=True)
    n_orm = int((orm.stdout or "0").strip() or 0)
    r["no_orm"] = (n_orm == 0, f"{n_orm} sqlalchemy files")

    py = repair._make_venv(C, "requirements.txt", lambda m: None)
    ok, err = repair._boot(py, C, "main") if py else (False, "no venv")
    r["boots"] = (ok, "OK" if ok else repair._signature(err))

    served, nroutes = False, 0
    if ok:
        rr = subprocess.run([py, "-c", "import main; print(len(main.app.routes))"],
                            cwd=C, capture_output=True, text=True)
        try:
            nroutes = int((rr.stdout or "").strip()); served = True
        except (ValueError, TypeError):
            served = False
    nsvc = sum(1 for v in (ho.get("code_contract") or {}).values() if v.get("kind") == "service")
    r["serves"] = (served, f"app.routes={nroutes}")
    r["routes>=services"] = (served and nroutes >= nsvc, f"{nroutes} routes vs {nsvc} services")

    t_ok, t_detail, t_applies = _traceability(pid, ho)
    if t_applies:
        r["traceable"] = (t_ok, t_detail)

    # FUNCTIONAL: the app must actually DO something — endpoints return real data, not `null`. This is
    # the check that a coherent-but-hollow skeleton fails. Only meaningful once it boots.
    if served and py:
        f_ok, f_detail, f_applies = _functional(C, py, log=lambda m: None)
        if f_applies:
            r["functional"] = (f_ok, f_detail)

    gated = [v[0] for v in r.values()]
    allgreen = all(gated)
    log("=== ACCEPTANCE ===")
    for k, (okk, detail) in r.items():
        log(f"  [{'GREEN' if okk else 'RED  '}] {k}: {detail}")
    if not t_applies:
        log(f"  [ n/a ] traceable: {t_detail}")
    log(f"=== {'ALL GREEN' if allgreen else 'NOT DONE'} ===")
    repair._cleanup(C)
    return allgreen, r


if __name__ == "__main__":
    ok, _ = acceptance(sys.argv[1] if len(sys.argv) > 1 else "185d83e85fc84e15ab77796c40e22eb4")
    sys.exit(0 if ok else 1)
