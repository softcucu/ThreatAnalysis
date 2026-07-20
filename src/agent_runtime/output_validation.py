"""Parse and validate agent JSON output against task-provided JSON Schema."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from agent_runtime.errors import OutputValidationError, TaskValidationError
from agent_runtime.task import AgentTask


_FENCED_BLOCK_RE = re.compile(
    r"```(?P<info>[^\n`]*)\n(?P<body>.*?)```",
    re.DOTALL,
)


def load_task_schema(task: AgentTask) -> Mapping[str, Any]:
    if task.output_schema is not None and task.output_schema_path is not None:
        raise TaskValidationError(
            f"Task {task.task_id} provides both output_schema and output_schema_path"
        )
    if task.output_schema is not None:
        if not isinstance(task.output_schema, Mapping):
            raise TaskValidationError(f"output_schema must be an object for task_id={task.task_id}")
        return task.output_schema
    if task.output_schema_path is not None:
        path = Path(task.output_schema_path)
        if not path.exists():
            raise TaskValidationError(f"output_schema_path does not exist: {path}")
        with path.open("r", encoding="utf-8") as fh:
            schema = json.load(fh)
        if not isinstance(schema, Mapping):
            raise TaskValidationError(f"output schema file must contain an object: {path}")
        return schema
    raise TaskValidationError(
        f"Task {task.task_id} must provide output_schema or output_schema_path"
    )


def parse_and_validate_output(task: AgentTask) -> Any:
    output_path = Path(task.output_path)
    if not output_path.exists():
        raise OutputValidationError(f"Agent output file does not exist: {output_path}")
    raw = output_path.read_text(encoding="utf-8")
    schema = load_task_schema(task)
    return parse_json_output_for_schema(raw, schema)


def parse_validate_and_write_output(task: AgentTask, raw: str | None = None) -> Any:
    """Parse agent text, validate it, and write canonical JSON to output_path."""

    output_path = Path(task.output_path)
    if raw is None:
        if not output_path.exists():
            raise OutputValidationError(f"Agent output file does not exist: {output_path}")
        raw = output_path.read_text(encoding="utf-8")

    schema = load_task_schema(task)
    data = parse_json_output_for_schema(raw, schema)
    write_json_output(output_path, data)
    return data


def write_json_output(path: str | Path, payload: Any) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def parse_json_output(raw: str) -> Any:
    values, errors = _parse_json_values(raw)
    if values:
        return values[0]

    raise _json_parse_error(raw, errors)


def parse_json_output_for_schema(raw: str, schema: Mapping[str, Any]) -> Any:
    values, errors = _parse_json_values(raw)
    validation_errors: list[OutputValidationError] = []
    for value in values:
        try:
            validate_json_schema(value, schema)
            return value
        except OutputValidationError as exc:
            validation_errors.append(exc)

    if validation_errors:
        raise validation_errors[-1]
    raise _json_parse_error(raw, errors)


def _parse_json_values(raw: str) -> tuple[list[Any], list[json.JSONDecodeError]]:
    text = raw.strip().lstrip("\ufeff").strip()
    errors: list[json.JSONDecodeError] = []
    values: list[Any] = []

    for candidate in _json_text_candidates(text):
        try:
            values.append(json.loads(candidate))
        except json.JSONDecodeError as exc:
            errors.append(exc)

    extracted, extract_errors = _extract_json_values(text)
    values.extend(extracted)
    errors.extend(extract_errors)
    return values, errors


def _json_parse_error(raw: str, errors: list[json.JSONDecodeError]) -> OutputValidationError:
    text = raw.strip().lstrip("\ufeff").strip()
    detail = errors[-1] if errors else "empty output"
    snippet = text[:300].replace("\n", "\\n")
    return OutputValidationError(
        f"Agent output does not contain valid JSON: {detail}. "
        f"Output starts with: {snippet!r}"
    )


def _json_text_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    if text:
        candidates.append(text)

    fenced_blocks = [
        (match.group("info").strip().lower(), match.group("body").strip())
        for match in _FENCED_BLOCK_RE.finditer(text)
    ]
    candidates.extend(
        body for info, body in fenced_blocks if body and _is_json_fence(info)
    )
    candidates.extend(
        body for info, body in fenced_blocks if body and not _is_json_fence(info)
    )
    return candidates


def _is_json_fence(info: str) -> bool:
    return not info or info.split()[0] == "json"


def _extract_json_values(text: str) -> tuple[list[Any], list[json.JSONDecodeError]]:
    decoder = json.JSONDecoder()
    errors: list[json.JSONDecodeError] = []
    values: list[Any] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char not in "{[":
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(text, index)
            values.append(value)
            index = max(end, index + 1)
        except json.JSONDecodeError as exc:
            errors.append(exc)
            index += 1

    if not values and not errors:
        errors.append(json.JSONDecodeError("No JSON object or array found", text, 0))
    return values, errors


def validate_json_schema(value: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    if "enum" in schema and value not in schema["enum"]:
        raise OutputValidationError(f"{path}: value {value!r} not in enum {schema['enum']!r}")

    if "const" in schema and value != schema["const"]:
        raise OutputValidationError(f"{path}: value {value!r} does not equal const {schema['const']!r}")

    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(value, expected_type):
        raise OutputValidationError(f"{path}: expected type {expected_type!r}, got {type(value).__name__}")

    if isinstance(value, dict):
        _validate_object(value, schema, path)
    elif isinstance(value, list):
        _validate_array(value, schema, path)
    elif isinstance(value, str):
        _validate_string(value, schema, path)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        _validate_number(value, schema, path)


def _validate_object(value: dict[str, Any], schema: Mapping[str, Any], path: str) -> None:
    required = schema.get("required", [])
    for key in required:
        if key not in value:
            raise OutputValidationError(f"{path}: missing required property {key!r}")

    properties = schema.get("properties", {})
    for key, property_schema in properties.items():
        if key in value:
            validate_json_schema(value[key], property_schema, f"{path}.{key}")

    additional = schema.get("additionalProperties", True)
    if additional is False:
        extras = sorted(set(value) - set(properties))
        if extras:
            raise OutputValidationError(f"{path}: additional properties not allowed: {extras!r}")
    elif isinstance(additional, Mapping):
        for key in set(value) - set(properties):
            validate_json_schema(value[key], additional, f"{path}.{key}")


def _validate_array(value: list[Any], schema: Mapping[str, Any], path: str) -> None:
    if "minItems" in schema and len(value) < int(schema["minItems"]):
        raise OutputValidationError(f"{path}: expected at least {schema['minItems']} items")
    if "maxItems" in schema and len(value) > int(schema["maxItems"]):
        raise OutputValidationError(f"{path}: expected at most {schema['maxItems']} items")
    item_schema = schema.get("items")
    if isinstance(item_schema, Mapping):
        for index, item in enumerate(value):
            validate_json_schema(item, item_schema, f"{path}[{index}]")


def _validate_string(value: str, schema: Mapping[str, Any], path: str) -> None:
    if "minLength" in schema and len(value) < int(schema["minLength"]):
        raise OutputValidationError(f"{path}: string is shorter than minLength={schema['minLength']}")
    if "maxLength" in schema and len(value) > int(schema["maxLength"]):
        raise OutputValidationError(f"{path}: string is longer than maxLength={schema['maxLength']}")


def _validate_number(value: int | float, schema: Mapping[str, Any], path: str) -> None:
    if "minimum" in schema and value < schema["minimum"]:
        raise OutputValidationError(f"{path}: number is smaller than minimum={schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        raise OutputValidationError(f"{path}: number is larger than maximum={schema['maximum']}")


def _matches_type(value: Any, expected_type: str | list[str]) -> bool:
    if isinstance(expected_type, list):
        return any(_matches_type(value, item) for item in expected_type)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    raise OutputValidationError(f"Unsupported JSON schema type: {expected_type!r}")
