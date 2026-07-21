#!/usr/bin/env python3
"""Command line entrypoint for running the threat analysis pipeline."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_runtime import (  # noqa: E402
    AgentScheduler,
    AgentSubmitter,
    ModelRouter,
    OpenCodeAgentRunner,
    ProgressPrinter,
    load_runtime_config,
)
from threat_analysis_harness import ThreatAnalysisLayout, ThreatAnalysisPipeline  # noqa: E402
from threat_analysis_harness.skills import default_skill_paths  # noqa: E402


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
        description="Run ThreatAnalysis with opencode serve.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="agent-runtime.json",
        help="模型与并发配置 JSON，默认 agent-runtime.json。",
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
    parser.add_argument(
        "--opencode-base-url",
        default=None,
        help="opencode serve 地址；未传且会启动 opencode 时自动选择随机未占用端口。",
    )
    parser.add_argument(
        "--opencode-command",
        default="opencode serve --hostname 127.0.0.1 --port 4096",
        help="启动 opencode serve 的命令字符串；未传 --opencode-base-url 时其中 --port 会被随机未占用端口覆盖。",
    )
    parser.add_argument(
        "--opencode-directory",
        default=None,
        help="opencode 运行的项目目录；未传时使用当前工作目录。",
    )
    parser.add_argument(
        "--no-start-opencode",
        action="store_true",
        help="不启动 opencode serve，只连接已有 server。",
    )
    parser.add_argument(
        "--opencode-username",
        default="opencode",
        help="opencode basic auth 用户名。",
    )
    parser.add_argument(
        "--opencode-password",
        default=None,
        help="opencode basic auth 密码；未传时读取 OPENCODE_PASSWORD。",
    )
    parser.add_argument(
        "--opencode-agent",
        default=None,
        help="发送给 opencode 的 agent 名称。",
    )
    parser.add_argument(
        "--delete-session",
        action="store_true",
        help="任务完成后删除对应 opencode session。",
    )
    parser.set_defaults(print_progress=None)
    parser.add_argument(
        "--print-progress",
        dest="print_progress",
        action="store_true",
        help="打印关键步骤和任务的开始/完成情况；未传时使用配置文件 progress.enabled。",
    )
    parser.add_argument(
        "--no-print-progress",
        dest="print_progress",
        action="store_false",
        help="关闭关键步骤和任务进度打印。",
    )
    parser.add_argument(
        "--server-timeout",
        type=float,
        default=None,
        help="opencode HTTP 请求超时时间，单位秒。",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=30.0,
        help="等待 opencode serve 启动的超时时间，单位秒。",
    )
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    config = load_runtime_config(args.config)
    run_id = _resolve_run_id(args)
    layout = ThreatAnalysisLayout.for_run(args.artifacts_root, run_id)
    start_command = None if args.no_start_opencode else tuple(shlex.split(args.opencode_command))
    base_url = args.opencode_base_url
    if args.no_start_opencode and base_url is None:
        base_url = "http://127.0.0.1:4096"
    password = args.opencode_password or os.environ.get("OPENCODE_PASSWORD")
    progress_enabled = (
        config.progress_enabled if args.print_progress is None else bool(args.print_progress)
    )
    progress = ProgressPrinter(enabled=progress_enabled)
    skill_paths = default_skill_paths(args.project_root)

    runner = OpenCodeAgentRunner(
        base_url=base_url,
        start_command=start_command,
        cwd=args.opencode_directory,
        timeout=args.server_timeout,
        startup_timeout=args.startup_timeout,
        username=args.opencode_username,
        password=password,
        agent=args.opencode_agent,
        delete_session=args.delete_session,
        skill_paths=(
            skill_paths.value_asset_map,
            skill_paths.high_risk_module_map,
            skill_paths.high_risk_module_merge,
            skill_paths.attack_tree_by_asset,
        ),
    )

    progress.emit(f"opencode server check started: base_url={base_url or 'auto'}")
    with runner:
        progress.emit(f"opencode server ready: base_url={runner.base_url}")
        scheduler = AgentScheduler(
            runner=runner,
            model_router=ModelRouter(config),
            progress_reporter=progress,
        )
        with scheduler:
            pipeline = ThreatAnalysisPipeline(
                submitter=AgentSubmitter(scheduler),
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


if __name__ == "__main__":
    raise SystemExit(main())
