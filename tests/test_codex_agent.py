import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_agent import CodexAgent, MAX_BYTES
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


def test_read():
    data = {"type": "read", "path": "examples/example1.py", "bytes": "hi", "sha1": "00"}
    agent = _agent_with_result(data)
    res = agent.run("read:examples/example1.py")
    assert res == data


def test_stat():
    data = {"type": "stat", "path": "p", "size": 5, "sha1": "aa"}
    agent = _agent_with_result(data)
    res = agent.run("stat:examples/example1.py")
    assert res == data


def test_py_functions():
    data = {"type": "py:functions", "path": "p", "functions": [{"name": "f", "args": 1}]}
    agent = _agent_with_result(data)
    res = agent.run("py:functions:examples/example1.py")
    assert res == data


def test_py_classes():
    data = {"type": "py:classes", "path": "p", "classes": [{"name": "C", "methods": ["m"]}]}
    agent = _agent_with_result(data)
    res = agent.run("py:classes:examples/example2.py")
    assert res == data


def test_discover():
    data = {"type": "discover", "claim": "c", "files": ["p"], "evidence": {}}
    agent = _agent_with_result(data)
    res = agent.run("codex:discover:p")
    assert res == data


def test_exec():
    data = {
        "type": "exec",
        "task": "stat:p",
        "result": {"type": "stat", "path": "p", "size": 1, "sha1": "aa"},
    }
    agent = _agent_with_result(data)
    res = agent.run("codex:exec:p::stat:p")
    assert res == data


def test_timeout():
    client = DummyCodexClient(error=CodexTimeout("boom"))
    workdir = str(Path(__file__).resolve().parents[1])
    agent = CodexAgent(client, workdir=workdir)
    res = agent.run("read:examples/example1.py")
    assert res["error"] == "timeout"


def test_codex_exit():
    result = CodexExecResult(stdout="", stderr="bad", returncode=2, duration_sec=0.0, cmd=["codex"])
    err = CodexError(result)
    client = DummyCodexClient(error=err)
    workdir = str(Path(__file__).resolve().parents[1])
    agent = CodexAgent(client, workdir=workdir)
    res = agent.run("read:examples/example1.py")
    assert res["error"] == "codex-exit"
    assert res["code"] == 2


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


def test_path_escape():
    client = DummyCodexClient()
    workdir = str(Path(__file__).resolve().parents[1])
    agent = CodexAgent(client, workdir=workdir)
    try:
        agent.run("read:../secret")
    except ValueError as e:
        assert "outside" in str(e)
    else:  # pragma: no cover
        assert False


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


def test_truncate_bytes():
    long_bytes = "x" * (MAX_BYTES + 10)
    data = {"type": "read", "path": "p", "bytes": long_bytes, "sha1": "aa"}
    agent = _agent_with_result(data)
    res = agent.run("read:examples/example1.py")
    assert len(res["bytes"]) == MAX_BYTES


def test_invalid_json():
    result = CodexExecResult(stdout="not json", stderr="", returncode=0, duration_sec=0.0, cmd=["codex"])
    client = DummyCodexClient(result=result)
    workdir = str(Path(__file__).resolve().parents[1])
    agent = CodexAgent(client, workdir=workdir)
    res = agent.run("read:examples/example1.py")
    assert res["error"] == "invalid-json"
