import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import Orchestrator, Condition


def fake_agent(goal: str) -> str:
    return ""


def test_generate_tasks_only_exec(monkeypatch):
    orch = Orchestrator(fake_agent)
    cond = Condition(description="c", accept="a", reject="r")
    fake = {
        "choices": [
            {
                "message": {
                    "function_call": {
                        "name": "emit_tasks",
                        "arguments": json.dumps(
                            {
                                "schema_version": 1,
                                "stage": "plan",
                                "tasks": [
                                    {"task": "t1", "why": "w1", "mode": "read"},
                                    {"task": "t2", "why": "w2", "mode": "exec"},
                                ]
                            }
                        ),
                    }
                }
            }
        ]
    }
    monkeypatch.setattr(
        "orchestrator.openai_generate_response", lambda *a, **k: fake
    )
    tasks = orch.generate_tasks(cond, Path("p.py"))
    assert tasks == [
        {
            "task": "codex:exec:p.py::t2",
            "why": "w2",
            "mode": "exec",
            "original": "t2",
        }
    ]


def test_judge_condition_requires_evidence(monkeypatch):
    orch = Orchestrator(fake_agent)

    def boom(*args, **kwargs):  # should not be called
        raise AssertionError("openai called")

    monkeypatch.setattr("orchestrator.openai_generate_response", boom)
    cond = Condition(description="x")
    assert orch.judge_condition(cond) == "unknown"


def test_resolve_condition_iterates_until_failed(tmp_path, monkeypatch):
    """Condition should iterate until judged non-unknown."""
    orch = Orchestrator(fake_agent)
    cond = Condition(description="parent")
    finding = tmp_path / "f.json"
    finding.write_text("{}")

    monkeypatch.setattr(orch, "generate_tasks", lambda c, p: [{"task": "t", "original": "t"}])
    monkeypatch.setattr(orch, "_execute_tasks", lambda fp, c, t: c.evidence.append("e"))
    states = iter(["unknown", "unknown", "failed"])
    monkeypatch.setattr(orch, "judge_condition", lambda c: next(states))
    monkeypatch.setattr(orch, "_narrow_subconditions", lambda c: [])

    orch.resolve_condition(cond, finding, max_steps=5)
    assert cond.state == "failed"
    assert len(cond.evidence) == 3  # ran three iterations


def test_subconditions_bubble_satisfied(tmp_path, monkeypatch):
    orch = Orchestrator(fake_agent)
    cond = Condition(description="parent")
    finding = tmp_path / "f.json"
    finding.write_text("{}")

    monkeypatch.setattr(orch, "generate_tasks", lambda c, p: [{"task": "t", "original": "t"}])
    monkeypatch.setattr(orch, "_execute_tasks", lambda fp, c, t: c.evidence.append("e"))

    def judge_stub(c):
        return "satisfied" if c.description.startswith("sub") else "unknown"

    monkeypatch.setattr(orch, "judge_condition", judge_stub)

    def narrow_stub(c):
        subs = [Condition("sub1"), Condition("sub2")]
        c.subconditions.extend(subs)
        return subs

    monkeypatch.setattr(orch, "_narrow_subconditions", narrow_stub)

    orch.resolve_condition(cond, finding)
    assert cond.state == "satisfied"
    assert [s.state for s in cond.subconditions] == ["satisfied", "satisfied"]


def test_subconditions_bubble_failed(tmp_path, monkeypatch):
    orch = Orchestrator(fake_agent)
    cond = Condition(description="parent")
    finding = tmp_path / "f.json"
    finding.write_text("{}")

    monkeypatch.setattr(orch, "generate_tasks", lambda c, p: [{"task": "t", "original": "t"}])
    monkeypatch.setattr(orch, "_execute_tasks", lambda fp, c, t: c.evidence.append("e"))

    def judge_stub(c):
        return "failed" if c.description.startswith("sub") else "unknown"

    monkeypatch.setattr(orch, "judge_condition", judge_stub)

    def narrow_stub(c):
        subs = [Condition("sub1"), Condition("sub2")]
        c.subconditions.extend(subs)
        return subs

    monkeypatch.setattr(orch, "_narrow_subconditions", narrow_stub)

    orch.resolve_condition(cond, finding)
    assert cond.state == "failed"


def test_execute_tasks_atomic_write_no_partial_on_error(tmp_path, monkeypatch):
    orch = Orchestrator(lambda x: "out")
    cond = Condition(description="c")
    finding = tmp_path / "f.json"
    finding.write_text(
        json.dumps({"tasks_log": [], "provenance": {"path": "examples/example1.py"}})
    )

    from util import io as uio

    def boom(src, dst):
        raise OSError("fail")

    monkeypatch.setattr(uio.os, "replace", boom)

    with pytest.raises(OSError):
        orch._execute_tasks(finding, cond, [{"task": "t", "original": "t"}])

    # Original file untouched and valid JSON
    assert json.loads(finding.read_text()) == {
        "tasks_log": [],
        "provenance": {"path": "examples/example1.py"},
    }
    assert list(tmp_path.iterdir()) == [finding]


def test_execute_tasks_updates_evidence(tmp_path):
    obs = {
        "schema_version": 1,
        "stage": "exec",
        "summary": "s",
        "citations": [],
        "notes": "",
    }
    orch = Orchestrator(lambda g: obs)
    cond = Condition(description="c")
    finding = tmp_path / "f.json"
    finding.write_text(
        json.dumps({"tasks_log": [], "provenance": {"path": "examples/example1.py"}})
    )
    tasks = [
        {
            "task": "codex:exec:examples/example1.py::x",
            "mode": "exec",
            "original": "x",
        }
    ]
    orch._execute_tasks(finding, cond, tasks)
    assert cond.evidence and json.loads(cond.evidence[0])["summary"] == "s"


def test_judgement_shortcuts(monkeypatch):
    orch = Orchestrator(fake_agent)
    fake = {
        "choices": [
            {
                "message": {
                    "function_call": {
                        "name": "judge_condition",
                        "arguments": json.dumps(
                            {
                                "schema_version": 1,
                                "stage": "judge",
                                "state": "satisfied",
                                "rationale": "ok",
                                "evidence_refs": [-1],
                            }
                        ),
                    }
                }
            }
        ]
    }
    monkeypatch.setattr(
        "orchestrator.openai_generate_response", lambda *a, **k: fake
    )
    cond = Condition(
        description="x",
        evidence=[
            json.dumps(
                {
                    "schema_version": 1,
                    "stage": "exec",
                    "summary": "s",
                    "citations": [],
                    "notes": "",
                }
            )
        ],
    )
    assert orch.judge_condition(cond) == "satisfied"
