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

from .opencode import _resolve_model


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
        out += [
            "", "## Persistence — go through the repository SEAM (already wired)",
            "- The data-access seam is ALREADY provided and imported by the scaffolds: "
            "`from app.repositories.store import repo`. Use it for ALL persistence — do not invent "
            "another data layer.",
            "- API: `repo(name).create(record_dict)` (returns the stored record with an `id`), "
            "`repo(name).get(id)`, `repo(name).list()`, `repo(name).update(id, dict)`, "
            "`repo(name).delete(id)`. Store FULL records — pass a dict of the entity's real fields, "
            "not a bare string — and read/filter/nest by iterating `repo(name).list()`.",
            "- Do NOT open your own database connections, call `sqlite3` directly, use an ORM "
            "(SQLAlchemy/SQLModel/`declarative_base`), or add any persistence dependency. The seam is "
            f"the single source of truth ({persistence} backs it, transparently).",
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
        "depends on. Their bodies are MINIMAL STUBS (a one-line `repo()` delegation or a trivial "
        "return). When a task assigns real behaviour to such a file, REPLACE the stub body with the "
        "real implementation the task describes (validation, the `repo()` queries/filtering/nesting, "
        "orchestration, error handling) — do NOT leave the minimal stub in place when a task asks for "
        "more. Keep the class/function NAMES, parameter lists and imports EXACTLY (other modules "
        "import them by those names), but DO rewrite the bodies and add internal helpers as needed.",
        "- Build ONE coherent application; every file fits the layout above.",
        "- REUSE what already exists. Never redefine a shared base/type/config/model that another "
        "file already defines — import it from its canonical module (this is about not DUPLICATING "
        "across files — it does NOT mean leaving a stub unimplemented).",
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
        # Pin the ACTIVE model (resolved from the server), never a hardcoded id — an agent frontmatter
        # `model:` OVERRIDES the opencode config, so a stale name here makes every step fail 'model not
        # found' (writes nothing). Resolving keeps the build agent agnostic to whatever model is loaded.
        f"model: local-llama/{_resolve_model()}\n"
        "---\n"
        "You are a senior engineer building ONE coherent, runnable application, file by file.\n"
        "ALWAYS obey AGENTS.md (the build contracts).\n"
        "WORK ONLY IN THE TARGET FILE the task names, and EDIT it with the `edit` tool. Keep your\n"
        "context SMALL: do NOT `read` whole files to explore — to check a name, signature, or field,\n"
        "use `grep`/`glob` for that SPECIFIC symbol. Reading many files blows the KV cache, forces\n"
        "compaction, and you never finish the edit. Reuse existing modules by importing them by name\n"
        "(never redefine a shared type) — but you do NOT need to read them to do so.\n"
        "Finish the job: implement every stub in the target file, then stop. Write complete, working\n"
        "code that runs: no placeholders, TODOs, mocks, or simulated logic.\n"
    )


#: A focused SUBAGENT for completing scaffold stubs. Restricted to read/edit/grep/glob (no bash), so
#: it implements the target file instead of exploring the tree. MEASURED: filling a 10-function service
#: this way took 25s with 1 read / 0 compaction, vs the primary builder agent reading ~33 files, hitting
#: KV-cache compaction, and never editing. Inherits the config's (agnostic) model — no pinned name.
def stubs_agent_md() -> str:
    return (
        "---\n"
        "description: Implements stub functions (NotImplementedError) in ONE target file\n"
        "mode: subagent\n"
        "temperature: 0\n"
        "tools:\n"
        "  read: true\n  edit: true\n  glob: true\n  grep: true\n  write: false\n  bash: false\n"
        "---\n"
        "You implement the stub functions in a SINGLE target file. Keep context SMALL: use `grep`/`glob` "
        "to check a specific symbol or signature — never `read` whole dependency files (it blows the KV "
        "cache and you never finish). Replace every `raise NotImplementedError`, `pass`, or trivial body "
        "with real, working logic, using the data seam `repo(name)` "
        "(create(dict)/get(id)/list()/update(id, dict)/delete(id)) from app.repositories.store — store "
        "FULL field dicts, and filter/nest by iterating `repo(name).list()`. Apply changes with the "
        "`edit` tool. Keep existing function/class names, parameter lists and imports EXACTLY; do not "
        "alter already-implemented logic.\n"
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
    with open(os.path.join(agent_dir, "stubs.md"), "w", encoding="utf-8") as f:
        f.write(stubs_agent_md())
    # Disable opencode's LSP for the build: measured ~50s/task of pyright analysis (GPU idle) vs
    # ~4s of actual inference. Correctness is our job (run-verify + guarded repair), not the
    # editor's — so LSP is pure dead weight here. This makes the build ~15x faster and GPU-bound.
    import json as _json
    with open(os.path.join(opencode_dir, "opencode.json"), "w", encoding="utf-8") as f:
        _json.dump({"lsp": False}, f)
