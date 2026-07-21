"""Concurrent scheduler for agent tasks."""

from __future__ import annotations

import threading
import time
from collections import defaultdict

from agent_runtime.errors import OutputValidationError, SchedulerStateError
from agent_runtime.model_router import ModelConfig, ModelRouter
from agent_runtime.output_validation import parse_validate_and_write_output
from agent_runtime.preflight import validate_task
from agent_runtime.progress import ProgressPrinter, ProgressReporter
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
        progress_reporter: ProgressReporter | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.runner = runner
        self.model_router = model_router
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.result_store = result_store or ResultStore()
        self.max_retries = (
            self.model_router.retry_max_retries if max_retries is None else int(max_retries)
        )
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.progress_reporter = progress_reporter
        if self.progress_reporter is None and self.model_router.progress_enabled:
            self.progress_reporter = ProgressPrinter(enabled=True)
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
        self._progress(f"runtime started: workers={self.model_router.global_concurrency}")
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
        self._progress("runtime stopped")

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
        self._progress(
            f"task queued: task_id={normalized.task_id} task_type={normalized.task_type} "
            f"output={normalized.output_path}"
        )
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
        task_started_at = time.time()
        try:
            model_config = self._selected_models.get(task.task_id) or self.model_router.route(
                task.task_type
            )
        except Exception as exc:
            result = AgentResult(
                task_id=task.task_id,
                task_type=task.task_type,
                status=TaskStatus.FAILED,
                output_path=task.output_path,
                error=str(exc),
                started_at=task_started_at,
                finished_at=time.time(),
                metadata=dict(task.metadata),
            )
            self.result_store.update(
                _with_runtime_retry_metadata(
                    result,
                    attempt=1,
                    max_retries=self.max_retries,
                    started_at=task_started_at,
                )
            )
            self._progress_result(result, fallback_started_at=task_started_at)
            return

        max_attempts = self.max_retries + 1
        prompt: str | None = None

        for attempt in range(1, max_attempts + 1):
            self.result_store.mark_running(
                task,
                started_at=task_started_at,
                model=model_config.model,
            )
            self._progress(
                f"task started: task_id={task.task_id} task_type={task.task_type} "
                f"model={model_config.model} attempt={attempt}/{max_attempts}"
            )

            try:
                if prompt is None:
                    prompt = self.prompt_builder.build(task, model_config)
                result = self._run_task_attempt(
                    task,
                    model_config,
                    prompt,
                )
            except Exception as exc:
                result = AgentResult(
                    task_id=task.task_id,
                    task_type=task.task_type,
                    status=TaskStatus.FAILED,
                    output_path=task.output_path,
                    model=model_config.model,
                    error=str(exc),
                    started_at=task_started_at,
                    finished_at=time.time(),
                    metadata=dict(task.metadata),
                )
            result = _with_runtime_retry_metadata(
                result,
                attempt=attempt,
                max_retries=self.max_retries,
                started_at=task_started_at,
            )
            if result.status != TaskStatus.FAILED or attempt >= max_attempts:
                self.result_store.update(result)
                self._progress_result(result, fallback_started_at=task_started_at)
                return

            self.result_store.update(
                result.with_status(
                    TaskStatus.RUNNING,
                    started_at=task_started_at,
                    finished_at=None,
                )
            )
            self._progress_retry(result, attempt=attempt, max_attempts=max_attempts)

    def _run_task_attempt(
        self,
        task: AgentTask,
        model_config: ModelConfig,
        prompt: str,
    ) -> AgentResult:
        started_at = time.time()
        try:
            result = self.runner.run(task, model_config, prompt)
            if result.status != TaskStatus.SUCCEEDED:
                return result

            try:
                output = parse_validate_and_write_output(task, raw=result.raw_output)
            except OutputValidationError as exc:
                self._progress(
                    f"task output validation failed; repair starting: task_id={task.task_id} "
                    f"task_type={task.task_type} model={model_config.model} "
                    f"error={_short_error(str(exc))}"
                )
                repaired = self._repair_output_after_validation_failure(
                    task,
                    model_config,
                    result,
                    exc,
                )
                if repaired is not None:
                    if repaired.status != TaskStatus.SUCCEEDED:
                        self._progress(
                            f"task output repair failed: task_id={task.task_id} "
                            f"task_type={task.task_type} model={model_config.model} "
                            f"error={_short_error(repaired.error)}"
                        )
                        return repaired
                    self._progress(
                        f"task output repair returned: task_id={task.task_id} "
                        f"task_type={task.task_type} model={model_config.model} "
                        f"raw_output={_raw_output_status(repaired.raw_output)}"
                    )
                    try:
                        output = parse_validate_and_write_output(
                            task,
                            raw=repaired.raw_output,
                        )
                    except Exception as repair_exc:
                        self._progress(
                            f"task output repair rejected: task_id={task.task_id} "
                            f"task_type={task.task_type} model={model_config.model} "
                            f"error={_short_error(str(repair_exc))}"
                        )
                        return repaired.with_status(
                            TaskStatus.FAILED,
                            error=str(repair_exc),
                            finished_at=time.time(),
                        )
                    self._progress(
                        f"task output repair accepted: task_id={task.task_id} "
                        f"task_type={task.task_type} model={model_config.model} "
                        f"output={task.output_path}"
                    )
                    return repaired.with_status(
                        TaskStatus.SUCCEEDED,
                        output=output,
                        raw_output=None,
                    )

                self._progress(
                    f"task output repair unavailable: task_id={task.task_id} "
                    f"task_type={task.task_type} model={model_config.model}"
                )
                return result.with_status(
                    TaskStatus.FAILED,
                    error=str(exc),
                    finished_at=time.time(),
                )
            except Exception as exc:
                return result.with_status(
                    TaskStatus.FAILED,
                    error=str(exc),
                    finished_at=time.time(),
                )

            return result.with_status(
                TaskStatus.SUCCEEDED,
                output=output,
                raw_output=None,
            )
        except Exception as exc:
            return AgentResult(
                task_id=task.task_id,
                task_type=task.task_type,
                status=TaskStatus.FAILED,
                output_path=task.output_path,
                model=model_config.model,
                error=str(exc),
                started_at=started_at,
                finished_at=time.time(),
                metadata=dict(task.metadata),
            )

    def _repair_output_after_validation_failure(
        self,
        task: AgentTask,
        model_config: ModelConfig,
        result: AgentResult,
        error: OutputValidationError,
    ) -> AgentResult | None:
        repair = getattr(self.runner, "repair_output_after_validation_failure", None)
        if not callable(repair):
            return None
        try:
            return repair(task, model_config, result, str(error))
        except Exception as exc:
            return result.with_status(
                TaskStatus.FAILED,
                error=f"{error}; output repair failed: {exc}",
                finished_at=time.time(),
            )

    def _progress(self, message: str) -> None:
        if self.progress_reporter is not None:
            self.progress_reporter.emit(message)

    def _progress_result(self, result: AgentResult, *, fallback_started_at: float) -> None:
        started_at = result.started_at or fallback_started_at
        finished_at = result.finished_at or time.time()
        duration = f"{finished_at - started_at:.1f}s"
        if result.status == TaskStatus.SUCCEEDED:
            self._progress(
                f"task completed: task_id={result.task_id} task_type={result.task_type} "
                f"model={result.model or '-'} duration={duration} output={result.output_path}"
            )
            return

        detail = _short_error(result.error)
        log_suffix = "" if not result.log_path else f" log={result.log_path}"
        self._progress(
            f"task failed: task_id={result.task_id} task_type={result.task_type} "
            f"model={result.model or '-'} duration={duration} error={detail}{log_suffix}"
        )

    def _progress_retry(
        self,
        result: AgentResult,
        *,
        attempt: int,
        max_attempts: int,
    ) -> None:
        detail = _short_error(result.error)
        log_suffix = "" if not result.log_path else f" log={result.log_path}"
        self._progress(
            f"task retrying: task_id={result.task_id} task_type={result.task_type} "
            f"model={result.model or '-'} attempt={attempt}/{max_attempts} "
            f"next_attempt={attempt + 1}/{max_attempts} error={detail}{log_suffix}"
        )

    def __enter__(self) -> "AgentScheduler":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.stop(wait=True)


def _short_error(value: str | None, *, limit: int = 200) -> str:
    text = "unknown error" if value is None else " ".join(value.split())
    if len(text) <= limit:
        return repr(text)
    return repr(text[: limit - 3] + "...")


def _raw_output_status(value: str | None) -> str:
    if value is None:
        return "missing"
    return f"{len(value)} chars"


def _with_runtime_retry_metadata(
    result: AgentResult,
    *,
    attempt: int,
    max_retries: int,
    started_at: float,
) -> AgentResult:
    metadata = dict(result.metadata)
    metadata["runtime_retry"] = {
        "attempt": attempt,
        "max_retries": max_retries,
        "max_attempts": max_retries + 1,
    }
    return result.with_status(
        result.status,
        started_at=started_at,
        finished_at=result.finished_at or time.time(),
        metadata=metadata,
    )
