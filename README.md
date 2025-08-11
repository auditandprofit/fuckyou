# Anchor — Deterministic Code Security Audits

*End‑to‑end, evidence‑driven audits optimized for **low false positives**.*

Anchor runs a fixed pipeline over a repository and produces signed‑off findings with traceable evidence. Each stage (orchestrator and agent/Codex) **works in isolation** behind a small, explicit contract, yet is aware it participates in a larger automated security audit.

---

## Core Principles

* **Isolation by contract**: Every stage reads inputs and emits strict JSON outputs. No shared memory, no hidden state.
* **Evidence over speculation**: Claims, conditions, and verdicts must cite concrete code regions.
* **Determinism**: LLM calls run with temperature=0 and bounded retries; agents are read‑only and offline (no network, no writes to the repo).
* **Low false positive rate**: The system defaults to **UNKNOWN** unless evidence meets acceptance criteria. Any failed condition flips the finding to **FALSE\_POSITIVE**.

---

## Pipeline Overview

> Stages: **discover → derive → plan → exec → judge → narrow**

1. **Seed Input** — A manifest enumerates code files to review.
2. **Produce Findings** — An agent “discovers” a falsifiable claim per file with 1–3 evidence highlights.
3. **Derive Conditions** — The orchestrator breaks the claim into minimal, testable conditions.
4. **Plan/Exec Loop** — For each condition, the orchestrator plans tasks; the agent executes them locally and returns strict JSON observations with citations.
5. **Judge** — Using latest successful observation(s), the orchestrator judges each condition: `satisfied | failed | unknown`.
6. **Narrow** — If `unknown`, derive sub‑conditions that directly target the missing evidence and iterate.
7. **Verdict** — Aggregate condition states to produce: `TRUE_POSITIVE | FALSE_POSITIVE | UNKNOWN`.

---

## Stage Contracts (I/O)

All stages communicate via strict JSON. The agent is policy‑restricted: **read‑only, no external processes, no network**.

### Discover → Finding

```json
{
  "schema_version": 1,
  "stage": "discover",
  "claim": "<falsifiable security claim incl. brief threat context>",
  "files": ["<repo-rel path>", "..."],
  "evidence": {
    "highlights": [
      {"path": "<repo-rel>", "region": {"start_line": 10, "end_line": 24}, "why": "<security-relevant>"}
    ]
  }
}
```

### Exec (task result)

```json
{
  "schema_version": 1,
  "stage": "exec",
  "summary": "<short conclusion or 'error: ...'>",
  "citations": [
    {"path": "<repo-rel>", "start_line": 120, "end_line": 137, "sha1": "<optional>"}
  ],
  "notes": "<optional>"
}
```

### Judge (internal output)

```json
{
  "schema_version": 1,
  "stage": "judge",
  "state": "satisfied | failed | unknown",
  "rationale": "<why the state holds>",
  "evidence_refs": [0]
}
```

> The orchestrator persists the evolving condition objects, task logs, and verdicts under `findings/`.

---

## What “Isolation” Means Here

* **Replaceable stages**: You can swap the agent or the orchestrator as long as the I/O contracts stay intact.
* **Stateless execution**: A stage must make decisions **only** from its input blobs and the repository contents; any caching is optional and keyed to request content.
* **Policy walls**: The agent enforces: *no network, no repo writes, no external processes*. The orchestrator writes artifacts only to `findings/`.

---

## Quickstart

1. **Install prerequisites**

   * Python 3.10+
   * OpenAI credentials in environment (for LLM stages)
   * A working `codex` CLI on your PATH (the deterministic executor wrapper)

2. **Create a manifest** (one repo‑relative file per line):

```
examples/example1.py
examples/example2.py
```

3. **Run the pipeline**

```bash
python run_pipeline.py --manifest manifest.txt --findings-dir findings --live
```

The run creates `findings/run_<timestamp>_<commit>/finding_*.json` with claims, conditions, task logs, and a final verdict.

---

## Live Mode

Enable a high‑signal, line‑oriented reporter:

* `ANCHOR_LIVE=1` (or pass `--live`)
* `ANCHOR_LIVE_FORMAT=text|json` (default: text)
* Pretty text auto‑detects TTY and width; JSON emits newline‑delimited events.

---

## Artifact Layout

```
findings/
  run_<UTCts>_<gitShort>/
    orchestrator.log
    run.json                      # metadata: model, retries, manifest SHA1, git, timings
    finding_<id>.json             # claim, evidence, conditions (+sub), tasks_log, verdict
```

`run.json` tracks counts, start/end times, commit/dirty state, and configured LLM parameters.

---

## Design Details

* **Determinism & Retries**: LLM temperature=0 with bounded retries (`ANCHOR_OPENAI_RETRIES`). Optional response memoization via `LLM_MEMO_DIR`.
* **Strict schemas**: Agent responses are validated; missing citations yield an `error: missing-citation` summary.
* **Verdict rules**:

  * All conditions `satisfied` ⇒ `TRUE_POSITIVE`
  * Any `failed` and none `satisfied` ⇒ `FALSE_POSITIVE`
  * Otherwise ⇒ `UNKNOWN`

---

## Extending Anchor

* Add new operation classes to the agent (e.g., `search`, `read-file`, `ast-parse`, `callgraph`, `dataflow`) without changing the contract.
* Implement alternative planners or judges as long as they accept the same inputs and emit the same outputs.

---

## Non‑Goals

* Heuristic “best guess” findings with no citations.
* Modifying the target repository during analysis.
* Hidden coupling between stages.

---

## Repository Map (key files)

* `run_pipeline.py` — CLI entrypoint, run orchestration, persist artifacts
* `orchestrator.py` — derive/plan/judge/narrow logic, contract enforcement
* `codex_agent.py` — agent wrapper around `codex` with strict policies
* `codex_dispatch.py` — deterministic subprocess runner with retries/backoff
* `util/` — reporting, paths, manifest validation, OpenAI wrapper, time, IO
* `examples/` — trivial sample inputs

---

**Anchor turns code into defensible security evidence.** If a claim can’t be proven, it isn’t a finding.

