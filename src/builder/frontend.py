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

import concurrent.futures
import glob
import json
import os
import urllib.request

from . import skills

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
    _think = os.environ.get("FRONTEND_THINKING", "1") not in ("0", "false", "False")
    body = json.dumps({"model": _resolve_model(), "messages": [{"role": "user", "content": prompt}],
                       "temperature": float(os.environ.get("FRONTEND_TEMP", "0.7")),
                       "max_tokens": int(os.environ.get("FRONTEND_MAX_TOKENS", "64000")),
                       "chat_template_kwargs": {"enable_thinking": _think}}).encode()
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
    # DETERMINISTIC modal-overlay guard (generic, any app): the model often marks a modal/overlay/dropdown
    # `class="hidden"` but styles it with opacity/visibility (or forgets the rule), so the invisible
    # overlay still covers the page and INTERCEPTS clicks — making the real buttons unclickable. Force the
    # common "hidden" conventions to display:none so a hidden element can never eat pointer events. The
    # standard show pattern removes the class (classList.remove('hidden')), so this never hides a shown one.
    guard = ("<style>.hidden,[hidden],.d-none,.is-hidden,.modal.hidden,.overlay.hidden"
             "{display:none !important;}</style>")
    if guard not in html:
        low = html.lower()
        h = low.rfind("</head>")
        if h != -1:
            html = html[:h] + guard + html[h:]
        else:
            html = guard + html
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
RELATIVE, same-origin paths — e.g. fetch("{eps[0]['path'] if eps else '/service/operation'}?...",
{{method:"POST"}}) — and NEVER prefix a domain, host, or API base URL. CRITICAL — the backend serves
these ONLY over HTTP POST: EVERY fetch(), including data-loading calls on page load, MUST pass
{{ method: "POST" }}; a bare fetch(url) defaults to GET and the server returns 404. Parameters go in the
query string, no request body. Render whatever JSON they return, degrading gracefully when absent:
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


def _load_product_spec(workspace: str) -> dict | None:
    """The Product Agent's spec lives in the repo's product/ area (a sibling of code/). When present it
    DRIVES the frontend: pages come from its screen map (framed as user tasks, with public/authenticated
    surfaces and a real landing), not from the technical aspects. Absent -> fall back to aspect pages."""
    p = os.path.join(workspace, os.pardir, "product", "product_spec.json")
    try:
        with open(p, encoding="utf-8") as f:
            spec = json.load(f)
        return spec if spec.get("screens") else None
    except (OSError, ValueError):
        return None


