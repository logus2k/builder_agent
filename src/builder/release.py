"""Release package — the deploy manifest a future Deployment Agent consumes.

Mirrors how the Analyst emits `package.json` and the Planner `plan.json`: the Builder emits
`release.json` into the repo `code/` area describing HOW to install and run the built app
(install commands, run/deploy command, port, artifacts) plus the build provenance (per-task
outcomes + run-verify verdict). Project-agnostic — everything derives from the build frame
(skeleton) and the build report.
"""

from __future__ import annotations

CONTRACT_VERSION = "1.0"


def _port_from(run_cmd: str, default: int = 8000) -> int:
    toks = run_cmd.split()
    if "--port" in toks:
        i = toks.index("--port")
        if i + 1 < len(toks) and toks[i + 1].isdigit():
            return int(toks[i + 1])
    return default


def build_release(report: dict, skeleton: dict, files: list[str], project: dict | None = None) -> dict:
    """Assemble the release package from the build report + frame. `files` are the code/ paths."""
    sk = skeleton or {}
    run_cmd = sk.get("run_cmd") or ""
    manifest = sk.get("manifest") or "requirements.txt"
    summary = report.get("summary", {}) or {}
    run_verify = ((report.get("assembly") or {}).get("run_verify")) or {}

    # A deploy command binds all interfaces for a server (uvicorn); otherwise use run_cmd as-is.
    deploy_cmd = run_cmd
    if "uvicorn" in run_cmd and "--host" not in run_cmd:
        deploy_cmd = run_cmd + " --host 0.0.0.0"
    port = _port_from(run_cmd)

    proj = project or (report.get("source") or {})
    return {
        "contract_version": CONTRACT_VERSION,
        "app": {
            "name": proj.get("project_name") or proj.get("name") or proj.get("project_id") or "app",
            "language": sk.get("language"),
            "stack": sk.get("stack"),
        },
        "entrypoint": sk.get("entrypoint"),
        "manifest": manifest,
        "install": [f"pip install -r {manifest}"],
        "run_cmd": run_cmd,
        "deploy_cmd": deploy_cmd,
        "port": port,
        "env": [],                         # future: infer required env vars from config usage
        "runnable": summary.get("runnable"),
        "build": {
            "built": summary.get("built"), "failed": summary.get("failed"),
            "no_output": summary.get("no_output"), "total": summary.get("total"),
            "files": len(files), "run_verify": run_verify,
        },
        "results": report.get("results", []),
        "artifacts": {"code_dir": "code", "files": files},
    }
