"""Preflight validation before accepting an agent task."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from agent_runtime.errors import TaskValidationError
from agent_runtime.model_router import ModelRouter
from agent_runtime.output_validation import load_task_schema
from agent_runtime.task import AgentTask


@dataclass(frozen=True)
class PreflightReport:
    task_id: str
    ok: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)


def validate_task(
    task: AgentTask,
    model_router: ModelRouter | None = None,
    known_task_ids: Iterable[str] = (),
) -> PreflightReport:
    if not task.task_id.strip():
        raise TaskValidationError("task_id is required")
    if task.task_id in set(known_task_ids):
        raise TaskValidationError(f"task_id already exists: {task.task_id}")
    if not task.task_type.strip():
        raise TaskValidationError(f"task_type is required for task_id={task.task_id}")
    if model_router is not None:
        model_router.route(task.task_type)

    skill_file = task.skill_file
    if not skill_file.exists():
        raise TaskValidationError(f"Skill file does not exist: {skill_file}")
    if not skill_file.is_file():
        raise TaskValidationError(f"Skill path is not a file: {skill_file}")

    missing_inputs = [path for path in task.input_files if not Path(path).exists()]
    if missing_inputs:
        raise TaskValidationError(
            f"Input files do not exist for task_id={task.task_id}: {missing_inputs}"
        )

    output_path = Path(task.output_path)
    if output_path.exists() and output_path.is_dir():
        raise TaskValidationError(f"output_path points to a directory: {output_path}")
    output_parent = output_path.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    if not output_parent.exists() or not output_parent.is_dir():
        raise TaskValidationError(f"Cannot create output directory: {output_parent}")

    load_task_schema(task)

    return PreflightReport(task_id=task.task_id, ok=True)
