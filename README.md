# ThreatAnalysis

威胁分析 AI Agent 编排框架，用于对代码仓和输入文档进行价值资产识别、高风险模块识别，并基于二者生成理论攻击树。

## 整体流程

主流程入口是 `ThreatAnalysisPipeline.run()`。一次威胁分析会按以下顺序执行：

1. 初始化本次运行的产物目录。
2. 并发启动价值资产识别任务和高风险模块 map 任务。
3. 合并价值资产识别结果，得到最终价值资产列表。
4. 等待全部高风险模块 map 任务完成，再启动高风险模块 merge 任务，得到最终高风险模块列表。
5. 按每个价值资产分别启动攻击树分析任务；每个攻击树任务都会收到当前价值资产和全部最终高风险模块。
6. 对攻击树结果做产物一致性对齐，合并所有攻击树输出，生成最终攻击树文档。

## 价值资产识别

价值资产阶段使用同一个 `value_asset_map` skill，但在运行时拆成 4 个 agent 任务：

- 数据资产
- 软件资产
- 硬件资产
- 服务资产

每个任务的 runtime prompt 只允许识别当前类别，例如“当前只识别数据类价值资产”。任务会携带价值资产 JSON schema，JSON 提取和 schema 校验由 agent runtime 执行。

四类任务完成后，程序会合并结果：

- 过滤掉类别不符合当前任务要求的资产。
- 按“资产类别 + 规范化资产名”去重。
- 对重复资产合并资产描述、攻击损失和判断原因。

最终结果写入：

```text
runs/<run_id>/value_assets/final/value-assets.json
```

## 高风险模块识别

高风险模块阶段分为 map 和 merge 两步。

map 阶段使用同一个 `high_risk_module_map` skill，但按 5 类高风险特征拆成多个 agent 任务：

- 管理和控制接口相关代码
- 不可信来源数据解析或处理代码
- 安全相关类代码
- 个人数据或者敏感数据代码
- Web 相关处理

如果传入多个 `high_risk_input_batches`，每个 batch 都会分别拆成这 5 类任务。每个分类任务只识别命中当前高风险特征的模块，并要求对应“是否涉及...”字段为 `是`；其他字段仍按代码证据真实填写。

map 任务解析校验后的 JSON 输出写入：

```text
runs/<run_id>/high_risk_modules/raw/
```

merge 阶段使用 `high_risk_module_merge` skill，读取所有 map 候选 JSON，处理命名不一致、功能重叠、同一模块命中多个高风险特征等情况，输出最终高风险模块列表。

最终结果写入：

```text
runs/<run_id>/high_risk_modules/final/high-risk-module-merge.json
```

## 攻击树分析

攻击树阶段按价值资产拆任务：每个最终价值资产对应一个 `attack_tree_by_asset` agent 任务。

每个攻击树任务都会生成一个结构化输入文件，包含：

```json
{
  "value_asset": "当前价值资产",
  "high_risk_modules": "全部最终高风险模块"
}
```

也就是说，攻击树任务只分析一个价值资产，但每次都会拿到完整的高风险模块列表，用于判断从外部暴露高风险模块到价值资产的影响路径。

攻击树任务输入写入：

```text
runs/<run_id>/task_inputs/attack-tree-by-asset-*.input.json
```

攻击树任务解析校验后的 JSON 输出写入：

```text
runs/<run_id>/attack_trees/raw/
```

所有资产的攻击树输出会被合并为：

```text
runs/<run_id>/attack_trees/final/attack_trees.json
```

## 产物一致性

为了保证价值资产、高风险模块和最终攻击树能够关联上，攻击树输出在合并前会做规范化对齐：

