from __future__ import annotations

from typing import Any, List

class LiveTextFormatter:
    """Pretty live reporter for text mode.

    Consumes existing Reporter events and renders a nested, human-scannable
    tree. Designed to be line-oriented and degrade on non-TTY streams.
    """

    def __init__(self, stream, is_tty: bool, width: int) -> None:
        self.s = stream
        self.is_tty = is_tty
        self.width = width or 100
        self.idx = {"finding": 0, "finding_total": 0}
        self.ctx = {
            "current_file": None,
            "current_condition": None,
            "current_condition_index": 0,
            "step": 0,
        }
        self.conditions: List[str] = []
        self.in_tasks = False
        self.colors = self._detect_colors(is_tty)

    # ------------------------------------------------------------------ utils
    def _detect_colors(self, enabled: bool) -> dict[str, str]:
        if not enabled:
            return {"green": "", "red": "", "yellow": "", "reset": ""}
        return {
            "green": "\x1b[32m",
            "red": "\x1b[31m",
            "yellow": "\x1b[33m",
            "reset": "\x1b[0m",
        }

    def handle(self, event: str, **data: Any) -> None:
        handler = getattr(self, f"_on_{event.replace(':', '_')}", None)
        if handler:
            handler(**data)
        self._refresh_current_line()

    def _print_line(self, indent: int, text: str) -> None:
        prefix = " " * indent
        self.s.write(prefix + text + ("" if text.endswith("\n") else "\n"))
        self.s.flush()

    def _build_current_line(self) -> str:
        parts = []
        step = self.ctx.get("step")
        if step:
            parts.append(f"step {step}")
        if self.ctx.get("current_file"):
            parts.append(self.ctx["current_file"])
        if self.ctx.get("current_condition"):
            parts.append(f"condition: \"{self.ctx['current_condition']}\"")
        if not parts:
            return ""
        return "CURRENT: " + "  ".join(parts)

    def _refresh_current_line(self) -> None:
        line = self._build_current_line()
        if not line:
            return
        if self.is_tty:
            pad = " " * max(0, self.width - len(line))
            self.s.write("\r" + line[: self.width] + pad)
            self.s.flush()
        else:
            self._print_line(0, line)

    def _truncate(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        half = max(0, (limit - 3) // 2)
        return text[:half] + "..." + text[-half:]

    # --------------------------------------------------------------- event handlers
    def _on_run_start(self, run_id: str, model: str, manifest: int) -> None:
        self.idx["finding_total"] = manifest
        self._print_line(0, f"RUN {run_id}  model={model}  manifest={manifest}")

    def _on_finding_open(self, claim: str, path: str, seed_source: str | None = None) -> None:
        self.idx["finding"] += 1
        self.ctx.update(
            {
                "current_file": path,
                "current_condition": None,
                "current_condition_index": 0,
                "step": 0,
            }
        )
        self.conditions = []
        self.in_tasks = False
        prefix = "└─ " if self.is_tty else "Finding: "
        extra = f" source={seed_source}" if seed_source else ""
        text = (
            f"{prefix}Finding {self.idx['finding']}/{self.idx['finding_total']}  {path}  \"{claim}\"{extra}"
        )
        if self.is_tty:
            self._print_line(0, text)
        else:
            self._print_line(0, text)

    def _on_condition_request(self, claim: str) -> None:
        self.ctx["current_condition"] = claim

    def _on_condition_derived(self, count: int, conditions: List[str]) -> None:
        self.conditions = conditions
        base = 3 if self.is_tty else 2
        header = "├─ Conditions" if self.is_tty else "Conditions"
        self._print_line(base, f"{header} ({count})")
        for i, desc in enumerate(conditions, 1):
            prefix = "│  " if self.is_tty else ""
            self._print_line(base, f"{prefix}{i}) {desc}")
        if conditions:
            self.ctx["current_condition"] = conditions[0]

    def _on_resolve_step(self, n: int) -> None:
        self.ctx["step"] = n
        if n == 1:
            # new condition start
            self.ctx["current_condition_index"] += 1
            if self.conditions and self.ctx["current_condition_index"] <= len(self.conditions):
                self.ctx["current_condition"] = self.conditions[
                    self.ctx["current_condition_index"] - 1
                ]
            self.in_tasks = False
        if not self.in_tasks:
            base = 3 if self.is_tty else 2
            text = "└─ Tasks" if self.is_tty else "Tasks"
            self._print_line(base, text)
            self.in_tasks = True

    def _on_tasks_plan(self, tasks: List[str]) -> None:
        base = 5 if self.is_tty else 4
        joined = ", ".join(tasks)
        self._print_line(base, f"• plan: {self._truncate(joined, self.width - base - 10)}")

    def _on_tasks_result(self, types: List[str]) -> None:
        base = 5 if self.is_tty else 4
        pieces = []
        for t in types:
            if t == "error" or not t:
                icon = f"{self.colors['red']}✗{self.colors['reset']}" if self.is_tty else "✗"
            else:
                icon = f"{self.colors['green']}✓{self.colors['reset']}" if self.is_tty else "✓"
            pieces.append(f"{t} {icon}")
        self._print_line(base, "• results: " + ", ".join(pieces))

    def _on_judge(self, state: str, rationale: str, shortcut: bool | None = False) -> None:
        base = 5 if self.is_tty else 4
        color = self.colors["green" if state == "satisfied" else "red" if state == "failed" else "yellow"]
        icon = "✓" if state == "satisfied" else "✗" if state == "failed" else "?"
        reset = self.colors["reset"]
        self._print_line(base, f"• judge: {color}{state}{reset} — {rationale}")
        idx = self.ctx.get("current_condition_index", 0)
        if idx and idx <= len(self.conditions):
            desc = self.conditions[idx - 1]
            cond_base = 3 if self.is_tty else 2
            prefix = "│  " if self.is_tty else ""
            self._print_line(
                cond_base,
                f"{prefix}{idx}) {desc}  {color}{icon}{reset} {state} — {rationale}",
            )

    def _on_subconditions_derived(self, count: int, conditions: List[str]) -> None:
        base = 5 if self.is_tty else 4
        self._print_line(base, f"Subconditions ({count})")
        for i, desc in enumerate(conditions):
            self._print_line(base + 2, f"{chr(97 + i)}) {desc}")

    def _on_finding_complete(self) -> None:
        self._print_line(0, "")
        self.ctx.update({"current_file": None, "current_condition": None, "step": 0})
        self.in_tasks = False

    def _on_run_end(self, findings: int, errors: int, duration: str) -> None:
        self._print_line(
            0, f"RUN complete findings={findings} errors={errors} duration={duration}"
        )

    def _on_unknown(self, **_: Any) -> None:
        pass
