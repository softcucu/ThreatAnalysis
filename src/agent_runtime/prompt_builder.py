"""Build the prompt passed to an agent runner."""

from __future__ import annotations

from agent_runtime.model_router import ModelConfig
from agent_runtime.task import AgentTask


_JSON_RESULT_INSTRUCTION_TEXT = "不允许输出json文件，直接返回json结果"
JSON_RESULT_INSTRUCTION = f"{_JSON_RESULT_INSTRUCTION_TEXT}。"


class PromptBuilder:
    def __init__(self, inline_inputs: bool = False, max_inline_chars: int = 200_000) -> None:
        self.inline_inputs = inline_inputs
        self.max_inline_chars = max_inline_chars

    def build(self, task: AgentTask, model_config: ModelConfig) -> str:
        _ = model_config
        return _append_json_result_instruction(task.runtime_prompt.strip())


def _append_json_result_instruction(prompt: str) -> str:
    if _JSON_RESULT_INSTRUCTION_TEXT in prompt:
        return prompt
    if not prompt:
        return JSON_RESULT_INSTRUCTION
    return f"{prompt}\n{JSON_RESULT_INSTRUCTION}"