- 攻击树中的 `value_asset` 会回填为当前最终价值资产的规范资产名、类别、描述和攻击损失。
- 根节点名称会对齐为 `攻击价值资产：<资产名>`。
- 叶子节点必须能匹配最终高风险模块，并且必须是外部暴露高风险模块。
- `is_high_risk_module=true` 的节点必须能匹配最终高风险模块。
- `related_high_risk_modules` 中的模块名会对齐为最终高风险模块的规范 `模块名称`。
- 攻击路径中漏列的高风险节点会补入 `related_high_risk_modules`。

普通内部节点可以是攻击树分析过程中基于代码新识别出的内部模块，不要求出现在最终高风险模块列表中。只有被标记为高风险模块、作为叶子节点，或出现在 `related_high_risk_modules` 中的模块，才必须来自最终高风险模块列表。

如果攻击树输出引用了无法匹配最终高风险模块的叶子节点或高风险节点，流程会抛出 `ArtifactConsistencyError`，避免产出无法关联的最终攻击树。

## 主要代码位置

- `src/threat_analysis_harness/pipeline.py`：整体流程编排。
- `src/threat_analysis_harness/stages/value_assets.py`：价值资产分类识别和程序合并。
- `src/threat_analysis_harness/stages/high_risk_modules.py`：高风险模块分类 map 和 merge 任务构造。
- `src/threat_analysis_harness/stages/attack_trees.py`：按资产生成攻击树任务、合并攻击树输出和产物一致性对齐。
- `src/threat_analysis_harness/task_agent_submitter.py`：harness 到 `task_agent.run_opencode_task()` 的同步提交适配器。
- `src/task_agent/`：新的 OpenCode/nga Serve 任务框架，负责模型池、队列、会话、重试和 JSON schema 校验。
- `skills/threat-analysis-harness/`：各阶段 agent 使用的 skill 提示词。
- `web/index.html`：威胁分析产物查看页，提供价值资产、高风险模块、内部节点和攻击树四个页签。

## 模型与 opencode 运行方式

当前 CLI 通过 `src/task_agent` 的公开接口 `run_opencode_task()` 执行模型任务。复制新的 YAML 示例并按实际环境调整：

```bash
cp src/task_agent/task-agent.example.yaml task-agent.yaml
```

关键配置：

- `context.project_dir`：被分析的源码或输入仓库目录。
- `context.work_dir`：agent 可写工作目录，任务过程中的写入会限制在这里。
- `context.workspace_dir`：task_agent 管理 Serve 进程和运行状态的组件工作区。
- `serve`：OpenCode/nga Serve 的工具、端口、超时、环境变量和 MCP 配置。
- `model_pool`：可用模型、能力等级、权重、并发和全局并发。

`TaskAgentSubmitter` 会把 harness 的业务任务字典转换成 `run_opencode_task()` 调用。业务任务类型仍保留为 `value_asset_map`、`high_risk_module_map`、`high_risk_module_merge` 和 `attack_tree_by_asset`，提交给 task_agent 时统一使用公开 API 支持的 `task_type="threat_analysis"`；默认 `required_capability="high"`。任务携带的 `output_schema` 会传给 task_agent，由 task_agent 负责 JSON 提取、同会话 JSON 修正和 schema 校验，校验后的 `result.structured` 会写入任务的 `output_path`。

新公开 API 不接收 `skill_path` 参数，因此适配器会把对应 `SKILL.md` 正文内联进 prompt；如果 skill 有 `references/` 目录，prompt 会列出引用资料路径，供模型在任务中读取。

## 命令行启动

推荐使用脚本启动完整威胁分析流程：

```bash
python3 scripts/run_threat_analysis.py \
  --config task-agent.yaml \
  --input /path/to/repository-or-input \
  --artifacts-root artifacts \
  --run-id demo-run
```

Serve 启动、复用、端口和环境变量由 `task-agent.yaml` 中的 `serve` 配置决定。

如果需要指定多个输入文件或目录，可以重复传入 `--input`：

```bash
python3 scripts/run_threat_analysis.py \
  --config task-agent.yaml \
  --input product.md \
  --input src \
  --input deploy
```

