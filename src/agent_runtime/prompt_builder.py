"""Build the prompt passed to an agent runner."""

from __future__ import annotations

from agent_runtime.model_router import ModelConfig
from agent_runtime.task import AgentTask


class PromptBuilder:
    def __init__(self, inline_inputs: bool = False, max_inline_chars: int = 200_000) -> None:
        self.inline_inputs = inline_inputs
        self.max_inline_chars = max_inline_chars

    def build(self, task: AgentTask, model_config: ModelConfig) -> str:
        _ = model_config
        return task.runtime_prompt.strip()
