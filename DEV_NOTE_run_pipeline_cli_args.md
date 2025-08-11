# Dev Note – Add CLI args for configurable settings

Currently, `run_pipeline.py` has **no** CLI arg parsing — all configuration is hardcoded.
To make it more flexible, we can wrap `main()` in an `argparse` layer and expose these knobs:

| Arg                  | Purpose                                              | Current default             |
| -------------------- | ---------------------------------------------------- | --------------------------- |
| `--manifest`         | Path to manifest file                                | `"manifest.txt"`            |
| `--findings-dir`     | Root dir for run outputs                             | `"findings"`                |
| `--prompt-prefix`    | Prefix passed to Orchestrator for initial findings   | `"Analyze file: "`          |
| `--version`          | Orchestrator version tag                             | `"0.1"`                     |
| `--repo-root`        | Path to repo root for Git metadata + file resolution | `util.paths.REPO_ROOT`      |
| `--model`            | LLM model name                                       | `util.openai.DEFAULT_MODEL` |
| `--reasoning-effort` | LLM reasoning level                                  | `DEFAULT_REASONING_EFFORT`  |
| `--service-tier`     | LLM service tier                                     | `DEFAULT_SERVICE_TIER`      |

**Example usage after patch:**

```bash
python run_pipeline.py \
  --manifest custom_manifest.txt \
  --findings-dir ./outputs \
  --prompt-prefix "Security review: " \
  --repo-root /path/to/codebase \
  --model o1 \
  --reasoning-effort medium
```

**Implementation sketch:**

```python
import argparse

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="manifest.txt")
    ap.add_argument("--findings-dir", default="findings")
    ap.add_argument("--prompt-prefix", default="Analyze file: ")
    ap.add_argument("--repo-root", default=str(REPO_ROOT))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    ap.add_argument("--service-tier", default=DEFAULT_SERVICE_TIER)
    return ap.parse_args()

def main():
    args = parse_args()
    # use args.manifest instead of hardcoded "manifest.txt"
    # pass args.repo_root to Git calls and CodexAgent(workdir=...)
    ...
```

**Special note for `--repo-root`:**

* Use it to `chdir` before running `git` commands in `util.git`
* Pass it to `CodexAgent(workdir=...)` and for resolving `REPO_ROOT`
