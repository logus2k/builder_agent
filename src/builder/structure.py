"""Agentic project structure — the Builder reasons about layout instead of guessing from names.

The Planner emits tasks whose `deliverable` is a bare, often collision-prone name
(`reservation.py` appears 8 times; paths are flattened into underscores). Dumping those into
one folder is not an application. So the Builder, being an agent, DECIDES the structure with the
local LLM and consolidates every decision into one coherent project:

  1. seed_skeleton() — the `builder_frame_architect` persona reads the whole plan + the Architect
     handover (reference, not prescription) and picks the project FRAME: language, stack,
     top-level layout, entrypoint, dependency manifest, run cmd.
  2. place_task() — the `builder_task_placer` persona, for each task, decides that task's real
     path within the frame and whether to CREATE a new file or EXTEND an existing one, against
     the LIVING codebase map. This resolves the collisions: eight `reservation.py` tasks are
     routed to distinct real files or consolidated into one file to extend.

Both are agent_server personas called by name (the house pattern — client.complete_json). Full
context is sent every call (the model's slot is 32K); nothing is truncated. Failures degrade to
a sane, explicit fallback (never a crash).
"""

from __future__ import annotations

from . import client

FRAME_AGENT = "builder_frame_architect"
PLACER_AGENT = "builder_task_placer"


def _attrs_str(attrs) -> str:
    """Component attributes may be dicts ({name,type,...}) or plain strings."""
    out = []
    for a in attrs or []:
        if isinstance(a, dict):
            n, t = a.get("name", ""), a.get("type")
            out.append(f"{n}:{t}" if t else n)
        else:
            out.append(str(a))
    return ", ".join(x for x in out if x)


def _ops_str(ops) -> str:
    """Interface operations may be dicts ({name,inputs,returns}) or plain strings."""
    out = []
    for o in ops or []:
        out.append(o.get("name", "") if isinstance(o, dict) else str(o))
    return ", ".join(x for x in out if x)


def _plan_view(plan: dict) -> str:
    """One line per task — the whole plan (no truncation; it fits the 32K slot)."""
    return "\n".join(
        f"- {t.get('task_id')}: [{t.get('kind','?')}] {t.get('title','')} "
        f"(wants: {t.get('deliverable','')})"
        for t in plan.get("tasks", []))


def _architecture_view(plan: dict, handover: dict | None = None) -> str:
    """The full DESIGN the Builder organizes code around (reference, not prescription): components
    (with responsibilities + attributes) and interfaces (with operations) per aspect. Falls back
    to plan.json's thin provenance if no handover."""
    src = plan.get("source") or {}
    name = src.get("project_name") or src.get("name") or "the application"
    by_aspect = (handover or {}).get("by_aspect") or {}
    if by_aspect:
        lines = [f"App: {name}", "", "Architecture (Architect handover — reference for the domain):"]
        for aspect, a in by_aspect.items():
            lines.append(f"\n## {aspect}: {a.get('scope','')}")
            for c in (a.get("components") or []):
                attrs = _attrs_str(c.get("attributes"))
                lines.append(f"  - component {c.get('name','')}: {c.get('responsibility','')}"
                             + (f" [attrs: {attrs}]" if attrs else ""))
            for i in (a.get("interfaces") or []):
                ops = _ops_str(i.get("operations"))
                lines.append(f"  - interface {i.get('name','')}: {i.get('purpose','')}"
                             + (f" [ops: {ops}]" if ops else ""))
        return "\n".join(lines)
    arch = plan.get("architecture") or {}
    aspects = arch.get("aspects") or arch.get("by_aspect") or {}
    names = list(aspects.keys()) if isinstance(aspects, dict) else (
        [a.get("name", "") for a in aspects] if isinstance(aspects, list) else [])
    return f"App: {name}\nArchitecture aspects: {', '.join(n for n in names if n) or '(none provided)'}"


