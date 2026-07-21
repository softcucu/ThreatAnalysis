import io
import json
import tempfile
import unittest
from pathlib import Path

from agent_runtime import (
    AgentScheduler,
    AgentSubmitter,
    FunctionAgentRunner,
    ModelRouter,
    ProgressPrinter,
    RuntimeConfig,
)
from agent_runtime.prompt_builder import JSON_RESULT_INSTRUCTION
from threat_analysis_harness import ThreatAnalysisLayout, ThreatAnalysisPipeline
from threat_analysis_harness.skills import default_skill_paths


ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "tests" / "fixtures" / "inputs" / "input.json"


def runtime_router():
    return ModelRouter(
        RuntimeConfig.from_dict(
            {
                "models": {
                    "value_asset_map": "test-value-model",
                    "high_risk_module_map": "test-high-risk-model",
                    "high_risk_module_merge": "test-merge-model",
                    "attack_tree_by_asset": "test-attack-tree-model",
                },
                "concurrency": {
                    "global": 3,
                    "by_task_type": {
                        "high_risk_module_merge": 1,
                    },
                },
            }
        )
    )


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
        "analysis_gaps": [],
    }


class ThreatAnalysisPipelineTests(unittest.TestCase):
    def test_pipeline_runs_all_stages_and_returns_validated_outputs(self):
        value_asset_categories_seen = []
        high_risk_categories_seen = []

        def run(task, model_config, prompt):
            self.assertIn(JSON_RESULT_INSTRUCTION, prompt)
            if task.task_type == "value_asset_map":
                category = task.metadata.get("asset_category")
                value_asset_categories_seen.append(category)
                self.assertIn("当前只识别", prompt)
                self.assertIn(f"必须全部为“{category}”", prompt)
                if category == "数据资产":
                    return json.dumps(VALUE_ASSETS, ensure_ascii=False)
                return "[]"
            if task.task_type == "high_risk_module_map":
                category = task.metadata.get("high_risk_category")
                high_risk_categories_seen.append(category)
                self.assertIn("当前只识别命中", prompt)
                self.assertIn(str(task.metadata.get("high_risk_field")), prompt)
                if category == "不可信来源数据解析或处理代码":
                    return json.dumps(HIGH_RISK_MODULES, ensure_ascii=False)
                return "[]"
            if task.task_type == "high_risk_module_merge":
                self.assertTrue(task.input_files)
                return json.dumps(HIGH_RISK_MODULES, ensure_ascii=False)
            if task.task_type == "attack_tree_by_asset":
                task_input = next(path for path in task.input_files if path.endswith(".input.json"))
                self.assertIn(task_input, prompt)
                self.assertIn("high_risk_modules 是全部最终高风险模块列表", prompt)
                task_input_payload = json.loads(Path(task_input).read_text(encoding="utf-8"))
                self.assertEqual(task_input_payload["high_risk_modules"], HIGH_RISK_MODULES)
                return json.dumps(
                    attack_tree_output(
                        asset_name="用户资料数据",
                        high_risk_module_name=" 用户认证模块 ",
                    ),
                    ensure_ascii=False,
                )
            raise AssertionError(task.task_type)

        progress_stream = io.StringIO()
        progress = ProgressPrinter(enabled=True, stream=progress_stream)
        with tempfile.TemporaryDirectory() as tmp:
            scheduler = AgentScheduler(
                runner=FunctionAgentRunner(run),
                model_router=runtime_router(),
                progress_reporter=progress,
            )
            with scheduler:
                pipeline = ThreatAnalysisPipeline(
                    submitter=AgentSubmitter(scheduler),
                    layout=ThreatAnalysisLayout.for_run(tmp, "run-001"),
                    skill_paths=default_skill_paths(ROOT),
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


if __name__ == "__main__":
    unittest.main()
