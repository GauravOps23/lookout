"""OpenAI-compatible chat client (Ollama, vLLM, ...). Tests replace `CLIENT` with a fake."""
import json
import re

import httpx

from app.errors import AppError


class ChatClient:
    def chat(self, cfg: dict, api_key: str, messages: list[dict], tools: list[dict] | None) -> dict:
        """Return {"content": str, "tool_calls": [{"id", "name", "args"}]}. Not streamed: the whole reply
        is returned and the API layer emits it as one `token` event."""
        body = {"model": cfg["model"], "messages": messages, "temperature": cfg["temperature"],
                "max_tokens": cfg["max_tokens"], "stream": False}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            r = httpx.post(cfg["base_url"].rstrip("/") + "/chat/completions", json=body, headers=headers,
                           timeout=cfg["timeout_s"])
        except httpx.TimeoutException:
            raise AppError("TIMEOUT", "The model did not answer in time") from None
        except httpx.HTTPError:
            raise AppError("UPSTREAM_ERROR", "The model endpoint is not reachable") from None
        if r.status_code != 200:
            raise AppError("UPSTREAM_ERROR", f"The model endpoint returned HTTP {r.status_code}")
        try:
            msg = r.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise AppError("UPSTREAM_ERROR", "Unexpected reply from the model endpoint") from None
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = None  # invalid JSON: the agent returns a VALIDATION error to the model
            calls.append({"id": c.get("id") or f"call_{len(calls)}", "name": fn.get("name", ""), "args": args})
        return {"content": msg.get("content") or "", "tool_calls": calls}


CLIENT = ChatClient()

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def parse_json_tool(content: str) -> dict | None:
    """tool_mode=json fallback: the model answers with {"tool": ..., "args": {...}}."""
    m = _JSON_BLOCK.search(content)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    if isinstance(d, dict) and isinstance(d.get("tool"), str):
        return {"id": "json_call", "name": d["tool"], "args": d.get("args") if isinstance(d.get("args"), dict) else {}}
    return None
