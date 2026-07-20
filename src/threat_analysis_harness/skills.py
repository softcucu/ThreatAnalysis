"""Skill path helpers for the threat analysis harness."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ThreatAnalysisSkillPaths:
    value_asset_map: Path
    high_risk_module_map: Path
    high_risk_module_merge: Path
    attack_tree_by_asset: Path


def default_skill_paths(project_root: str | Path = ".") -> ThreatAnalysisSkillPaths:
    root = Path(project_root) / "skills" / "threat-analysis-harness"
    return ThreatAnalysisSkillPaths(
        value_asset_map=root / "value-assets" / "value-asset-map",
        high_risk_module_map=root / "high-risk-modules" / "high-risk-module-map",
        high_risk_module_merge=root / "high-risk-modules" / "high-risk-module-merge",
        attack_tree_by_asset=root / "attack-trees" / "attack-tree-by-asset",
    )
