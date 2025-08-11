from __future__ import annotations

import os
import subprocess
from pathlib import Path

import sys

# ensure repo root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from util.hotspots import find as find_hotspots
from util.imports import dep_lenses
from util.git_diff import git_changed_files


def test_hotspots_returns_category_and_score(tmp_path):
    f = tmp_path / "net.py"
    f.write_text("import requests\n")
    res = find_hotspots(tmp_path)
    assert len(res) == 1
    path, category, score = res[0]
    assert path == f
    assert category == "network"
    assert score > 0


def test_dep_lenses(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests\njinja2\n")
    (tmp_path / "app.py").write_text("import httpx\n")
    lenses = dep_lenses(tmp_path)
    assert "ssrf" in lenses
    assert "template" in lenses


def test_git_diff(tmp_path):
    subprocess.check_call(["git", "init"], cwd=tmp_path, stdout=subprocess.DEVNULL)
    p = tmp_path / "a.py"
    p.write_text("a=1\n")
    subprocess.check_call(["git", "add", "a.py"], cwd=tmp_path)
    subprocess.check_call(["git", "commit", "-m", "init"], cwd=tmp_path, stdout=subprocess.DEVNULL)
    p.write_text("a=2\n")
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        changed = git_changed_files("HEAD", None)
    finally:
        os.chdir(cwd)
    assert Path("a.py") in changed

