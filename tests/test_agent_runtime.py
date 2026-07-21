import io
import json
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock
from pathlib import Path

from agent_runtime import (
    AgentScheduler,
    AgentSubmitter,
    AgentTask,
    CommandAgentRunner,
    FunctionAgentRunner,
    ModelRouter,
    OpenCodeAgentRunner,
    ProgressPrinter,
    RuntimeConfig,
    TaskStatus,
)
from agent_runtime.errors import TaskValidationError
from agent_runtime.config import load_runtime_config
from agent_runtime.output_validation import parse_json_output, parse_json_output_for_schema
from agent_runtime.preflight import validate_task
from agent_runtime.prompt_builder import JSON_RESULT_INSTRUCTION, PromptBuilder
from agent_runtime.queue import TaskQueue
from agent_runtime.runner import JSON_OUTPUT_REPAIR_PROMPT
from agent_runtime.skills import install_opencode_skill


ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "tests" / "fixtures" / "skills" / "example-skill"
INPUT = ROOT / "tests" / "fixtures" / "inputs" / "input.json"
OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["task_id", "model"],
    "properties": {
        "task_id": {"type": "string"},
        "model": {"type": "string"},
    },
    "additionalProperties": False,
}


def router(global_concurrency=2, by_task_type=None, max_retries=None):
    data = {
        "models": {
            "unit": {"model": "test-model"},
            "slow": {"model": "slow-model"},
        },
        "concurrency": {
            "global": global_concurrency,
            "by_task_type": by_task_type or {},
        },
    }
    if max_retries is not None:
        data["retry"] = {"max_retries": max_retries}
    return ModelRouter(
        RuntimeConfig.from_dict(data)
    )


def resource_router():
    return ModelRouter(
        RuntimeConfig.from_dict(
            {
                "models": {
                    "unit": [
                        {"model": "model-a", "resource": "shared-a"},
                        {"model": "model-b", "resource": "shared-b"},
                    ],
                    "slow": {"model": "model-c", "resource": "shared-a"},
                },
                "model_resources": {
                    "shared-a": {"concurrency": 1},
                    "shared-b": {"concurrency": 1},
                },
                "concurrency": {"global": 3},
            }
        )
    )


def shared_resource_router():
    return ModelRouter(
        RuntimeConfig.from_dict(
            {
                "models": {
                    "unit": {"model": "model-a", "resource": "shared"},
                    "slow": {"model": "model-b", "resource": "shared"},
                },
                "model_resources": {
                    "shared": {"concurrency": 1},
                },
                "concurrency": {"global": 2},
            }
        )
    )


def opencode_router():
    return ModelRouter(
        RuntimeConfig.from_dict(
            {
                "models": {
                    "unit": {"model": "test-provider/test-model"},
                }
            }
        )
    )