def _derive_pages_from_spec(spec: dict, handover: dict, requirements: list[dict]):
    """Pages from the PRODUCT screen map: each screen is a page (user task), its endpoints are the
    contract concepts whose req_ids overlap the screen's requirements. The landing screen becomes the
    homepage (index.html). Returns (pages, app_name, nav_public, nav_admin)."""
    contract = handover.get("code_contract") or {}
    all_eps = _endpoints_from_contract(contract)
    ep_reqids = {ep["path"]: set((contract.get(ep["path"].strip("/").split("/")[0]) or {}).get("req_ids") or [])
                 for ep in all_eps}
    req_by_id = {r["req_id"]: r for r in requirements if isinstance(r, dict) and r.get("req_id")}
    # A screen belongs to the PUBLIC storefront or the ADMIN dashboard by its SURFACE, not by whether it
    # needs a login (a "book a table" screen is on the public site but may require sign-in to complete).
    surf_aud = {s.get("id"): s.get("audience") for s in (spec.get("surfaces") or [])}
    landing = spec.get("landing_screen")
    screens = spec.get("screens") or []

    # AUTH is ENVIRONMENT-provided (oauth2-proxy): a screen whose SOLE purpose is signing in (EVERY action
    # is a sign-in/authenticate action) is infrastructure the app must NOT build — the Product agent
    # sometimes emits a "Google Login" screen, which the frontend would render as a dead /login the tester
    # walks into and stalls. Drop those screens; sign-in is triggered by navigating to the gated route (see
    # the per-page login note). Deterministic: a CLOSED auth-verb vocabulary (same approach as the
    # Architect's capabilities.is_auth_operation), applied to the screen's actions — never a semantic guess.
    _AUTH_ACT_TOKENS = ("sign in", "sign-in", "signin", "log in", "log-in", "login", "authenticate",
                        "authentication", "oauth", "sign up", "sign-up", "signup", "sso")
    def _is_auth_only(s):
        acts = [a for a in (s.get("key_actions") or []) if a and a.strip()]
        return bool(acts) and all(any(t in a.lower() for t in _AUTH_ACT_TOKENS) for a in acts)
    screens = [s for s in screens if not _is_auth_only(s)]

    def _is_public(s):
        return surf_aud.get(s.get("surface"), s.get("audience", "public")) == "public"

    if not any(s.get("is_landing") or s.get("id") == landing for s in screens):
        pub = next((s for s in screens if _is_public(s)), screens[0] if screens else None)
        if pub:
            pub["is_landing"] = True
    pages = []
    for s in screens:
        sid = s.get("id") or _slug(s.get("title", ""))
        sreqids = set(s.get("requirements") or [])
        reqs = [req_by_id[i] for i in sreqids if i in req_by_id]
        eps = [ep for ep in all_eps if ep_reqids[ep["path"]] & sreqids] or all_eps[:6]
        is_landing = bool(s.get("is_landing")) or sid == landing
        pages.append({"slug": "index" if is_landing else _slug(sid), "screen_id": sid,
                      "title": s.get("title") or sid, "purpose": s.get("purpose", ""),
                      "persona": s.get("primary_persona", ""),
                      "surface_audience": "public" if _is_public(s) else "admin",
                      "requires_login": s.get("audience") == "authenticated",
                      "actions": s.get("key_actions") or [], "reqs": reqs, "eps": eps,
                      "is_landing": is_landing})
    app_name = spec.get("product_name") or (handover.get("source_package", {}) or {}).get("project_name") or "Application"
    href = lambda p: "/" if p["is_landing"] else f"/{p['slug']}.html"
    nav_public = [{"label": p["title"], "href": href(p)} for p in pages if p["surface_audience"] == "public"]
    nav_admin = [{"label": p["title"], "href": href(p)} for p in pages if p["surface_audience"] != "public"]
    return pages, app_name, nav_public, nav_admin


