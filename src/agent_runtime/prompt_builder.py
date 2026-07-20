"""Build the prompt passed to an agent runner."""

from __future__ import annotations

import json
from pathlib import Path

from agent_runtime.model_router import ModelConfig
from agent_runtime.output_validation import load_task_schema
from agent_runtime.skills import skill_name_from_path
from agent_runtime.task import AgentTask


class PromptBuilder:
    def __init__(self, inline_inputs: bool = False, max_inline_chars: int = 200_000) -> None:
        self.inline_inputs = inline_inputs
        self.max_inline_chars = max_inline_chars

    def build(self, task: AgentTask, model_config: ModelConfig) -> str:
        _ = model_config
        sections = [
            task.runtime_prompt.strip(),
            self._format_task_contract(task),
            "输出 JSON Schema：\n"
            + "```json\n"
            + json.dumps(load_task_schema(task), ensure_ascii=False, indent=2)
            + "\n```",
        ]
        if task.input_files:
            sections.append("输入文件：\n" + self._format_input_files(task))
        return "\n\n".join(section for section in sections if section.strip())

    def _format_task_contract(self, task: AgentTask) -> str:
        lines = [
            "执行要求：",
            f"- 调用 skill：`{skill_name_from_path(task.skill_path)}`",
            f"- 任务 ID：`{task.task_id}`",
            f"- 任务类型：`{task.task_type}`",
            "- 只输出符合 JSON Schema 的 JSON 文本，直接作为本次回复返回。",
            "- 不要创建、修改或写入任何结果文件。",
            "- 不要用 Markdown 代码块包裹，不要在 JSON 前后添加说明文字。",
            "- 只处理本任务指定范围，不要扩展到其他阶段或其他分类。",
        ]
        return "\n".join(lines)

    def _format_input_files(self, task: AgentTask) -> str:
        if not self.inline_inputs:
            return "\n".join(f"- `{path}`" for path in task.input_files)

        rendered: list[str] = []
        for raw_path in task.input_files:
            path = Path(raw_path)
            text = path.read_text(encoding="utf-8", errors="replace")
            if len(text) > self.max_inline_chars:
                text = text[: self.max_inline_chars] + "\n...[truncated]..."
            rendered.append(f"`{path}`\n\n```text\n{text}\n```")
        return "\n\n".join(rendered)