class AgentRuntimeTests(unittest.TestCase):
    def make_task(self, output_dir, task_id="task-1", task_type="unit", priority=100):
        return AgentTask(
            task_id=task_id,
            task_type=task_type,
            skill_path=str(SKILL),
            runtime_prompt="Produce output.",
            input_files=(str(INPUT),),
            output_path=str(Path(output_dir) / f"{task_id}.json"),
            output_schema=OUTPUT_SCHEMA,
            priority=priority,
        )

    def test_preflight_rejects_missing_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = AgentTask(
                task_id="bad",
                task_type="unit",
                skill_path=str(SKILL),
                runtime_prompt="x",
                input_files=(str(Path(tmp) / "missing.json"),),
                output_path=str(Path(tmp) / "out.json"),
                output_schema=OUTPUT_SCHEMA,
            )
            with self.assertRaises(TaskValidationError):
                validate_task(task, model_router=router())

    def test_submitter_runs_task_and_writes_output(self):
        def run(task, model_config, prompt):
            self.assertEqual(prompt, f"Produce output.\n{JSON_RESULT_INSTRUCTION}")
            self.assertNotIn("调用 skill：`example-skill`", prompt)
            self.assertNotIn("输出 JSON Schema：", prompt)
            self.assertNotIn("输入文件：", prompt)
            self.assertNotIn("Example Skill", prompt)
            self.assertNotIn("# Runtime Prompt", prompt)
            self.assertNotIn("# Model", prompt)
            self.assertEqual(model_config.model, "test-model")
            return json.dumps({"task_id": task.task_id, "model": model_config.model})

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FunctionAgentRunner(run),
                model_router=router(),
            )
            with scheduler:
                submitter = AgentSubmitter(scheduler)
                handle = submitter.submit(self.make_task(tmp))
                result = handle.wait(timeout=5)

            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertEqual(result.output["task_id"], "task-1")
            self.assertEqual(json.loads(Path(result.output_path).read_text())["task_id"], "task-1")

    def test_submitter_extracts_json_and_writes_canonical_output(self):
        def run(task, model_config, prompt):
            return (
                "模型输出如下：\n"
                "```json\n"
                + json.dumps({"task_id": task.task_id, "model": model_config.model})
                + "\n```\n"
                "已完成。"
            )

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FunctionAgentRunner(run),
                model_router=router(),
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(self.make_task(tmp)).wait(timeout=5)

            output_text = Path(result.output_path).read_text(encoding="utf-8")
            raw_text = Path(result.output_path + ".raw.txt").read_text(encoding="utf-8")

        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(json.loads(output_text), {"task_id": "task-1", "model": "test-model"})
        self.assertNotIn("```", output_text)
        self.assertIn("```json", raw_text)
        self.assertIsNone(result.raw_output)

    def test_command_runner_reads_json_output_not_stale_raw_file(self):
        command = [
            sys.executable,
            "-c",
            (
                "import json, pathlib, sys; "
                "pathlib.Path(sys.argv[1]).write_text("
                "json.dumps(dict(task_id='task-1', model=sys.argv[2])), "
                "encoding='utf-8')"
            ),
            "{output_path}",
            "{model}",
        ]

        with tempfile.TemporaryDirectory() as tmp:
            task = self.make_task(tmp)
            Path(task.output_path + ".raw.txt").write_text("not json", encoding="utf-8")
            scheduler = AgentScheduler(
                runner=CommandAgentRunner(command),
                model_router=router(),
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(task).wait(timeout=5)

        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.output, {"task_id": "task-1", "model": "test-model"})

    def test_command_runner_fails_when_json_output_file_is_missing(self):
        command = [
            sys.executable,
            "-c",
            (
                "import json, pathlib, sys; "
                "pathlib.Path(sys.argv[1]).write_text("
                "json.dumps(dict(task_id='task-1', model=sys.argv[2])), "
                "encoding='utf-8')"
            ),
            "{raw_output_path}",
            "{model}",
        ]

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=CommandAgentRunner(command),
                model_router=router(),
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(self.make_task(tmp)).wait(timeout=5)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("Agent output file does not exist", result.error)

    def test_prompt_builder_keeps_runtime_prompt_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt = PromptBuilder().build(self.make_task(tmp), router().route("unit"))

        self.assertEqual(prompt, f"Produce output.\n{JSON_RESULT_INSTRUCTION}")
        self.assertNotIn("调用 skill：`example-skill`", prompt)
        self.assertNotIn("输出 JSON Schema：", prompt)
        self.assertNotIn("输入文件：", prompt)
        self.assertNotIn(str(INPUT), prompt)
        self.assertNotIn(str(Path(tmp) / "task-1.json"), prompt)
        self.assertNotIn("写入输出文件", prompt)
        self.assertNotIn("Example Skill", prompt)
        self.assertNotIn("# Skill", prompt)
        self.assertNotIn("# Runtime Prompt", prompt)
        self.assertNotIn("# Model", prompt)
        self.assertNotIn("resource", prompt)

    def test_prompt_builder_does_not_duplicate_json_result_instruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = replace(
                self.make_task(tmp),
                runtime_prompt=f"Produce output.\n{JSON_RESULT_INSTRUCTION}",
            )
            prompt = PromptBuilder().build(task, router().route("unit"))

        self.assertEqual(prompt, f"Produce output.\n{JSON_RESULT_INSTRUCTION}")

    def test_opencode_skill_install_copies_skill_directory_payloads(self):
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as workspace:
            source = Path(source_tmp) / "threat-skill"
            references = source / "references"
            references.mkdir(parents=True)
            (source / "SKILL.md").write_text("# Threat Skill\n", encoding="utf-8")
            (references / "catalog.json").write_text("[]", encoding="utf-8")

            installed = install_opencode_skill(source, workspace)

            self.assertEqual(
                installed,
                Path(workspace).resolve() / ".opencode" / "skills" / "threat-skill",
            )
            self.assertEqual((installed / "SKILL.md").read_text(encoding="utf-8"), "# Threat Skill\n")
            self.assertEqual((installed / "references" / "catalog.json").read_text(), "[]")
            opencode_config = json.loads((Path(workspace) / "opencode.json").read_text())
            self.assertEqual(
                opencode_config["skills"]["paths"],
                [str((Path(workspace) / ".opencode" / "skills").resolve())],
            )

    def test_opencode_skill_install_preserves_existing_config(self):
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as workspace:
            source = Path(source_tmp) / "threat-skill"
            source.mkdir()
            (source / "SKILL.md").write_text("# Threat Skill\n", encoding="utf-8")
            config_path = Path(workspace) / "opencode.json"
            config_path.write_text(
                json.dumps(
                    {
                        "provider": {"test": {}},
                        "skills": {
                            "paths": ["/existing/skills"],
                            "urls": ["https://example.test/skill.md"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            install_opencode_skill(source, workspace)

            opencode_config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(opencode_config["provider"], {"test": {}})
            self.assertEqual(opencode_config["skills"]["urls"], ["https://example.test/skill.md"])
            self.assertEqual(
                opencode_config["skills"]["paths"],
                [str((Path(workspace) / ".opencode" / "skills").resolve()), "/existing/skills"],
            )

    def test_opencode_runner_start_installs_configured_skills(self):
        class FakeOpenCodeRunner(OpenCodeAgentRunner):
            def _healthcheck(self):
                return True

        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as workspace:
            first = Path(source_tmp) / "first-skill"
            second = Path(source_tmp) / "second-skill"
            first.mkdir()
            second.mkdir()
            (first / "SKILL.md").write_text("# First\n", encoding="utf-8")
            (second / "SKILL.md").write_text("# Second\n", encoding="utf-8")

            runner = FakeOpenCodeRunner(
                start_command=None,
                cwd=workspace,
                skill_paths=(first, second),
            )
            runner.start()

            installed = Path(workspace) / ".opencode" / "skills"
            self.assertEqual((installed / "first-skill" / "SKILL.md").read_text(), "# First\n")
            self.assertEqual((installed / "second-skill" / "SKILL.md").read_text(), "# Second\n")
            opencode_config = json.loads((Path(workspace) / "opencode.json").read_text())
            self.assertEqual(opencode_config["skills"]["paths"], [str(installed.resolve())])

    def test_opencode_runner_starts_on_random_free_port(self):
        class FakeProcess:
            def poll(self):
                return None

        class FakeOpenCodeRunner(OpenCodeAgentRunner):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.started = False
                self.recorded_command = ()
                self.recorded_directory = None

            def _healthcheck(self):
                return self.started

            def _popen(self, command, directory):
                self.started = True
                self.recorded_command = tuple(command)
                self.recorded_directory = directory
                return FakeProcess()

        with tempfile.TemporaryDirectory() as workspace:
            runner = FakeOpenCodeRunner(
                cwd=workspace,
                start_command=("opencode", "serve", "--hostname", "127.0.0.1", "--port", "4096"),
            )
            with mock.patch("agent_runtime.runner._find_free_port", return_value=45678):
                runner.start()

        port = runner.recorded_command[runner.recorded_command.index("--port") + 1]
        self.assertNotEqual(port, "4096")
        self.assertEqual(runner.base_url, f"http://127.0.0.1:{port}")
        self.assertEqual(runner.recorded_directory, Path(workspace).resolve())

    def test_progress_reporter_outputs_key_task_steps(self):
        def run(task, model_config, prompt):
            return json.dumps({"task_id": task.task_id, "model": model_config.model})

        stream = io.StringIO()
        progress = ProgressPrinter(enabled=True, stream=stream)
        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FunctionAgentRunner(run),
                model_router=router(),
                progress_reporter=progress,
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(self.make_task(tmp)).wait(timeout=5)

        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        progress_text = stream.getvalue()
        self.assertIn("runtime started: workers=2", progress_text)
        self.assertIn("task queued: task_id=task-1 task_type=unit", progress_text)
        self.assertIn("task started: task_id=task-1 task_type=unit", progress_text)
        self.assertIn("task completed: task_id=task-1 task_type=unit", progress_text)
        self.assertIn("runtime stopped", progress_text)

    def test_scheduler_uses_progress_switch_from_config(self):
        scheduler = AgentScheduler(
            runner=FunctionAgentRunner(lambda task, model_config, prompt: "[]"),
            model_router=ModelRouter(
                RuntimeConfig.from_dict(
                    {
                        "models": {"unit": "test-model"},
                        "progress": {"enabled": True},
                    }
                )
            ),
        )

        self.assertIsInstance(scheduler.progress_reporter, ProgressPrinter)
        self.assertTrue(scheduler.progress_reporter.enabled)

    def test_scheduler_retries_failed_task_by_default_three_times(self):
        attempts = 0
        stream = io.StringIO()
        progress = ProgressPrinter(enabled=True, stream=stream)

        def run(task, model_config, prompt):
            nonlocal attempts
            attempts += 1
            if attempts <= 3:
                raise RuntimeError(f"transient failure {attempts}")
            return json.dumps({"task_id": task.task_id, "model": model_config.model})

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FunctionAgentRunner(run),
                model_router=router(),
                progress_reporter=progress,
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(self.make_task(tmp)).wait(timeout=5)

        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(attempts, 4)
        self.assertEqual(
            result.metadata["runtime_retry"],
            {"attempt": 4, "max_retries": 3, "max_attempts": 4},
        )
        progress_text = stream.getvalue()
        self.assertIn("task retrying: task_id=task-1 task_type=unit", progress_text)
        self.assertIn("next_attempt=4/4", progress_text)

    def test_scheduler_retries_output_validation_failure(self):
        attempts = 0

        def run(task, model_config, prompt):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return json.dumps({"task_id": task.task_id, "extra": True})
            return json.dumps({"task_id": task.task_id, "model": model_config.model})

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FunctionAgentRunner(run),
                model_router=router(max_retries=1),
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(self.make_task(tmp)).wait(timeout=5)

        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(attempts, 2)
        self.assertEqual(result.metadata["runtime_retry"]["attempt"], 2)
        self.assertEqual(result.metadata["runtime_retry"]["max_retries"], 1)

    def test_scheduler_respects_configured_retry_count(self):
        attempts = 0

        def run(task, model_config, prompt):
            nonlocal attempts
            attempts += 1
            raise RuntimeError(f"still failing {attempts}")

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FunctionAgentRunner(run),
                model_router=router(max_retries=1),
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(self.make_task(tmp)).wait(timeout=5)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(attempts, 2)
        self.assertIn("still failing 2", result.error)
        self.assertEqual(
            result.metadata["runtime_retry"],
            {"attempt": 2, "max_retries": 1, "max_attempts": 2},
        )

    def test_parse_json_output_extracts_embedded_json(self):
        self.assertEqual(
            parse_json_output('说明文字\n{"task_id": "task-1", "model": "test-model"}\n结束'),
            {"task_id": "task-1", "model": "test-model"},
        )

    def test_schema_parser_skips_json_that_does_not_match_schema(self):
        raw = (
            '中间片段：{"task_id": "incomplete"}\n'
            '最终结果：{"task_id": "task-1", "model": "test-model"}'
        )

        self.assertEqual(
            parse_json_output_for_schema(raw, OUTPUT_SCHEMA),
            {"task_id": "task-1", "model": "test-model"},
        )

    def test_schema_parser_wraps_comma_separated_items_for_array_schema(self):
        schema = {"type": "array", "items": OUTPUT_SCHEMA}
        raw = (
            '{"task_id": "task-1", "model": "test-model"},\n'
            '{"task_id": "task-2", "model": "test-model"}'
        )

        self.assertEqual(
            parse_json_output_for_schema(raw, schema),
            [
                {"task_id": "task-1", "model": "test-model"},
                {"task_id": "task-2", "model": "test-model"},
            ],
        )

    def test_schema_parser_wraps_newline_separated_items_for_array_schema(self):
        schema = {"type": "array", "items": OUTPUT_SCHEMA}
        raw = (
            '{"task_id": "task-1", "model": "test-model"}\n'
            '{"task_id": "task-2", "model": "test-model"}'
        )

        self.assertEqual(
            parse_json_output_for_schema(raw, schema),
            [
                {"task_id": "task-1", "model": "test-model"},
                {"task_id": "task-2", "model": "test-model"},
            ],
        )

    def test_schema_parser_wraps_bulleted_items_for_array_schema(self):
        schema = {"type": "array", "items": OUTPUT_SCHEMA}
        raw = (
            '1. {"task_id": "task-1", "model": "test-model"}\n'
            '2. {"task_id": "task-2", "model": "test-model"}'
        )

        self.assertEqual(
            parse_json_output_for_schema(raw, schema),
            [
                {"task_id": "task-1", "model": "test-model"},
                {"task_id": "task-2", "model": "test-model"},
            ],
        )

    def test_opencode_runner_installs_skill_and_invokes_it_in_message_prompt(self):
        requests = []

        class FakeOpenCodeRunner(OpenCodeAgentRunner):
            def start(self):
                return None

            def _request_json(self, method, path, payload=None, *, query=None):
                requests.append((method, path, payload, query))
                if method == "GET" and path == "/skill":
                    return [{"name": "example-skill", "location": "test", "content": ""}]
                if method == "POST" and path == "/session":
                    return {"id": "session-001", "title": payload.get("title")}
                if method == "POST" and path == "/session/session-001/message":
                    return {
                        "info": {
                            "id": "assistant-1",
                            "role": "assistant",
                            "sessionID": "session-001",
                        },
                        "parts": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "task_id": "task-1",
                                        "model": "test-provider/test-model",
                                    }
                                ),
                            },
                        ],
                    }
                if method == "GET" and path == "/session/session-001/message":
                    return [
                        {
                            "info": {
                                "id": "assistant-1",
                                "role": "assistant",
                                "sessionID": "session-001",
                            },
                            "parts": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "task_id": "task-1",
                                            "model": "test-provider/test-model",
                                        }
                                    ),
                                },
                            ],
                        }
                    ]
                raise AssertionError((method, path, payload, query))

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FakeOpenCodeRunner(start_command=None, cwd=tmp),
                model_router=opencode_router(),
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(self.make_task(tmp)).wait(timeout=5)
            installed_skill = Path(tmp) / ".opencode" / "skills" / "example-skill" / "SKILL.md"
            installed_skill_text = installed_skill.read_text(encoding="utf-8")
            raw_text = Path(result.output_path + ".raw.txt").read_text(encoding="utf-8")

        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.output["model"], "test-provider/test-model")
        self.assertEqual(requests[0][1], "/skill")
        self.assertIsNone(requests[0][3])
        self.assertEqual(requests[1][1], "/session")
        self.assertEqual(requests[2][1], "/session/session-001/message")
        self.assertEqual(
            requests[2][2]["model"],
            {"providerID": "test-provider", "modelID": "test-model"},
        )
        self.assertEqual(requests[2][2]["parts"][0]["type"], "text")
        self.assertTrue(requests[2][2]["parts"][0]["text"].startswith("/example-skill\n\n"))
        self.assertIn("Produce output.", requests[2][2]["parts"][0]["text"])
        self.assertIn(JSON_RESULT_INSTRUCTION, requests[2][2]["parts"][0]["text"])
        self.assertNotIn("Example Skill", requests[2][2]["parts"][0]["text"])
        self.assertIn('"task_id": "task-1"', raw_text)
        self.assertEqual(requests[1][3]["directory"], str(Path(tmp).resolve()))
        self.assertIn("Example Skill", installed_skill_text)

    def test_opencode_runner_repairs_each_output_validation_failure_attempt(self):
        requests = []
        sessions = []
        messages = []

        class FakeOpenCodeRunner(OpenCodeAgentRunner):
            def start(self):
                return None

            def _request_json(self, method, path, payload=None, *, query=None):
                requests.append((method, path, payload, query))
                if method == "GET" and path == "/skill":
                    return [{"name": "example-skill", "location": "test", "content": ""}]
                if method == "POST" and path == "/session":
                    session_id = f"session-{len(sessions) + 1:03d}"
                    sessions.append(session_id)
                    return {"id": session_id, "title": payload.get("title")}
                if method == "POST" and path.endswith("/message"):
                    session_id = path.split("/")[2]
                    text = payload["parts"][0]["text"]
                    messages.append((session_id, text))
                    if text == JSON_OUTPUT_REPAIR_PROMPT:
                        repair_text = (
                            json.dumps(
                                {
                                    "task_id": "task-1",
                                    "model": "test-provider/test-model",
                                }
                            )
                            if session_id == "session-002"
                            else "still not json"
                        )
                        return {
                            "info": {
                                "id": "assistant-repair",
                                "role": "assistant",
                                "sessionID": session_id,
                            },
                            "parts": [{"type": "text", "text": repair_text}],
                        }
                    return {
                        "info": {
                            "id": "assistant-invalid",
                            "role": "assistant",
                            "sessionID": session_id,
                        },
                        "parts": [{"type": "text", "text": "not json"}],
                    }
                if method == "GET" and path.endswith("/message"):
                    session_id = path.split("/")[2]
                    return [
                        {
                            "info": {
                                "id": "assistant-invalid",
                                "role": "assistant",
                                "sessionID": session_id,
                            },
                            "parts": [{"type": "text", "text": "not json"}],
                        }
                    ]
                raise AssertionError((method, path, payload, query))

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FakeOpenCodeRunner(start_command=None, cwd=tmp),
                model_router=opencode_router(),
                max_retries=1,
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(self.make_task(tmp)).wait(timeout=5)
            raw_text = Path(result.output_path + ".raw.txt").read_text(encoding="utf-8")

        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.output["model"], "test-provider/test-model")
        self.assertEqual(sessions, ["session-001", "session-002"])
        self.assertEqual(messages[1], ("session-001", JSON_OUTPUT_REPAIR_PROMPT))
        self.assertEqual(messages[-1], ("session-002", JSON_OUTPUT_REPAIR_PROMPT))
        self.assertEqual(result.metadata["runtime_retry"]["attempt"], 2)
        self.assertTrue(result.metadata["opencode"]["repair_attempted"])
        self.assertIn('"task_id": "task-1"', raw_text)
        self.assertNotIn("not json", raw_text)

    def test_opencode_runner_repair_output_uses_messages_after_repair_prompt(self):
        repair_sent = False
        repair_message_gets = 0

        def user_message(message_id, text):
            return {
                "info": {"id": message_id, "role": "user", "sessionID": "session-001"},
                "parts": [{"type": "text", "text": text}],
            }

        def assistant_message(message_id, text):
            return {
                "info": {"id": message_id, "role": "assistant", "sessionID": "session-001"},
                "parts": [{"type": "text", "text": text}],
            }

        invalid_json = json.dumps({"task_id": "wrong"})
        final_json = json.dumps(
            {
                "task_id": "task-1",
                "model": "test-provider/test-model",
            }
        )

        class FakeOpenCodeRunner(OpenCodeAgentRunner):
            def start(self):
                return None

            def _request_json(self, method, path, payload=None, *, query=None):
                nonlocal repair_sent, repair_message_gets
                if method == "GET" and path == "/skill":
                    return [{"name": "example-skill", "location": "test", "content": ""}]
                if method == "POST" and path == "/session":
                    return {"id": "session-001", "title": payload.get("title")}
                if method == "POST" and path == "/session/session-001/message":
                    text = payload["parts"][0]["text"]
                    if text == JSON_OUTPUT_REPAIR_PROMPT:
                        repair_sent = True
                        return user_message("user-repair", text)
                    return assistant_message("assistant-invalid", invalid_json)
                if method == "GET" and path == "/session/session-001/message":
                    messages = [
                        user_message("user-initial", "Produce output."),
                        assistant_message("assistant-invalid", invalid_json),
                    ]
                    if repair_sent:
                        repair_message_gets += 1
                        messages.append(user_message("user-repair", JSON_OUTPUT_REPAIR_PROMPT))
                        messages.extend(
                            [
                                assistant_message("assistant-repair-analysis", "先修复输出。"),
                                assistant_message("assistant-repair-final", final_json),
                            ]
                        )
                    return messages
                raise AssertionError((method, path, payload, query))

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FakeOpenCodeRunner(start_command=None, cwd=tmp),
                model_router=opencode_router(),
                max_retries=0,
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(self.make_task(tmp)).wait(timeout=5)
            raw_text = Path(result.output_path + ".raw.txt").read_text(encoding="utf-8")

        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.output["model"], "test-provider/test-model")
        self.assertEqual(repair_message_gets, 1)
        self.assertIn("先修复输出。", raw_text)
        self.assertIn('"task_id": "task-1"', raw_text)
        self.assertNotIn('"task_id": "wrong"', raw_text)

    def test_opencode_runner_fetches_public_messages_when_prompt_response_is_not_assistant(self):
        requests = []

        class FakeOpenCodeRunner(OpenCodeAgentRunner):
            def start(self):
                return None

            def _request_json(self, method, path, payload=None, *, query=None):
                requests.append((method, path, payload, query))
                if method == "GET" and path == "/skill":
                    return [{"name": "example-skill", "location": "test", "content": ""}]
                if method == "POST" and path == "/session":
                    return {"id": "session-001", "title": payload.get("title")}
                if method == "POST" and path == "/session/session-001/message":
                    return {
                        "info": {"id": "user-1", "role": "user", "sessionID": "session-001"},
                        "parts": [{"type": "text", "text": "Produce output."}],
                    }
                if method == "GET" and path == "/session/session-001/message":
                    return [
                        {
                            "info": {"id": "user-1", "role": "user", "sessionID": "session-001"},
                            "parts": [{"type": "text", "text": "Produce output."}],
                        },
                        {
                            "info": {
                                "id": "assistant-1",
                                "role": "assistant",
                                "sessionID": "session-001",
                            },
                            "parts": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "task_id": "task-1",
                                            "model": "test-provider/test-model",
                                        }
                                    ),
                                }
                            ],
                        },
                    ]
                raise AssertionError((method, path, payload, query))

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FakeOpenCodeRunner(start_command=None, cwd=tmp),
                model_router=opencode_router(),
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(self.make_task(tmp)).wait(timeout=5)

        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.output["model"], "test-provider/test-model")
        self.assertEqual(requests[3][1], "/session/session-001/message")
        self.assertEqual(requests[3][3]["limit"], "100")

    def test_opencode_runner_raw_output_collects_all_assistant_messages_for_schema_parser(self):
        class FakeOpenCodeRunner(OpenCodeAgentRunner):
            def start(self):
                return None

            def _request_json(self, method, path, payload=None, *, query=None):
                if method == "GET" and path == "/skill":
                    return [{"name": "example-skill", "location": "test", "content": ""}]
                if method == "POST" and path == "/session":
                    return {"id": "session-001", "title": payload.get("title")}
                if method == "POST" and path == "/session/session-001/message":
                    return {
                        "info": {"id": "user-1", "role": "user", "sessionID": "session-001"},
                        "parts": [{"type": "text", "text": "Produce output."}],
                    }
                if method == "GET" and path == "/session/session-001/message":
                    return [
                        {
                            "info": {
                                "id": "assistant-1",
                                "role": "assistant",
                                "sessionID": "session-001",
                            },
                            "parts": [{"type": "text", "text": "先完成分析。"}],
                        },
                        {
                            "info": {
                                "id": "assistant-2",
                                "role": "assistant",
                                "sessionID": "session-001",
                            },
                            "parts": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "task_id": "task-1",
                                            "model": "test-provider/test-model",
                                        }
                                    ),
                                }
                            ],
                        },
                        {
                            "info": {
                                "id": "assistant-3",
                                "role": "assistant",
                                "sessionID": "session-001",
                            },
                            "parts": [{"type": "text", "text": "总结：任务完成。"}],
                        },
                    ]
                raise AssertionError((method, path, payload, query))

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FakeOpenCodeRunner(start_command=None, cwd=tmp),
                model_router=opencode_router(),
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(self.make_task(tmp)).wait(timeout=5)
            raw_text = Path(result.output_path + ".raw.txt").read_text(encoding="utf-8")

        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.output["task_id"], "task-1")
        self.assertIn("先完成分析。", raw_text)
        self.assertIn('"task_id": "task-1"', raw_text)
        self.assertIn("总结：任务完成。", raw_text)

    def test_opencode_runner_raw_output_uses_only_assistant_text_parts(self):
        class FakeOpenCodeRunner(OpenCodeAgentRunner):
            def start(self):
                return None

            def _request_json(self, method, path, payload=None, *, query=None):
                if method == "GET" and path == "/skill":
                    return [{"name": "example-skill", "location": "test", "content": ""}]
                if method == "POST" and path == "/session":
                    return {"id": "session-001", "title": payload.get("title")}
                if method == "POST" and path == "/session/session-001/message":
                    return {
                        "info": {
                            "id": "assistant-1",
                            "role": "assistant",
                            "sessionID": "session-001",
                        },
                        "parts": [
                            {
                                "type": "tool",
                                "tool": "read",
                                "content": [
                                    {"type": "text", "text": '{"task_id": "wrong"}'},
                                ],
                                "state": {"output": "hidden tool output"},
                            },
                            {"type": "reasoning", "text": "hidden reasoning"},
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "task_id": "task-1",
                                        "model": "test-provider/test-model",
                                    }
                                ),
                            },
                        ],
                    }
                if method == "GET" and path == "/session/session-001/message":
                    return [
                        {
                            "info": {
                                "id": "assistant-1",
                                "role": "assistant",
                                "sessionID": "session-001",
                            },
                            "parts": [
                                {
                                    "type": "tool",
                                    "tool": "read",
                                    "content": [
                                        {"type": "text", "text": '{"task_id": "wrong"}'},
                                    ],
                                    "state": {"output": "hidden tool output"},
                                },
                                {"type": "reasoning", "text": "hidden reasoning"},
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "task_id": "task-1",
                                            "model": "test-provider/test-model",
                                        }
                                    ),
                                },
                            ],
                        }
                    ]
                raise AssertionError((method, path, payload, query))

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FakeOpenCodeRunner(start_command=None, cwd=tmp),
                model_router=opencode_router(),
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(self.make_task(tmp)).wait(timeout=5)
            raw_text = Path(result.output_path + ".raw.txt").read_text(encoding="utf-8")

        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.output["task_id"], "task-1")
        self.assertEqual(
            raw_text,
            json.dumps(
                {
                    "task_id": "task-1",
                    "model": "test-provider/test-model",
                }
            ),
        )
        self.assertNotIn("wrong", raw_text)
        self.assertNotIn("hidden", raw_text)

    def test_opencode_runner_fails_without_assistant_text_response(self):
        class FakeOpenCodeRunner(OpenCodeAgentRunner):
            def start(self):
                return None

            def _request_json(self, method, path, payload=None, *, query=None):
                if method == "GET" and path == "/skill":
                    return [{"name": "example-skill", "location": "test", "content": ""}]
                if method == "POST" and path == "/session":
                    return {"id": "session-001", "title": payload.get("title")}
                if method == "POST" and path == "/session/session-001/message":
                    return {
                        "info": {"id": "user-1", "role": "user", "sessionID": "session-001"},
                        "parts": [{"type": "text", "text": "Produce output."}],
                    }
                if method == "GET" and path == "/session/session-001/message":
                    return [
                        {
                            "info": {"id": "user-1", "role": "user", "sessionID": "session-001"},
                            "parts": [{"type": "text", "text": "Produce output."}],
                        }
                    ]
                raise AssertionError((method, path, payload, query))

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FakeOpenCodeRunner(start_command=None, cwd=tmp),
                model_router=opencode_router(),
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(self.make_task(tmp)).wait(timeout=5)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("OpenCode session completed without assistant text response", result.error)

    def test_opencode_runner_fails_when_installed_skill_is_not_visible(self):
        class FakeOpenCodeRunner(OpenCodeAgentRunner):
            def start(self):
                return None

            def _request_json(self, method, path, payload=None, *, query=None):
                if method == "GET" and path == "/skill":
                    return [{"name": "other-skill", "location": "test", "content": ""}]
                raise AssertionError((method, path, payload, query))

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FakeOpenCodeRunner(start_command=None, cwd=tmp),
                model_router=opencode_router(),
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(self.make_task(tmp)).wait(timeout=5)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("OpenCode skill is not visible after install", result.error)
        self.assertIn("example-skill", result.error)
        self.assertIn("other-skill", result.error)

    def test_opencode_skill_verification_uses_plain_skill_endpoint(self):
        requests = []

        class FakeOpenCodeRunner(OpenCodeAgentRunner):
            def _request_json(self, method, path, payload=None, *, query=None):
                requests.append((method, path, payload, query))
                return [{"name": "example-skill", "location": "test", "content": ""}]

        runner = FakeOpenCodeRunner(start_command=None)
        runner._verify_skill_available("example-skill", Path("/tmp/runtime"))

        self.assertEqual(requests, [("GET", "/skill", None, None)])

    def test_opencode_request_with_directory_includes_context_header(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            return FakeResponse()

        runner = OpenCodeAgentRunner(start_command=None)
        runner._urlopen = fake_urlopen
        response = runner._request_json(
            "POST",
            "/session",
            {"title": "test"},
            query={"directory": "/tmp/runtime workspace"},
        )

        self.assertEqual(response, {"ok": True})
        self.assertIn("directory=%2Ftmp%2Fruntime+workspace", captured["url"])
        self.assertEqual(
            captured["headers"]["X-opencode-directory"],
            "/tmp/runtime%20workspace",
        )

    def test_opencode_runner_supports_explicit_model_parameters_for_skill_command(self):
        runner = OpenCodeAgentRunner(start_command=None)
        payload = runner._command_payload(
            "hello",
            "example-skill",
            RuntimeConfig.from_dict(
                {
                    "models": {
                        "unit": {
                            "model": "alias",
                            "parameters": {
                                "opencode_model": {
                                    "providerID": "test-provider",
                                    "modelID": "test-model",
                                }
                            },
                        }
                    }
                }
            ).models["unit"],
        )

        self.assertEqual(payload["model"], "test-provider/test-model")

    def test_schema_validation_failure_returns_failed_result(self):
        def run(task, model_config, prompt):
            return json.dumps({"task_id": task.task_id, "extra": True})

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FunctionAgentRunner(run),
                model_router=router(),
            )
            with scheduler:
                submitter = AgentSubmitter(scheduler)
                result = submitter.submit(self.make_task(tmp)).wait(timeout=5)

            self.assertEqual(result.status, TaskStatus.FAILED)
            self.assertIn("missing required property", result.error)

    def test_scheduler_respects_task_type_concurrency(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def run(task, model_config, prompt):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return json.dumps({"task_id": task.task_id, "model": model_config.model})

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FunctionAgentRunner(run),
                model_router=router(global_concurrency=4, by_task_type={"slow": 1}),
            )
            with scheduler:
                submitter = AgentSubmitter(scheduler)
                handles = [
                    submitter.submit(self.make_task(tmp, task_id=f"slow-{i}", task_type="slow"))
                    for i in range(4)
                ]
                results = submitter.wait_all(handles, timeout=10)

            self.assertTrue(all(result.status == TaskStatus.SUCCEEDED for result in results))
            self.assertEqual(max_active, 1)

    def test_scheduler_uses_multiple_models_for_same_task_type(self):
        started = threading.Barrier(2)
        models = []
        lock = threading.Lock()

        def run(task, model_config, prompt):
            with lock:
                models.append(model_config.model)
            started.wait(timeout=5)
            time.sleep(0.02)
            return json.dumps({"task_id": task.task_id, "model": model_config.model})

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FunctionAgentRunner(run),
                model_router=resource_router(),
            )
            with scheduler:
                submitter = AgentSubmitter(scheduler)
                handles = [
                    submitter.submit(self.make_task(tmp, task_id=f"unit-{index}"))
                    for index in range(2)
                ]
                results = submitter.wait_all(handles, timeout=10)

        self.assertTrue(all(result.status == TaskStatus.SUCCEEDED for result in results))
        self.assertCountEqual(models, ["model-a", "model-b"])

    def test_scheduler_limits_shared_model_resource_across_task_types(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def run(task, model_config, prompt):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return json.dumps({"task_id": task.task_id, "model": model_config.model})

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FunctionAgentRunner(run),
                model_router=shared_resource_router(),
            )
            with scheduler:
                submitter = AgentSubmitter(scheduler)
                handles = [
                    submitter.submit(self.make_task(tmp, task_id="unit-1", task_type="unit")),
                    submitter.submit(self.make_task(tmp, task_id="slow-1", task_type="slow")),
                ]
                results = submitter.wait_all(handles, timeout=10)

        self.assertTrue(all(result.status == TaskStatus.SUCCEEDED for result in results))
        self.assertEqual(max_active, 1)

    def test_queue_returns_high_priority_first_when_both_are_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = TaskQueue()
            high = self.make_task(tmp, task_id="high", priority=1)
            low = self.make_task(tmp, task_id="low", priority=100)
            queue.put(low)
            queue.put(high)

            self.assertEqual(queue.get_available(lambda task: True).task_id, "high")
            self.assertEqual(queue.get_available(lambda task: True).task_id, "low")

    def test_load_runtime_config_from_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent-runtime.json"
            path.write_text(
                json.dumps(
                    {
                        "models": {
                            "unit": [
                                "test-model",
                                {"model": "backup-model", "resource": "backup"},
                            ]
                        },
                        "model_resources": {
                            "test-model": {"concurrency": 2},
                            "backup": 1,
                        },
                        "concurrency": {"by_task_type": {"unit": 2}},
                        "progress": {"enabled": True},
                        "retry": {"max_retries": 2},
                    }
                ),
                encoding="utf-8",
            )
            config = load_runtime_config(path)

        self.assertEqual(config.global_concurrency, 3)
        self.assertEqual(config.models["unit"].model, "test-model")
        self.assertEqual(config.model_routes["unit"][1].model, "backup-model")
        self.assertEqual(config.model_routes["unit"][1].resource_name, "backup")
        self.assertEqual(config.model_resource_limits["test-model"], 2)
        self.assertEqual(config.model_resource_limits["backup"], 1)
        self.assertEqual(config.task_type_concurrency["unit"], 2)
        self.assertTrue(config.progress_enabled)
        self.assertEqual(config.retry_max_retries, 2)

    def test_runtime_config_defaults_to_three_retries(self):
        config = RuntimeConfig.from_dict({"models": {"unit": "test-model"}})

        self.assertEqual(config.retry_max_retries, 3)

    def test_task_can_load_schema_from_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.json"
            schema_path.write_text(json.dumps(OUTPUT_SCHEMA), encoding="utf-8")
            task = AgentTask(
                task_id="schema-path",
                task_type="unit",
                skill_path=str(SKILL),
                runtime_prompt="Produce output.",
                input_files=(str(INPUT),),
                output_path=str(Path(tmp) / "schema-path.json"),
                output_schema_path=str(schema_path),
            )
            report = validate_task(task, model_router=router())

        self.assertTrue(report.ok)


if __name__ == "__main__":
    unittest.main()
