"""Thread-safe task status and result store."""

from __future__ import annotations

import time
from threading import Condition

from agent_runtime.errors import DuplicateTaskError, TaskNotFoundError, TaskTimeoutError
from agent_runtime.task import AgentResult, AgentTask, TaskStatus


class ResultStore:
    def __init__(self) -> None:
        self._condition = Condition()
        self._results: dict[str, AgentResult] = {}

    def register(self, task: AgentTask) -> None:
        with self._condition:
            if task.task_id in self._results:
                raise DuplicateTaskError(f"Duplicate task_id: {task.task_id}")
            self._results[task.task_id] = AgentResult.queued(task)
            self._condition.notify_all()

    def update(self, result: AgentResult) -> None:
        with self._condition:
            if result.task_id not in self._results:
                raise TaskNotFoundError(result.task_id)
            self._results[result.task_id] = result
            self._condition.notify_all()

    def mark_running(self, task: AgentTask, started_at: float, model: str) -> None:
        with self._condition:
            current = self._require(task.task_id)
            self._results[task.task_id] = current.with_status(
                TaskStatus.RUNNING,
                started_at=started_at,
                model=model,
            )
            self._condition.notify_all()

    def get(self, task_id: str) -> AgentResult:
        with self._condition:
            return self._require(task_id)

    def wait(self, task_id: str, timeout: float | None = None) -> AgentResult:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                result = self._require(task_id)
                if result.status.terminal:
                    return result
                if timeout is not None:
                    remaining = deadline - time.monotonic()  # type: ignore[operator]
                    if remaining <= 0:
                        raise TaskTimeoutError(f"Timed out waiting for task_id={task_id}")
                    self._condition.wait(remaining)
                else:
                    self._condition.wait()

    def wait_all(self, task_ids: list[str], timeout: float | None = None) -> list[AgentResult]:
        deadline = None if timeout is None else time.monotonic() + timeout
        results: list[AgentResult] = []
        for task_id in task_ids:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            results.append(self.wait(task_id, remaining))
        return results

    def task_ids(self) -> set[str]:
        with self._condition:
            return set(self._results)

    def _require(self, task_id: str) -> AgentResult:
        try:
            return self._results[task_id]
        except KeyError as exc:
            raise TaskNotFoundError(task_id) from exc
