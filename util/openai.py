"""Thin wrapper around the OpenAI client used by phase mode."""

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

_client = None


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
    model: str = "o3",
    reasoning_effort: str = "high",
    service_tier: str = "flex",
    temperature: float = 0,
    **extra: Any,
):
    """Wrapper around ``client.responses.create`` with defaults."""
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
    return response


def openai_parse_function_call(response: Any) -> Tuple[Optional[str], Any]:
    """Extract function call data from a Responses API result."""
    fc = None
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) in {"function_call", "tool_call"}:
            fc = item
            break
    if not fc:
        output = getattr(response, "output", None)
        msg = output[0] if output else None
        content = getattr(msg, "content", []) if msg else []
        for item in content:
            if getattr(item, "type", None) == "tool_call":
                fc = item
                break
    if not fc:
        return None, None
    name = getattr(fc, "name", None)
    args_str = getattr(fc, "arguments", "") or "{}"
    try:
        data = json.loads(args_str)
    except json.JSONDecodeError:
        data = {}
    logging.info("Function call %s with %s", name, data)
    return name, data
