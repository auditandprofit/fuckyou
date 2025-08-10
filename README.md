# Anchor Architecture

This repository demonstrates a deterministic, evidence-driven workflow for validating bug claims. The process is divided into the following stages:

1. **Seed Input**
   - Begin with a single manifest file that lists N code files.
   - For each file in the manifest, invoke an agent using a fixed, prefixed prompt template.

2. **Produce Findings**
   - Treat every agent response as a finding: a concise claim referencing specific files and accompanying evidence.
   - Persist each finding as an individual file under `findings/`.

3. **Orchestrate per Finding**
   - Pass each stored finding to the orchestrator.
   - The finding conveys the bug claim and the subset of repository files relevant to that claim.

4. **Generate Conditions from Finding**
   - The orchestrator deterministically derives a minimal set of conditions that must hold for the bug to be real or exploitable.
   - Each condition is a concrete, checkable assertion tied to the claim and its related files.

5. **Evidence/Task Loop**
   - For every condition, the orchestrator emits tasks for agents to gather precise context (code slices, call graphs, configs, versions, sinks/sources, etc.).
   - Agents execute the tasks and return new context.
   - Using the context, the orchestrator decides:
     - the condition holds and is satisfied,
     - the condition is disproved and fails, or
     - the condition remains uncertain, prompting narrower sub-conditions that target the missing context.

6. **Iterate Until Resolved**
   - Repeat the task and sub-condition cycle until each condition is either satisfied or failed, or depth/resource limits are reached.
   - Decisions are based solely on retrieved evidence; no randomness or heuristic guessing is involved.

7. **Outputs**
   - Persist updated finding artifacts, including condition states and evidence trails, back into `findings/`.
   - Only production-grade objects and files are produced; no extraneous or decorative content is saved.