def _prompt_product(page: dict, app_name: str, one_liner: str,
                    nav_public: list[dict], nav_admin: list[dict]) -> str:
    """Prompt for a product SCREEN — framed as a user task with its persona, surface, actions and the
    product navigation, so the model builds a designed page (public storefront vs owner admin), not a
    CRUD dump. Domain conveyed only by the requirements + purpose; the app kind is never named."""
    reqlist = "\n".join(f"- {r['text']}" for r in page["reqs"]) or "- (support the actions below)"
    eplist = "\n".join(f"- POST {e['path']}"
                       + (f"?{'&'.join(k + '=<v>' for k in e['params'])}" if e['params'] else "")
                       + f"   (returns {e['returns']})" for e in page["eps"]) or "- (no backend calls)"
    pub = " · ".join(f'{l["label"]} → {l["href"]}' for l in nav_public) or "Home → /"
    adm = " · ".join(f'{l["label"]} → {l["href"]}' for l in nav_admin) or "(none)"
    admin_home = nav_admin[0]["href"] if nav_admin else "/"   # the REAL admin landing page path
    is_public = page["surface_audience"] == "public"
    # Auth is ENVIRONMENT-provided (a shared oauth2-proxy). A sign-in required action must NOT build its own
    # login form/page — that both duplicates infra the app doesn't own and creates a dead /login the tester
    # walks into. Instead gate via the proxy sign-in + /whoami. This mirrors the google-auth skill.
    login_note = (" This action is for a SIGNED-IN user, but authentication is provided by the ENVIRONMENT "
                  "(a shared sign-in proxy) — do NOT build a login form, a password/email field, a 'Sign in "
                  "with Google' prompt, or a separate login page. To gate the action: on load call "
                  "fetch('/whoami', {method:'POST'}).then(r=>r.json()) -> {authenticated, email}; if the user "
                  "is NOT authenticated, send the browser to '/oauth2/sign_in?rd=' + "
                  "encodeURIComponent(location.pathname) (this runs the sign-in and returns to this page); "
                  "once authenticated, proceed with the action and show who is signed in."
                  if page.get("requires_login") else "")
    if is_public:
        surface = ("This is a page in the PUBLIC customer-facing site. Design it as a polished public "
                   "storefront: a clear header with the site navigation, a welcoming layout, and the "
                   f"primary action made prominent. Show a top navigation bar with the PUBLIC links: {pub}. "
                   f"Include a discreet owner/admin link that points EXACTLY to \"{admin_home}\" — use that "
                   "exact href, do NOT invent a path like \"/admin\" or \"/login\"." + login_note)
    else:
        surface = ("This is an AUTHENTICATED admin/owner page (the back-office). Design it as an admin "
                   "dashboard: a persistent side or top admin navigation, and content laid out as "
                   f"management panels/tables/forms. Show the ADMIN navigation: {adm}. Include a link back "
                   "to the public site (/). Assume the owner is already signed in (no real auth needed).")
    hero = ("Because this is the LANDING page, open with a short hero that states what the site offers to "
            "its visitor and surfaces the single most important action.\n") if page["is_landing"] else ""
    prompt = f"""You are a senior product designer and frontend engineer building the "{page['title']}"
screen of "{app_name}" — {one_liner}. Infer visual style ONLY from the requirements and this screen's
purpose; never assume a specific kind of business by name.

SCREEN PURPOSE: {page['purpose']}
PRIMARY USER: {page['persona']}
THE USER MUST BE ABLE TO: {', '.join(page['actions']) or 'use the features the requirements describe'}

{surface}
{hero}
Implement these requirements:
{reqlist}

The backend already exists and SERVES THIS PAGE. Call these REAL endpoints with fetch() using RELATIVE,
same-origin paths (e.g. fetch("{page['eps'][0]['path'] if page['eps'] else '/service/op'}?...", {{method:"POST"}}));
NEVER prefix a domain, host, or API base URL.
CRITICAL — the backend serves these ONLY over HTTP POST. EVERY fetch() call, INCLUDING the data-loading
calls you run on page load (get/list/details), MUST pass {{ method: "POST" }}. A bare fetch(url) defaults
to GET and the server returns 404, so the page shows no data. Parameters go in the query string, no
request body. Render whatever JSON they return, degrading gracefully when fields are absent:
{eplist}

Rules: a clean, professional, responsive layout (works on mobile); render the navigation described
above as a real, working header/sidebar with those links. If the requirements call for switching
languages, add a working toggle. Any modal/dialog/overlay/dropdown that starts hidden MUST be hidden
with `display:none` (toggle it by adding/removing a class that sets display) — never leave a hidden
overlay in the layout with only opacity/visibility, because it will cover the page and intercept clicks,
making the real buttons unclickable. IMPORTANT: when a value must be sent to the backend as a large blob
(e.g. an uploaded image), send it in a way that does NOT put the whole blob in the URL query string.
Do NOT load any external script, stylesheet, CDN, font, or image, and do not use
`integrity`/`crossorigin`. If (and only if) the requirements call for a map, embed it with a plain
`<iframe src="https://www.openstreetmap.org/export/embed.html?...">`. Output the COMPLETE, fully-written
HTML document — every element, all CSS, all JavaScript, no "...", no placeholder comments, no
abbreviations. Output ONLY the HTML document — no explanation, no markdown fences."""
    # Inject any vetted SKILL (auth / upload / LLM ...) whose triggers match THIS screen — so a capability
    # feature is built from the real pattern, not faked (a dead "Sign in with Google", a base64-in-URL
    # upload). Selection is by the house reranker; empty (no match / skills dir absent) is a no-op.
    req_text = " ".join([r.get("text", "") for r in page["reqs"]]
                        + [page.get("purpose", "")] + (page.get("actions") or []))
    try:
        prompt += skills.skills_prompt(skills.select_skills(req_text, "frontend"))
    except Exception:  # noqa: BLE001 — skills are best-effort; never break generation
        pass
    return prompt


