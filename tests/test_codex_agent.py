import sys
import json
from pathlib import Path
import pytest

import sys
import json
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_agent import CodexAgent
from codex_dispatch import CodexError, CodexExecResult, CodexTimeout


class DummyCodexClient:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def exec(self, **kwargs):
        if self._error:
            raise self._error
        return self._result


def _agent_with_result(data):
    result = CodexExecResult(
        stdout=json.dumps(data),
        stderr="",
        returncode=0,
        duration_sec=0.01,
        cmd=["codex"],
    )
    client = DummyCodexClient(result=result)
    workdir = str(Path(__file__).resolve().parents[1])
    return CodexAgent(client, workdir=workdir)
def test_discover():
    data = {
        "schema_version": 1,
        "stage": "discover",
        "claim": "c",
        "files": ["p"],
        "evidence": {},
    }
    agent = _agent_with_result(data)
    res = agent.run("codex:discover:p")
    assert res == data


def test_exec():
    data = {
        "schema_version": 1,
        "stage": "exec",
        "summary": "ok",
        "citations": [],
        "notes": "",
    }
    agent = _agent_with_result(data)
    res = agent.run("codex:exec:p::stat:p")
    assert res == data


def test_timeout_exec():
    client = DummyCodexClient(error=CodexTimeout("boom"))
    workdir = str(Path(__file__).resolve().parents[1])
    agent = CodexAgent(client, workdir=workdir)
    res = agent.run("codex:exec:p::x")
    assert res["summary"].startswith("error:")


def test_codex_exit_exec():
    result = CodexExecResult(stdout="", stderr="bad", returncode=2, duration_sec=0.0, cmd=["codex"])
    err = CodexError(result)
    client = DummyCodexClient(error=err)
    workdir = str(Path(__file__).resolve().parents[1])
    agent = CodexAgent(client, workdir=workdir)
    res = agent.run("codex:exec:p::x")
    assert res["summary"].startswith("error:")


def test_unknown_verb():
    client = DummyCodexClient()
    workdir = str(Path(__file__).resolve().parents[1])
    agent = CodexAgent(client, workdir=workdir)
    try:
        agent.run("foo:bar")
    except ValueError as e:
        assert "unsupported task" in str(e)
    else:  # pragma: no cover
        assert False, "ValueError not raised"


def test_path_escape_custom_verbs():
    client = DummyCodexClient()
    workdir = str(Path(__file__).resolve().parents[1])
    agent = CodexAgent(client, workdir=workdir)
    try:
        agent.run("codex:discover:../secret")
    except ValueError as e:
        assert "outside" in str(e)
    else:  # pragma: no cover
        assert False
    try:
        agent.run("codex:exec:../secret::stat:foo")
    except ValueError as e:
        assert "outside" in str(e)
    else:  # pragma: no cover
        assert False


def test_invalid_json():
    result = CodexExecResult(stdout="not json", stderr="", returncode=0, duration_sec=0.0, cmd=["codex"])
    client = DummyCodexClient(result=result)
    workdir = str(Path(__file__).resolve().parents[1])
    agent = CodexAgent(client, workdir=workdir)
    res = agent.run("codex:exec:p::x")
    assert res["summary"].startswith("error:")
