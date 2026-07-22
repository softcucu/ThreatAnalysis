#!/usr/bin/env python3
"""Command line entrypoint for running the threat analysis pipeline."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from threat_analysis_harness import ThreatAnalysisLayout, ThreatAnalysisPipeline  # noqa: E402
from threat_analysis_harness.skills import default_skill_paths  # noqa: E402
from threat_analysis_harness.task_agent_submitter import TaskAgentSubmitter  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        result = run(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Threat analysis failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run ThreatAnalysis with task_agent.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="task-agent.yaml",
        help="task_agent YAML 配置，默认 task-agent.yaml。",
    )
    parser.add_argument(
        "-i",
        "--input",
        action="append",
        required=True,
        help="输入文件或目录，可重复传入。",
    )
    parser.add_argument(
        "--high-risk-batch",
        action="append",
        nargs="+",
        help="高风险模块识别输入 batch。可重复传入；未传时使用 --input。",
    )
    parser.add_argument(
        "--attack-tree-context",
        action="append",
        default=[],
        help="攻击树额外上下文文件，可重复传入。",
    )
    parser.add_argument(
        "--artifacts-root",
        default="artifacts",
        help="产物根目录，默认 artifacts。",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="本次运行 ID；未传时自动生成。",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="继续已有 run：如果任务输出文件已存在且通过 JSON schema 校验，则跳过该任务。",
    )
    parser.add_argument(
        "--project-root",
        default=str(PROJECT_ROOT),
        help="项目根目录，用于定位 skills，默认脚本所在仓库根目录。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="等待每批 agent 任务的超时时间，单位秒。",
    )
    parser.set_defaults(print_progress=True)
    parser.add_argument(
        "--print-progress",
        dest="print_progress",
        action="store_true",
        help="打印关键步骤进度。",
    )
    parser.add_argument(
        "--no-print-progress",
        dest="print_progress",
        action="store_false",
        help="关闭关键步骤和任务进度打印。",
    )
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    run_id = _resolve_run_id(args)
    layout = ThreatAnalysisLayout.for_run(args.artifacts_root, run_id)
    progress = ConsoleProgress(enabled=bool(args.print_progress))
    skill_paths = default_skill_paths(args.project_root)
    submitter = TaskAgentSubmitter(config_path=args.config)

    try:
        pipeline = ThreatAnalysisPipeline(
            submit_tasks=submitter.submit_tasks,
            layout=layout,
            skill_paths=skill_paths,
            progress_reporter=progress,
        )
        result = pipeline.run(
            input_files=[Path(path) for path in args.input],
            high_risk_input_batches=_high_risk_batches(args.high_risk_batch),
            attack_tree_context_files=[Path(path) for path in args.attack_tree_context],
            timeout=args.timeout,
            resume=args.resume,
        )
    finally:
        submitter.shutdown_sync()

    return {
        "run_id": run_id,
        "artifacts_root": str(Path(args.artifacts_root)),
        "resume": bool(args.resume),
        "value_assets": len(result.value_assets),
        "high_risk_modules": len(result.high_risk_modules),
        "attack_trees": len(result.attack_trees.get("attack_trees", [])),
        "outputs": {
            "value_assets": str(layout.value_assets_final_dir / "value-assets.json"),
            "high_risk_modules": str(
                layout.high_risk_final_dir / "high-risk-module-merge.json"
            ),
            "attack_trees": str(layout.attack_trees_final_dir / "attack_trees.json"),
        },
    }


def _high_risk_batches(raw_batches: list[list[str]] | None) -> list[list[Path]] | None:
    if not raw_batches:
        return None
    return [[Path(path) for path in batch] for batch in raw_batches]


def _resolve_run_id(args: argparse.Namespace) -> str:
    if args.run_id:
        return str(args.run_id)
    if not args.resume:
        return time.strftime("%Y%m%d-%H%M%S")

    latest = _latest_run_id(args.artifacts_root)
    if latest is None:
        raise ValueError("--resume requires --run-id when no previous run exists")
    return latest


def _latest_run_id(artifacts_root: str | Path) -> str | None:
    runs_dir = Path(artifacts_root) / "runs"
    if not runs_dir.exists():
        return None

    run_dirs = [path for path in runs_dir.iterdir() if path.is_dir()]
    if not run_dirs:
        return None

    latest = max(run_dirs, key=lambda path: path.stat().st_mtime)
    return latest.name


class ConsoleProgress:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled

    def emit(self, message: str) -> None:
        if self.enabled:
            print(f"[threat-analysis] {message}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
