"""Frontend generation — the Builder builds the UI/UX from the frontend requirements, AFTER the
backend, using the real endpoints the backend exposes.

This is the same shape as backend generation (requirements -> code via the model), not a deterministic
scaffold: UI is design, so it must be generated. It is NOT the generic openapi console — each page is
generated from the actual UI requirements plus the concrete endpoints (derived from the code contract)
and the data those endpoints return, so the model builds a menu page, an admin page, a reservation
form, etc. — real pages, wired to the API.

Model-bounded: page quality is only as good as the local model; every generated page is validated
(must be a real HTML document) and falls back to a minimal working template if the model output is
unusable, so the app always has a serving, non-broken UI.
"""

from __future__ import annotations

import json
import os
import urllib.request

# The OpenAI-compatible persona server. NOT BUILDER_LLM_URL (:8500) — that is opencode's raw model
# backend and does not speak /v1/chat/completions.
MODEL_URL = os.environ.get("AGENT_SERVER_URL", "http://localhost:7701").rstrip("/")


def _resolve_model() -> str:
    """Use whatever CHAT model is ACTIVE on the server — never pin a specific one. An explicit
    FRONTEND_LLM_MODEL override wins; otherwise pick the active chat model from /v1/models (so
    swapping the loaded model on the server changes the model here, with no code change)."""
    override = os.environ.get("FRONTEND_LLM_MODEL")
    if override:
        return override
    try:
        data = json.load(urllib.request.urlopen(f"{MODEL_URL}/v1/models", timeout=5)).get("data", [])
        # an active model that is a CHAT model (exclude embedding/reranker backends)
        for m in data:
            mid = m.get("id", "")
            if m.get("active") and (m.get("kind") == "chat"
                                    or (m.get("display_name") and "bge" not in mid
                                        and "rerank" not in mid.lower() and "embed" not in mid.lower())):
                return mid
    except Exception:
        pass
    return "gemma-4"                                     # last-resort fallback

#: Generic (domain-agnostic) markers of a UI/presentation requirement — verbs about what a USER sees
#: or does, not backend obligations. No project vocabulary ("menu", "reservation", ...) — pages are
#: derived from the Analyst's aspects, so the decomposition works for ANY domain, not the fixture.
_UI_TERMS = ("display", "page", "interface", "screen", "button", "language", "browse", "view",
             "public", "show", "form", "map", "responsive", "mobile", "render", "select", "navigate",
             "layout", "list", "search", "filter", "dashboard", "portal", "upload", "download", "print")


def _slug(name: str) -> str:
    """A filesystem/URL-safe page name from an aspect title (no regex)."""
    s = "".join(c if c.isalnum() else "_" for c in (name or "page").lower())
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_") or "page"


def _endpoints_from_contract(contract: dict) -> list[dict]:
    """Every POST endpoint the backend exposes: /{service}/{op} with the op's input params. Derived
    deterministically from the contract (same mapping the router scaffold uses)."""
    eps = []
    for key, v in contract.items():
        # Route by CONTENT, not by the concept's `kind` label: any export that is a FUNCTION is an
        # operation and gets an endpoint; a `class` export is a data model and does not. The Architect
        # sometimes co-locates operations on an entity (e.g. `reservation` owns `submit_reservation`),
        # so gating on kind=="service" silently drops those ops. Mirrors scaffold_api's routing rule.
        for e in v.get("exports", []):
            if e.get("kind") != "function":
                continue
            op = e.get("symbol")
            if not op:
                continue
            params = [p.get("name") for p in e.get("inputs", []) if p.get("name")]
            eps.append({"path": f"/{key}/{op}", "op": op, "params": params,
                        "returns": e.get("returns")})
    return eps


def _match(text: str, keywords) -> bool:
    t = (text or "").lower()
    return any(k in t for k in keywords)


