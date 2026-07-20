"""Public submission facade used by business stages."""

from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.scheduler import AgentScheduler
from agent_runtime.task import AgentResult, AgentTask


@dataclass(frozen=True)
class TaskHandle:
    task_id: str
    submitter: "AgentSubmitter"

    def wait(self, timeout: float | None = None) -> AgentResult:
        return self.submitter.wait(self.task_id, timeout)


class AgentSubmitter:
    """Small API surface that stages depend on."""

    def __init__(self, scheduler: AgentScheduler):
        self.scheduler = scheduler

    def submit(self, task: AgentTask | dict) -> TaskHandle:
        task_id = self.scheduler.submit(task)
        return TaskHandle(task_id=task_id, submitter=self)

    def submit_many(self, tasks: list[AgentTask | dict]) -> list[TaskHandle]:
        return [TaskHandle(task_id=task_id, submitter=self) for task_id in self.scheduler.submit_many(tasks)]

    def wait(self, task_id: str, timeout: float | None = None) -> AgentResult:
        return self.scheduler.wait(task_id, timeout)

    def wait_all(self, handles: list[TaskHandle], timeout: float | None = None) -> list[AgentResult]:
        return self.scheduler.wait_all([handle.task_id for handle in handles], timeout)
