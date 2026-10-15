"""Contract-driven scaffolding — the Builder IMPLEMENTS the Architect's code contract instead of
inventing structure. For each concept in the contract we deterministically write a module SKELETON:
its dependency imports (by the contract's exact names, from the contract's canonical module paths)
and a typed stub for each export (a class with its fields, a function with its signature). Structure
and the public interface are therefore correct BY CONSTRUCTION — collisions are impossible (one
canonical module per concept) and imports/exports line up (both sides read the same contract). The
model's job shrinks to filling bodies, not deciding names or layout.

The concept->path mapping is the ONE stack-specific step (the Architect's contract is stack-agnostic:
concepts, kinds, symbols). Here, for a Python/app layout: entity -> app/models/<key>.py, service ->
app/services/<key>.py. Other frames map differently; the contract is unchanged.
"""

from __future__ import annotations

import os


def _path_of(key: str, kind: str) -> str:
    sub = "models" if kind == "entity" else "services"
    return f"app/{sub}/{key}.py"


def module_paths(contract: dict) -> dict:
    """concept key -> its canonical module path (deterministic, one per concept)."""
    return {key: _path_of(key, v.get("kind", "service")) for key, v in contract.items()}


def _module_dotted(path: str) -> str:
    return path[:-3].replace("/", ".")


# ---- persistence seam ---------------------------------------------------------------------------
# The contract implies data-access (entities are stored/fetched) but names no layer, so model-filled
# service bodies invent `app.db.database` / repositories that don't exist and get stubbed. We provide
# a REAL, dependency-free in-memory repository the scaffold wires services to, so CRUD operations
# actually work by construction. Swap the backing store for sqlite/a driver without touching services.
_STORE_PY = '''"""Generic in-memory repository — real, working persistence for the scaffolded services.

Deterministic and dependency-free: a service calls repo("<name>").create/get/list/update/delete and
gets real data back, persisted for the process lifetime. This is the data-access seam the contract
implies; the backing dict can be swapped for sqlite or a driver without changing the service interface.
"""
import json
from itertools import count
from threading import Lock


def _coerce(data):
    """Turn a STRUCTURED payload into its real value so records/args carry real fields. Frontends send a
    whole object (or list) as a JSON string in a query param (create_item?item={"name":..} or
    verify?deps=[{..},{..}]); stored/passed verbatim it stays opaque text whose fields can't be read,
    iterated, or filtered. A JSON object OR array string is parsed to its structure; a bare scalar is left
    alone. Returning a list matters for service args typed as collections (a body that iterates the param
    would otherwise crash on the raw string); Repo.create routes any non-dict to its "value" slot, so
    broadening this to lists is safe for the persistence path too."""
    if isinstance(data, str):
        s = data.strip()
        if s[:1] in ("{", "["):
            try:
                parsed = json.loads(s)
            except (ValueError, TypeError):
                return data
            if isinstance(parsed, (dict, list)):
                return parsed
    return data


# Free-text search sentinels. A generated storefront loads its default catalog view by calling the
# search endpoint with a "show everything" placeholder (query="all", "*", "any", or None). The model's
# search body is a substring match (`query.lower() in name.lower()`), for which those literal words match
# nothing, so the page loads EMPTY even though records exist. Folding every wildcard sentinel to "" makes
# the SAME substring match return ALL records — in Python "" in s is always True — so a search with no
# real term lists everything, the universal convention. Deterministic, no regex; the API layer applies it
# only to free-text search params on list/search operations (see scaffold_api), never to equality filters.
_WILDCARD = ("", "all", "*", "any", "%", "none", "null", "undefined")


def _wildcard(q):
    """Normalize a free-text search term: a wildcard/placeholder query becomes "" (which matches all)."""
    if q is None:
        return ""
    if isinstance(q, str) and q.strip().lower() in _WILDCARD:
        return ""
    return q


class Repo:
    def __init__(self):
        self._items = {}
        self._ids = count(1)
        self._lock = Lock()

    def _key(self, ident):
        try:
            return int(ident)
        except (TypeError, ValueError):
            return ident

    def create(self, data=None):
        data = _coerce(data)
        with self._lock:
            i = next(self._ids)
        rec = {"id": i}
        if isinstance(data, dict):
            rec.update(data)
        elif data is not None:
            rec["value"] = data
        self._items[i] = rec
        return rec

    def get(self, ident):
        return self._items.get(self._key(ident))

    def list(self, **filters):
        items = list(self._items.values())
        # optional equality filters (repo("x").list(type="asset")): model bodies routinely pass filter
        # kwargs expecting server-side filtering; support it so those calls work instead of raising.
        if filters:
            items = [r for r in items if all(r.get(k) == v for k, v in filters.items())]
        return items

    def update(self, ident, data=None):
        data = _coerce(data)
        k = self._key(ident)
        if k in self._items:
            if isinstance(data, dict):
                self._items[k].update(data)
            elif data is not None:
                self._items[k]["value"] = data
        return self._items.get(k)

    def delete(self, ident):
        k = self._key(ident)
        return {"deleted": self._items.pop(k, None) is not None, "id": ident}


_REGISTRY = {}
_RLOCK = Lock()


# Registered data ENTITIES (module-names), injected at scaffold time by scaffold_persistence. Lets
# _canon_store fold a COMPOUND store name (product_metadata, product_catalog) to the entity it extends,
# WITHOUT merging a name that is itself a registered entity (order_history stays distinct from order).
# Empty (no contract) => plain plural folding only, the original conservative behaviour.
_ENTITIES = frozenset()


def _depluralize(n):
    if len(n) > 3 and n.endswith("ies"):
        return n[:-3] + "y"                      # categories -> category
    if len(n) > 2 and n.endswith("s") and not n.endswith("ss"):
        return n[:-1]                            # products -> product, orders -> order
    return n


def _canon_store(name):
    """Canonical store key so writes and reads of the SAME entity always meet. A model names one entity's
    store inconsistently: singular/plural (product vs products) AND compound (product vs product_metadata),
    so a create and its update/list land in DIFFERENT stores and never meet. Fold to ONE key:
      1. an exact registered entity (or its plural) wins — a name that IS an entity is never merged;
      2. a compound name folds to the registered entity it extends (product_metadata -> product), longest
         match, but ONLY when the compound is not itself a registered entity (order_history stays);
      3. with no entity info, fall back to plain singular/plural folding.
    Deterministic, no regex; conservative — never merges two registered entities."""
    n = (name or "").strip().lower()
    if not n:
        return n
    s = _depluralize(n)
    if _ENTITIES:
        if n in _ENTITIES:
            return n                             # already a canonical entity
        if s in _ENTITIES:
            return s                             # plural spelling of an entity
        for e in sorted(_ENTITIES, key=len, reverse=True):   # longest entity prefix wins
            if n == e or n.startswith(e + "_") or s == e or s.startswith(e + "_"):
                return e
    return s


def repo(name):
    """Return the process-wide singleton Repo for `name` (created on first use). Singular/plural spellings
    of the same entity resolve to one store (see _canon_store)."""
    with _RLOCK:
        return _REGISTRY.setdefault(_canon_store(name), Repo())
'''

