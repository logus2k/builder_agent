"""agent_server client — the house pattern (see analyst_agent / architect_agent).

One stateless call per item: `model` carries the agent PRESET NAME (a registered persona), we
send only the user content, and the preset supplies the system prompt + sampling. This is the
documented "A1" way to use the active local LLM — the Builder's reasoning (project frame, file
placement) runs through `builder_*` personas registered in agent_server, exactly like the
Analyst's and Architect's roles. (opencode, the code-writing executor, talks to the same active
model directly at :8500 — that is its nature; our *reasoning* goes through personas.)

Stdlib-only (no new dependency); no regex. Degrades to None on transport/parse failure so a
single flaky call never crashes a build.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

AGENT_SERVER_URL = os.environ.get("AGENT_SERVER_URL", "http://localhost:7701").rstrip("/")
#: Generous: a queued request behind a busy slot legitimately takes minutes.
TIMEOUT = float(os.environ.get("BUILDER_LLM_TIMEOUT", "900"))


def _parse_json(content: str) -> dict | None:
    """raw -> fence-strip -> brace-slice (the house 3-tier parse, without regex)."""
    content = (content or "").strip()
    for candidate in (content,
                      content.strip("`").lstrip("json").strip() if content.startswith("`") else content,
                      content[content.find("{"): content.rfind("}") + 1] if "{" in content and "}" in content else ""):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def complete_json(agent: str, user_content: str, timeout: float | None = None) -> dict | None:
    """Call the `agent` persona (by name) with one user message, expecting a JSON object.
    Returns the parsed dict, or None on transport/parse failure."""
    payload = json.dumps({
        "model": agent,
        "messages": [{"role": "user", "content": user_content}],
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(f"{AGENT_SERVER_URL}/v1/chat/completions", data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return _parse_json(content)
