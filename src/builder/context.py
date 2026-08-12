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


def _stack_policy(handover: dict | None) -> dict:
    """The Architect's proportionality decision (persistence + sanctioned dependency allow-list),
    carried in the handover as `stack_policy`. Empty when absent (older handover) — then the build
    keeps its prior bottom-up behaviour."""
    return (handover or {}).get("stack_policy") or {}


def agents_md(skeleton: dict, handover: dict | None = None) -> str:
    sk = skeleton or {}
    policy = _stack_policy(handover)
    sanctioned = [d for d in (policy.get("sanctioned_dependencies") or []) if isinstance(d, str)]
    persistence = policy.get("recommended_persistence") or ""
    orm_ok = bool(policy.get("relational_orm_required"))
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

    # PERSISTENCE POLICY (from the Architect's proportionality review) — the simplest persistence
    # the requirements justify. This is what stops an unrequested ORM being invented (and with it
    # the whole class of column-type/session/relationship footguns).
    if persistence:
        out += ["", "## Persistence — use EXACTLY this, nothing heavier",
                f"- Persistence approach: {persistence}."]
        if not orm_ok:
            out += [
                "- Use Python's standard-library `sqlite3` DIRECTLY with SQL (CREATE TABLE, "
                "parameterized INSERT/SELECT/UPDATE). Do NOT use an ORM — no SQLAlchemy, SQLModel, "
                "Django ORM, or `declarative_base`. Open the database from a settings path "
                "(`sqlite3.connect(settings.DATABASE_PATH)`).",
                "- For identifiers use a TEXT column holding `uuid.uuid4().hex` (stdlib) or an "
                "INTEGER PRIMARY KEY — NEVER a library-specific UUID column type.",
                "- Keep data-access in one module (e.g. a small repository/db helper); routers and "
                "services call it — they do not open connections themselves.",
            ]

    # ALLOWED DEPENDENCIES — the top-down allow-list. Importing anything else is REJECTED by the
    # build's conformance check, so the file would have to be regenerated. Keep to this list.
    if sanctioned:
        out += ["", "## Allowed dependencies — the ONLY third-party packages you may import",
                f"- {', '.join(sanctioned)}.",
                "- Import NOTHING else third-party. If a task seems to need another package, use "
                "the Python standard library instead (e.g. `sqlite3`, `json`, `uuid`, `datetime`, "
                "`http.client`). Any unsanctioned import will be REJECTED and the file rebuilt."]

    out += [
        "",
        "## Contracts — MUST follow so the files form ONE runnable app",
        "- Some modules under app/models/ and app/services/ are PRE-CREATED as design-contract "
        "scaffolds carrying the exact imports and class/function signatures the rest of the app "
        "depends on. When you open such a file, FILL its bodies — never rename, re-signature, or "
        "remove its classes/functions or imports; other modules import them by those exact names.",
        "- Build ONE coherent application; every file fits the layout above.",
        "- REUSE what already exists. Never redefine a shared base/type/config/model that another "
        "file already defines — import it from its canonical module.",
        "- Export new public symbols from their package `__init__` so other modules can import them.",
        "- Import ONLY packages in the allowed-dependencies list above; keep them in the manifest.",
        "- Use types and APIs appropriate to the stack, so the app actually runs.",
        "- Use each library's CURRENT major-version conventions — do NOT use imports removed in the "
        "installed version (e.g. with Pydantic v2, import `BaseSettings` from `pydantic_settings` and "
        "add `pydantic-settings` to the manifest; never `from pydantic import BaseSettings`).",
        "- Declare EVERY configuration value the app reads in ONE settings/config module (database "
        "path, secrets, and the LLM settings below), each overridable from the environment with a "
        "sane default. If a module reads `settings.X`, `X` MUST be declared in that settings class.",
        "- AVOID CIRCULAR IMPORTS: sibling modules must not import each other's classes at module "
        "load. Put cross-module type-only imports under `if TYPE_CHECKING:`, and import shared "
        "pieces from a single base module — never sideways between siblings."
        + (" Use string references for ORM relationships (relationship('Item'))." if orm_ok else ""),
        "- Write complete, working code. No placeholders, TODOs, mocks, or simulated logic.",
        "",
        "## Platform capability — a local LLM is available; use it, never fake an AI service",
        "A local, OpenAI-compatible LLM is available to this application for ANY AI feature. It is",
        "MULTIMODAL: it accepts images and returns text, so image-description / vision features are",
        "REAL, not simulated. Implement every AI / LLM / vision / embedding feature as a real call",
        "to it, reading its configuration from the environment (with sane localhost defaults):",
        "  - LLM_BASE_URL  — OpenAI-compatible base, e.g. http://localhost:8500/v1",
        "  - LLM_API_KEY   — any string (the local server needs no real key)",
        "  - LLM_MODEL     — e.g. gemma-4",
        "Call it with the standard OpenAI chat-completions API. For VISION, put the image in the",
        "message content as an image_url (a data: base64 URI or a URL); the model returns a text",
        "description. NEVER invent an external provider, and NEVER return placeholder/simulated",
        "text for an AI feature — the capability is real and local.",
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
    """Write AGENTS.md + the custom builder agent + a project opencode config into the workspace."""
    os.makedirs(workspace, exist_ok=True)
    with open(os.path.join(workspace, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write(agents_md(skeleton, handover))
    opencode_dir = os.path.join(workspace, ".opencode")
    agent_dir = os.path.join(opencode_dir, "agent")
    os.makedirs(agent_dir, exist_ok=True)
    with open(os.path.join(agent_dir, "builder.md"), "w", encoding="utf-8") as f:
        f.write(builder_agent_md(skeleton))
    # Disable opencode's LSP for the build: measured ~50s/task of pyright analysis (GPU idle) vs
    # ~4s of actual inference. Correctness is our job (run-verify + guarded repair), not the
    # editor's — so LSP is pure dead weight here. This makes the build ~15x faster and GPU-bound.
    import json as _json
    with open(os.path.join(opencode_dir, "opencode.json"), "w", encoding="utf-8") as f:
        _json.dump({"lsp": False}, f)