如果高风险模块识别需要独立 batch，可以重复传入 `--high-risk-batch`：

```bash
python3 scripts/run_threat_analysis.py \
  --config task-agent.yaml \
  --input product.md \
  --high-risk-batch src/api src/auth \
  --high-risk-batch src/parser src/protocol
```

如果需要继续已有 run，加上 `--resume`。未传 `--run-id` 时会自动选择 `artifacts/runs/` 下最近修改的 run；也可以显式传入同一个 `--run-id`。pipeline 会检查每个任务的 `output_path`，如果文件已存在且可解析为 JSON，就跳过该任务并复用该输出；不存在或 JSON 不可解析的任务会重新执行。由 task_agent 生成的任务输出在生成时已经完成对应 JSON schema 校验：

```bash
python3 scripts/run_threat_analysis.py \
  --config task-agent.yaml \
  --input product.md \
  --run-id demo-run \
  --resume
```

常用参数：

- `--config`：task_agent YAML 配置文件路径。
- `--input`：输入文件或目录，可重复传入。
- `--high-risk-batch`：高风险模块识别输入 batch，可重复传入。
- `--attack-tree-context`：攻击树额外上下文文件，可重复传入。
- `--artifacts-root` / `--run-id`：产物根目录和本次运行 ID。
- `--timeout`：等待每批 agent 任务的超时时间。
- `--resume`：继续已有 run；未传 `--run-id` 时使用最近修改的 run，任务输出文件已存在且可解析为 JSON 时跳过该任务。
- `--print-progress` / `--no-print-progress`：控制是否打印 pipeline 关键步骤进度。

脚本结束后会输出本次 run ID、产物数量和最终 JSON 路径。

也可以在代码中直接组装 pipeline：

```python
from threat_analysis_harness import (
    TaskAgentSubmitter,
    ThreatAnalysisLayout,
    ThreatAnalysisPipeline,
)
from threat_analysis_harness.skills import default_skill_paths

skill_paths = default_skill_paths()
layout = ThreatAnalysisLayout.for_run("artifacts", "demo-run")
submitter = TaskAgentSubmitter(config_path="task-agent.yaml")

try:
    pipeline = ThreatAnalysisPipeline(
        submit_tasks=submitter.submit_tasks,
        layout=layout,
        skill_paths=skill_paths,
    )
    result = pipeline.run(
        input_files=["/path/to/repository-or-input"],
        timeout=None,
    )
finally:
    submitter.shutdown_sync()
```

`ThreatAnalysisPipeline` 只依赖 `pipeline_submit_tasks(tasks, timeout=None)` 这个函数契约；如果替换 runtime，只需要在装配层传入另一个同签名函数。

`tasks` 和返回值都使用普通 JSON 字典。任务 JSON 字段约定为：

- 必填：`task_id`、`task_type`、`skill_path`、`runtime_prompt`、`output_path`。
- 可选：`input_files`、`output_schema`、`output_schema_path`、`metadata`、`priority`。
- 返回结果至少包含：`task_id`、`task_type`、`status`、`output_path`、`output`；失败时包含 `error`。

适配器每个任务会把完整 prompt 写入 `<output_path>.prompt.txt`，把模型原始文本写入 `<output_path>.raw.txt`，并把简要运行信息写入 `<output_path>.log`。

## Web 查看页

查看页是静态页面，可直接打开：

```text
web/index.html
```

页面可分别导入以下 JSON 产物：

- `runs/<run_id>/value_assets/final/value-assets.json`
- `runs/<run_id>/high_risk_modules/final/high-risk-module-merge.json`
- `runs/<run_id>/attack_trees/final/attack_trees.json`

导入后，页面会以表格展示价值资产和高风险模块，从攻击树中提取内部节点，并用根到叶的树状图展示每棵攻击树及叶子节点匹配的攻击模式标题。

## 测试

当前测试可通过标准库 `unittest` 运行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
