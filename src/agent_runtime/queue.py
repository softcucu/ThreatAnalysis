"""Thread-safe task queue with predicate-based retrieval."""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from threading import Condition
from typing import Callable

from agent_runtime.task import AgentTask


@dataclass(order=True)
class _QueuedTask:
    priority: int
    sequence: int
    task: AgentTask = field(compare=False)


class TaskQueue:
    """A small in-memory queue for scheduler-owned tasks."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._sequence = itertools.count()
        self._items: list[_QueuedTask] = []
        self._closed = False

    def put(self, task: AgentTask) -> None:
        with self._condition:
            if self._closed:
                raise RuntimeError("TaskQueue is closed")
            self._items.append(_QueuedTask(task.priority, next(self._sequence), task))
            self._items.sort()
            self._condition.notify_all()

    def get_available(
        self,
        can_run: Callable[[AgentTask], bool],
        timeout: float | None = None,
    ) -> AgentTask | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                if self._closed and not self._items:
                    return None
                for index, queued in enumerate(self._items):
                    if can_run(queued.task):
                        return self._items.pop(index).task
                if timeout is not None:
                    remaining = deadline - time.monotonic()  # type: ignore[operator]
                    if remaining <= 0:
                        return None
                    self._condition.wait(remaining)
                else:
                    self._condition.wait()

    def qsize(self) -> int:
        with self._condition:
            return len(self._items)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def notify(self) -> None:
        with self._condition:
            self._condition.notify_all()