def seed_skeleton(plan: dict, handover: dict | None = None) -> dict:
    """Decide the project frame (builder_frame_architect persona). Degrades to a generic
    Python/FastAPI frame if the model is unreachable (explicit, not silent)."""
    user = (f"{_architecture_view(plan, handover)}\n\n"
            f"TASKS ({len(plan.get('tasks', []))} total):\n{_plan_view(plan)}\n\n"
            f"Decide the PROJECT FRAME as specified.")
    out = client.complete_json(FRAME_AGENT, user)
    if not out or not out.get("entrypoint"):
        return {"language": "python", "stack": "FastAPI", "layout": ["app/", "tests/"],
                "entrypoint": "app/main.py", "manifest": "requirements.txt",
                "run_cmd": "uvicorn app.main:app", "conventions": "code under app/, tests under tests/",
                "_fallback": True}
    out.setdefault("manifest", "requirements.txt")
    out.setdefault("language", "python")
    return out


def _safe_rel(path: str) -> str:
    """Keep an LLM-proposed path relative and safe: strip leading slashes, drop '.'/'..'
    segments. (Safety normalization only — NOT deriving structure from the name.)"""
    parts = [p for p in (path or "").replace("\\", "/").split("/") if p and p not in (".", "..")]
    return "/".join(parts)


def new_map(skeleton: dict) -> dict:
    return {"skeleton": skeleton, "files": []}


def _files_view(cmap: dict) -> str:
    files = cmap.get("files", [])
    if not files:
        return "(none yet)"
    return "\n".join(f"- {f['path']}: {f.get('purpose','')}" for f in files)


def place_task(task: dict, cmap: dict, design_hint: str = "") -> dict:
    """Decide THIS task's file path + action (builder_task_placer persona) against the living
    map, then update the map. `design_hint` carries the Architect design element(s) this task
    realizes — reference, not prescription. Returns {task_id, path, action, rationale}.
    Degrades to a frame-consistent default path."""
    sk = cmap["skeleton"]
    hint = (f"\nARCHITECTURE REFERENCE (inspiration, not prescription — you decide the code "
            f"layout):\n{design_hint}\n") if design_hint else ""
    user = (f"PROJECT FRAME:\n  language: {sk.get('language')}\n  stack: {sk.get('stack')}\n"
            f"  layout: {sk.get('layout')}\n  entrypoint: {sk.get('entrypoint')}\n"
            f"  conventions: {sk.get('conventions')}\n\n"
            f"FILES PLACED SO FAR:\n{_files_view(cmap)}\n{hint}\n"
            f"THIS TASK:\n  id: {task.get('task_id')}\n  kind: {task.get('kind')}\n"
            f"  title: {task.get('title')}\n  wants deliverable: {task.get('deliverable')}\n"
            f"  instructions: {task.get('instructions') or ''}\n\n"
            f"Decide its path and action.")
    out = client.complete_json(PLACER_AGENT, user) or {}
    path = _safe_rel(out.get("path", ""))
    action = out.get("action") if out.get("action") in ("create", "extend") else None
    if not path:
        # Explicit fallback: place by kind under the frame (safety net for an LLM miss).
        kind = (task.get("kind") or "code").lower()
        base = (task.get("deliverable") or f"{task.get('task_id')}.py").split("/")[-1]
        sub = {"schema": "app/schemas", "test": "tests", "config": "config"}.get(kind, "app")
        path = f"{sub}/{base}"
        action = "create"

    existing = next((f for f in cmap["files"] if f["path"] == path), None)
    if existing:
        action = "extend"
        if task.get("task_id") not in existing["task_ids"]:
            existing["task_ids"].append(task.get("task_id"))
    else:
        action = action or "create"
        cmap["files"].append({"path": path, "purpose": task.get("title", ""),
                              "task_ids": [task.get("task_id")]})
    return {"task_id": task.get("task_id"), "path": path, "action": action,
            "rationale": (out.get("rationale") or "")}
