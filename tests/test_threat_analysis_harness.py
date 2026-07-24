import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from threat_analysis_harness import (
    ThreatAnalysisLayout,
    ThreatAnalysisPipeline,
    run_threat_analysis,
)
from threat_analysis_harness.errors import OutputSchemaValidationError
from threat_analysis_harness.output_validation import validate_json_schema
from threat_analysis_harness.schemas import (
    ATTACK_TREE_SCHEMA,
    HIGH_RISK_MODULES_SCHEMA,
    VALUE_ASSETS_SCHEMA,
)
from threat_analysis_harness.stages.attack_trees import (
    AttackTreeStage,
    normalize_attack_tree_output,
)
from threat_analysis_harness.stages.base import existing_success_result


ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "tests" / "fixtures" / "inputs" / "input.json"


VALUE_ASSETS = [
    {
        "资产名": "用户个人数据",
        "资产类别": "数据资产",
        "资产描述": "系统处理和保存的用户身份信息。",
        "攻击损失": "数据泄露可能导致隐私和合规风险。",
        "判断为价值资产的原因": "用户资料代码和查询接口体现系统保存并处理用户身份信息。",
    }
]


HIGH_RISK_MODULES = [
    {
        "模块名称": "用户认证模块",
        "代码目录": "src/auth",
        "面临威胁": "身份伪造、认证绕过、凭据泄露",
        "是否涉及设备或系统对外提供管理和控制接口相关的代码": "否",
        "是否涉及对不可信来源数据进行解析或处理的代码": "是",
        "是否涉及安全相关类代码(如，认证、授权、接入控制、加解密、密钥管理、日志审计、软件完整性保护等模块)": "是",
        "是否涉及个人数据或者敏感数据的代码": "是",
        "是否涉及web相关处理": "是",
        "是否外部暴露面": "是",
        "判断为高风险模块的原因": "src/auth 处理外部登录请求、用户凭据和令牌签发，应作为用户认证模块。",
    }
]


def attack_tree_output(
    *,
    asset_name: str = "用户个人数据",
    high_risk_module_name: str = "用户认证模块",
):
    return {
        "attack_trees": [
            {
                "tree_id": "AT-001",
                "value_asset": {
                    "asset_name": asset_name,
                    "asset_category": "数据资产",
                    "asset_description": "系统处理和保存的用户身份信息。",
                    "attack_loss": "数据泄露可能导致隐私和合规风险。",
                },
                "nodes": [
                    {
                        "node_id": "R-001",
                        "node_type": "根节点",
                        "node_name": f"攻击价值资产：{asset_name}",
                        "description": "导致用户个人数据泄露或被越权使用。",
                        "module_name": None,
                        "is_high_risk_module": False,
                        "external_exposure": False,
                        "external_interface_description": None,
                    },
                    {
                        "node_id": "L-001",
                        "node_type": "叶子节点",
                        "node_name": high_risk_module_name,
                        "description": "处理外部登录请求和凭据。",
                        "module_name": high_risk_module_name,
                        "is_high_risk_module": True,
                        "external_exposure": True,
                        "external_interface_description": "登录接口接收外部 HTTP 请求。",
                    },
                    {
                        "node_id": "I-001",
                        "node_type": "内部节点",
                        "node_name": "用户资料处理组件",
                        "description": "读取和整理用户资料供认证流程使用。",
                        "module_name": "用户资料处理组件",
                        "is_high_risk_module": False,
                        "external_exposure": False,
                        "external_interface_description": None,
                    },
                ],
                "edges": [
                    {
                        "edge_id": "E-001",
                        "source_node_id": "L-001",
                        "target_node_id": "I-001",
                        "influence_type": "调用",
                        "description": "认证模块会调用用户资料处理组件读取用户身份资料。",
                    },
                    {
                        "edge_id": "E-002",
                        "source_node_id": "I-001",
                        "target_node_id": "R-001",
                        "influence_type": "直接影响",
                        "description": "用户资料处理组件可直接影响用户个人数据访问。",
                    }
                ],
                "attack_paths": [
                    {
                        "path_id": "AP-001",
                        "path_name": "认证入口影响用户个人数据",
                        "node_ids": ["L-001", "I-001", "R-001"],
                        "edge_ids": ["E-001", "E-002"],
                        "path_description": "用户认证模块 -> 用户资料处理组件 -> 攻击价值资产：用户个人数据",
                        "related_high_risk_modules": [
                            {
                                "module_name": high_risk_module_name,
                                "node_id": "L-001",
                                "external_exposure": True,
                                "path_role": "外部攻击入口",
                                "association_description": "该模块是外部登录入口。",
                            }
                        ],
                        "attack_patterns": [
                            {
                                "pattern_id": "CAPEC-1",
                                "pattern_name": "访问功能未正确地约束访问控制列表",
                                "association_description": "该模式与认证入口和数据访问影响相匹配。",
                            }
                        ],
                    }
                ],
            }
        ],
    }


