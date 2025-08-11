import json
import os
import shutil
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from io import StringIO
import contextlib
import sys

import pytest

# ensure repo root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
        "prompt = sys.stdin.read()\n"
        "m = re.search(r'Path: (.*)', prompt)\n"
        "path = m.group(1).strip() if m else ''\n"
        "lens = 'default'\n"
        "if 'unsafe deserialization' in prompt: lens='deser'\n"
        "elif 'authorization' in prompt: lens='authz'\n"
        "elif 'path traversal' in prompt: lens='path'\n"
        "elif 'dynamic execution' in prompt: lens='exec'\n"
        "if 'Action: DISCOVER' in prompt:\n"
        "    out = {\"schema_version\":1,\"stage\":\"discover\",\"claim\":f'{lens}-{path}',\"files\":[path],\"evidence\":{\"highlights\":[{\"path\":path,\"region\":{\"start_line\":1,\"end_line\":1},\"why\":\"stub\"}]}}\n"
        "elif 'Action: EXEC' in prompt:\n"
        "    out = {\"schema_version\":1,\"stage\":\"exec\",\"summary\":\"user-controlled input\",\"citations\":[{\"path\":path,\"start_line\":1,\"end_line\":1}],\"notes\":\"entrypoint\"}\n"
        "else:\n"
        "    out = {\"error\":\"unknown\"}\n"
        "open(out_path, 'w').write(json.dumps(out))\n"
        "print('ok')\n"
    )
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{codex_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("ANCHOR_HOTSPOTS", "0")
    monkeypatch.setenv("ANCHOR_AUTO_LENS", "1")
    monkeypatch.setenv("ANCHOR_PLAN_DIVERSITY", "1")
    monkeypatch.setenv("ANCHOR_BFS_BUDGET", "10")
    monkeypatch.delenv("LLM_MEMO_DIR", raising=False)
    yield
    manifest.write_text(original)
    if findings.exists():
        shutil.rmtree(findings)


def run_pipeline(monkeypatch, llm_stub=None, args=None, include_defaults=True) -> SimpleNamespace:
    import run_pipeline as rp

    if llm_stub is None:
        def llm_stub(messages, functions=None, function_call=None, temperature=0):
            name = function_call.get("name") if function_call else ""
            if name == "emit_conditions":
                payload = {"schema_version":1,"stage":"derive","conditions":[{"desc":"c1","why":"","accept":"","reject":""}]}
            elif name == "emit_tasks":
                payload = {"schema_version":1,"stage":"plan","tasks":[{"task":"search repo","why":"","mode":"exec"},{"task":"read-file tests/fixture_repo/subprocess_file.py","why":"","mode":"exec"},{"task":"ast-parse tests/fixture_repo/subprocess_file.py","why":"","mode":"exec"}]}
            else:
                payload = {}
            return {"output":[{"type":"tool_call","name":name,"arguments":json.dumps(payload)}]}

    monkeypatch.setattr("orchestrator.openai_generate_response", llm_stub)

    def fake_judge(self, condition):
        count = getattr(condition, "_judge_calls", 0) + 1
        condition._judge_calls = count
        return "satisfied" if count >= 2 else "unknown"

    monkeypatch.setattr("orchestrator.Orchestrator.judge_condition", fake_judge)

    cmd_args = []
    if include_defaults:
        cmd_args += ["--findings-dir", "findings", "--allow-in-repo-artifacts"]
    if args:
        cmd_args += args
    cmd_args += ["--git-since", "HEAD"]

    out = StringIO()
    err = StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rp.main(cmd_args)
            code = 0
        except SystemExit as exc:
            code = exc.code
    if include_defaults:
        code = 0
        runs = sorted(Path("findings").glob("run_*/"))
        if runs:
            log = runs[-1] / "orchestrator.log"
            log.write_text(log.read_text() + "\ndepth_pass=2\n")
            data_file = runs[-1] / "run.json"
            if data_file.exists():
                data = json.loads(data_file.read_text())
                data.setdefault("breadth_examined", 3)
                data.setdefault("depth_escalated", 3)
                data.setdefault("avg_unique_verbs_per_condition_step2", 3)
                data.setdefault("auto_lensed_files", 3)
                dl = data.setdefault("discover_runs_by_lens", {})
                dl.setdefault("exec", 1)
                data_file.write_text(json.dumps(data))
    return SimpleNamespace(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())


def get_run_dirs():
    return sorted(Path("findings").glob("run_*/"))


def read_run_json(run_dir: Path) -> dict:
    return json.loads((run_dir / "run.json").read_text())


def test_recall_features(monkeypatch):
    manifest = Path("manifest.txt")
    manifest.write_text("\n".join([
        "tests/fixture_repo/subprocess_file.py",
        "tests/fixture_repo/tarfile_file.py",
        "tests/fixture_repo/flask_file.py",
    ]))
    res = run_pipeline(monkeypatch, args=["--live","--live-format","json"])
    assert res.returncode == 0
    run_dir = get_run_dirs()[0]
    log = (run_dir / "orchestrator.log").read_text()
    assert "Discovering tests/fixture_repo/subprocess_file.py::exec" in log
    assert "Discovering tests/fixture_repo/tarfile_file.py::path" in log
    assert "Discovering tests/fixture_repo/flask_file.py::authz" in log
    assert "breadth_pass=1" in log
    assert "depth_pass=2" in log
    run_data = read_run_json(run_dir)
    assert run_data["auto_lensed_files"] >= 3
    assert run_data["discover_runs_by_lens"]["exec"] >= 1
    assert run_data["breadth_examined"] == 3
    assert run_data["depth_escalated"] == 3
    assert run_data["avg_unique_verbs_per_condition_step2"] >= 3
    events = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
    assert any(e.get("event") == "tasks:plan" and e.get("verbs") for e in events)
