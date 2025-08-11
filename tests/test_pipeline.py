import json
import sys
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from io import StringIO
import contextlib

import pytest

# ensure repo root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from util.io import atomic_write


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    findings = Path("findings")
    if findings.exists():
        shutil.rmtree(findings)
    findings.mkdir()
    manifest = Path("manifest.txt")
    original = manifest.read_text()

    codex_dir = Path(tempfile.mkdtemp())
    codex = codex_dir / "codex"
    codex.write_text(
        "#! /usr/bin/env python3\n"
        "import sys, json, re, os\n"
        "out_path = sys.argv[sys.argv.index('--output-last-message') + 1]\n"
        "work = sys.argv[sys.argv.index('-C') + 1]\n"
        "os.chdir(work)\n"
        "sys.path.insert(0, os.getcwd())\n"
        "prompt = sys.stdin.read()\n"
        "m = re.search(r'Path: (.*)', prompt)\n"
        "path = m.group(1).strip() if m else ''\n"
        "if 'Action: DISCOVER' in prompt:\n"
        "    out = {\"schema_version\":1,\"stage\":\"discover\",\"claim\":f'Review {path}',\"files\":[path],\"evidence\":{\"highlights\":[{\"path\":path,\"region\":{\"start_line\":1,\"end_line\":1},\"why\":\"stub\"}]}}\n"
        "elif 'Action: EXEC' in prompt:\n"
        "    out = {\"schema_version\":1,\"stage\":\"exec\",\"summary\":\"ok\",\"citations\":[{\"path\":path,\"start_line\":1,\"end_line\":1}],\"notes\":\"\"}\n"
        "else:\n"
        "    out = {\"error\":\"unknown\"}\n"
        "open(out_path, 'w').write(json.dumps(out))\n"
        "print('ok')\n"
    )
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{codex_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("ANCHOR_HOTSPOTS", "0")
    yield
    manifest.write_text(original)
    if findings.exists():
        shutil.rmtree(findings)


def run_pipeline(monkeypatch, llm_stub=None, args=None) -> SimpleNamespace:
    import run_pipeline as rp

    if llm_stub is None:
            llm_stub = lambda *a, **k: {
            "output": [
                {
                    "type": "tool_call",
                    "name": "emit_conditions",
                    "arguments": "{\"schema_version\":1,\"stage\":\"derive\",\"conditions\": []}",
                }
            ]
        }

    monkeypatch.setattr("orchestrator.openai_generate_response", llm_stub)
    out = StringIO()
    err = StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rp.main(args or [])
            code = 0
        except SystemExit as exc:
            code = exc.code
    return SimpleNamespace(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())


def get_run_dirs():
    return sorted(Path("findings").glob("run_*/"))


def read_run_json(run_dir: Path) -> dict:
    return json.loads((run_dir / "run.json").read_text())


def test_stable_ids_and_run_separation(monkeypatch):
    manifest = Path("manifest.txt")
    manifest.write_text("\n".join([
        "examples/example1.py",
        "examples/example2.py",
    ]))
    res1 = run_pipeline(monkeypatch)
    assert res1.returncode == 0
    dirs_after_first = get_run_dirs()
    assert len(dirs_after_first) == 1
    first_dir = dirs_after_first[0]
    files_first = {p.name for p in first_dir.glob("finding_*.json")}

    manifest.write_text("\n".join([
        "examples/example2.py",
        "examples/example1.py",
    ]))
    res2 = run_pipeline(monkeypatch)
    assert res2.returncode == 0
    dirs_after_second = get_run_dirs()
    assert len(dirs_after_second) == 2
    second_dir = sorted(dirs_after_second)[-1]
    files_second = {p.name for p in second_dir.glob("finding_*.json")}

    assert files_first == files_second


def test_invalid_manifest_errors(monkeypatch):
    manifest = Path("manifest.txt")
    # duplicate and missing
    manifest.write_text("\n".join([
        "examples/example1.py",
        "examples/example1.py",
    ]))
    res = run_pipeline(monkeypatch)
    assert res.returncode != 0
    assert get_run_dirs() == []

    # out-of-repo
    manifest.write_text("../examples/example1.py")
    res = run_pipeline(monkeypatch)
    assert res.returncode != 0
    assert get_run_dirs() == []

    # missing file
    manifest.write_text("examples/missing.py")
    res = run_pipeline(monkeypatch)
    assert res.returncode != 0
    assert get_run_dirs() == []


def test_run_json_timestamps_counts(monkeypatch):
    manifest = Path("manifest.txt")
    manifest.write_text("\n".join([
        "examples/example1.py",
        "examples/example2.py",
    ]))
    res = run_pipeline(monkeypatch)
    assert res.returncode == 0
    run_dir = get_run_dirs()[0]
    run_data = read_run_json(run_dir)
    assert run_data["finished_at"] is not None
    assert run_data["counts"]["manifest_files"] == 2
    assert run_data["counts"]["findings_written"] == 2
    assert run_data["counts"]["errors"] == 0


def test_seeded_findings_have_claim_and_evidence(monkeypatch):
    manifest = Path("manifest.txt")
    manifest.write_text("examples/example1.py")
    res = run_pipeline(monkeypatch)
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
        assert "verdict" in data


def test_manifest_is_single_source(monkeypatch):
    import run_pipeline as rp

    manifest = Path("manifest.txt")
    manifest.write_text("examples/example1.py")
    called = {}

    def fake_validate(path):
        called["validate"] = True
        return [Path("examples/example1.py")]

    def fake_gather(self, files):
        called["gather"] = files
        return []

    monkeypatch.setattr(rp, "validate_manifest", fake_validate)
    monkeypatch.setattr("orchestrator.Orchestrator.gather_initial_findings", fake_gather)

    monkeypatch.setattr(
        "orchestrator.openai_generate_response",
        lambda *a, **k: {
        "output": [
            {
                "type": "tool_call",
                "name": "emit_conditions",
                "arguments": "{\"schema_version\":1,\"stage\":\"derive\",\"conditions\": []}",
            }
        ]
        },
    )
    rp.main()
    assert called.get("validate") is True
    assert called.get("gather") == [Path("examples/example1.py")]


def test_pipeline_aborts_on_llm_failure(monkeypatch):
    manifest = Path("manifest.txt")
    manifest.write_text("examples/example1.py")

    def boom(*a, **k):
        raise RuntimeError("no llm")

    res = run_pipeline(monkeypatch, llm_stub=boom)
    assert res.returncode != 0

    run_dir = get_run_dirs()[0]
    log = (run_dir / "orchestrator.log").read_text()
    assert "Aborting run" in log
    finding_file = next(run_dir.glob("finding_*.json"))
    data = json.loads(finding_file.read_text())
    assert data["conditions"] == []
    assert data["tasks_log"] == []
    run_data = read_run_json(run_dir)
    assert run_data["counts"]["errors"] == 1


def test_atomic_write_no_partial_on_error(tmp_path, monkeypatch):
    target = tmp_path / "out.txt"

    def boom(src, dst):
        raise OSError("fail")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(target, b"data")
    assert not target.exists()
    assert not any(tmp_path.iterdir())
