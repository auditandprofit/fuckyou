import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import run_agent


def test_read():
    res = run_agent("read:examples/example1.py")
    assert res["type"] == "read"
    assert "bytes" in res and "sha1" in res


def test_stat():
    res = run_agent("stat:examples/example1.py")
    assert res["type"] == "stat"
    assert "size" in res and "sha1" in res


def test_py_functions():
    res = run_agent("py:functions:examples/example1.py")
    funcs = res.get("functions", [])
    assert {"name": "add", "args": 2} in funcs


def test_py_classes():
    res = run_agent("py:classes:examples/example2.py")
    classes = res.get("classes", [])
    assert {"name": "Greeter", "methods": ["greet"]} in classes
