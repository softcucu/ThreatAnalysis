"""Threat analysis business pipeline built on top of agent_runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from agent_runtime import AgentSubmitter

from threat_analysis_harness.artifacts import ThreatAnalysisLayout
from threat_analysis_harness.skills import ThreatAnalysisSkillPaths, default_skill_paths
from threat_analysis_harness.stages.attack_trees import AttackTreeStage
from threat_analysis_harness.stages.base import require_all_success, require_success
from threat_analysis_harness.stages.high_risk_modules import HighRiskModuleStage
from threat_analysis_harness.stages.value_assets import ValueAssetStage


@dataclass(frozen=True)
class ThreatAnalysisResult:
    value_assets: list[dict[str, Any]]
    high_risk_modules: list[dict[str, Any]]
    attack_trees: dict[str, Any]


class ThreatAnalysisPipeline:
    def __init__(
        self,
        *,
        submitter: AgentSubmitter,
        layout: ThreatAnalysisLayout,
        skill_paths: ThreatAnalysisSkillPaths | None = None,
    ) -> None:
        self.submitter = submitter
        self.layout = layout
        self.skill_paths = skill_paths or default_skill_paths()
        self.value_assets = ValueAssetStage(
            submitter=submitter,
            layout=layout,
            skill_path=self.skill_paths.value_asset_map,
        )
        self.high_risk_modules = HighRiskModuleStage(
            submitter=submitter,
            layout=layout,
            map_skill_path=self.skill_paths.high_risk_module_map,
            merge_skill_path=self.skill_paths.high_risk_module_merge,
        )
        self.attack_trees = AttackTreeStage(
            submitter=submitter,
            layout=layout,
            skill_path=self.skill_paths.attack_tree_by_asset,
        )

    def run(
        self,
        *,
        input_files: Sequence[str | Path],
        high_risk_input_batches: Sequence[Sequence[str | Path]] | None = None,
        attack_tree_context_files: Sequence[str | Path] = (),
        timeout: float | None = None,
    ) -> ThreatAnalysisResult:
        self.layout.ensure()
        high_risk_batches = high_risk_input_batches or [input_files]

        value_tasks = self.value_assets.build_category_tasks(input_files=input_files)
        high_risk_map_tasks = self.high_risk_modules.build_map_tasks(
            input_batches=high_risk_batches,
        )

        value_handles = self.submitter.submit_many(value_tasks)
        high_risk_map_handles = self.submitter.submit_many(high_risk_map_tasks)

        value_results = require_all_success(self.submitter.wait_all(value_handles, timeout))
        value_assets = self.value_assets.merge_category_outputs(
            [
                (str(result.metadata.get("asset_category", "")), result.output or [])
                for result in value_results
            ]
        )
        high_risk_map_results = require_all_success(
            self.submitter.wait_all(high_risk_map_handles, timeout)
        )
        candidate_files = [result.output_path for result in high_risk_map_results]
        merge_task = self.high_risk_modules.build_merge_task(candidate_files=candidate_files)
        high_risk_modules = require_success(self.submitter.submit(merge_task).wait(timeout)).output

        attack_trees = self.attack_trees.run(
            value_assets=value_assets,
            high_risk_modules=high_risk_modules,
            context_files=attack_tree_context_files,
            timeout=timeout,
        )
        return ThreatAnalysisResult(
            value_assets=value_assets,
            high_risk_modules=high_risk_modules,
            attack_trees=attack_trees,
        )
