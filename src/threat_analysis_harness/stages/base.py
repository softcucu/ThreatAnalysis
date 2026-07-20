"""Shared helpers for agent-backed business stages."""

from __future__ import annotations

from typing import Iterable

from agent_runtime import AgentResult, TaskStatus

from threat_analysis_harness.errors import StageExecutionError


def require_success(result: AgentResult) -> AgentResult:
    if result.status != TaskStatus.SUCCEEDED:
        raise StageExecutionError(
            f"Task {result.task_id} failed with status={result.status.value}: {result.error}"
        )
    return result


def require_all_success(results: Iterable[AgentResult]) -> list[AgentResult]:
    return [require_success(result) for result in results]
