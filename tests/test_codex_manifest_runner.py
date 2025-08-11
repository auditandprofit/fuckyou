import json
import os
from pathlib import Path
import hashlib

from codex_manifest_runner import run_manifest


def _make_codex_stub(tmp_path: Path) -> Path:
    script = tmp_path / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        "out_path = sys.argv[sys.argv.index('--output-last-message') + 1]\n"
        "work_dir = sys.argv[sys.argv.index('-C') + 1]\n"
        "pathlib.Path(out_path).write_text(work_dir)\n"
        "print('ran', work_dir)\n"
    )
    script.chmod(0o755)
    return script


def test_run_manifest_invokes_codex(tmp_path: Path) -> None:
    codex_bin = _make_codex_stub(tmp_path)

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "file1.txt").write_text("a1")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "file2.txt").write_text("b2")

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": ["a/file1.txt", "b/file2.txt"]}))

    out_dir = tmp_path / "outs"
    prev = os.getcwd()
    os.chdir(tmp_path)
    try:
        run_manifest(str(codex_bin), manifest, out_dir)
    finally:
        os.chdir(prev)

    digest1 = hashlib.sha256("a/file1.txt".encode()).hexdigest()[:8]
    digest2 = hashlib.sha256("b/file2.txt".encode()).hexdigest()[:8]
    assert (out_dir / f"{digest1}.txt").read_text() == "a"
    assert (out_dir / f"{digest2}.txt").read_text() == "b"
