"""Release package — the deploy manifest a future Deployment Agent consumes.

Mirrors how the Analyst emits `package.json` and the Planner `plan.json`: the Builder emits
`release.json` into the repo `code/` area describing HOW to install and run the built app
(install commands, run/deploy command, port, artifacts) plus the build provenance (per-task
outcomes + run-verify verdict). Project-agnostic — everything derives from the build frame
(skeleton) and the build report.
"""

from __future__ import annotations

import ast
import os

CONTRACT_VERSION = "1.0"


def discover_env(workspace: str, files: list[str]) -> list[str]:
    """Env vars the built code actually reads — os.environ.get('X'), os.getenv('X'),
    os.environ['X'] — found by parsing (AST, no regex). Generic: whatever the app needs
    (DATABASE_URL, LLM_BASE_URL, secrets…) so the Deployment Agent knows what to provide."""
    found: set[str] = set()
    for rel in files:
        if not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse(open(os.path.join(workspace, rel), encoding="utf-8").read())
        except (SyntaxError, OSError, ValueError):
            continue
        for node in ast.walk(tree):
            # os.getenv("X")  or  os.environ.get("X")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
                a = node.args[0]
                key = a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None
                if key and node.func.attr == "getenv":
                    found.add(key)
                elif key and node.func.attr == "get" and isinstance(node.func.value, ast.Attribute) \
                        and node.func.value.attr == "environ":
                    found.add(key)
            # os.environ["X"]
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) \
                    and node.value.attr == "environ":
                idx = node.slice
                if isinstance(idx, ast.Constant) and isinstance(idx.value, str):
                    found.add(idx.value)
            # pydantic-settings: class Settings(BaseSettings): FIELD: type = default  -> FIELD is an env var
            if isinstance(node, ast.ClassDef) and any(
                    (isinstance(b, ast.Name) and b.id == "BaseSettings")
                    or (isinstance(b, ast.Attribute) and b.attr == "BaseSettings") for b in node.bases):
                for stmt in node.body:                       # class body only (skip nested Config)
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        found.add(stmt.target.id)
                    elif isinstance(stmt, ast.Assign):
                        for t in stmt.targets:
                            if isinstance(t, ast.Name):
                                found.add(t.id)
    found.discard("model_config")   # pydantic v2 settings attribute, not an env var
    return sorted(found)


def _port_from(run_cmd: str, default: int = 8000) -> int:
    toks = run_cmd.split()
    if "--port" in toks:
        i = toks.index("--port")
        if i + 1 < len(toks) and toks[i + 1].isdigit():
            return int(toks[i + 1])
    return default


def build_release(report: dict, skeleton: dict, files: list[str], project: dict | None = None,
                  env: list[str] | None = None) -> dict:
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
        "env": env or [],                  # env vars the built code reads (discovered from the code)
        "runnable": summary.get("runnable"),
        "build": {
            "built": summary.get("built"), "failed": summary.get("failed"),
            "no_output": summary.get("no_output"), "total": summary.get("total"),
            "files": len(files), "run_verify": run_verify,
        },
        "results": report.get("results", []),
        "artifacts": {"code_dir": "code", "files": files},
    }