def _fetch_paths(html: str) -> list[str]:
    """Extract the same-origin path from every `fetch("/...")` in the page — a plain lexical scan (no
    regex): find each `fetch(`, read the following quoted/backtick literal, keep the path before any
    `?`/`${`/`#`. Used to catch calls to endpoints that don't exist."""
    paths, i, n = [], 0, len(html)
    while True:
        j = html.find("fetch(", i)
        if j == -1:
            break
        k = j + 6
        while k < n and html[k] in " \t\n":
            k += 1
        if k < n and html[k] in "\"'`":
            q = html[k]
            k += 1
            start = k
            while k < n and html[k] != q:
                k += 1
            lit = html[start:k]
            for stop in ("?", "${", "#", "`"):
                p = lit.find(stop)
                if p != -1:
                    lit = lit[:p]
            if lit.startswith("/"):
                paths.append(lit)
        i = j + 6
    return paths


_INFRA_PREFIX = ("/uploads", "/static", "/health", "/metrics", "/logs", "/operation", "/docs", "/openapi")
_ASSET_EXT = (".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".gif", ".webp",
              ".json", ".woff", ".woff2", ".map")


def _invented_endpoints(html: str, real_paths: set, api_groups: set | None = None) -> list[str]:
    """Same-origin paths the page fetches that are NOT a real backend endpoint, NOT a page/asset, and NOT
    a known infra path — i.e. the model invented them (e.g. /menu_management/get_counts, an entirely made-
    up group). These 404/405 at runtime. External URLs never appear (they don't start with '/')."""
    invented = []
    for p in _fetch_paths(html):
        if p in real_paths or p == "/" or p.startswith("#"):
            continue
        if p.startswith(_INFRA_PREFIX) or p.lower().endswith(_ASSET_EXT):
            continue
        invented.append(p)
    return sorted(set(invented))


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
    # PRODUCT-DRIVEN when the Product Agent has run: pages come from the screen map (user tasks, public
    # vs admin, a real landing), so the app is a product — not an index of the internal modules.
    spec = _load_product_spec(workspace)
    product_mode = bool(spec)
    if product_mode:
        pages, app_name, nav_public, nav_admin = _derive_pages_from_spec(spec, handover, requirements)
        one_liner = spec.get("one_liner", "")
        # clear stale pages from a prior (aspect-based) build so orphaned module pages aren't served
        keep = {f"{p['slug']}.html" for p in pages} | {"_val.html"}
        for old in glob.glob(os.path.join(fe_dir, "*.html")):
            if os.path.basename(old) not in keep:
                try:
                    os.remove(old)
                except OSError:
                    pass
        log(f"frontend: PRODUCT-driven — {len(pages)} screen(s): {[p['title'] for p in pages]} "
            f"({len(nav_public)} public + {len(nav_admin)} admin); landing={spec.get('landing_screen')}")
    else:
        pages, app_name = _derive_pages(handover, requirements)
        nav_public = nav_admin = None
        one_liner = ""
        log(f"frontend: derived {len(pages)} page(s) from aspects: {[p['title'] for p in pages]}")

    # A hung/runaway generation trickles tokens forever, so urllib's socket timeout never fires and one
    # page can block the whole build. Guard every page with a WALL-CLOCK deadline; on expiry, abandon the
    # attempt (the leaked worker is harmless) and fall back to a working template.
    page_deadline = float(os.environ.get("FRONTEND_PAGE_TIMEOUT", "200"))
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    # the REAL backend endpoints — a page must only fetch these (guards against invented paths -> 405)
    real_paths = {e["path"] for e in _endpoints_from_contract(handover.get("code_contract") or {})}
    api_groups = {p.strip("/").split("/")[0] for p in real_paths}

    built, generated, fallback, broken = [], 0, 0, 0
    for pg in pages:
        slug, title, page_reqs, page_eps = pg["slug"], pg["title"], pg["reqs"], pg["eps"]
        base_prompt = (_prompt_product(pg, app_name, one_liner, nav_public, nav_admin) if product_mode
                       else _prompt(title, app_name, page_reqs, page_eps))
        html, clean, correction = None, False, ""
        for attempt in range(max_attempts):
            try:
                cand = ex.submit(_call_model, base_prompt + correction, log).result(timeout=page_deadline)
            except concurrent.futures.TimeoutError:
                log(f"  [frontend] {slug}: model call exceeded {page_deadline:.0f}s — abandoning, using fallback")
                break
            except Exception as ex_:
                log(f"  [frontend] {slug}: model error ({type(ex_).__name__})")
                break
            if not _valid(cand):
                continue
            html = cand                                  # structurally valid; keep as best-so-far
            invented = _invented_endpoints(cand, real_paths, api_groups)
            if invented and attempt < max_attempts - 1:
                correction = ("\n\nCORRECTION — you called endpoints that DO NOT EXIST and will 404/405: "
                              + ", ".join(invented[:6]) + ". Call ONLY these real endpoints (HTTP POST, "
                              "params in the query string): " + ", ".join(sorted(real_paths)[:50])
                              + ". Remove the invented calls (or replace with a real endpoint above); if "
                              "no endpoint provides some data, render an empty state instead of inventing one.")
                log(f"  [frontend] {slug}: invented endpoints {invented[:4]} — retry")
                continue
            errs = validate(slug, cand) if validate else []
            if invented:
                errs = list(errs) + [f"invented endpoints: {invented[:4]}"]
            if not errs:
                clean = True
                break
            log(f"  [frontend] {slug}: attempt {attempt + 1} rejected — {errs[:2]}")
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

    ex.shutdown(wait=False)                               # release idle workers (a hung one is abandoned)

    if product_mode and any(b["page"] == "index" for b in built):
        # the product's LANDING screen was generated as index.html — a real homepage, not a module index
        log(f"frontend: PRODUCT app — {generated} clean + {broken} broken + {fallback} fallback page(s); "
            f"home = landing screen")
    else:
        # fallback (no product spec, or no landing): a generic Home linking every derived page
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
    return {"pages": built, "generated": generated, "broken": broken, "fallback": fallback,
            "product_mode": product_mode}