_CREATE = ("create", "add", "register", "submit", "new", "insert", "upload", "save", "store",
           "post", "generate", "make", "build")
_GET = ("get", "fetch", "retrieve", "find", "show", "read", "lookup", "resolve")
_LIST = ("list", "browse", "all", "search", "query", "index", "view")
_UPDATE = ("update", "edit", "modify", "confirm", "cancel", "approve", "reject", "set", "change",
           "associate", "assign", "toggle", "switch", "activate", "deactivate", "publish")
_DELETE = ("delete", "remove", "discard", "clear", "unregister", "revoke")

# Free-text search params: on a list/search operation, a param with one of these names carries a search
# TERM (substring-matched), not an equality key. Only these get wildcard-normalized (see _wildcard) so a
# "show everything" query returns all. Equality filters (user_id, category, status) are deliberately absent.
_SEARCH_PARAM = ("query", "q", "search", "search_term", "searchterm", "term", "keyword", "keywords",
                 "text", "filter", "phrase")


def _verb(op_name: str) -> str | None:
    """Classify an operation into a CRUD verb by its leading token (deterministic, lexical)."""
    head = (op_name or "").split("_")[0].lower()
    for verbs, kind in ((_CREATE, "create"), (_GET, "get"), (_LIST, "list"),
                        (_UPDATE, "update"), (_DELETE, "delete")):
        if head in verbs:
            return kind
    return None


def _has_id_input(inputs: list) -> bool:
    """True if an operation already takes an identifier (a param named `id` or ending in `_id`)."""
    for p in inputs or []:
        n = (p.get("name") or "").lower()
        if n == "id" or n.endswith("_id"):
            return True
    return False


def normalize_crud_ids(contract: dict, log=print) -> int:
    """DETERMINISTIC contract normalization: an UPDATE or DELETE operation must take an identifier to
    locate the record. When the design omitted one (e.g. `update_menu(menu)`), prepend an `id` input so
    the generated endpoint ACCEPTS an id and the CRUD body updates/deletes BY id — otherwise the body
    falls back to using the payload as the key and update/delete-by-id silently returns None. Mutates the
    contract in place (so both the endpoint scaffold and the body scaffold see the id). Returns the count."""
    added = 0
    for v in contract.values():
        for e in v.get("exports", []):
            if e.get("kind") != "function":
                continue
            if _verb(e.get("symbol", "")) in ("update", "delete") and not _has_id_input(e.get("inputs")):
                e.setdefault("inputs", []).insert(0, {"name": "id", "type": "int"})
                added += 1
    if added:
        log(f"  [contract-norm] added an id input to {added} update/delete op(s) that lacked one")
    return added


def _bare_type(t) -> str:
    """Strip container/optional wrappers to the base type name (`list[Menu]`/`Menu | None` -> `Menu`)."""
    if not isinstance(t, str):
        return ""
    return (t.replace("list[", "").replace("List[", "").replace("Optional[", "")
            .replace("]", "").replace(" | None", "").strip())


