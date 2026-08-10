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


class JobManager:
    """Owns builder jobs; runs each in a worker thread. Polling-only (no socket.io)."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    def _run_builder(self, job: Job, cap: int | None, retries: int) -> None:
        job.status = "running"
        job.started_at = time.time()
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
            counter = {"done": 0, "total": 0}

            def _log(msg: str = "") -> None:
                st = str(msg).strip()
                if st.startswith("placed ") and "into" in st and "file" in st:
                    parts = st.split()
                    if "into" in parts:
                        n = parts[parts.index("into") + 1]
                        counter["total"] = int(n) if n.isdigit() else counter["total"]
                    job.progress = {"stage": "build", "status": "progress",
                                    "done": 0, "total": counter["total"]}
                elif st.startswith("["):
                    counter["done"] += 1
                    job.progress = {"stage": "build", "status": "progress",
                                    "done": counter["done"], "total": counter["total"],
                                    "last": st[:120]}

            job.stage = "build"
            job.progress = {"stage": "build", "status": "progress", "done": 0, "total": 0}
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
        except Exception as e:  # noqa: BLE001 — surface any pipeline failure to the client
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"

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


@api.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = jm.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job.snapshot()
