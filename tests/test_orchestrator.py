import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import Orchestrator, Condition


def fake_agent(goal: str) -> str:
    return ""


def test_judge_condition_requires_evidence(monkeypatch):
    orch = Orchestrator(fake_agent)

    def boom(*args, **kwargs):  # should not be called
        raise AssertionError("openai called")

    monkeypatch.setattr("orchestrator.openai_generate_response", boom)
    cond = Condition(description="x")
    assert orch.judge_condition(cond) == "unknown"
