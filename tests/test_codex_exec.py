import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from codex_exec import invoke_codex


def _make_codex_stub(tmp_path: Path) -> Path:
    script = tmp_path / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, pathlib\n"
        "out_path = sys.argv[sys.argv.index('--output-last-message') + 1]\n"
        "prompt = sys.stdin.read().strip()\n"
        "pathlib.Path(out_path).write_text('```json\\n' + json.dumps({'echo': prompt}) + '\\n```')\n"
        "print('ok')\n"
    )
    script.chmod(0o755)
    return script


def test_invoke_codex_parses_last_message(tmp_path: Path) -> None:
    codex_bin = _make_codex_stub(tmp_path)
    output_file = tmp_path / "out.txt"
    res = invoke_codex(
        codex_bin=str(codex_bin),
        prompt="hello",
        work_dir=str(tmp_path),
        output_path=str(output_file),
        timeout=1,
    )
    assert res == {"echo": "hello"}

