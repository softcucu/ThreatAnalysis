"""Build the prompt passed to an agent runner."""

from __future__ import annotations

import json
from pathlib import Path

from agent_runtime.model_router import ModelConfig
from agent_runtime.output_validation import load_task_schema
from agent_runtime.task import AgentTask


class PromptBuilder:
    def __init__(self, inline_inputs: bool = False, max_inline_chars: int = 200_000) -> None:
        self.inline_inputs = inline_inputs
        self.max_inline_chars = max_inline_chars

    def build(self, task: AgentTask, model_config: ModelConfig) -> str:
        skill_text = task.skill_file.read_text(encoding="utf-8")
        sections = [
            "# Skill",
            skill_text,
            "# Runtime Prompt",
            task.runtime_prompt,
            "# Task",
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2),
            "# Model",
            json.dumps(
                {
                    "model": model_config.model,
                    "resource": model_config.resource_name,
                    "parameters": dict(model_config.parameters),
                },
                ensure_ascii=False,
                indent=2,
            ),
            "# Output JSON Schema",
            json.dumps(load_task_schema(task), ensure_ascii=False, indent=2),
        ]
        if task.input_files:
            sections.extend(["# Input Files", self._format_input_files(task)])
        return "\n\n".join(sections)

    def _format_input_files(self, task: AgentTask) -> str:
        if not self.inline_inputs:
            return "\n".join(f"- {path}" for path in task.input_files)

        rendered: list[str] = []
        for raw_path in task.input_files:
            path = Path(raw_path)
            text = path.read_text(encoding="utf-8", errors="replace")
            if len(text) > self.max_inline_chars:
                text = text[: self.max_inline_chars] + "\n...[truncated]..."
            rendered.append(f"## {path}\n\n```text\n{text}\n```")
        return "\n\n".join(rendered)