def _call_model(prompt: str, log) -> str:
    # Thinking ON at temperature 0.7, 64K budget. MEASURED (all 5 pages, both modes, same prompt):
    #   - temp 0.2 + thinking ON  -> a low-entropy repetition loop ("Wait, the prompt says: ...") that
    #     never closes </think>, hits the 64K cap (finish_reason=length) and emits ZERO html. The 0.2
    #     near-greedy decoder gets trapped in that basin; it is NOT a parsing bug nor the model's size.
    #   - temp 0.7 + thinking ON  -> reasoning completes and closes; all 5 pages finish_reason=stop with
    #     complete, correctly-wired html in ~20-48s and a real 5-18K-char reasoning trace.
    # The server returns the reasoning in a separate `reasoning_content` field, so `content` is already
    # clean html when </think> closes; the </think> strip below is belt-and-suspenders for the rare case
    # the block is inlined. Keep temp >= 0.5 with thinking on — 0.2 reintroduces the loop.
    body = json.dumps({"model": _resolve_model(), "messages": [{"role": "user", "content": prompt}],
                       "temperature": float(os.environ.get("FRONTEND_TEMP", "0.7")),
                       "max_tokens": int(os.environ.get("FRONTEND_MAX_TOKENS", "64000")),
                       "chat_template_kwargs": {"enable_thinking": True}}).encode()
    req = urllib.request.Request(f"{MODEL_URL}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    out = json.load(urllib.request.urlopen(req, timeout=240))
    html = out["choices"][0]["message"]["content"]
    # DROP the reasoning first: keep only what comes AFTER the last </think>. The model drafts HTML
    # inside <think>, so extracting <!doctype...</html> from the whole response would splice thinking-
    # draft into the saved file. We save ONLY the real answer that follows the think phase.
    if "</think>" in html:
        html = html.rsplit("</think>", 1)[1]
    if "```" in html:                                   # strip a markdown fence if the model added one
        parts = html.split("```")
        html = parts[1] if len(parts) > 1 else html
        if html.lower().startswith("html"):
            html = html[4:]
    # then keep only the HTML document (drop any prose the model put around it)
    low = html.lower()
    start = low.find("<!doctype")
    if start == -1:
        start = low.find("<html")
    end = low.rfind("</html>")
    if start != -1 and end != -1:
        html = html[start:end + len("</html>")]
    return _sanitize(html.strip())


def _strip_attr(html: str, attr: str) -> str:
    """Remove every `attr="..."` / `attr='...'` occurrence (no regex — quote-scan). The model adds
    `integrity`/`crossorigin` on CDN tags with stale hashes, which the browser then BLOCKS, crashing
    the page (`L is not defined`). Stripping them lets the resource load instead of hard-failing."""
    out = html
    low = out.lower()
    i = 0
    while True:
        j = low.find(attr + "=", i)
        if j == -1:
            return out
        k = j + len(attr) + 1
        if k < len(out) and out[k] in ("'", '"'):
            end = out.find(out[k], k + 1)
            if end == -1:
                return out
            out = out[:j] + out[end + 1:]
            low = out.lower()
            i = j
        else:
            i = j + len(attr) + 1


def _sanitize(html: str) -> str:
    for attr in ("integrity", "crossorigin"):
        html = _strip_attr(html, attr)
    # The model likes to hardcode a placeholder API host (`https://api.example.com/...`) as the fetch
    # base, so every call goes to a domain that doesn't resolve (ERR_NAME_NOT_RESOLVED). Neutralize the
    # known placeholder hosts to a RELATIVE path so fetches hit the same origin that serves the page.
    for host in ("https://api.example.com", "http://api.example.com",
                 "https://www.example.com", "https://example.com", "http://example.com",
                 "https://your-api.com", "http://localhost:8000", "http://127.0.0.1:8000"):
        html = html.replace(host, "")
    return html


def _valid(html: str) -> bool:
    low = html.lower()
    return len(html) > 200 and ("<html" in low or "<!doctype" in low) and "</body>" in low


def _fallback(title: str, reqs: list[dict], eps: list[dict]) -> str:
    """A minimal but real page if the model output is unusable — lists the page's endpoints as a
    working form (never a broken page)."""
    cards = ""
    for e in eps:
        fields = "".join(f'<input placeholder="{p}" data-p="{p}">' for e2 in [e] for p in e["params"])
        cards += (f'<div class=c><b>{e["op"]}</b><br>{fields}'
                  f'<button onclick="call(this,\'{e["path"]}\')">Send</button><pre></pre></div>')
    return f"""<!doctype html><html><head><meta charset=utf-8><title>{title}</title>
<style>body{{font-family:system-ui;margin:2rem;max-width:760px}}.c{{border:1px solid #ccc;padding:1rem;margin:.6rem 0;border-radius:8px}}input{{margin:.2rem;padding:.3rem}}</style>
</head><body><h1>{title}</h1><p><a href="/">Home</a></p>{cards}
<script>async function call(b,p){{const ins=[...b.parentNode.querySelectorAll('input')];
const q=ins.filter(i=>i.value).map(i=>i.dataset.p+'='+encodeURIComponent(i.value)).join('&');
const r=await fetch(p+(q?'?'+q:''),{{method:'POST'}});b.nextElementSibling.textContent='HTTP '+r.status+'\\n'+await r.text();}}</script>
</body></html>"""


def _prompt(title: str, app_name: str, reqs: list[dict], eps: list[dict]) -> str:
    reqlist = "\n".join(f"- {r['text']}" for r in reqs)
    eplist = "\n".join(f"- POST {e['path']}"
                       + (f"?{'&'.join(k + '=<v>' for k in e['params'])}" if e['params'] else "")
                       + f"   (returns {e['returns']})" for e in eps)
    # Domain-agnostic: the DOMAIN is conveyed only by the requirements — the prompt never names it, so
    # the same stage builds a clinic, a library, or a storefront from ITS requirements.
    return f"""You are a senior frontend engineer building the "{title}" page of a web application
named "{app_name}". Infer the application's domain and appropriate styling ONLY from the requirements
below — do not assume any particular kind of business. Produce ONE complete, self-contained HTML
document (inline CSS + vanilla JS, no external libraries or frameworks).

Implement these product requirements:
{reqlist}

The backend already exists and SERVES THIS PAGE. Call these REAL endpoints with fetch() using
RELATIVE, same-origin paths — e.g. fetch("{eps[0]['path'] if eps else '/service/operation'}?...") —
and NEVER prefix a domain, host, or API base URL (no "https://api.example.com", no
"http://localhost"). HTTP POST, parameters in the query string, no request body; render whatever JSON
they return, degrading gracefully when fields are absent:
{eplist}

Rules: a clean, professional, responsive layout appropriate to the domain (works on mobile); include
a link back to "/" (Home); if the requirements call for switching languages, add a working toggle. Do
NOT load any external script, stylesheet, CDN, or use `integrity`/`crossorigin` attributes —
everything must be self-contained. If (and only if) the requirements call for a map, embed it with a
plain `<iframe src="https://www.openstreetmap.org/export/embed.html?...">` (no JavaScript map
library). Output the COMPLETE, FULLY-WRITTEN HTML document — every element, all CSS, and all
JavaScript written out in full. Do NOT use "...", ellipses, placeholder comments ("<!-- ... -->",
"// rest of code", "your code here"), or abbreviations of any kind; a reader must be able to run the
file as-is. Output ONLY the HTML document — no explanation, no markdown fences."""


def _derive_pages(handover: dict, requirements: list[dict]) -> tuple[list[dict], str]:
    """Derive the page set from the Analyst's ASPECTS + the contract — no hardcoded sitemap, no domain
    vocabulary. One page per aspect that carries a UI requirement; the page's endpoints are the
    contract concepts whose req_ids overlap that aspect (concept.req_ids ∩ aspect.req_ids). Returns
    (pages, app_name). Generalizes: a library / CRM / clinic app yields ITS aspects as pages."""
    contract = handover.get("code_contract") or {}
    by_aspect = handover.get("by_aspect") or {}
    all_eps = _endpoints_from_contract(contract)
    ui_by_id = {r["req_id"]: r for r in requirements
                if isinstance(r, dict) and r.get("req_id") and _match(r.get("text", ""), _UI_TERMS)}
    ep_reqids = {}                                        # endpoint path -> its concept's req_ids
    for ep in all_eps:
        key = ep["path"].strip("/").split("/")[0]
        ep_reqids[ep["path"]] = set((contract.get(key) or {}).get("req_ids") or [])

    pages = []
    for aspect_name, a in by_aspect.items():
        aspect_ids = set(a.get("req_ids") or [])
        page_reqs = [ui_by_id[i] for i in aspect_ids if i in ui_by_id]
        if not page_reqs:                                # aspect with no user-facing requirement
            continue
        page_eps = [ep for ep in all_eps if ep_reqids[ep["path"]] & aspect_ids] or all_eps[:6]
        pages.append({"slug": _slug(aspect_name), "title": aspect_name,
                      "reqs": page_reqs, "eps": page_eps})
    app_name = (handover.get("source_package", {}) or {}).get("project_name") or "Application"
    return pages, app_name


def generate(workspace: str, handover: dict, requirements: list[dict], log=print,
             validate=None, max_attempts: int = 3) -> dict:
    """Generate the frontend from the UI requirements + the backend's real endpoints — pages DERIVED
    from the Analyst's aspects (domain-agnostic), one HTML file per aspect that has a UI requirement,
    plus a generated Home that links them.

    `validate(name, html) -> [error, ...]` is the GATE: each generated page is rendered and its JS
    console checked; a page with errors is regenerated (up to `max_attempts`). Same principle as the
    backend acceptance harness — RUN the artifact, don't trust it.

    Returns {pages, generated (clean), broken (failed the gate), fallback}."""
    fe_dir = os.path.join(workspace, "frontend")
    os.makedirs(fe_dir, exist_ok=True)
    pages, app_name = _derive_pages(handover, requirements)
    log(f"frontend: derived {len(pages)} page(s) from aspects: {[p['title'] for p in pages]}")

    built, generated, fallback, broken = [], 0, 0, 0
    for pg in pages:
        slug, title, page_reqs, page_eps = pg["slug"], pg["title"], pg["reqs"], pg["eps"]
        prompt = _prompt(title, app_name, page_reqs, page_eps)
        html, clean = None, False
        for attempt in range(max_attempts):
            try:
                cand = _call_model(prompt, log)
            except Exception as ex:
                log(f"  [frontend] {slug}: model error ({type(ex).__name__})")
                break
            if not _valid(cand):
                continue
            html = cand                                  # structurally valid; keep as best-so-far
            errs = validate(slug, cand) if validate else []
            if not errs:
                clean = True
                break
            log(f"  [frontend] {slug}: attempt {attempt + 1} rejected — JS errors: {errs[:2]}")
        if html is None:
            html = _fallback(title, page_reqs, page_eps); fallback += 1
        elif clean or validate is None:
            generated += 1
        else:
            broken += 1
        with open(os.path.join(fe_dir, f"{slug}.html"), "w", encoding="utf-8") as f:
            f.write(html)
        built.append({"page": slug, "title": title, "reqs": [r["req_id"] for r in page_reqs]})
        log(f"  [frontend] {slug}.html <- {len(page_reqs)} req(s), {len(page_eps)} endpoint(s)")

    # deterministic Home linking every derived page — generic, always works, no domain assumptions
    links = "".join(f'<li><a href="/{b["page"]}.html">{b["title"]}</a></li>' for b in built) \
        or "<li>No user-facing pages were derived from the requirements.</li>"
    home = (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{app_name}</title><style>body{{font-family:system-ui;margin:2rem;max-width:760px}}"
            f"a{{color:#2563eb;text-decoration:none}}li{{margin:.4rem 0}}</style></head>"
            f"<body><h1>{app_name}</h1><p>Sections:</p><ul>{links}</ul></body></html>")
    with open(os.path.join(fe_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(home)
    log(f"frontend: {generated} clean + {broken} broken + {fallback} fallback page(s) + Home")
    return {"pages": built, "generated": generated, "broken": broken, "fallback": fallback}