_NON_ENTITY_TYPES = {"", "void", "none", "bool", "boolean", "str", "string", "int", "integer",
                     "float", "dict", "any", "object", "list", "tuple"}


def _repo_name(sym: str, returns, key: str) -> str:
    """The store an operation reads/writes. Prefer the entity in its return type; when that isn't an
    entity (void/bool/…), fall back to the noun in the operation name (`delete_menu` -> `menu`), so
    create/get/update/delete on the same entity share one store. Deterministic."""
    t = _bare_type(returns).lower().replace(" ", "_")
    if t and t not in _NON_ENTITY_TYPES:
        return t
    parts = (sym or "").split("_")
    if len(parts) > 1:
        return "_".join(parts[1:]).lower()
    return key


# ── Endpoint parameter binding ──────────────────────────────────────────── #
# A FastAPI handler parameter with NO annotation is bound from the QUERY STRING.
# For a scalar that is merely sloppy; for a structured payload it is wrong (the
# object arrives as opaque text); for file bytes the request line overflows and
# the upload can NEVER work. The annotation is what decides where FastAPI reads
# the value from, so the scaffold emits one for every endpoint parameter:
#   scalar      -> str/int/float/bool   -> query string
#   structured  -> dict / list          -> request BODY
#   binary      -> UploadFile           -> multipart body
# Deterministic and project-agnostic: keyed on the contract's declared type, and
# on the parameter name only to tell a payload from a reference to one.

_PY_SCALAR = {
    "str": "str", "string": "str", "text": "str", "uuid": "str", "email": "str",
    "date": "str", "datetime": "str", "timestamp": "str",
    "int": "int", "integer": "int", "long": "int",
    "float": "float", "number": "float", "decimal": "float",
    "bool": "bool", "boolean": "bool",
}

#: names that carry the bytes themselves
_BINARY_PARAM_WORDS = frozenset({
    "file", "files", "upload", "image", "images", "photo", "picture",
    "avatar", "logo", "attachment", "document", "blob", "bytes",
})
#: trailing words that make a file-shaped name a REFERENCE to a file, not the
#: file — `image_id` and `file_name` are scalars and belong in the query string.
_REFERENCE_WORDS = frozenset({
    "id", "ids", "name", "names", "url", "urls", "uri", "path", "paths",
    "key", "keys", "ref", "type", "types", "count", "size",
})


def _name_words(name: str) -> list:
    """Split an identifier into lowercase words on underscores AND camelCase
    boundaries, so `tenantId` and `tenant_id` read the same. No regex."""
    words, current = [], ""
    for ch in (name or ""):
        if ch == "_":
            if current:
                words.append(current.lower())
            current = ""
        elif ch.isupper() and current and not current[-1].isupper():
            words.append(current.lower())
            current = ch
        else:
            current += ch
    if current:
        words.append(current.lower())
    return [w for w in words if w]


def _is_reference_name(name: str) -> bool:
    """True when the name denotes a HANDLE to a thing rather than the thing:
    `image_id`, `tenantId`, `file_name`. The declared type may still say the
    entity (an Architect contract routinely types `image_id` as `FoodImage`),
    but an identifier is a scalar however it is typed — so the NAME wins here,
    otherwise the client is asked to post a whole object to supply an id."""
    words = _name_words(name)
    return len(words) > 1 and words[-1] in _REFERENCE_WORDS


def _is_binary_param(name: str) -> bool:
    words = _name_words(name)
    if not words:
        return False
    if _is_reference_name(name):
        return False
    return bool(set(words) & _BINARY_PARAM_WORDS)


def _singular(n: str) -> str:
    """Plural fold for entity-name matching. (The persistence TEMPLATE has its own copy
    of this inside the generated store module; this one is the scaffold's own.)"""
    if len(n) > 3 and n.endswith("ies"):
        return n[:-3] + "y"
    if len(n) > 2 and n.endswith("s") and not n.endswith("ss"):
        return n[:-1]
    return n


def _entity_names(contract: dict) -> set:
    """Every entity the contract names — concept keys and exported classes, singular
    and plural. An untyped parameter named after one carries that entity, not a scalar."""
    names = set()
    for key, v in (contract or {}).items():
        for candidate in (key, v.get("concept")):
            if isinstance(candidate, str) and candidate:
                names.add(candidate.lower())
                names.add(_singular(candidate.lower()))
        for e in v.get("exports", []) or []:
            if e.get("kind") == "class" and e.get("symbol"):
                sym = e["symbol"].lower()
                names.add(sym)
                names.add(_singular(sym))
    return names


def _declared_types(contract: dict) -> set:
    """Every type the contract actually DECLARES (its exported classes). An input
    typed as something never declared is a DANGLING reference — the Architect
    naming a type it never defined (e.g. `language: Language` where no Language
    class exists). A dangling type is not an entity, so binding it to the request
    body would make the client post an object for what is a plain value."""
    return {e["symbol"] for v in (contract or {}).values()
            for e in (v.get("exports") or [])
            if e.get("kind") == "class" and e.get("symbol")}


