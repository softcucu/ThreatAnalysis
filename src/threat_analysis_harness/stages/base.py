"""Shared helpers for agent-backed business stages."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable, Protocol

from agent_runtime import AgentResult, AgentTask, SubmitTasks, TaskStatus
from agent_runtime.output_validation import parse_validate_and_write_output

from threat_analysis_harness.errors import StageExecutionError


class ProgressReporter(Protocol):
    def emit(self, message: str) -> None:
        ...


def require_success(result: AgentResult) -> AgentResult:
    if result.status != TaskStatus.SUCCEEDED:
        raise StageExecutionError(
            f"Task {result.task_id} failed with status={result.status.value}: {result.error}"
        )
    return result


def require_all_success(results: Iterable[AgentResult]) -> list[AgentResult]:
    return [require_success(result) for result in results]


def existing_success_result(task: AgentTask) -> AgentResult | None:
    if not Path(task.output_path).exists():
        return None

    try:
        output = parse_validate_and_write_output(task)
    except Exception:
        return None

    now = time.time()
    return AgentResult(
        task_id=task.task_id,
        task_type=task.task_type,
        status=TaskStatus.SUCCEEDED,
        output_path=task.output_path,
        started_at=now,
        finished_at=now,
        returncode=0,
        output=output,
        metadata=dict(task.metadata),
    )


def run_or_resume_tasks(
    *,
    submit_tasks: SubmitTasks,
    tasks: list[AgentTask],
    resume: bool = False,
    timeout: float | None = None,
    progress_reporter: ProgressReporter | None = None,
) -> list[AgentResult]:
    results, pending, pending_indexes = resume_existing_tasks(
        tasks,
        resume=resume,
        progress_reporter=progress_reporter,
    )

    if pending:
        pending_results = submit_tasks(pending, timeout=timeout)
        fill_pending_results(results, pending_indexes, pending_results)

    return completed_results(results)


def run_or_resume_task(
    *,
    submit_tasks: SubmitTasks,
    task: AgentTask,
    resume: bool = False,
    timeout: float | None = None,
    progress_reporter: ProgressReporter | None = None,
) -> AgentResult:
    return run_or_resume_tasks(
        submit_tasks=submit_tasks,
        tasks=[task],
        resume=resume,
        timeout=timeout,
        progress_reporter=progress_reporter,
    )[0]


def resume_existing_tasks(
    tasks: list[AgentTask],
    *,
    resume: bool = False,
    progress_reporter: ProgressReporter | None = None,
) -> tuple[list[AgentResult | None], list[AgentTask], list[int]]:
    results: list[AgentResult | None] = [None] * len(tasks)
    pending: list[AgentTask] = []
    pending_indexes: list[int] = []

    for index, task in enumerate(tasks):
        existing = existing_success_result(task) if resume else None
        if existing is not None:
            results[index] = existing
            if progress_reporter is not None:
                progress_reporter.emit(
                    f"task resumed: task_id={task.task_id} task_type={task.task_type} "
                    f"output={task.output_path}"
                )
            continue
        pending.append(task)
        pending_indexes.append(index)

    return results, pending, pending_indexes


def fill_pending_results(
    results: list[AgentResult | None],
    pending_indexes: list[int],
    pending_results: Iterable[AgentResult],
) -> None:
    for index, result in zip(pending_indexes, pending_results):
        results[index] = result


def completed_results(results: list[AgentResult | None]) -> list[AgentResult]:
    return [result for result in results if result is not None]
