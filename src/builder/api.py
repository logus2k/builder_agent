"""Builder HTTP service — mirrors the Analyst/Architect/Planner job pattern.

The Builder was a batch CLI (`scripts/build_plan.py`). This thin server exposes the same run
as an async job so FACTORY (reqoach) can trigger it and poll progress, exactly like the
Planner's `planner:run`:

  POST /projects/{pid}/builder:run   -> start a run, returns {job_id}
  GET  /jobs/{job_id}                -> status snapshot (status, stage, progress, error)
  GET  /health

A run: read the Planner's plan.json from the project repo's `plans/` area -> build each
feasible task in dependency order with opencode (local Gemma) -> verify deterministically ->
publish the produced files into the project repo's `code/` area so FACTORY commits them
(mirrors how the Planner publishes `plans/`). Each run executes in a worker thread (many
opencode calls); the JobManager keeps a snapshot the UI polls. No socket.io — polling matches
the Overview's poller.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, Request

from . import build as build_mod

__version__ = "0.1.0"

#: Where the Planner's plan.json lives in the project repo (same single-owner layout the
#: Analyst/Architect/Planner use: requirements/ architecture/ plans/ code/).
REPOS_ROOT = build_mod.REPOS_ROOT


def _plan_path(pid: str) -> str:
    return os.path.join(REPOS_ROOT, pid, "plans", "plan.json")


@dataclass
class Job:
    job_id: str
    project_id: str
    actor: str | None = None               # authenticated email (forwarded by the edge on write)
    status: str = "queued"                 # queued | running | done | error
    stage: str | None = None
    progress: dict = field(default_factory=dict)
    error: str | None = None
    result: dict | None = None
    started_at: float | None = None

    def snapshot(self) -> dict:
        return {"job_id": self.job_id, "project_id": self.project_id, "kind": "builder",
                "status": self.status, "stage": self.stage, "progress": self.progress,
                "error": self.error, "result": self.result,
                "elapsed_s": round(time.time() - self.started_at) if self.started_at else None}


_FACTORY_RELAY = os.environ.get("FACTORY_RELAY_URL", "http://localhost:7803").rstrip("/")


def _factory_emit(pid: str, stage: str, status: str = "progress",
                  message: str | None = None, progress: dict | None = None) -> None:
    """Best-effort: push a stage event to the FACTORY socket.io hub (the Analyst relay) so the pipeline
    Overview's DEVELOPMENT lane updates LIVE instead of by polling. Never breaks a run on telemetry."""
    import json as _json
    import urllib.request as _u
    try:
        data = _json.dumps({"lane": "development", "stage": stage, "status": status,
                            "message": message, "progress": progress}).encode()
        _u.urlopen(_u.Request(f"{_FACTORY_RELAY}/relay/{pid}", data=data,
                              headers={"Content-Type": "application/json"}, method="POST"), timeout=2)
    except Exception:
        pass


