import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from threat_analysis_harness.task_agent_submitter import TaskAgentSubmitter


class TaskAgentSubmitterTests(unittest.TestCase):
    def test_submitter_maps_harness_task_to_task_agent_and_writes_output(self):
        calls = []

        async def fake_run_opencode_task(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                session_id="session-001",
                status="success",
                text='[{"ok": true}]',
                structured=[{"ok": True}],
                model="provider/model",
            )

        with tempfile.TemporaryDirectory() as tmp:
            task = _task(tmp, required_capability="low")
            submitter = TaskAgentSubmitter(
                config_path=Path(tmp) / "task-agent.yaml",
                invalid_json_retry_count=3,
                run_opencode_task=fake_run_opencode_task,
            )

            result = submitter.submit_tasks([task], timeout=5)[0]

            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["output"], [{"ok": True}])
            self.assertEqual(result["metadata"]["stage"], "unit")
            self.assertEqual(result["metadata"]["task_agent"]["session_id"], "session-001")
            output_path = Path(task["output_path"])
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), [{"ok": True}])
            self.assertEqual(
                output_path.with_suffix(".json.raw.txt").read_text(encoding="utf-8"),
                '[{"ok": true}]',
            )

        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["task_name"], "unit-task")
        self.assertEqual(call["task_type"], "threat_analysis")
        self.assertEqual(call["required_capability"], "low")
        self.assertEqual(call["invalid_json_retry_count"], 3)
        self.assertEqual(call["output_schema"], {"type": "array"})
        self.assertEqual(call["prompt"], "/unit-skill\n\nRuntime prompt")
        self.assertIn("Runtime prompt", call["prompt"])
        self.assertNotIn("Skill body", call["prompt"])
        self.assertNotIn("references/ref.txt", call["prompt"])

    def test_submitter_returns_failed_result_for_task_agent_failure(self):
        async def fake_run_opencode_task(**kwargs):
            return SimpleNamespace(
                session_id="session-002",
                status="timeout",
                text="timed out",
                structured=None,
                model="provider/model",
            )

        with tempfile.TemporaryDirectory() as tmp:
            task = _task(tmp)
            submitter = TaskAgentSubmitter(run_opencode_task=fake_run_opencode_task)

            result = submitter.submit_tasks([task])[0]

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["returncode"], 124)
            self.assertEqual(result["error"], "timed out")
            self.assertIsNone(result["output"])
            self.assertFalse(Path(task["output_path"]).exists())


def _task(tmp: str, **extra):
    root = Path(tmp)
    task = {
        "task_id": "unit-task",
        "task_type": "value_asset_map",
        "skill_name": "unit-skill",
        "runtime_prompt": "Runtime prompt",
        "input_files": [str(root / "input.md")],
        "output_path": str(root / "out.json"),
        "output_schema": {"type": "array"},
        "metadata": {"stage": "unit"},
    }
    task.update(extra)
    return task


if __name__ == "__main__":
    unittest.main()
