from pathlib import Path
from test_pipeline import run_pipeline, clean_env


def test_run_pipeline_live(monkeypatch, clean_env):
    manifest = Path("manifest.txt")
    manifest.write_text("examples/example1.py")
    res = run_pipeline(monkeypatch, args=["--live"])
    assert res.returncode == 0