class JobManager:
    """Owns builder jobs; runs each in a worker thread. Live lane updates over socket.io via the
    Analyst relay (see _factory_emit); the job snapshot is also kept for replay/robustness."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    def _run_builder(self, job: Job, cap: int | None, retries: int) -> None:
        job.status = "running"
        job.started_at = time.time()
        _factory_emit(job.project_id, "backend", "running", "Build started")
        try:
            job.stage = "fetch"
            job.progress = {"stage": "fetch", "status": "progress"}
            path = _plan_path(job.project_id)
            if not os.path.isfile(path):
                raise FileNotFoundError(f"no plan.json for project (expected {path}); run the Planner first")
            with open(path, encoding="utf-8") as f:
                plan = json.load(f)

            # Build into the repo's code/ area (same repo the other agents publish to).
            workspace = build_mod.repo_code_workspace(job.project_id)

            # The build log drives the progress bar. Two-phase build: it logs
            # "placed N tasks into M files" (M = total to generate), then one "[outcome] path (…)"
            # line per FILE as it is generated.
            # Two DISTINCT development stages surfaced to the FACTORY Overview: BACKEND (build the
            # API from the plan) then FRONTEND (generate the UI). The Overview's Development lane reads
            # job.stage + job.progress, so each shows as its own activity with its own done/total.
            counter = {"done": 0, "total": 0, "phase": "backend", "retries": 0,
                       "fe_done": 0, "fe_total": 0}

            def _log(msg: str = "") -> None:
                st = str(msg).strip()
                if st.startswith("frontend:") and "derived" in st and "page" in st:
                    # "frontend: derived N page(s) from aspects: [...]"
                    parts = st.split()
                    n = parts[parts.index("derived") + 1] if "derived" in parts else "0"
                    counter["phase"] = "frontend"
                    counter["fe_total"] = int(n) if n.isdigit() else 0
                    job.stage = "frontend"
                    job.progress = {"stage": "frontend", "status": "progress",
                                    "done": 0, "total": counter["fe_total"]}
                elif st.startswith("frontend stage:"):
                    counter["phase"] = "frontend"
                    job.stage = "frontend"
                    job.progress = {"stage": "frontend", "status": "progress",
                                    "done": counter["fe_done"], "total": counter["fe_total"], "last": st[:120]}
                elif st.startswith("[frontend]") and ".html" in st:
                    counter["fe_done"] += 1
                    job.progress = {"stage": "frontend", "status": "progress",
                                    "done": counter["fe_done"], "total": counter["fe_total"], "last": st[:120]}
                elif st.startswith("placed ") and "into" in st and "file" in st:
                    parts = st.split()
                    if "into" in parts:
                        n = parts[parts.index("into") + 1]
                        counter["total"] = int(n) if n.isdigit() else counter["total"]
                    job.progress = {"stage": "backend", "status": "progress",
                                    "done": 0, "total": counter["total"]}
                elif counter["phase"] == "backend" and (st.startswith("[built") or
                        st.startswith("[no_output") or st.startswith("[failed")):
                    # Count only FILE-OUTCOME lines (not [scaffold]/[heal]/etc.), so done ≤ total files.
                    counter["done"] += 1
                    if "retries=" in st:      # report retries transparently, never hide them
                        try:
                            counter["retries"] += int(st.split("retries=")[1].split(")")[0].strip())
                        except (ValueError, IndexError):
                            pass
                    job.progress = {"stage": "backend", "status": "progress",
                                    "done": counter["done"], "total": counter["total"],
                                    "retries": counter["retries"], "last": st[:120]}
                # Push a live lane update only when the stage or the done-count actually changed
                # (once per file + at stage boundaries) — not on every log line — to keep the relay light.
                sig = (job.stage, (job.progress or {}).get("done"))
                if sig != counter.get("_sig"):
                    counter["_sig"] = sig
                    _factory_emit(job.project_id, job.stage, "progress", st[:100], job.progress)

            job.stage = "backend"
            job.progress = {"stage": "backend", "status": "progress", "done": 0, "total": 0}
            report = build_mod.build_plan(plan, workspace, cap=cap, retries=retries, log=_log)

            job.stage = "publish"
            job.progress = {"stage": "publish", "status": "progress"}
            commit = build_mod.publish_to_repo(job.project_id)

            summary = report.get("summary", {})
            job.result = {**summary, "workspace": report.get("workspace"),
                          "repo": commit}
            job.status = "done"
            job.stage = "done"
            job.progress = {"stage": "done", "status": "done", **summary}
            _factory_emit(job.project_id, "done", "done",
                          f"{summary.get('built', 0)} built · runnable {summary.get('runnable', '?')}", summary)
        except Exception as e:  # noqa: BLE001 — surface any pipeline failure to the client
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            _factory_emit(job.project_id, "error", "error", job.error)

    def create_builder_run(self, pid: str, actor: str | None = None,
                           cap: int | None = None, retries: int = 2) -> Job:
        job = Job(job_id=uuid.uuid4().hex, project_id=pid, actor=actor)
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run_builder, args=(job, cap, retries), daemon=True).start()
        return job


api = FastAPI(title="builder-agent", version=__version__)
jm = JobManager()


@api.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "builder-agent", "version": __version__,
            "jobs": len(jm.jobs)}


@api.post("/projects/{pid}/builder:run")
def builder_run(pid: str, request: Request, cap: int | None = None, retries: int = 2) -> dict:
    """Start a build run for a project. Returns the job id to poll. Optional `cap` limits how
    many feasible tasks are built (for a quick pass); `retries` is opencode's no-output retry."""
    actor = request.headers.get("x-auth-request-email")
    job = jm.create_builder_run(pid, actor=actor, cap=cap, retries=retries)
    return {"job_id": job.job_id, "project_id": pid, "status": job.status}


@api.post("/projects/{pid}/builder:fix")
async def builder_fix(pid: str, request: Request) -> dict:
    """Testing->Development feedback loop: fix functional-test failures. Body:
    `{failures: [{endpoint, detail, traceback}], report: "<tester report>"}`. Each failure is fixed
    ONE at a time via direct-completion (build.fix_failures) and `code/` is republished. Synchronous
    (fast — ~1s/fix). Returns {fixed, skipped, repo}. The tester posts one issue per call so each is
    solved individually with the report attached (per the feedback-loop design)."""
    body = await request.json()
    failures = body.get("failures") or []
    report = body.get("report") or ""
    if not failures:
        raise HTTPException(400, "no failures provided")
    _factory_emit(pid, "backend", "running", f"Fixing {len(failures)} test failure(s)")
    try:
        out = build_mod.fix_failures(pid, failures, report=report)
    except Exception as e:  # noqa: BLE001
        _factory_emit(pid, "error", "error", f"{type(e).__name__}: {e}")
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    nf, ns = len(out.get("fixed", [])), len(out.get("skipped", []))
    _factory_emit(pid, "done", "done", f"{nf} fixed · {ns} skipped", {"fixed": nf, "skipped": ns})
    return {"project_id": pid, **out}


@api.post("/projects/{pid}/builder:fix-page")
async def builder_fix_page(pid: str, request: Request) -> dict:
    """Testing->Development FRONTEND loop: regenerate one flagged page. Body `{slug, report}`. Synchronous
    (~one page). The tester posts one flagged page per call with its failure report (dead button / dead
    link / invented endpoint / non-completing journey); the page is rebuilt with skills + guardrail."""
    body = await request.json()
    slug = (body.get("slug") or "").strip()
    report = body.get("report") or ""
    if not slug:
        raise HTTPException(400, "no slug provided")
    _factory_emit(pid, "frontend", "running", f"Fixing page {slug}")
    try:
        out = build_mod.fix_page(pid, slug, report)
    except Exception as e:  # noqa: BLE001
        _factory_emit(pid, "error", "error", f"{type(e).__name__}: {e}")
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    _factory_emit(pid, "done", "done", f"page {slug} regenerated={out.get('regenerated')}")
    return {"project_id": pid, **out}


@api.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = jm.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job.snapshot()