class ProgressRecorder:
    def __init__(self, stream: io.StringIO):
        self.stream = stream

    def emit(self, message: str) -> None:
        self.stream.write(message + "\n")


def task_result(task, output):
    output_path = Path(task["output_path"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "task_id": task["task_id"],
        "task_type": task["task_type"],
        "status": "succeeded",
        "output_path": task["output_path"],
        "returncode": 0,
        "output": output,
        "metadata": dict(task.get("metadata", {})),
    }


class ThreatAnalysisPipelineTests(unittest.TestCase):
    def test_pipeline_runs_all_stages_with_runtime_contract(self):
        value_asset_categories_seen = []
        high_risk_categories_seen = []

        def submit_tasks(tasks, *, timeout=None):
            self.assertEqual(timeout, 5)
            results = []
            for task in tasks:
                prompt = task["runtime_prompt"]
                self.assertNotIn("skill_path", task)
                self.assertNotIn("不允许输出json文件，直接返回json结果", prompt)
                if task["task_type"] == "value_asset_map":
                    self.assertEqual(task["skill_name"], "value-asset-map")
                    self.assertIs(task["output_schema"], VALUE_ASSETS_SCHEMA)
                    category = task.get("metadata", {}).get("asset_category")
                    value_asset_categories_seen.append(category)
                    self.assertIn("当前只识别", prompt)
                    self.assertIn(f"必须全部为“{category}”", prompt)
                    output = VALUE_ASSETS if category == "数据资产" else []
                    results.append(task_result(task, output))
                    continue
                if task["task_type"] == "high_risk_module_map":
                    self.assertEqual(task["skill_name"], "high-risk-module-map")
                    self.assertIs(task["output_schema"], HIGH_RISK_MODULES_SCHEMA)
                    category = task.get("metadata", {}).get("high_risk_category")
                    high_risk_categories_seen.append(category)
                    self.assertIn("当前只识别命中", prompt)
                    self.assertIn(str(task.get("metadata", {}).get("high_risk_field")), prompt)
                    output = (
                        HIGH_RISK_MODULES
                        if category == "不可信来源数据解析或处理代码"
                        else []
                    )
                    results.append(task_result(task, output))
                    continue
                if task["task_type"] == "high_risk_module_merge":
                    self.assertEqual(task["skill_name"], "high-risk-module-merge")
                    self.assertIs(task["output_schema"], HIGH_RISK_MODULES_SCHEMA)
                    self.assertTrue(task["input_files"])
                    results.append(task_result(task, HIGH_RISK_MODULES))
                    continue
                if task["task_type"] == "attack_tree_by_asset":
                    self.assertEqual(task["skill_name"], "attack-tree-by-asset")
                    self.assertIs(task["output_schema"], ATTACK_TREE_SCHEMA)
                    task_input = next(
                        path for path in task["input_files"] if path.endswith(".input.json")
                    )
                    self.assertIn(task_input, prompt)
                    self.assertIn("high_risk_modules 是全部最终高风险模块列表", prompt)
                    task_input_payload = json.loads(Path(task_input).read_text(encoding="utf-8"))
                    self.assertEqual(task_input_payload["high_risk_modules"], HIGH_RISK_MODULES)
                    results.append(
                        task_result(
                            task,
                            attack_tree_output(
                                asset_name="用户资料数据",
                                high_risk_module_name=" 用户认证模块 ",
                            ),
                        )
                    )
                    continue
                raise AssertionError(task["task_type"])
            return results

        progress_stream = io.StringIO()
        progress = ProgressRecorder(progress_stream)
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = ThreatAnalysisPipeline(
                submit_tasks=submit_tasks,
                layout=ThreatAnalysisLayout.for_run(tmp, "run-001"),
                progress_reporter=progress,
            )
            result = pipeline.run(input_files=[INPUT], timeout=5)

            self.assertCountEqual(
                value_asset_categories_seen,
                ["数据资产", "软件资产", "硬件资产", "服务资产"],
            )
            self.assertCountEqual(
                high_risk_categories_seen,
                [
                    "管理和控制接口相关代码",
                    "不可信来源数据解析或处理代码",
                    "安全相关类代码",
                    "个人数据或者敏感数据代码",
                    "Web 相关处理",
                ],
            )
            self.assertEqual(result.value_assets[0]["资产名"], "用户个人数据")
            self.assertEqual(result.high_risk_modules[0]["模块名称"], "用户认证模块")
            self.assertEqual(len(result.attack_trees["attack_trees"]), 1)
            attack_tree = result.attack_trees["attack_trees"][0]
            self.assertEqual(attack_tree["value_asset"]["asset_name"], "用户个人数据")
            nodes_by_id = {node["node_id"]: node for node in attack_tree["nodes"]}
            self.assertEqual(nodes_by_id["R-001"]["node_name"], "攻击价值资产：用户个人数据")
            self.assertEqual(nodes_by_id["L-001"]["module_name"], "用户认证模块")
            self.assertEqual(nodes_by_id["I-001"]["module_name"], "用户资料处理组件")
            self.assertFalse(nodes_by_id["I-001"]["is_high_risk_module"])
            related_modules = attack_tree["attack_paths"][0]["related_high_risk_modules"]
            self.assertEqual(related_modules[0]["module_name"], "用户认证模块")
            final_value_assets = (
                Path(tmp) / "runs" / "run-001" / "value_assets" / "final" / "value-assets.json"
            )
            self.assertTrue(final_value_assets.exists())
            final_attack_tree = (
                Path(tmp) / "runs" / "run-001" / "attack_trees" / "final" / "attack_trees.json"
            )
            self.assertTrue(final_attack_tree.exists())
            progress_text = progress_stream.getvalue()
            self.assertIn("pipeline started: artifacts=", progress_text)
            self.assertIn("value asset map started: tasks=4", progress_text)
            self.assertIn("high-risk module map started: tasks=5", progress_text)
            self.assertIn("high-risk module merge completed: modules=1", progress_text)
            self.assertIn("attack tree analysis completed: trees=1", progress_text)
            self.assertIn("pipeline completed: duration=", progress_text)

    def test_pipeline_resume_skips_existing_task_outputs(self):
        def submit_tasks(tasks, *, timeout=None):
            results = []
            for task in tasks:
                if task["task_type"] == "value_asset_map":
                    output = (
                        VALUE_ASSETS
                        if task.get("metadata", {}).get("asset_category") == "数据资产"
                        else []
                    )
                    results.append(task_result(task, output))
                    continue
                if task["task_type"] == "high_risk_module_map":
                    output = (
                        HIGH_RISK_MODULES
                        if task.get("metadata", {}).get("high_risk_category")
                        == "不可信来源数据解析或处理代码"
                        else []
                    )
                    results.append(task_result(task, output))
                    continue
                if task["task_type"] == "high_risk_module_merge":
                    results.append(task_result(task, HIGH_RISK_MODULES))
                    continue
                if task["task_type"] == "attack_tree_by_asset":
                    results.append(task_result(task, attack_tree_output()))
                    continue
                raise AssertionError(task["task_type"])
            return results

        unexpected_calls = []

        def fail_if_called(tasks, *, timeout=None):
            unexpected_calls.extend(task["task_id"] for task in tasks)
            raise AssertionError(f"resume should skip tasks: {unexpected_calls}")

        progress_stream = io.StringIO()
        progress = ProgressRecorder(progress_stream)
        with tempfile.TemporaryDirectory() as tmp:
            layout = ThreatAnalysisLayout.for_run(tmp, "run-001")
            pipeline = ThreatAnalysisPipeline(
                submit_tasks=submit_tasks,
                layout=layout,
            )
            pipeline.run(input_files=[INPUT], timeout=5)

            pipeline = ThreatAnalysisPipeline(
                submit_tasks=fail_if_called,
                layout=layout,
                progress_reporter=progress,
            )
            result = pipeline.run(input_files=[INPUT], timeout=5, resume=True)

        self.assertEqual(unexpected_calls, [])
        self.assertEqual(result.value_assets, VALUE_ASSETS)
        self.assertEqual(result.high_risk_modules, HIGH_RISK_MODULES)
        self.assertEqual(len(result.attack_trees["attack_trees"]), 1)
        progress_text = progress_stream.getvalue()
        self.assertIn("task resumed: task_id=value-asset-map-data", progress_text)
        self.assertIn("task resumed: task_id=high-risk-module-merge", progress_text)
        self.assertIn("task resumed: task_id=attack-tree-by-asset-001", progress_text)

    def test_attack_tree_allows_unmatched_internal_node_marked_high_risk(self):
        output = attack_tree_output()
        internal_node = output["attack_trees"][0]["nodes"][2]
        internal_node["is_high_risk_module"] = True

        normalized = normalize_attack_tree_output(
            output,
            value_asset=VALUE_ASSETS[0],
            high_risk_modules=HIGH_RISK_MODULES,
        )

        nodes_by_id = {
            node["node_id"]: node for node in normalized["attack_trees"][0]["nodes"]
        }
        self.assertEqual(nodes_by_id["I-001"]["module_name"], "用户资料处理组件")
        self.assertFalse(nodes_by_id["I-001"]["is_high_risk_module"])
        self.assertFalse(nodes_by_id["I-001"]["external_exposure"])
        related_modules = normalized["attack_trees"][0]["attack_paths"][0][
            "related_high_risk_modules"
        ]
        self.assertEqual(
            [module["module_name"] for module in related_modules],
            ["用户认证模块"],
        )

    def test_attack_tree_schema_rejects_empty_attack_trees(self):
        with self.assertRaisesRegex(
            OutputSchemaValidationError,
            "expected at least 1 items",
        ):
            validate_json_schema({"attack_trees": []}, ATTACK_TREE_SCHEMA)

    def test_attack_tree_resume_retries_existing_empty_output(self):
        submitted_task_ids = []

        def submit_tasks(tasks, *, timeout=None):
            submitted_task_ids.extend(task["task_id"] for task in tasks)
            return [task_result(task, attack_tree_output()) for task in tasks]

        with tempfile.TemporaryDirectory() as tmp:
            layout = ThreatAnalysisLayout.for_run(tmp, "run-001")
            layout.ensure()
            stale_output = layout.attack_trees_raw_dir / "attack-tree-by-asset-001.json"
            stale_output.write_text('{"attack_trees": []}\n', encoding="utf-8")

            stage = AttackTreeStage(submit_tasks=submit_tasks, layout=layout)
            result = stage.run(
                value_assets=VALUE_ASSETS,
                high_risk_modules=HIGH_RISK_MODULES,
                resume=True,
            )

        self.assertEqual(submitted_task_ids, ["attack-tree-by-asset-001"])
        self.assertEqual(len(result["attack_trees"]), 1)

    def test_resume_reads_existing_json_without_schema_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "output.json"
            output_path.write_text("[]\n", encoding="utf-8")
            task = {
                "task_id": "schema-not-checked",
                "task_type": "unit",
                "skill_name": "example-skill",
                "runtime_prompt": "Produce output.",
                "output_path": str(output_path),
                "output_schema": {"type": "object"},
            }

            result = existing_success_result(task)

        self.assertIsNotNone(result)
        self.assertEqual(result["output"], [])

    def test_run_threat_analysis_api_returns_output_paths(self):
        class FakeSubmitter:
            def submit_tasks(self, tasks, *, timeout=None):
                results = []
                for task in tasks:
                    if task["task_type"] == "value_asset_map":
                        output = (
                            VALUE_ASSETS
                            if task.get("metadata", {}).get("asset_category") == "数据资产"
                            else []
                        )
                        results.append(task_result(task, output))
                        continue
                    if task["task_type"] == "high_risk_module_map":
                        output = (
                            HIGH_RISK_MODULES
                            if task.get("metadata", {}).get("high_risk_category")
                            == "不可信来源数据解析或处理代码"
                            else []
                        )
                        results.append(task_result(task, output))
                        continue
                    if task["task_type"] == "high_risk_module_merge":
                        results.append(task_result(task, HIGH_RISK_MODULES))
                        continue
                    if task["task_type"] == "attack_tree_by_asset":
                        results.append(task_result(task, attack_tree_output()))
                        continue
                    raise AssertionError(task["task_type"])
                return results

        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "artifacts"
            with patch(
                "threat_analysis_harness.threat_analysis.TaskAgentSubmitter",
                return_value=FakeSubmitter(),
            ):
                result = run_threat_analysis(
                    code_path=INPUT,
                    output_path=output_root,
                    product_mcp="product-mcp",
                    attack_modes={"mode": ["intro", "skill-name"]},
                )

            self.assertIs(result["result"], True)
            self.assertEqual(
                result["value_asset_path"],
                str(output_root / "value_assets" / "final" / "value-assets.json"),
            )
            self.assertEqual(
                result["attack_tree_path"],
                str(output_root / "attack_trees" / "final" / "attack_trees.json"),
            )
            self.assertEqual(
                result["high_risk_modules_path"],
                str(
                    output_root
                    / "high_risk_modules"
                    / "final"
                    / "high-risk-module-merge.json"
                ),
            )
            self.assertTrue(Path(result["value_asset_path"]).exists())
            self.assertTrue(Path(result["attack_tree_path"]).exists())
            self.assertTrue(Path(result["high_risk_modules_path"]).exists())

    def test_run_threat_analysis_api_resume_reuses_existing_outputs(self):
        class FakeSubmitter:
            def submit_tasks(self, tasks, *, timeout=None):
                results = []
                for task in tasks:
                    if task["task_type"] == "value_asset_map":
                        output = (
                            VALUE_ASSETS
                            if task.get("metadata", {}).get("asset_category") == "数据资产"
                            else []
                        )
                        results.append(task_result(task, output))
                        continue
                    if task["task_type"] == "high_risk_module_map":
                        output = (
                            HIGH_RISK_MODULES
                            if task.get("metadata", {}).get("high_risk_category")
                            == "不可信来源数据解析或处理代码"
                            else []
                        )
                        results.append(task_result(task, output))
                        continue
                    if task["task_type"] == "high_risk_module_merge":
                        results.append(task_result(task, HIGH_RISK_MODULES))
                        continue
                    if task["task_type"] == "attack_tree_by_asset":
                        results.append(task_result(task, attack_tree_output()))
                        continue
                    raise AssertionError(task["task_type"])
                return results

        class FailSubmitter:
            def submit_tasks(self, tasks, *, timeout=None):
                raise AssertionError("resume should skip existing outputs")

        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "artifacts"
            with patch(
                "threat_analysis_harness.threat_analysis.TaskAgentSubmitter",
                side_effect=[FakeSubmitter(), FailSubmitter()],
            ):
                first = run_threat_analysis(
                    code_path=INPUT,
                    output_path=output_root,
                )
                second = run_threat_analysis(
                    code_path=INPUT,
                    output_path=output_root,
                    is_resume=True,
                )

        self.assertIs(first["result"], True)
        self.assertIs(second["result"], True)

    def test_run_threat_analysis_api_returns_reason_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_threat_analysis(
                code_path="",
                output_path=Path(tmp) / "artifacts",
            )

        self.assertIs(result["result"], False)
        self.assertIn("code_path is required", result["reason"])


if __name__ == "__main__":
    unittest.main()