#: type-name endings that denote an aggregate rather than a value
_STRUCTURE_SUFFIXES = (
    "form", "data", "payload", "request", "input", "spec", "details", "detail",
    "info", "config", "configuration", "options", "params", "parameters",
    "record", "entry", "object", "dto",
)


def _is_structured_type_name(type_name: str) -> bool:
    """True when an UNDECLARED type name denotes a structure rather than a value.

    Two independent signals, both name-only because there is no declaration to
    consult: an explicit structure suffix (`ReservationForm`, `OrderPayload`), or a
    compound identifier (`ShippingAddress`) — a single bare word like `Language` or
    `Currency` reads as a value.
    """
    words = _name_words(type_name)
    if not words:
        return False
    if words[-1] in _STRUCTURE_SUFFIXES:
        return True
    return len(words) > 1


def _param_binding(param_type, name: str, entities: set, declared: set | None = None) -> tuple:
    """(annotation, where) for one endpoint parameter. where: binary|body|query."""
    if _is_binary_param(name):
        return "UploadFile", "binary"
    if _is_reference_name(name):
        # an identifier/handle — scalar in the query string, whatever the
        # contract declares its type to be
        base = _bare_type(param_type).lower()
        return _PY_SCALAR.get(base, "str"), "query"
    raw = str(param_type or "").strip().lower()
    if raw.startswith(("list", "array", "sequence", "set[")):
        return "list", "body"
    base = _bare_type(param_type).lower()
    if base in _PY_SCALAR:
        return _PY_SCALAR[base], "query"
    if base in ("dict", "object", "any", "json", "map"):
        return "dict", "body"
    if base and base not in _NON_ENTITY_TYPES:
        if declared is not None and _bare_type(param_type) not in declared:
            # Dangling: the contract names a type it never declares. Most are plain
            # values the Architect over-typed (`language: Language`) — but some name
            # a STRUCTURE (`reservationForm: ReservationForm`), and sending a form in
            # the query string is the very defect this binding exists to prevent. A
            # dangling type is structured when its own name says so.
            if _is_structured_type_name(_bare_type(param_type)):
                return "dict", "body"
            return "str", "query"
        return "dict", "body"          # a named entity — a structured payload
    if not base and (name or "").lower() in entities:
        return "dict", "body"          # untyped, but named after a contract entity
    return "str", "query"


def _crud_body(verb: str | None, params: list[str], repo_name: str) -> str | None:
    """Real body delegating to the repository, or None for a non-CRUD op (left as a stub)."""
    if not verb:
        return None
    r = f'repo("{repo_name}")'
    if verb == "create":
        return f"return {r}.create({params[0] if params else '{}'})"
    if verb == "get":
        return f"return {r}.get({params[0] if params else 'None'})"
    if verb == "list":
        return f"return {r}.list()"
    if verb == "update":
        ident = params[0] if params else "None"
        data = params[1] if len(params) > 1 else (params[0] if params else "{}")
        return f"return {r}.update({ident}, {data})"
    if verb == "delete":
        return f"return {r}.delete({params[0] if params else 'None'})"
    return None


