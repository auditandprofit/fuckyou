import json

from util.openai import openai_parse_function_call


def test_parse_responses_top_level():
    resp = {
        "output": [
            {
                "type": "tool_call",
                "name": "emit_tasks",
                "arguments": json.dumps({"tasks": ["codex:exec:x::y"]}),
            }
        ]
    }
    assert openai_parse_function_call(resp) == ("emit_tasks", {"tasks": ["codex:exec:x::y"]})


def test_parse_responses_nested_content():
    resp = {
        "output": [
            {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "emit_conditions",
                        "input": {"conditions": ["A"]},
                    }
                ]
            }
        ]
    }
    assert openai_parse_function_call(resp) == (
        "emit_conditions",
        {"conditions": ["A"]},
    )


def test_parse_chat_tool_calls():
    resp = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "emit_tasks",
                                "arguments": json.dumps({"tasks": []}),
                            }
                        }
                    ]
                }
            }
        ]
    }
    assert openai_parse_function_call(resp) == ("emit_tasks", {"tasks": []})


def test_parse_chat_function_call():
    resp = {
        "choices": [
            {
                "message": {
                    "function_call": {
                        "name": "emit_conditions",
                        "arguments": json.dumps({"conditions": ["X"]}),
                    }
                }
            }
        ]
    }
    assert openai_parse_function_call(resp) == (
        "emit_conditions",
        {"conditions": ["X"]},
    )

