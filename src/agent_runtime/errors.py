"""Agent runtime exceptions."""


class AgentRuntimeError(Exception):
    """Base exception for agent runtime failures."""


class TaskValidationError(AgentRuntimeError):
    """Raised when a task cannot be accepted for execution."""


class DuplicateTaskError(TaskValidationError):
    """Raised when a task id is submitted more than once."""


class ModelRouteError(AgentRuntimeError):
    """Raised when no model configuration exists for a task type."""


class TaskNotFoundError(AgentRuntimeError):
    """Raised when a task id is unknown to the result store."""


class TaskTimeoutError(AgentRuntimeError):
    """Raised when waiting for a task result times out."""


class SchedulerStateError(AgentRuntimeError):
    """Raised when scheduler lifecycle operations are invalid."""


class OutputValidationError(AgentRuntimeError):
    """Raised when agent output is not valid JSON or violates its schema."""
