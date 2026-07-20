"""Agent runner implementations."""

from __future__ import annotations

import base64
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence
from urllib import error, parse, request

from agent_runtime.model_router import ModelConfig
from agent_runtime.skills import install_opencode_skill, skill_name_from_path
from agent_runtime.task import AgentResult, AgentTask, TaskStatus


class AgentRunner(Protocol):
    def run(self, task: AgentTask, model_config: ModelConfig, prompt: str) -> AgentResult:
        """Run one task and return its terminal result."""


class FunctionAgentRunner:
    """Adapter for tests or in-process agent integrations."""

    def __init__(
        self,
        func: Callable[[AgentTask, ModelConfig, str], str | AgentResult],
    ) -> None:
        self._func = func

    def run(self, task: AgentTask, model_config: ModelConfig, prompt: str) -> AgentResult:
        started = time.time()
        output_path = Path(task.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        raw_output_path = _raw_output_path(output_path)
        try:
            value = self._func(task, model_config, prompt)
            if isinstance(value, AgentResult):
                return value
            raw_output_path.write_text(value, encoding="utf-8")
            return AgentResult(
                task_id=task.task_id,
                task_type=task.task_type,
                status=TaskStatus.SUCCEEDED,
                output_path=str(output_path),
                model=model_config.model,
                started_at=started,
                finished_at=time.time(),
                returncode=0,
                raw_output=value,
                metadata=dict(task.metadata),
            )
        except Exception as exc:  # pragma: no cover - exercised through tests as behavior
            return AgentResult(
                task_id=task.task_id,
                task_type=task.task_type,
                status=TaskStatus.FAILED,
                output_path=str(output_path),
                model=model_config.model,
                error=str(exc),
                started_at=started,
                finished_at=time.time(),
                metadata=dict(task.metadata),
            )


class CommandAgentRunner:
    """Run an external agent command.

    Command arguments may contain placeholders:
    {prompt_file}, {output_path}, {raw_output_path}, {skill_path},
    {skill_file}, {task_id}, {task_type}, and {model}.
    """

    def __init__(
        self,
        command_template: tuple[str, ...] | list[str],
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> None:
        self.command_template = tuple(command_template)
        self.cwd = cwd
        self.env = None if env is None else dict(env)
        self.timeout = timeout

    def run(self, task: AgentTask, model_config: ModelConfig, prompt: str) -> AgentResult:
        started = time.time()
        output_path = Path(task.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        raw_output_path = _raw_output_path(output_path)
        prompt_file = output_path.with_suffix(output_path.suffix + ".prompt.txt")
        log_file = output_path.with_suffix(output_path.suffix + ".log")
        prompt_file.write_text(prompt, encoding="utf-8")
        command = [
            part.format(
                prompt_file=str(prompt_file),
                output_path=str(output_path),
                raw_output_path=str(raw_output_path),
                skill_path=task.skill_path,
                skill_file=str(task.skill_file),
                task_id=task.task_id,
                task_type=task.task_type,
                model=model_config.model,
            )
            for part in (model_config.command or self.command_template)
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=self.cwd,
                env=self.env,
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
            log_file.write_text(
                "COMMAND: "
                + " ".join(command)
                + "\n\nSTDOUT:\n"
                + completed.stdout
                + "\n\nSTDERR:\n"
                + completed.stderr,
                encoding="utf-8",
            )
            status = TaskStatus.SUCCEEDED if completed.returncode == 0 else TaskStatus.FAILED
            return AgentResult(
                task_id=task.task_id,
                task_type=task.task_type,
                status=status,
                output_path=str(output_path),
                model=model_config.model,
                log_path=str(log_file),
                error=None if status == TaskStatus.SUCCEEDED else completed.stderr.strip(),
                started_at=started,
                finished_at=time.time(),
                returncode=completed.returncode,
                raw_output=None,
                metadata=dict(task.metadata),
            )
        except Exception as exc:
            log_file.write_text(str(exc), encoding="utf-8")
            return AgentResult(
                task_id=task.task_id,
                task_type=task.task_type,
                status=TaskStatus.FAILED,
                output_path=str(output_path),
                model=model_config.model,
                log_path=str(log_file),
                error=str(exc),
                started_at=started,
                finished_at=time.time(),
                metadata=dict(task.metadata),
            )


class OpenCodeAgentRunner:
    """Run tasks through an ``opencode serve`` HTTP server.

    The runner starts or connects to one background OpenCode server. Each
    AgentTask creates a separate OpenCode session and sends the generated prompt
    as a message. The task model is passed in the message body, so model routing
    remains controlled by RuntimeConfig.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:4096",
        start_command: tuple[str, ...] | list[str] | None = (
            "opencode",
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            "4096",
        ),
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        startup_timeout: float = 30.0,
        username: str | None = None,
        password: str | None = None,
        agent: str | None = None,
        delete_session: bool = False,
        install_skills: bool = True,
        use_skill_command: bool = False,
        skill_paths: Sequence[str | Path] = (),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.start_command = None if start_command is None else tuple(start_command)
        self.cwd = cwd
        self.env = None if env is None else dict(env)
        self.timeout = timeout
        self.startup_timeout = startup_timeout
        self.username = username or "opencode"
        self.password = password
        self.agent = agent
        self.delete_session = delete_session
        self.install_skills = install_skills
        self.use_skill_command = use_skill_command
        self.skill_paths = tuple(skill_paths)
        self._process: subprocess.Popen[str] | None = None
        self._start_lock = threading.Lock()

    def __enter__(self) -> "OpenCodeAgentRunner":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.stop()

    def start(self) -> None:
        with self._start_lock:
            self._install_configured_skills()
            if self._healthcheck():
                return
            if self.start_command is None:
                raise RuntimeError(f"OpenCode server is not reachable: {self.base_url}")

            self._process = subprocess.Popen(
                self.start_command,
                cwd=self.cwd,
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            deadline = time.monotonic() + self.startup_timeout
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    raise RuntimeError(
                        f"opencode serve exited early with returncode={self._process.returncode}"
                    )
                if self._healthcheck():
                    return
                time.sleep(0.2)

            raise RuntimeError(f"Timed out waiting for OpenCode server: {self.base_url}")

    def stop(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        self._process = None

    def run(self, task: AgentTask, model_config: ModelConfig, prompt: str) -> AgentResult:
        started = time.time()
        output_path = Path(task.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        raw_output_path = _raw_output_path(output_path)
        prompt_file = output_path.with_suffix(output_path.suffix + ".prompt.txt")
        log_file = output_path.with_suffix(output_path.suffix + ".log")
        prompt_file.write_text(prompt, encoding="utf-8")
        directory = self._directory()
        skill_name = skill_name_from_path(task.skill_path)

        session_id: str | None = None
        try:
            if self.install_skills:
                install_opencode_skill(task.skill_path, directory)
            self.start()
            session = self._post_json(
                "/session",
                {"title": task.task_id},
                query=self._opencode_query(directory),
            )
            session_id = _session_id(session)
            if not session_id:
                raise RuntimeError(f"OpenCode did not return a session id: {session!r}")

            if self.use_skill_command:
                response = self._post_json(
                    f"/session/{session_id}/command",
                    self._command_payload(prompt, skill_name, model_config),
                    query=self._opencode_query(directory),
                )
            else:
                message_payload = self._message_payload(
                    _skill_invocation_prompt(skill_name, prompt),
                    model_config,
                )
                response = self._post_json(
                    f"/session/{session_id}/message",
                    message_payload,
                    query=self._opencode_query(directory),
                )
                response = self._wait_for_final_message(session_id, directory) or response
            output_text = _extract_response_text(response)
            raw_output_path.write_text(output_text, encoding="utf-8")
            log_file.write_text(
                json.dumps(
                    {
                        "base_url": self.base_url,
                        "directory": str(directory),
                        "session_id": session_id,
                        "task_id": task.task_id,
                        "task_type": task.task_type,
                        "skill": skill_name,
                        "model": model_config.model,
                        "waited_for_completion": not self.use_skill_command,
                        "response": response,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return AgentResult(
                task_id=task.task_id,
                task_type=task.task_type,
                status=TaskStatus.SUCCEEDED,
                output_path=str(output_path),
                model=model_config.model,
                log_path=str(log_file),
                started_at=started,
                finished_at=time.time(),
                returncode=0,
                raw_output=output_text,
                metadata=dict(task.metadata),
            )
        except Exception as exc:
            log_file.write_text(str(exc), encoding="utf-8")
            return AgentResult(
                task_id=task.task_id,
                task_type=task.task_type,
                status=TaskStatus.FAILED,
                output_path=str(output_path),
                model=model_config.model,
                log_path=str(log_file),
                error=str(exc),
                started_at=started,
                finished_at=time.time(),
                metadata=dict(task.metadata),
            )
        finally:
            if self.delete_session and session_id:
                try:
                    self._request_json(
                        "DELETE",
                        f"/session/{session_id}",
                        query=self._opencode_query(directory),
                    )
                except Exception:
                    pass

    def _directory(self) -> Path:
        return Path(self.cwd).expanduser().resolve() if self.cwd else Path.cwd().resolve()

    def _install_configured_skills(self) -> None:
        if not self.install_skills:
            return
        directory = self._directory()
        for skill_path in self.skill_paths:
            install_opencode_skill(skill_path, directory)

    def _opencode_query(self, directory: Path) -> dict[str, str]:
        return {"directory": str(directory)}

    def _command_payload(
        self,
        prompt: str,
        skill_name: str,
        model_config: ModelConfig,
    ) -> dict:
        payload: dict = {
            "command": skill_name,
            "arguments": prompt,
            "model": _opencode_model_string(model_config),
        }
        agent = model_config.parameters.get("agent") or self.agent
        if agent:
            payload["agent"] = str(agent)
        variant = model_config.parameters.get("variant")
        if variant:
            payload["variant"] = str(variant)
        return payload

    def _message_payload(self, prompt: str, model_config: ModelConfig) -> dict:
        payload: dict = {
            "model": _opencode_model(model_config),
            "parts": [{"type": "text", "text": prompt}],
        }
        agent = model_config.parameters.get("agent") or self.agent
        if agent:
            payload["agent"] = str(agent)
        if "system" in model_config.parameters:
            payload["system"] = model_config.parameters["system"]
        if "tools" in model_config.parameters:
            payload["tools"] = model_config.parameters["tools"]
        return payload

    def _wait_for_final_message(self, session_id: str, directory: Path) -> object | None:
        try:
            self._request_json(
                "POST",
                f"/api/session/{session_id}/wait",
                query=self._opencode_query(directory),
            )
        except RuntimeError as exc:
            if _is_opencode_http_404(exc):
                return None
            raise
        messages = self._request_json(
            "GET",
            f"/api/session/{session_id}/message",
            query={**self._opencode_query(directory), "order": "desc", "limit": "20"},
        )
        message = _latest_assistant_message(messages)
        if message is None:
            raise RuntimeError(f"OpenCode session completed without assistant response: {session_id}")
        return message

    def _healthcheck(self) -> bool:
        try:
            self._request_json("GET", "/global/health")
            return True
        except Exception:
            return False

    def _post_json(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        query: Mapping[str, str] | None = None,
    ) -> object:
        return self._request_json("POST", path, payload, query=query)

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        query: Mapping[str, str] | None = None,
    ) -> object:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = self.base_url + path
        if query:
            url += "?" + parse.urlencode(query)
        req = request.Request(
            url,
            data=body,
            method=method,
            headers={"Accept": "application/json"},
        )
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if self.password is not None:
            token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8"))
            req.add_header("Authorization", "Basic " + token.decode("ascii"))
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenCode HTTP {exc.code} for {method} {path}: {detail}") from exc
        if not raw:
            return None
        return json.loads(raw)


def _session_id(session: object) -> str | None:
    if not isinstance(session, Mapping):
        return None
    value = (
        session.get("id")
        or session.get("ID")
        or session.get("sessionID")
        or session.get("session_id")
    )
    return None if value is None else str(value)


def _opencode_model(model_config: ModelConfig) -> dict[str, str]:
    configured = model_config.parameters.get("opencode_model")
    if isinstance(configured, Mapping):
        provider_id = configured.get("providerID") or configured.get("provider_id")
        model_id = configured.get("modelID") or configured.get("model_id")
        if provider_id and model_id:
            return {
                "providerID": str(provider_id),
                "modelID": str(model_id),
            }

    provider_id = (
        model_config.parameters.get("providerID")
        or model_config.parameters.get("provider_id")
        or model_config.parameters.get("provider")
    )
    model_id = model_config.parameters.get("modelID") or model_config.parameters.get("model_id")
    if provider_id and model_id:
        return {
            "providerID": str(provider_id),
            "modelID": str(model_id),
        }

    provider, separator, model = model_config.model.partition("/")
    if separator and provider and model:
        return {
            "providerID": provider,
            "modelID": model,
        }

    raise ValueError(
        "OpenCode model must be configured as 'provider/model' "
        "or with parameters.opencode_model={providerID, modelID}"
    )


def _opencode_model_string(model_config: ModelConfig) -> str:
    model = _opencode_model(model_config)
    return f"{model['providerID']}/{model['modelID']}"


def _skill_invocation_prompt(skill_name: str, prompt: str) -> str:
    return f"/{skill_name}\n\n{prompt}"


def _is_opencode_http_404(exc: RuntimeError) -> bool:
    return "OpenCode HTTP 404 " in str(exc)


def _latest_assistant_message(messages: object) -> object | None:
    if isinstance(messages, Mapping):
        items = messages.get("items")
    else:
        items = messages
    if not isinstance(items, list):
        return None

    for item in items:
        if not isinstance(item, Mapping) or item.get("type") != "assistant":
            continue
        text = _assistant_message_text(item)
        if text.strip():
            return {
                "text": text.strip(),
                "message": item,
            }
    return None


def _assistant_message_text(message: Mapping[str, object]) -> str:
    content = message.get("content")
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if not isinstance(item, Mapping) or item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _extract_response_text(response: object) -> str:
    text = _collect_text(response)
    if text.strip():
        return text.strip()
    return json.dumps(response, ensure_ascii=False, indent=2)


def _collect_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _collect_text(item)))
    if not isinstance(value, Mapping):
        return ""

    for collection_key in ("parts", "content"):
        parts = value.get(collection_key)
        if isinstance(parts, list):
            text = "\n".join(part for item in parts if (part := _collect_text(item)))
            if text:
                return text

    for key in ("text", "content", "output", "message"):
        item = value.get(key)
        if isinstance(item, str):
            return item
    return ""


def _raw_output_path(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".raw.txt")
