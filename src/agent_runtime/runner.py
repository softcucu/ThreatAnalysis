"""Agent runner implementations."""

from __future__ import annotations

import base64
import json
import socket
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
        base_url: str | None = None,
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
        self.start_command = None if start_command is None else tuple(start_command)
        self.base_url = (base_url or "http://127.0.0.1:4096").rstrip("/")
        self._auto_port = base_url is None and self.start_command is not None
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
            if self._process is not None and self._process.poll() is None and self._healthcheck():
                return
            if self.start_command is None:
                if self._healthcheck():
                    return
                raise RuntimeError(f"OpenCode server is not reachable: {self.base_url}")

            directory = self._directory()
            start_command = self._prepare_start_command()
            self._process = self._popen(start_command, directory)

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

    def _popen(self, command: Sequence[str], directory: Path) -> subprocess.Popen[str]:
        return subprocess.Popen(
            tuple(command),
            cwd=str(directory),
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

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
            if self.install_skills:
                self._verify_skill_available(skill_name, directory)
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
            response = self._assistant_response_or_latest(response, session_id, directory)
            if response is None:
                raise RuntimeError(
                    f"OpenCode session completed without assistant text response: {session_id}"
                )
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

    def _prepare_start_command(self) -> tuple[str, ...]:
        if self.start_command is None:
            raise RuntimeError("OpenCode start command is not configured")
        command = tuple(self.start_command)
        if not self._auto_port:
            return command

        hostname = _command_option(command, "--hostname") or "127.0.0.1"
        port = _find_free_port(hostname)
        command = _replace_or_append_option(command, "--port", str(port))
        self.base_url = f"http://{_client_host(hostname)}:{port}"
        return command

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

    def _assistant_response_or_latest(
        self,
        response: object,
        session_id: str,
        directory: Path,
    ) -> object | None:
        if _assistant_response_text(response).strip():
            return response

        messages = self._request_json(
            "GET",
            f"/session/{session_id}/message",
            query={**self._opencode_query(directory), "limit": "20"},
        )
        message = _latest_assistant_message(messages)
        if message is None:
            return None
        return message

    def _verify_skill_available(self, skill_name: str, directory: Path) -> None:
        try:
            skills = self._request_json("GET", "/skill")
        except RuntimeError as exc:
            if _is_opencode_http_404(exc):
                return
            raise

        names = _skill_names(skills)
        if names is None or skill_name in names:
            return

        visible = ", ".join(sorted(names)) or "(none)"
        raise RuntimeError(
            "OpenCode skill is not visible after install: "
            f"skill={skill_name!r}, directory={str(directory)!r}, "
            f"skills_dir={str((directory / '.opencode' / 'skills').resolve())!r}, "
            f"config={str((directory / 'opencode.json').resolve())!r}, "
            f"visible_skills=[{visible}]. "
            "OpenCode /skill is checked without directory query/header; make sure "
            "opencode serve is running in the same directory where these skills are installed."
        )

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
        if query and query.get("directory"):
            req.add_header(
                "x-opencode-directory",
                parse.quote(str(query["directory"]), safe="/:\\"),
            )
        try:
            with self._urlopen(req) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenCode HTTP {exc.code} for {method} {path}: {detail}") from exc
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            snippet = raw[:300].replace("\n", "\\n")
            raise RuntimeError(
                f"OpenCode returned non-JSON response for {method} {path}: {exc}. "
                f"Response starts with: {snippet!r}"
            ) from exc

    def _urlopen(self, req: request.Request) -> object:
        opener = request.build_opener(request.ProxyHandler({}))
        return opener.open(req, timeout=self.timeout)


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


def _command_option(command: Sequence[str], option: str) -> str | None:
    for index, part in enumerate(command):
        if part == option and index + 1 < len(command):
            return command[index + 1]
        prefix = option + "="
        if part.startswith(prefix):
            return part[len(prefix):]
    return None


def _replace_or_append_option(command: Sequence[str], option: str, value: str) -> tuple[str, ...]:
    parts = list(command)
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == option:
            if index + 1 < len(parts):
                parts[index + 1] = value
            else:
                parts.append(value)
            return tuple(parts)
        prefix = option + "="
        if part.startswith(prefix):
            parts[index] = option + "=" + value
            return tuple(parts)
        index += 1
    parts.extend((option, value))
    return tuple(parts)


def _find_free_port(hostname: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_bind_host(hostname), 0))
        return int(sock.getsockname()[1])


def _bind_host(hostname: str) -> str:
    if hostname in {"0.0.0.0", "::", ""}:
        return "127.0.0.1"
    return hostname


def _client_host(hostname: str) -> str:
    if hostname in {"0.0.0.0", "::", ""}:
        return "127.0.0.1"
    return hostname


def _skill_invocation_prompt(skill_name: str, prompt: str) -> str:
    return f"/{skill_name}\n\n{prompt}"


def _is_opencode_http_404(exc: RuntimeError) -> bool:
    return "OpenCode HTTP 404 " in str(exc)


def _skill_names(skills: object) -> set[str] | None:
    if isinstance(skills, Mapping):
        items = skills.get("items")
    else:
        items = skills
    if not isinstance(items, list):
        return None

    names: set[str] = set()
    for item in items:
        if isinstance(item, Mapping) and item.get("name"):
            names.add(str(item["name"]))
    return names


def _latest_assistant_message(messages: object) -> object | None:
    if isinstance(messages, Mapping):
        items = messages.get("items")
    else:
        items = messages
    if not isinstance(items, list):
        return None

    for item in reversed(items):
        if not isinstance(item, Mapping) or _message_role(item) != "assistant":
            continue
        text = _assistant_response_text(item)
        if text.strip():
            return item
    return None


def _assistant_response_text(response: object) -> str:
    if not isinstance(response, Mapping):
        return ""

    if _message_role(response) != "assistant":
        return ""
    return _extract_response_text(response)


def _message_role(message: Mapping[str, object]) -> str | None:
    role = message.get("role") or message.get("type")
    if isinstance(role, str):
        return role

    info = message.get("info")
    if isinstance(info, Mapping):
        role = info.get("role") or info.get("type")
        if isinstance(role, str):
            return role
    return None


def _extract_response_text(response: object) -> str:
    return "\n".join(_extract_text_parts(response)).strip()


def _extract_text_parts(value: object) -> list[str]:
    lines: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if text:
            lines.append(text)
        return lines
    if isinstance(value, list):
        for item in value:
            lines.extend(_extract_text_parts(item))
        return lines
    if not isinstance(value, Mapping):
        return lines

    part_type = value.get("type")
    if part_type == "text" and isinstance(value.get("text"), str):
        text = value["text"].strip()
        if text:
            lines.append(text)
        return lines
    if part_type:
        return lines

    for key in ("parts", "content"):
        item = value.get(key)
        if isinstance(item, list):
            lines.extend(_extract_text_parts(item))
    return lines


def _raw_output_path(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".raw.txt")
