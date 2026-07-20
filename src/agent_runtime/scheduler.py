"""Concurrent scheduler for agent tasks."""

from __future__ import annotations

import threading
import time
from collections import defaultdict

from agent_runtime.errors import SchedulerStateError
from agent_runtime.model_router import ModelConfig, ModelRouter
from agent_runtime.output_validation import parse_and_validate_output
from agent_runtime.preflight import validate_task
from agent_runtime.prompt_builder import PromptBuilder
from agent_runtime.queue import TaskQueue
from agent_runtime.result_store import ResultStore
from agent_runtime.runner import AgentRunner
from agent_runtime.task import AgentResult, AgentTask, TaskStatus, normalize_tasks


class AgentScheduler:
    def __init__(
        self,
        *,
        runner: AgentRunner,
        model_router: ModelRouter,
        prompt_builder: PromptBuilder | None = None,
        result_store: ResultStore | None = None,
    ) -> None:
        self.runner = runner
        self.model_router = model_router
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.result_store = result_store or ResultStore()
        self._queue = TaskQueue()
        self._threads: list[threading.Thread] = []
        self._active_by_type: dict[str, int] = defaultdict(int)
        self._active_by_resource: dict[str, int] = defaultdict(int)
        self._selected_models: dict[str, ModelConfig] = {}
        self._active_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop_event.clear()
        for index in range(self.model_router.global_concurrency):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"agent-runtime-worker-{index + 1}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def stop(self, wait: bool = True) -> None:
        self._stop_event.set()
        self._queue.close()
        if wait:
            for thread in self._threads:
                thread.join(timeout=5)
        self._threads.clear()
        self._started = False

    def submit(self, task: AgentTask | dict) -> str:
        normalized = task if isinstance(task, AgentTask) else AgentTask.from_dict(task)
        if not self._started:
            raise SchedulerStateError("AgentScheduler must be started before submit")
        validate_task(
            normalized,
            model_router=self.model_router,
            known_task_ids=self.result_store.task_ids(),
        )
        self.result_store.register(normalized)
        self._queue.put(normalized)
        return normalized.task_id

    def submit_many(self, tasks: list[AgentTask | dict]) -> list[str]:
        task_ids: list[str] = []
        for task in normalize_tasks(tasks):
            task_ids.append(self.submit(task))
        return task_ids

    def wait(self, task_id: str, timeout: float | None = None) -> AgentResult:
        return self.result_store.wait(task_id, timeout)

    def wait_all(self, task_ids: list[str], timeout: float | None = None) -> list[AgentResult]:
        return self.result_store.wait_all(task_ids, timeout)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            task = self._queue.get_available(self._can_run, timeout=0.1)
            if task is None:
                continue
            try:
                self._run_task(task)
            finally:
                self._release_model(task)
                self._queue.notify()

    def _can_run(self, task: AgentTask) -> bool:
        with self._active_lock:
            task_type_limit = self.model_router.task_type_concurrency_limit(task.task_type)
            if (
                task_type_limit is not None
                and self._active_by_type[task.task_type] >= task_type_limit
            ):
                return False

            for model_config in self.model_router.routes(task.task_type):
                resource_name = model_config.resource_name
                resource_limit = self.model_router.resource_concurrency_limit(resource_name)
                if self._active_by_resource[resource_name] >= resource_limit:
                    continue
                self._active_by_type[task.task_type] += 1
                self._active_by_resource[resource_name] += 1
                self._selected_models[task.task_id] = model_config
                return True
            return False

    def _release_model(self, task: AgentTask) -> None:
        with self._active_lock:
            model_config = self._selected_models.pop(task.task_id, None)
            if model_config is not None:
                resource_name = model_config.resource_name
                self._active_by_resource[resource_name] -= 1
                if self._active_by_resource[resource_name] <= 0:
                    self._active_by_resource.pop(resource_name, None)

            self._active_by_type[task.task_type] -= 1
            if self._active_by_type[task.task_type] <= 0:
                self._active_by_type.pop(task.task_type, None)

    def _run_task(self, task: AgentTask) -> None:
        started_at = time.time()
        try:
            model_config = self._selected_models.get(task.task_id) or self.model_router.route(
                task.task_type
            )
            self.result_store.mark_running(task, started_at=started_at, model=model_config.model)
            prompt = self.prompt_builder.build(task, model_config)
            result = self.runner.run(task, model_config, prompt)
            if result.status == TaskStatus.SUCCEEDED:
                output = parse_and_validate_output(task)
                result = result.with_status(TaskStatus.SUCCEEDED, output=output)
            self.result_store.update(result)
        except Exception as exc:
            self.result_store.update(
                AgentResult(
                    task_id=task.task_id,
                    task_type=task.task_type,
                    status=TaskStatus.FAILED,
                    output_path=task.output_path,
                    error=str(exc),
                    started_at=started_at,
                    finished_at=time.time(),
                    metadata=dict(task.metadata),
                )
            )

    def __enter__(self) -> "AgentScheduler":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.stop(wait=True)
