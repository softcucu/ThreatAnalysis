"""Task and result data models used by the agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence


class TaskStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


@dataclass(frozen=True)
class AgentTask:
    """A unit of work submitted by a business stage to the agent runtime."""

    task_id: str
    task_type: str
    skill_path: str
    runtime_prompt: str
    output_path: str
    input_files: tuple[str, ...] = ()
    output_schema: Mapping[str, Any] | None = None
    output_schema_path: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    priority: int = 100

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentTask":
        return cls(
            task_id=str(data["task_id"]),
            task_type=str(data["task_type"]),
            skill_path=str(data["skill_path"]),
            runtime_prompt=str(data.get("runtime_prompt", "")),
            output_path=str(data["output_path"]),
            input_files=tuple(str(path) for path in data.get("input_files", ())),
            output_schema=data.get("output_schema"),
            output_schema_path=(
                None if data.get("output_schema_path") is None else str(data["output_schema_path"])
            ),
            metadata=dict(data.get("metadata", {})),
            priority=int(data.get("priority", 100)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "skill_path": self.skill_path,
            "runtime_prompt": self.runtime_prompt,
            "input_files": list(self.input_files),
            "output_path": self.output_path,
            "output_schema": self.output_schema,
            "output_schema_path": self.output_schema_path,
            "metadata": dict(self.metadata),
            "priority": self.priority,
        }

    @property
    def skill_file(self) -> Path:
        path = Path(self.skill_path)
        return path if path.is_file() else path / "SKILL.md"


@dataclass(frozen=True)
class AgentResult:
    """The runtime result for one submitted task."""

    task_id: str
    task_type: str
    status: TaskStatus
    output_path: str
    model: str | None = None
    log_path: str | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    returncode: int | None = None
    output: Any | None = None
    raw_output: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def queued(cls, task: AgentTask) -> "AgentResult":
        return cls(
            task_id=task.task_id,
            task_type=task.task_type,
            status=TaskStatus.QUEUED,
            output_path=task.output_path,
            metadata=dict(task.metadata),
        )

    def with_status(self, status: TaskStatus, **updates: Any) -> "AgentResult":
        data = self.to_dict()
        data.update(updates)
        data["status"] = status.value
        return AgentResult.from_dict(data)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentResult":
        return cls(
            task_id=str(data["task_id"]),
            task_type=str(data["task_type"]),
            status=TaskStatus(str(data["status"])),
            output_path=str(data["output_path"]),
            model=None if data.get("model") is None else str(data["model"]),
            log_path=None if data.get("log_path") is None else str(data["log_path"]),
            error=None if data.get("error") is None else str(data["error"]),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            returncode=data.get("returncode"),
            output=data.get("output"),
            raw_output=None if data.get("raw_output") is None else str(data["raw_output"]),
            metadata=dict(data.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self.status.value,
            "output_path": self.output_path,
            "model": self.model,
            "log_path": self.log_path,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "returncode": self.returncode,
            "output": self.output,
            "raw_output": self.raw_output,
            "metadata": dict(self.metadata),
        }


def normalize_tasks(tasks: Sequence[AgentTask | Mapping[str, Any]]) -> list[AgentTask]:
    normalized: list[AgentTask] = []
    for task in tasks:
        normalized.append(task if isinstance(task, AgentTask) else AgentTask.from_dict(task))
    return normalized
