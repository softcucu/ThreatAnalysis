import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_runtime import (
    AgentScheduler,
    AgentSubmitter,
    AgentTask,
    FunctionAgentRunner,
    ModelRouter,
    OpenCodeAgentRunner,
    RuntimeConfig,
    TaskStatus,
)
from agent_runtime.errors import TaskValidationError
from agent_runtime.config import load_runtime_config
from agent_runtime.preflight import validate_task
from agent_runtime.queue import TaskQueue


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


def router(global_concurrency=2, by_task_type=None):
    return ModelRouter(
        RuntimeConfig.from_dict(
            {
                "models": {
                    "unit": {"model": "test-model"},
                    "slow": {"model": "slow-model"},
                },
                "concurrency": {
                    "global": global_concurrency,
                    "by_task_type": by_task_type or {},
                },
            }
        )
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
            self.assertIn("Example Skill", prompt)
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

    def test_opencode_runner_creates_session_and_sends_model(self):
        requests = []

        class FakeOpenCodeRunner(OpenCodeAgentRunner):
            def start(self):
                return None

            def _request_json(self, method, path, payload=None):
                requests.append((method, path, payload))
                if method == "POST" and path == "/session":
                    return {"id": "session-001", "title": payload.get("title")}
                if method == "POST" and path == "/session/session-001/message":
                    return {
                        "parts": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "task_id": "task-1",
                                        "model": payload["model"],
                                    }
                                ),
                            }
                        ]
                    }
                raise AssertionError((method, path, payload))

        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FakeOpenCodeRunner(start_command=None),
                model_router=router(),
            )
            with scheduler:
                result = AgentSubmitter(scheduler).submit(self.make_task(tmp)).wait(timeout=5)

        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.output["model"], "test-model")
        self.assertEqual(requests[0][1], "/session")
        self.assertEqual(requests[1][1], "/session/session-001/message")
        self.assertEqual(requests[1][2]["model"], "test-model")
        self.assertEqual(requests[1][2]["parts"][0]["type"], "text")

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
