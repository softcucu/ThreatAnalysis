# Agent 工作指南

本文档面向后续参与本仓库工作的 AI Agent 或工程协作者，目标是快速建立项目上下文，并在修改代码、skill、测试或运行配置时保持产物兼容。

## 项目概览

ThreatAnalysis 是一个威胁分析 AI Agent 编排框架。它读取代码仓或输入文档，依次完成：

1. 价值资产识别。
2. 高风险模块识别。
3. 基于价值资产和高风险模块生成理论攻击树。
4. 输出可供 Web 静态查看页导入的 JSON 产物。

核心业务编排入口是 `ThreatAnalysisPipeline.run()`，代码位于 `src/threat_analysis_harness/pipeline.py`。实际模型任务通过 `src/threat_analysis_harness/task_agent_submitter.py` 适配到 `src/task_agent/` 的 `run_opencode_task()`。

## 目录职责

- `src/threat_analysis_harness/`：威胁分析业务流水线、阶段实现、产物目录布局和 JSON schema。
- `src/threat_analysis_harness/stages/`：价值资产、高风险模块、攻击树三个阶段的任务构造、合并和一致性处理。
- `src/threat_analysis_harness/task_agent_submitter.py`：harness 到新 `task_agent` 公开接口的同步提交适配器。
- `src/task_agent/`：新的 OpenCode/nga Serve 任务框架，负责模型池、队列、会话、重试、事件流和 JSON schema 校验。
- `src/agent_runtime/`：旧通用 Agent 运行时，当前保留兼容代码和测试。
- `src/threat_analysis_harness/skills/`：opencode 运行时使用的 skill 提示词和引用资料。
- `src/threat_analysis_harness/main.py`：命令行入口，解析参数后调用 `run_threat_analysis()`。
- `web/`：静态产物查看页，可直接打开 `web/index.html` 导入最终 JSON。
- `tests/`：标准库 `unittest` 测试和 fixture。
- `origin/`：原始需求、分析方法或参考资料，修改业务规则前应先查看这里。

## 运行方式

复制示例配置并按实际模型资源调整：

```bash
cp src/task_agent/task-agent.example.yaml task-agent.yaml
```

运行完整威胁分析：

```bash
PYTHONPATH=src python3 -m threat_analysis_harness.main \
  --code-path /path/to/repository-or-input \
  --output-path artifacts/demo-run
```

Serve 启动、复用、端口、环境变量、OpenCode 原生配置和模型池由 `task-agent.yaml` 控制。harness 运行所需 skill 路径应配置在 `serve.opencode_config.skills.paths`。

查看产物时直接打开 `web/index.html`，分别导入最终的价值资产、高风险模块和攻击树 JSON。

## 测试命令

当前项目没有依赖第三方测试框架，使用标准库 `unittest`：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

修改流水线阶段、schema、task_agent submitter、运行框架或输出校验逻辑后，应运行完整测试。只改 Markdown 文档时可不运行测试，但最终说明中要明确测试未运行的原因。

## 关键数据流

一次完整运行的主要产物目录格式为：

```text
<output-path>/
  task_inputs/
  value_assets/
    raw/
    final/value-assets.json
  high_risk_modules/
    raw/
    final/high-risk-module-merge.json
  attack_trees/
    raw/
    final/attack_trees.json
```

价值资产阶段会按四类资产并发拆分任务：数据资产、软件资产、硬件资产、服务资产。程序会过滤类别不符的结果，并按“资产类别 + 规范化资产名”合并去重。

高风险模块阶段会按五类高风险特征拆分 map 任务，并通过单独的 merge 任务合并候选模块。

攻击树阶段会按最终价值资产逐个启动任务。每个攻击树任务只分析一个价值资产，但会收到完整最终高风险模块列表。合并攻击树前会执行一致性对齐：价值资产名称、根节点、叶子节点和 `related_high_risk_modules` 都必须能和最终产物关联；内部节点不能匹配最终高风险模块时会按普通内部节点保留。攻击树 schema 要求非空树、非空边和非空攻击路径，resume 时会对攻击树 raw 输出重新跑 schema 校验，空树不会被当作成功产物复用。

## 重要约定

- Agent 任务会携带 `src/threat_analysis_harness/schemas.py` 中对应业务 schema；JSON 提取、schema 校验、同会话修正和规范化写入由 `task_agent` 与 `TaskAgentSubmitter` 协作完成。
- `task_agent.run_opencode_task()` 会在传入 `output_schema` 时追加 JSON 输出约束；业务 harness 不直接拼接 runtime 指令。
- `TaskAgentSubmitter` 不读取或内联 `SKILL.md`；skill 由 `task-agent.yaml` 的 `serve.opencode_config.skills.paths` 提供，适配器只在 prompt 中使用 `/skill-name` 调用。
- 包内命令行入口只调用 `run_threat_analysis()`；第三方接口当前接收单个 `code_path` 和 `output_path`，通过 `is_resume=True` 或命令行 `--resume` 复用同一 `output_path` 下已有任务 JSON 输出。
- harness 业务 `task_type` 保持原值；提交给 task_agent 时默认映射为公开 API 支持的 `task_type="threat_analysis"`。
- 模型选择、能力等级和并发由 `task-agent.yaml` 的 `model_pool` 控制。
- 不要让业务阶段直接依赖某个具体模型；模型选择应保留在 task_agent 配置和 submitter 装配层。
- `ThreatAnalysisPipeline` 只依赖 `submit_tasks(tasks, timeout=None)` 函数；`tasks` 和返回结果均为普通 JSON 字典。替换 runtime 时，在装配层替换同签名函数或 adapter，不要让 harness 直接依赖新 runtime 的内部类型。

## 修改建议

- 改业务拆分逻辑时，优先查看 `src/threat_analysis_harness/stages/` 和对应测试。
- 改输出字段、枚举或必填项时，必须同步更新 schema、skill 提示词、测试 fixture 和 Web 展示逻辑。
- 改 skill 时，保持 `SKILL.md` 的输出格式要求和程序 schema 一致；如果新增引用资料，放在对应 skill 的 `references/` 目录。
- 改攻击树一致性规则时，重点检查 `src/threat_analysis_harness/stages/attack_trees.py`，避免生成无法关联最终高风险模块的攻击路径。
- 改调度、并发或模型路由时，优先补充对应运行框架测试，覆盖资源池限流、候选模型选择和失败结果。
- 改 Web 页时保持静态可用，不引入构建步骤，除非同时补齐运行说明。

## 编码风格

- Python 代码使用标准库为主，保持类型标注和 dataclass 风格。
- 文件读写统一使用 UTF-8。
- JSON 写入保持 `ensure_ascii=False` 和缩进格式，避免破坏中文产物可读性。
- 保持错误信息可定位，尤其是输出校验、schema 校验和 artifact 一致性错误。
- 控制改动范围，不做与当前任务无关的重构或格式化。