_INDEX_HTML = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Application</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; margin: 0; background: #0e1116; color: #e6e6e6; }
  header { background: #161b22; padding: 1rem 1.5rem; border-bottom: 1px solid #30363d; }
  header h1 { margin: 0; font-size: 1.2rem; }
  header a { color: #58a6ff; font-size: .85rem; }
  main { max-width: 920px; margin: 1.5rem auto; padding: 0 1rem; }
  .grp { margin: 1.5rem 0 .5rem; font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; color: #8b949e; }
  .ep { border: 1px solid #30363d; border-radius: 8px; padding: .9rem 1rem; margin: .6rem 0; background: #161b22; }
  .ep h3 { margin: 0 0 .5rem; font-size: .95rem; font-family: ui-monospace, monospace; }
  .m { font-size: .7rem; padding: .1rem .4rem; border-radius: 4px; background: #238636; color: #fff; margin-right: .4rem; }
  input { margin: .2rem .3rem .2rem 0; padding: .35rem .5rem; background: #0d1117; color: #e6e6e6;
          border: 1px solid #30363d; border-radius: 5px; }
  button { padding: .4rem .9rem; cursor: pointer; background: #238636; color: #fff; border: 0; border-radius: 5px; }
  pre { background: #0d1117; padding: .5rem .7rem; border-radius: 5px; overflow: auto; margin: .5rem 0 0;
        font-size: .8rem; white-space: pre-wrap; word-break: break-all; }
</style>
</head>
<body>
<header><h1 id="title">Application</h1><a href="/docs">API docs →</a></header>
<main id="app">loading…</main>
<script>
async function load() {
  const spec = await (await fetch("/openapi.json")).json();
  document.getElementById("title").textContent = (spec.info && spec.info.title) || "Application";
  const app = document.getElementById("app"); app.innerHTML = "";
  const groups = {};
  for (const [path, item] of Object.entries(spec.paths)) {
    const g = path.split("/").filter(Boolean)[0] || "api";
    (groups[g] = groups[g] || []).push([path, item]);
  }
  for (const [g, eps] of Object.entries(groups)) {
    const h = document.createElement("div"); h.className = "grp"; h.textContent = g; app.appendChild(h);
    for (const [path, item] of eps) {
      for (const [method, op] of Object.entries(item)) {
        if (!["get", "post", "put", "delete"].includes(method)) continue;
        const params = (op.parameters || []).filter(p => p.in === "query");
        const div = document.createElement("div"); div.className = "ep";
        div.innerHTML = `<h3><span class="m">${method.toUpperCase()}</span>${path}</h3>`;
        const inputs = {};
        params.forEach(p => {
          const i = document.createElement("input"); i.placeholder = p.name; div.appendChild(i); inputs[p.name] = i;
        });
        const btn = document.createElement("button"); btn.textContent = "Call";
        const out = document.createElement("pre"); out.style.display = "none";
        btn.onclick = async () => {
          const q = Object.entries(inputs).filter(([k, i]) => i.value !== "")
            .map(([k, i]) => `${encodeURIComponent(k)}=${encodeURIComponent(i.value)}`).join("&");
          const url = path + (q ? `?${q}` : "");
          out.style.display = "block"; out.textContent = "…";
          try {
            const r = await fetch(url, { method: method.toUpperCase() });
            out.textContent = `HTTP ${r.status}\\n` + await r.text();
          } catch (e) { out.textContent = "ERROR " + e; }
        };
        div.appendChild(btn); div.appendChild(out); app.appendChild(div);
      }
    }
  }
}
load().catch(e => { document.getElementById("app").textContent = "Failed to load API: " + e; });
</script>
</body>
</html>
'''


def scaffold_frontend(workspace: str, log=print) -> str:
    """Write a generic, self-contained web UI driven by the app's own /openapi.json: a form per
    endpoint that calls the API and shows the response. Works for ANY backend the pipeline builds —
    the deliverable is a clickable application, not just API docs. Returns its path."""
    fe = os.path.join(workspace, "frontend")
    os.makedirs(fe, exist_ok=True)
    rel = "frontend/index.html"
    with open(os.path.join(workspace, rel), "w", encoding="utf-8") as f:
        f.write(_INDEX_HTML)
    log(f"  [scaffold] {rel} (generic openapi-driven web UI)")
    return rel


def scaffold_persistence(workspace: str, contract: dict | None = None, log=print) -> str:
    """Write the real in-memory repository module (once). Returns its module path. When `contract` is
    given, inject its ENTITY names into the store so _canon_store can fold compound store spellings
    (product_metadata -> product) to one key while keeping distinct entities (order_history) apart."""
    pkg = os.path.join(workspace, "app", "repositories")
    os.makedirs(pkg, exist_ok=True)
    open(os.path.join(pkg, "__init__.py"), "a", encoding="utf-8").close()
    rel = "app/repositories/store.py"
    body = _STORE_PY
    if contract:
        ents = sorted({k for k, v in contract.items() if v.get("kind") == "entity" and k})
        if ents:
            inject = "_ENTITIES = frozenset({" + ", ".join(repr(e) for e in ents) + "})"
            body = body.replace("_ENTITIES = frozenset()", inject, 1)
    with open(os.path.join(workspace, rel), "w", encoding="utf-8") as f:
        f.write(body)
    log(f"  [scaffold] {rel} (generic in-memory repository — real data-access seam)")
    return rel


# ---- authentication seam (environment-provided) -------------------------------------------------
# When the Architect records an `environment_capabilities` auth obligation, authentication is provided
# by the shared oauth2-proxy (it performs the Google sign-in and injects X-Auth-Request-Email). The app
# implements NO OAuth; it only READS that header. We scaffold that read deterministically so the
# invariant holds regardless of model output — the same reasoning as the CRUD seam above.
_SECURITY_PY = '''"""Authentication seam — identity comes from the ENVIRONMENT, not this app.

A shared oauth2-proxy fronts the domain: it performs the Google sign-in and injects the authenticated
user's email as the `X-Auth-Request-Email` request header. This app implements NO OAuth, sessions, or
tokens — it TRUSTS that proxy-injected header as the identity. The deploy stage registers the app behind
oauth2-proxy (see nginx_register_app); locally (no proxy) the header is absent and the user is anonymous.
"""
from fastapi import HTTPException, Request

AUTH_HEADER = "x-auth-request-email"


def get_authenticated_user(request: Request):
    """Return the signed-in user's email (proxy-injected), or None when unauthenticated / local-dev.
    Never trust a client-supplied email — only this proxy-injected header is authoritative."""
    return request.headers.get(AUTH_HEADER)


def require_user(request: Request) -> str:
    """Same as get_authenticated_user, but 401 when absent — use to gate a protected endpoint."""
    email = get_authenticated_user(request)
    if not email:
        raise HTTPException(status_code=401, detail="authentication required")
    return email
'''


def scaffold_security(workspace: str, log=print) -> str:
    """Write the deterministic authentication seam (app/security.py). Returns its module path."""
    os.makedirs(os.path.join(workspace, "app"), exist_ok=True)
    open(os.path.join(workspace, "app", "__init__.py"), "a", encoding="utf-8").close()
    rel = "app/security.py"
    with open(os.path.join(workspace, rel), "w", encoding="utf-8") as f:
        f.write(_SECURITY_PY)
    log(f"  [scaffold] {rel} (auth seam — reads X-Auth-Request-Email; app implements no OAuth)")
    return rel


def scaffold(workspace: str, contract: dict, log=print, bodies: str = "working") -> dict:
    """Write a skeleton module for every contract concept. Returns what was written.

    `bodies` controls how function bodies are seeded:
      - "working"     — a repo()-delegating CRUD body (green by construction). Used as the finalize
                        FALLBACK to backfill any operation the model left unimplemented.
      - "placeholder" — `raise NotImplementedError(...)`, so the model SEES the operation needs real
                        logic and fills it (a working stub reads as 'already done' and gets skipped).
                        Used for the INITIAL scaffold the model builds on."""
    paths = module_paths(contract)
    written = []
    # ensure package __init__.py files exist along the way
    pkgs = set()
    for rel in paths.values():
        d = os.path.dirname(rel)
        while d and d != ".":
            pkgs.add(d)
            d = os.path.dirname(d)
    for pkg in sorted(pkgs):
        os.makedirs(os.path.join(workspace, pkg), exist_ok=True)
        init = os.path.join(workspace, pkg, "__init__.py")
        if not os.path.isfile(init):
            open(init, "w", encoding="utf-8").close()

    for key, v in contract.items():
        rel = paths[key]
        header = [f'"""{v.get("concept", key)} ({v.get("kind")}) — generated from the code contract."""']
        # dependency imports: each dependency's exported symbols by their exact contract names. These are
        # referenced only in type positions (field-type comments / annotations, or heal-filled hints), NEVER
        # in executable scaffold code, and concepts routinely reference each other (tenant <-> tenant_config),
        # so importing them eagerly at module load forms circular-import chains that crash the boot the moment
        # a model module is imported at runtime (e.g. a router that mounts an entity-hosted operation). Guard
        # them under TYPE_CHECKING: available to type-checkers and as heal context, inert at runtime.
        imports = []
        type_imports = []
        for dep in v.get("depends_on", []):
            dv = contract.get(dep)
            if not dv or dep == key:
                continue
            syms = [e["symbol"] for e in dv.get("exports", []) if e.get("symbol")]
            if syms:
                type_imports.append(f"    from {_module_dotted(paths[dep])} import {', '.join(sorted(set(syms)))}")
        # exports: a filled body per contract export — CRUD operations delegate to the real repository
        # (working by construction); classes keep their fields; non-CRUD ops stay stubs for the model.
        body_lines, needs_repo = [], False
        for e in v.get("exports", []):
            sym = e.get("symbol")
            if not sym:
                continue
            if e.get("kind") == "class":
                body_lines.append(f"class {sym}:")
                fields = [f for f in e.get("fields", []) if f.get("name")]
                if fields:
                    for f in fields:
                        body_lines.append(f"    {f['name']} = None  # contract field: {f.get('type','')}")
                else:
                    body_lines.append("    pass")
                body_lines.append("")
            elif e.get("kind") == "function":
                params = [p["name"] for p in e.get("inputs", []) if p.get("name")]
                # An upload parameter reaches the service as the raw FILE BYTES — the
                # router does `await file.read()`. Say so in the seeded signature: the
                # model extends this stub, and without the annotation it assumes a dict
                # and writes `image_data.copy()`, which is an AttributeError at runtime.
                sig_params = [f"{n}: bytes" if _is_binary_param(n) else n for n in params]
                body_lines.append(f"def {sym}({', '.join(sig_params)}):")
                if bodies == "placeholder":
                    # An explicit not-yet-implemented body: the model recognizes it must supply the real
                    # logic (a working stub reads as done and is skipped). finalize backfills any left.
                    body_lines.append(
                        f'    raise NotImplementedError("{sym}: implement per the assigned task(s)")')
                    needs_repo = True                    # keep the seam imported and ready to use
                else:
                    verb = _verb(sym)
                    crud = _crud_body(verb, params, _repo_name(sym, e.get("returns"), key))
                    if crud:
                        body_lines.append(f"    {crud}")
                        needs_repo = True
                    else:
                        body_lines.append(f"    ...  # custom operation (no CRUD mapping) -> {e.get('returns')}")
                body_lines.append("")
        if needs_repo:
            imports.append("from app.repositories.store import repo")
        type_block = (["from typing import TYPE_CHECKING", "", "if TYPE_CHECKING:"]
                      + sorted(type_imports)) if type_imports else []
        lines = header + sorted(imports) + type_block + [""] + body_lines
        content = "\n".join(lines) + "\n"
        abspath = os.path.join(workspace, rel)
        os.makedirs(os.path.dirname(abspath), exist_ok=True)
        with open(abspath, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(rel)
        log(f"  [scaffold] {rel} exports={[e.get('symbol') for e in v.get('exports',[])]} "
            f"deps={v.get('depends_on')}")
    return {"modules": len(written), "paths": written}


def scaffold_api(workspace: str, contract: dict, log=print, auth: bool = False) -> dict:
    """Extend the scaffold to the DELIVERY layer so the WHOLE structure is coherent by construction,
    not just models/services: for each service, a router module that imports the service functions by
    their exact contract names and exposes an endpoint per operation; and the entrypoint (main.py)
    that imports every router and mounts it. Routers therefore stop inventing imports (the cause of
    the residual `missing_module` drift), and every service is reachable (routes wired by
    construction). FastAPI-specific — the one stack-aware step, kept small."""
    # Routability follows CONTENT, not the concept's `kind` label: any concept that exports at least
    # one FUNCTION owns operations and gets a router; the operations are exactly its function exports
    # (class exports are data models, never routed). The Architect sometimes co-locates operations on
    # an entity (e.g. `reservation` owns `submit_reservation`), so gating on kind=="service" silently
    # drops those endpoints. `kind` still drives file placement (module_paths), only not routing.
    def _ops(v):
        return [e for e in v.get("exports", []) if e.get("kind") == "function" and e.get("symbol")]
    routable = {k: v for k, v in contract.items() if _ops(v)}
    paths = module_paths(contract)
    os.makedirs(os.path.join(workspace, "app/api"), exist_ok=True)
    open(os.path.join(workspace, "app/api/__init__.py"), "a", encoding="utf-8").close()
    routers = []
    hygiene = []          # route-hygiene findings across every router written here
    needs_multipart = False   # any UploadFile emitted -> the app needs python-multipart
    for key, v in routable.items():
        rel = f"app/api/{key}.py"
        ops = _ops(v)
        syms = [e["symbol"] for e in ops]
        lines = [f'"""{v.get("concept", key)} API router — generated from the code contract."""',
                 "__FASTAPI_IMPORT__"]
        if syms:
            lines.append(f"from {_module_dotted(paths[key])} import {', '.join(sorted(set(syms)))}")
        lines += ["", f'router = APIRouter(prefix="/{key}", tags=["{key}"])', ""]
        needs_wildcard = False
        needs_coerce = False
        needs_upload = False
        needs_form = False
        entities = _entity_names(contract)
        declared = _declared_types(contract)
        for e in ops:
            op = e["symbol"]
            names = [p["name"] for p in e.get("inputs", []) if p.get("name")]
            # READ operations (get/list) load data on page open; a missing or mis-named query param must
            # DEGRADE GRACEFULLY (return empty) rather than hard-fail with 422 and blank the page. So their
            # endpoint params are OPTIONAL (=None). WRITE operations keep required params (you cannot create
            # without data). Deterministic, project-agnostic — keyed on the CRUD verb, not any field name.
            is_read = _verb(op) in ("get", "list")
            # SEARCH DEGRADES TO LIST-ALL: on a list/search op, a free-text search param called with a
            # "show everything" placeholder (query="all"/"*"/None) is folded to "" so the model's substring
            # match returns every record instead of none — the universal "empty search lists all" convention.
            # Deterministic, keyed on the CRUD verb + param name, project-agnostic; equality filters untouched.
            is_search = _verb(op) == "list"
            # ANNOTATE every parameter — the annotation is what binds it to the query
            # string, the request body, or a multipart upload (see _param_binding).
            # Without one FastAPI puts EVERYTHING in the query string, which silently
            # breaks structured payloads and makes file upload impossible.
            bindings = [
                (p_.get("name"), *_param_binding(p_.get("type"), p_.get("name"), entities, declared))
                for p_ in e.get("inputs", []) if p_.get("name")
            ]
            has_upload = any(where == "binary" for _, _, where in bindings)
            parts = []
            for n, ann, where in bindings:
                if where == "binary":
                    parts.append((n, f"{n}: {ann}", False))      # UploadFile takes no default
                elif has_upload and where == "body":
                    # A structured param CANNOT be a JSON body next to an UploadFile:
                    # the request is multipart, so FastAPI reads it as a form field and
                    # then refuses to coerce that string into a dict — the endpoint
                    # becomes unsatisfiable by ANY client (422 whatever you send).
                    # Take it as a Form field and parse it at the boundary, which is
                    # exactly what _coerce already does for JSON-in-a-string.
                    needs_form = True
                    parts.append((n, f"{n}: str = Form(...)", True))
                elif is_read:
                    parts.append((n, f"{n}: {ann} | None = None", True))
                else:
                    parts.append((n, f"{n}: {ann}", False))
            # Python requires defaulted parameters last; emit in that order and call by
            # KEYWORD so the service still receives its arguments correctly.
            parts.sort(key=lambda t: t[2])
            sig = ", ".join(frag for _, frag, _ in parts)
            # STRUCTURED PARAMS ARRIVE AS JSON STRINGS: a list/dict param sent in a query string reaches the
            # service as TEXT (dependencyList='[{...}]'); a body that iterates or subscripts it then crashes
            # (`'str' object has no attribute 'get'`). Coerce every param through _coerce (parses a JSON
            # object/array string to its real value, leaves plain scalars untouched) at the boundary so
            # services receive structured data. Safe because no generated service parses JSON itself. Search
            # params are additionally wildcard-normalized (above). Deterministic, project-agnostic.
            binary_params = {n for n, _, where in bindings if where == "binary"}

            def _arg(n):
                if n in binary_params:
                    return f"await {n}.read()"      # the BYTES, not the upload object
                if is_search and n.lower() in _SEARCH_PARAM:
                    return f"_wildcard(_coerce({n}))"
                return f"_coerce({n})"

            call = ", ".join(f"{n}={_arg(n)}" for n in names)
            if any(n not in binary_params for n in names):
                needs_coerce = True
            if has_upload:
                needs_upload = True
                needs_multipart = True
            if is_search and any(n.lower() in _SEARCH_PARAM for n in names):
                needs_wildcard = True
            lines.append(f'@router.post("/{op}")')
            lines.append(f"{'async def' if has_upload else 'def'} {op}_endpoint({sig}):")
            lines.append(f"    return {op}({call})")
            lines.append("")
        fastapi_names = (["APIRouter"] + (["UploadFile"] if needs_upload else [])
                         + (["Form"] if needs_form else []))
        lines[1] = f"from fastapi import {', '.join(fastapi_names)}"
        helpers = (["_coerce"] if needs_coerce else []) + (["_wildcard"] if needs_wildcard else [])
        if helpers:
            # import the boundary helpers next to the fastapi import (the store module is always scaffolded)
            lines.insert(2, f"from app.repositories.store import {', '.join(helpers)}")
        router_path = os.path.join(workspace, rel)
        with open(router_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        routers.append(key)
        log(f"  [scaffold-api] {rel} mounts {len(v.get('exports', []))} operation(s)")
        # REGRESSION GUARD: re-read what we just wrote and assert every endpoint
        # parameter is annotated. An unannotated parameter is bound from the query
        # string, which silently breaks structured payloads and makes upload
        # impossible — the defect _param_binding exists to prevent. If this ever
        # fires, the GENERATOR is wrong; do not patch the output.
        from . import verify as _verify
        for _f in _verify.route_hygiene(router_path, rel):
            hygiene.append(_f)
            log(f"  [scaffold-api] !! {_f['severity']}: {_f['rule']} {rel}:{_f['line']} — {_f['message']}")

    # entrypoint: import every router and mount it -> all services reachable by construction; then
    # serve the generated frontend/ (the real UI pages) as static files at "/", so the API routes
    # (added first) still win and the pages are served for everything else.
    m = ['"""Application entrypoint — generated from the code contract."""',
         "import os", "from fastapi import FastAPI", "from fastapi.staticfiles import StaticFiles"]
    if auth:
        m.append("from fastapi import Request")
        m.append("from app.security import get_authenticated_user")
    for key in routers:
        m.append(f"from app.api.{key} import router as {key}_router")
    m += ["", "app = FastAPI()"]
    for key in routers:
        m.append(f"app.include_router({key}_router)")
    # AUTH SEAM (environment-provided): expose who the oauth2-proxy signed in. The app implements no
    # OAuth — this only READS the proxy-injected X-Auth-Request-Email header. Reachable by construction.
    if auth:
        # accept BOTH GET and POST: generated frontends call it either way (the google-auth skill's
        # sign-in check POSTs), and a method mismatch is a 405 dead-end, not an auth decision.
        m += ["",
              "@app.api_route(\"/whoami\", methods=[\"GET\", \"POST\"])",
              "def whoami(request: Request):",
              "    email = get_authenticated_user(request)",
              "    return {\"authenticated\": bool(email), \"email\": email}"]
    m += ["",
          '_FE = os.path.join(os.path.dirname(__file__), "frontend")',
          "if os.path.isdir(_FE):",
          '    app.mount("/", StaticFiles(directory=_FE, html=True), name="site")',
          ""]
    with open(os.path.join(workspace, "main.py"), "w", encoding="utf-8") as f:
        f.write("\n".join(m) + "\n")
    log(f"  [scaffold-api] main.py mounts {len(routers)} router(s) + serves frontend/ at /")
    # An UploadFile route needs python-multipart at RUNTIME; without it FastAPI
    # raises on the first multipart request. The scaffold emits the route, so the
    # scaffold owns the dependency — never leave it to whether the frame architect
    # happened to list it.
    if needs_multipart:
        _ensure_requirement(workspace, "python-multipart", log)

    return {"routers": len(routers), "entrypoint": "main.py", "hygiene": hygiene}


def _ensure_requirement(workspace: str, package: str, log=print) -> bool:
    """Add `package` to the app's requirements.txt if absent. Returns True if added."""
    path = os.path.join(workspace, "requirements.txt")
    try:
        existing = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
    except OSError:
        return False
    for line in existing.splitlines():
        name = line.strip().split("==")[0].split(">=")[0].split("[")[0].strip().lower()
        if name == package.lower():
            return False
    try:
        with open(path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(package + "\n")
    except OSError:
        return False
    log(f"  [scaffold-api] added runtime dependency: {package}")
    return True
