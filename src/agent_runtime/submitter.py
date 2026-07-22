"""Public task submission entry points."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence, Union

from agent_runtime.scheduler import AgentScheduler
from agent_runtime.task import AgentResult, AgentTask


TaskJson = Mapping[str, Any]
TaskResultJson = Dict[str, Any]
AgentTaskPayload = Union[AgentTask, TaskJson]


@dataclass(frozen=True)
class TaskHandle:
    task_id: str
    submitter: "AgentSubmitter"

    def wait(self, timeout: float | None = None) -> AgentResult:
        return self.submitter.wait(self.task_id, timeout)


class AgentSubmitter:
    """Backward-compatible object facade around a scheduler."""

    def __init__(self, scheduler: AgentScheduler):
        self.scheduler = scheduler

    def submit_tasks(
        self,
        tasks: Sequence[TaskJson],
        *,
        timeout: float | None = None,
    ) -> list[TaskResultJson]:
        return submit_tasks(self.scheduler, tasks, timeout=timeout)

    def submit(self, task: AgentTaskPayload) -> TaskHandle:
        task_id = self.scheduler.submit(_to_agent_task(task))
        return TaskHandle(task_id=task_id, submitter=self)

    def submit_many(self, tasks: Sequence[AgentTaskPayload]) -> list[TaskHandle]:
        agent_tasks = [_to_agent_task(task) for task in tasks]
        return [
            TaskHandle(task_id=task_id, submitter=self)
            for task_id in self.scheduler.submit_many(agent_tasks)
        ]

    def wait(
        self,
        task_id: str,
        timeout: float | None = None,
    ) -> AgentResult:
        return self.scheduler.wait(task_id, timeout)

    def wait_all(
        self,
        handles: list[TaskHandle],
        timeout: float | None = None,
    ) -> list[AgentResult]:
        return self.scheduler.wait_all([handle.task_id for handle in handles], timeout)


def submit_tasks(
    scheduler: AgentScheduler,
    tasks: Sequence[TaskJson],
    *,
    timeout: float | None = None,
) -> list[TaskResultJson]:
    """Submit JSON task definitions and return JSON task results."""

    task_ids = scheduler.submit_many([_to_agent_task(task) for task in tasks])
    return [_to_result_json(result) for result in scheduler.wait_all(task_ids, timeout)]


def _to_agent_task(task: AgentTaskPayload) -> AgentTask:
    if isinstance(task, AgentTask):
        return task
    if isinstance(task, Mapping):
        return AgentTask.from_dict(task)
    raise TypeError(f"Unsupported task payload: {type(task).__name__}")


def _to_result_json(result: AgentResult) -> TaskResultJson:
    return {
        "task_id": result.task_id,
        "task_type": result.task_type,
        "status": result.status.value,
        "output_path": result.output_path,
        "model": result.model,
        "log_path": result.log_path,
        "error": result.error,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "returncode": result.returncode,
        "output": result.output,
        "raw_output": result.raw_output,
        "metadata": dict(result.metadata),
    }
