"""Thin wrapper around the OpenAI client used by phase mode."""

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
        tools.extend({"type": "function", **f} for f in functions)

    params: Dict[str, Any] = {
        "model": model,
        "input": messages,
        "tools": tools,
        "reasoning": {"effort": reasoning_effort},
        "service_tier": service_tier,
        "temperature": temperature,
        **extra,
    }

    logging.info("Sending:\n%s", messages)
    response = client.responses.create(**params)
    logging.info("Received:\n%s", response)
    save_cache(key, response)
    return response


def _get(obj, attr, default=None):
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


def openai_parse_function_call(response: Any) -> Tuple[Optional[str], Any]:
    """Extract function call data from a Responses API result."""
    fc = None
    output = _get(response, "output", []) or []
    for item in output:
        if _get(item, "type") in {"function_call", "tool_call"}:
            fc = item
            break
    if not fc and output:
        msg = output[0]
        content = _get(msg, "content", []) or []
        for item in content:
            if _get(item, "type") == "tool_call":
                fc = item
                break
    if not fc:
        return None, None
    name = _get(fc, "name")
    args_str = _get(fc, "arguments", "") or "{}"
    try:
        data = json.loads(args_str)
    except json.JSONDecodeError:
        data = {}
    logging.info("Function call %s with %s", name, data)
    return name, data
