import json
import json
import sys
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# ensure repo root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from util.io import atomic_write


@pytest.fixture(autouse=True)
def clean_env():
    findings = Path("findings")
    if findings.exists():
        shutil.rmtree(findings)
    findings.mkdir()
    manifest = Path("manifest.txt")
    original = manifest.read_text()
    yield
    manifest.write_text(original)
    if findings.exists():
        shutil.rmtree(findings)


def run_pipeline() -> subprocess.CompletedProcess:
    return subprocess.run([
        "python",
        "run_pipeline.py",
    ], capture_output=True, text=True)


def get_run_dirs():
    return sorted(Path("findings").glob("run_*/"))


def read_run_json(run_dir: Path) -> dict:
    return json.loads((run_dir / "run.json").read_text())


def test_stable_ids_and_run_separation():
    manifest = Path("manifest.txt")
    manifest.write_text("\n".join([
        "examples/example1.py",
        "examples/example2.py",
    ]))
    res1 = run_pipeline()
    assert res1.returncode == 0
    dirs_after_first = get_run_dirs()
    assert len(dirs_after_first) == 1
    first_dir = dirs_after_first[0]
    files_first = {p.name for p in first_dir.glob("finding_*.json")}

    manifest.write_text("\n".join([
        "examples/example2.py",
        "examples/example1.py",
    ]))
    res2 = run_pipeline()
    assert res2.returncode == 0
    dirs_after_second = get_run_dirs()
    assert len(dirs_after_second) == 2
    second_dir = sorted(dirs_after_second)[-1]
    files_second = {p.name for p in second_dir.glob("finding_*.json")}

    assert files_first == files_second


def test_invalid_manifest_errors():
    manifest = Path("manifest.txt")
    # duplicate and missing
    manifest.write_text("\n".join([
        "examples/example1.py",
        "examples/example1.py",
    ]))
    res = run_pipeline()
    assert res.returncode != 0
    assert get_run_dirs() == []

    # out-of-repo
    manifest.write_text("../examples/example1.py")
    res = run_pipeline()
    assert res.returncode != 0
    assert get_run_dirs() == []

    # missing file
    manifest.write_text("examples/missing.py")
    res = run_pipeline()
    assert res.returncode != 0
    assert get_run_dirs() == []


def test_run_json_timestamps_counts():
    manifest = Path("manifest.txt")
    manifest.write_text("\n".join([
        "examples/example1.py",
        "examples/example2.py",
    ]))
    res = run_pipeline()
    assert res.returncode == 0
    run_dir = get_run_dirs()[0]
    run_data = read_run_json(run_dir)
    assert run_data["finished_at"] is not None
    assert run_data["counts"]["manifest_files"] == 2
    assert run_data["counts"]["findings_written"] == 2
    assert run_data["counts"]["errors"] == 0


def test_seeded_findings_have_claim_and_evidence():
    manifest = Path("manifest.txt")
    manifest.write_text("examples/example1.py")
    res = run_pipeline()
    assert res.returncode == 0
    run_dir = get_run_dirs()[0]
    finding_files = list(run_dir.glob("finding_*.json"))
    assert finding_files
    for fp in finding_files:
        data = json.loads(fp.read_text())
        assert data["claim"]
        assert data["evidence"].get("seed") is not None
        assert "tasks_log" in data
        assert "conditions" in data


def test_atomic_write_no_partial_on_error(tmp_path, monkeypatch):
    target = tmp_path / "out.txt"

    def boom(src, dst):
        raise OSError("fail")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(target, b"data")
    assert not target.exists()
    assert not any(tmp_path.iterdir())