def regenerate_page(workspace: str, handover: dict, requirements: list[dict], slug: str,
                    report: str = "", log=print) -> dict:
    """Regenerate ONE flagged page (the Testing->Development frontend loop). Rebuilds the page from its
    product screen with skills injected + the invented-endpoint guardrail + the tester's failure report as
    an explicit correction, so a dead button / dead link / invented endpoint / non-completing flow is
    fixed. Returns {slug, regenerated}. Independent-harness sanctioned: consumes the tester's findings."""
    fe_dir = os.path.join(workspace, "frontend")
    spec = _load_product_spec(workspace)
    if not spec:
        return {"slug": slug, "regenerated": False, "reason": "no product spec"}
    pages, app_name, nav_public, nav_admin = _derive_pages_from_spec(spec, handover, requirements)
    pg = next((p for p in pages if p["slug"] == slug or f"{p['slug']}.html" == slug), None)
    if pg is None:
        return {"slug": slug, "regenerated": False, "reason": "no matching screen"}
    real_paths = {e["path"] for e in _endpoints_from_contract(handover.get("code_contract") or {})}
    api_groups = {p.strip("/").split("/")[0] for p in real_paths}
    correction = ("\n\nTHIS PAGE FAILED functional testing — FIX IT. Tester report:\n" + report[:1200]
                  + "\n\nEvery button/link/control MUST actually work (perform its action, call a REAL "
                  "endpoint, advance the UI) — no dead no-op, no placeholder alert, no invented endpoint.")
    base_prompt = _prompt_product(pg, app_name, spec.get("one_liner", ""), nav_public, nav_admin) + correction
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    deadline = float(os.environ.get("FRONTEND_PAGE_TIMEOUT", "200"))
    html, extra = None, ""
    for attempt in range(3):
        try:
            cand = ex.submit(_call_model, base_prompt + extra, log).result(timeout=deadline)
        except Exception:  # noqa: BLE001
            break
        if not _valid(cand):
            continue
        html = cand
        invented = _invented_endpoints(cand, real_paths, api_groups)
        if invented and attempt < 2:
            extra = ("\n\nCORRECTION — these endpoints DO NOT EXIST: " + ", ".join(invented[:6])
                     + ". Call ONLY: " + ", ".join(sorted(real_paths)[:50]))
            continue
        break
    ex.shutdown(wait=False)
    if html is None:
        return {"slug": slug, "regenerated": False, "reason": "generation failed"}
    with open(os.path.join(fe_dir, f"{pg['slug']}.html"), "w", encoding="utf-8") as f:
        f.write(html)
    log(f"regenerated {pg['slug']}.html from tester report")
    return {"slug": pg["slug"], "regenerated": True}
