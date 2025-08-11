"""Thin wrapper around the OpenAI client used by phase mode."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import hashlib

_client = None

DEFAULT_MODEL = "o3"
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_SERVICE_TIER = "flex"


def get_cache_key(*, model: str, messages: List[Dict], functions: Any, function_call: Any) -> str:
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "functions": functions,
            "function_call": function_call,
        },
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def load_cache(key: str):
    memo_dir = os.environ.get("LLM_MEMO_DIR")
    if not memo_dir:
        return None
    path = Path(memo_dir) / f"{key}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_cache(key: str, response: Any) -> None:
    memo_dir = os.environ.get("LLM_MEMO_DIR")
    if not memo_dir:
        return
    Path(memo_dir).mkdir(parents=True, exist_ok=True)
    try:
        data = response.model_dump()
    except Exception:
        try:
            data = json.loads(response.model_dump_json())
        except Exception:
            data = json.loads(json.dumps(response, default=str))
    path = Path(memo_dir) / f"{key}.json"
    path.write_text(json.dumps(data))

    # Maintain a lockfile mapping keys to cached files for deterministic replay.
    lock_path = Path(memo_dir) / "lock.json"
    try:
        lock = json.loads(lock_path.read_text())
    except Exception:
        lock = {}
    lock[key] = f"{key}.json"
    lock_path.write_text(json.dumps(lock, indent=2))


def openai_configure_api(api_key: Optional[str] = None):
    """Retrieve key, build global client, log success."""
    global _client
    if _client is not None:
        return _client
    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover - import failure path
        logging.warning("openai package not available: %s", exc)
        return None
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        logging.warning("OPENAI_API_KEY is not set")
        return None
    _client = OpenAI(api_key=key)
    logging.info("OpenAI client configured")
    return _client


def openai_generate_response(
    *,
    messages: List[Dict[str, str]],
    functions: Optional[List[Dict[str, Any]]] = None,
    function_call: Optional[str | Dict[str, str]] = "auto",
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    service_tier: str = DEFAULT_SERVICE_TIER,
    temperature: float = 0,
    **extra: Any,
):
    """Wrapper around ``client.responses.create`` with defaults."""
    key = get_cache_key(
        model=model, messages=messages, functions=functions, function_call=function_call
    )
    cached = load_cache(key)
    if cached is not None:
        return cached

    client = openai_configure_api()
    if client is None:
        raise RuntimeError("OpenAI client is not configured")

    tools: List[Dict[str, Any]] = []
    if functions:
        # Responses API flat tool schema: {"type":"function","name":...,"description":...,"parameters":...}
        tools = [
            {
                "type": "function",
                "name": f["name"],
                "description": f.get("description", ""),
                "parameters": f.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
            for f in functions
        ]

    # Prefer Responses API. If callers passed Chat-style `messages`, we still put them in `input`.
    params: Dict[str, Any] = {
        "model": model,
        "input": messages,  # Responses accepts free-form input; we pass chat-style for continuity.
        "reasoning": {"effort": reasoning_effort},
        "service_tier": service_tier,
        **extra,
    }
    if not model.startswith("o"):
        params["temperature"] = temperature

    if tools:
        params["tools"] = tools
        if isinstance(function_call, dict) and function_call.get("name"):
            params["tool_choice"] = {
                "type": "function",
                "name": function_call["name"],
            }
        elif function_call in (None, "auto"):
            params["tool_choice"] = "auto"

    logging.info("Sending:\n%s", messages)
    response = client.responses.create(**params)
    logging.info("Received (truncated):\n%s", str(response)[:4000])
    save_cache(key, response)
    return response


def _get(obj, attr, default=None):
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


def openai_parse_function_call(response: Any) -> Tuple[Optional[str], Any]:
    """Extract function call data from Responses API or Chat Completions."""
    # --- Responses API path ---
    output = _get(response, "output", []) or []
    # Direct items (e.g., {"type":"tool_call"/"function_call"/"tool_use"})
    for item in output:
        t = _get(item, "type")
        if t in {"tool_call", "function_call", "tool_use"}:
            name = _get(item, "name") or _get(_get(item, "function", {}), "name")
            args_raw = _get(item, "arguments") or _get(item, "input") or "{}"
            try:
                args = args_raw if isinstance(args_raw, dict) else json.loads(args_raw)
            except Exception:
                args = {}
            return name, args
    # Nested inside message.content (some SDKs wrap tool calls there)
    if output:
        content = _get(output[0], "content", []) or []
        for c in content:
            if _get(c, "type") in {"tool_call", "function_call", "tool_use"}:
                name = _get(c, "name") or _get(_get(c, "function", {}), "name")
                args_raw = _get(c, "arguments") or _get(c, "input") or "{}"
                try:
                    args = args_raw if isinstance(args_raw, dict) else json.loads(args_raw)
                except Exception:
                    args = {}
                return name, args

    # --- Chat Completions fallback ---
    choices = _get(response, "choices", []) or []
    if choices:
        msg = _get(choices[0], "message", {}) or {}
        # tool_calls (Tools API)
        tcs = _get(msg, "tool_calls", []) or []
        if tcs:
            tc = tcs[0]
            func = _get(tc, "function", {}) or {}
            name = _get(func, "name")
            args_raw = _get(func, "arguments", "{}") or "{}"
            try:
                args = json.loads(args_raw)
            except Exception:
                args = {}
            return name, args
        # function_call (legacy)
        fc = _get(msg, "function_call", {}) or {}
        if fc:
            name = _get(fc, "name")
            args_raw = _get(fc, "arguments", "{}") or "{}"
            try:
                args = json.loads(args_raw)
            except Exception:
                args = {}
            return name, args
    return None, {}
