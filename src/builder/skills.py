"""FACTORY skills — vetted capability patterns injected into generation so the builder produces REAL
integrations (auth, LLM, uploads) instead of faking them.

Skills are authored as opencode-native / Claude-compatible `SKILL.md` files (YAML frontmatter `name` +
`description`, then the pattern body). They are discovered by opencode and Claude alike; here we also
LOAD them and inject the relevant ones into the builder's direct-completion prompts (the path that
actually runs), selected by the house RERANKER over each skill's description — never string matching.

Generic: selection is by semantic relevance of a skill's description to the requirement/page text, so it
generalises to ANY app and ANY skill added later.
"""

from __future__ import annotations

import os

from . import assemble   # reuse the house reranker (_rerank, sigmoid-scaled 0..1)

#: Where SKILL.md files live inside the builder container (mount ~/.claude/skills here). Each skill is
#: <dir>/<name>/SKILL.md, matching the opencode/Claude convention.
SKILLS_DIR = os.environ.get("SKILLS_DIR", "/app/skills")
#: A skill is injected when its description scores at least this against the requirement text. The
#: reranker is sigmoid-scaled; capability descriptions are written with explicit trigger vocabulary, so a
#: genuine match scores high and unrelated skills score low. Tunable via env.
#: Reranker cutoff. MEASURED against this bge-reranker: a genuine capability match scores >= ~0.009 while
#: unrelated requirements score exactly 0.0, so a small floor cleanly separates match from no-match.
SELECT_THRESHOLD = float(os.environ.get("SKILL_SELECT_THRESHOLD", "0.005"))


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body). Minimal parser — name/description/metadata.surfaces + body."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm_raw, body = parts[1], parts[2]
    fm: dict = {}
    in_meta = False
    for line in fm_raw.splitlines():
        if not line.strip():
            continue
        if line.startswith("metadata:"):
            in_meta = True
            continue
        if in_meta and line.startswith("  ") and ":" in line:
            k, v = line.strip().split(":", 1)
            fm.setdefault("metadata", {})[k.strip()] = v.strip()
            continue
        in_meta = False
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, body.strip()


def load_skills(skills_dir: str | None = None) -> list[dict]:
    """Load every <dir>/<name>/SKILL.md into {name, description, surfaces, body}. Empty if none/missing."""
    base = skills_dir or SKILLS_DIR
    out: list[dict] = []
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        p = os.path.join(base, name, "SKILL.md")
        if not os.path.isfile(p):
            continue
        try:
            fm, body = _parse_frontmatter(open(p, encoding="utf-8").read())
        except OSError:
            continue
        if not fm.get("name") or not fm.get("description"):
            continue
        meta = fm.get("metadata", {}) or {}
        surfaces = meta.get("surfaces", "")
        out.append({"name": fm["name"], "description": fm["description"],
                    "surfaces": [s.strip() for s in surfaces.split(",") if s.strip()],
                    # short, high-signal trigger phrase for the reranker; fall back to the description
                    "match_text": meta.get("triggers") or fm["description"],
                    "body": body})
    return out


def select_skills(requirement_text: str, surface: str, skills: list[dict] | None = None,
                  threshold: float | None = None, top_k: int = 3) -> list[dict]:
    """Rerank the loaded skills by relevance of their DESCRIPTION to the requirement/page text and return
    those above `threshold` for the given surface (frontend|backend). Reranker down / no skills -> []."""
    skills = load_skills() if skills is None else skills
    # Only FACTORY capability skills are eligible — they DECLARE a surface (backend/frontend). This
    # excludes harness skills that happen to share ~/.claude/skills (e.g. Claude Code's `ci-ready`,
    # which declares no surface) from leaking into app generation.
    cands = [s for s in skills if s["surfaces"] and surface in s["surfaces"]]
    if not cands or not (requirement_text or "").strip():
        return []
    scores = assemble._rerank(requirement_text, [s["match_text"] for s in cands])
    thr = SELECT_THRESHOLD if threshold is None else threshold
    ranked = sorted(((sc, s) for sc, s in zip(scores, cands)), key=lambda t: t[0], reverse=True)
    return [s for sc, s in ranked[:top_k] if sc >= thr]


def skills_prompt(selected: list[dict]) -> str:
    """Format selected skills for injection into a generation prompt. Empty string if none."""
    if not selected:
        return ""
    blocks = []
    for s in selected:
        blocks.append(f"### SKILL: {s['name']}\n{s['description']}\n\n{s['body']}")
    return ("\n\nYou MUST follow these vetted capability patterns (SKILLS) for any feature they cover — "
            "wire the REAL implementation shown. Do NOT substitute a placeholder: no `alert(\"Redirecting"
            "...\")`, no TODO comment, no empty handler, no stub that returns a fake value — the control "
            "must actually perform the action (call the endpoint, set the session, upload the file, advance "
            "the UI). A handler that only alerts or does nothing is a bug.\n\n" + "\n\n---\n\n".join(blocks))
