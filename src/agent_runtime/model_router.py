"""Task-type to model routing and concurrency configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from agent_runtime.errors import ModelRouteError


DEFAULT_MAX_RETRIES = 3


@dataclass(frozen=True)
class ModelConfig:
    model: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    command: tuple[str, ...] | None = None
    resource: str | None = None

    @classmethod
    def from_value(cls, value: str | Mapping[str, Any]) -> "ModelConfig":
        if isinstance(value, str):
            return cls(model=value)
        command = value.get("command")
        return cls(
            model=str(value["model"]),
            parameters=dict(value.get("parameters", {})),
            command=None if command is None else tuple(str(part) for part in command),
            resource=None if value.get("resource") is None else str(value["resource"]),
        )

    @property
    def resource_name(self) -> str:
        return self.resource or self.model


@dataclass(frozen=True)
class RuntimeConfig:
    models: Mapping[str, ModelConfig]
    model_routes: Mapping[str, tuple[ModelConfig, ...]]
    model_resource_limits: Mapping[str, int] = field(default_factory=dict)
    global_concurrency: int = 1
    task_type_concurrency: Mapping[str, int] = field(default_factory=dict)
    progress_enabled: bool = False
    retry_max_retries: int = DEFAULT_MAX_RETRIES

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RuntimeConfig":
        model_routes = {
            str(task_type): _model_configs_from_value(value)
            for task_type, value in data.get("models", {}).items()
        }
        models = {task_type: configs[0] for task_type, configs in model_routes.items() if configs}
        model_resource_limits = _model_resource_limits_from_value(data.get("model_resources", {}))
        concurrency = data.get("concurrency", {})
        default_global_concurrency = _default_global_concurrency(model_resource_limits)
        return cls(
            models=models,
            model_routes=model_routes,
            model_resource_limits=model_resource_limits,
            global_concurrency=int(
                concurrency.get(
                    "global",
                    data.get("global_concurrency", default_global_concurrency),
                )
            ),
            task_type_concurrency={
                str(task_type): int(limit)
                for task_type, limit in concurrency.get("by_task_type", {}).items()
            },
            progress_enabled=_progress_enabled_from_data(data),
            retry_max_retries=_retry_max_retries_from_data(data),
        )


class ModelRouter:
    """Resolve model settings for a task type."""

    def __init__(self, config: RuntimeConfig):
        if config.global_concurrency < 1:
            raise ValueError("global_concurrency must be >= 1")
        if config.retry_max_retries < 0:
            raise ValueError("retry max_retries must be >= 0")
        self._config = config

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModelRouter":
        return cls(RuntimeConfig.from_dict(data))

    @property
    def global_concurrency(self) -> int:
        return self._config.global_concurrency

    @property
    def progress_enabled(self) -> bool:
        return self._config.progress_enabled

    @property
    def retry_max_retries(self) -> int:
        return self._config.retry_max_retries

    def route(self, task_type: str) -> ModelConfig:
        try:
            return self._config.models[task_type]
        except KeyError as exc:
            raise ModelRouteError(f"No model configured for task_type={task_type!r}") from exc

    def routes(self, task_type: str) -> tuple[ModelConfig, ...]:
        try:
            configs = self._config.model_routes[task_type]
        except KeyError as exc:
            raise ModelRouteError(f"No model configured for task_type={task_type!r}") from exc
        if not configs:
            raise ModelRouteError(f"No model configured for task_type={task_type!r}")
        return configs

    def concurrency_limit(self, task_type: str) -> int:
        return int(self._config.task_type_concurrency.get(task_type, self.global_concurrency))

    def task_type_concurrency_limit(self, task_type: str) -> int | None:
        value = self._config.task_type_concurrency.get(task_type)
        return None if value is None else int(value)

    def resource_concurrency_limit(self, resource_name: str) -> int:
        return int(self._config.model_resource_limits.get(resource_name, self.global_concurrency))


def _model_configs_from_value(
    value: str | Mapping[str, Any] | list[Any],
) -> tuple[ModelConfig, ...]:
    if isinstance(value, list):
        if not value:
            raise ValueError("model list cannot be empty")
        return tuple(ModelConfig.from_value(item) for item in value)
    return (ModelConfig.from_value(value),)


def _model_resource_limits_from_value(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}

    limits: dict[str, int] = {}
    for resource_name, resource_config in value.items():
        if isinstance(resource_config, Mapping):
            limit = int(resource_config.get("concurrency", 1))
        else:
            limit = int(resource_config)
        if limit < 1:
            raise ValueError(f"model resource concurrency must be >= 1: {resource_name}")
        limits[str(resource_name)] = limit
    return limits


def _default_global_concurrency(model_resource_limits: Mapping[str, int]) -> int:
    if not model_resource_limits:
        return 1
    return max(1, sum(int(limit) for limit in model_resource_limits.values()))


def _progress_enabled_from_data(data: Mapping[str, Any]) -> bool:
    if "print_progress" in data:
        return _bool_from_value(data["print_progress"])
    if "verbose" in data:
        return _bool_from_value(data["verbose"])

    progress = data.get("progress", {})
    if isinstance(progress, Mapping):
        return _bool_from_value(progress.get("enabled", False))
    return _bool_from_value(progress)


def _retry_max_retries_from_data(data: Mapping[str, Any]) -> int:
    missing = object()
    retry = data.get("retry", missing)
    if isinstance(retry, Mapping):
        value = retry.get("max_retries", data.get("max_retries", DEFAULT_MAX_RETRIES))
    elif retry is missing:
        value = data.get("max_retries", DEFAULT_MAX_RETRIES)
    elif retry is False:
        value = 0
    elif retry is True:
        value = DEFAULT_MAX_RETRIES
    elif retry is None:
        value = DEFAULT_MAX_RETRIES
    else:
        value = retry
    return int(value)


def _bool_from_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
