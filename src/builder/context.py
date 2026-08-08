"""Project context for opencode — generated per-project from the build frame (skeleton), never
hardcoded to any one project.

Two artifacts are written into the build workspace before any task runs:
  - AGENTS.md — opencode auto-loads this into EVERY step's context (verified: opencode walks up
    from the working dir collecting AGENTS.md). It carries the shared BUILD CONTRACTS (the layout,
    where shared types live, "reuse don't redefine", "export from package __init__", "no
    placeholders") so every file the agent writes fits ONE coherent, runnable application.
  - .opencode/agent/builder.md — a custom opencode agent (temperature 0, a build-focused prompt)
    used for every task, so the whole build runs as one persona in one continued session.

All specifics come from the skeleton (stack, layout, entrypoint, conventions) — this module only
frames them into contracts, so it works for any project the frame describes.
"""

from __future__ import annotations

import os

from .opencode import MODEL


def entrypoint_module(skeleton: dict) -> str:
    """`app/main.py` -> `app.main` (drop extension, path -> dotted). Empty if none."""
    ep = (skeleton or {}).get("entrypoint") or ""
    ep = ep.replace("\\", "/").strip("/")
    if ep.endswith(".py"):
        ep = ep[:-3]
    return ep.replace("/", ".")


def agents_md(skeleton: dict, handover: dict | None = None) -> str:
    sk = skeleton or {}
    out = [
        "# Build contracts",
        f"Stack: {sk.get('stack', sk.get('language', 'unspecified'))}. "
        f"Entrypoint: {sk.get('entrypoint', '?')}. Run: {sk.get('run_cmd', '?')}. "
        f"Dependency manifest: {sk.get('manifest', 'requirements.txt')}.",
        "",
        "## Project layout (follow it)",
    ]
    for item in sk.get("layout", []) or []:
        out.append(f"- {item}")
    if sk.get("conventions"):
        out += ["", f"Conventions: {sk['conventions']}"]
    out += [
        "",
        "## Contracts — MUST follow so the files form ONE runnable app",
        "- Build ONE coherent application; every file fits the layout above.",
        "- REUSE what already exists. Never redefine a shared base/type/config/model that another "
        "file already defines — import it from its canonical module.",
        "- Export new public symbols from their package `__init__` so other modules can import them.",
        "- Keep every dependency you import listed in the manifest.",
        "- Use types and APIs appropriate to the stack, so the app actually runs.",
        "- Write complete, working code. No placeholders, TODOs, mocks, or simulated logic.",
    ]
    return "\n".join(out) + "\n"


def builder_agent_md(skeleton: dict) -> str:
    return (
        "---\n"
        "description: Builds complete, integrated files for one coherent runnable app, per AGENTS.md\n"
        "mode: primary\n"
        "temperature: 0\n"
        f"model: {MODEL}\n"
        "---\n"
        "You are a senior engineer building ONE coherent, runnable application, file by file.\n"
        "ALWAYS read and obey AGENTS.md (the build contracts). Before writing a file, look at the\n"
        "files already in the project and REUSE their modules/classes — never redefine a shared\n"
        "type (e.g. a declarative base, a config, a client) or a symbol that already exists; import\n"
        "it from its canonical module. After adding a public symbol, export it from its package\n"
        "`__init__`. Write complete, working code that runs: no placeholders, TODOs, mocks, or\n"
        "simulated logic.\n"
    )


def write_project_context(workspace: str, skeleton: dict, handover: dict | None = None) -> None:
    """Write AGENTS.md + the custom builder agent into the workspace (idempotent)."""
    os.makedirs(workspace, exist_ok=True)
    with open(os.path.join(workspace, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write(agents_md(skeleton, handover))
    agent_dir = os.path.join(workspace, ".opencode", "agent")
    os.makedirs(agent_dir, exist_ok=True)
    with open(os.path.join(agent_dir, "builder.md"), "w", encoding="utf-8") as f:
        f.write(builder_agent_md(skeleton))
