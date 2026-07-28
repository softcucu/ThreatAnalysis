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

每个攻击树任务的 runtime prompt 会直接携带当前价值资产，并读取已有的最终高风险模块文件：

```text
runs/<run_id>/high_risk_modules/final/high-risk-module-merge.json
```

也就是说，攻击树任务只分析一个价值资产，同时使用完整的最终高风险模块列表判断从外部暴露高风险模块到价值资产的影响路径；流程不会额外生成重复的 `task_inputs` 文件。

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
- 单颗攻击树生成后会立即校验叶子节点引用的 `module_id`。如果叶子引用了非外部暴露高风险模块，流程会使用原任务的 `session_id` 在同一会话中直接追加修正提示（不重复调用 `/attack-tree-by-asset`），并按完整 JSON Schema 重新输出；修正结果仍存在同类问题时，会删除对应攻击路径以及不再使用的节点和边。若一棵树的全部路径均因此被删除，该树不会进入最终合并结果。
- 内部节点如果能匹配最终高风险模块，会对齐为最终高风险模块的规范 `模块名称`。
- 内部节点如果不能匹配最终高风险模块，会按普通内部节点保留；即使模型误标 `is_high_risk_module=true`，也会降级为 `false`。
- `related_high_risk_modules` 中的模块名会对齐为最终高风险模块的规范 `模块名称`。
- 攻击路径中漏列的高风险节点会补入 `related_high_risk_modules`。
- 攻击树任务过程中会为最终高风险模块生成稳定的内部 `module_id`，动态 schema
  只允许引用本次模块目录中的 ID；叶子节点和 `related_high_risk_modules` 优先通过
  ID 对齐，模型使用别名或名称格式差异时仍会回填规范模块名称。
- `module_id` 只存在于攻击树 raw 任务输出，合并最终产物前会移除，因此最终
  高风险模块和攻击树 JSON 的公开字段及结构保持不变。
- 模型原始输出必须至少包含一棵树、一条边和一条攻击路径；空攻击树会在 schema 校验阶段失败并触发任务修正/重试，resume 时也不会复用。只有“非外部暴露模块被重复用作叶子”这一降级处理可能在裁剪后使某个资产不再贡献攻击树。

普通内部节点可以是攻击树分析过程中基于代码新识别出的内部模块，不要求出现在最终高风险模块列表中。只有作为叶子节点，或出现在 `related_high_risk_modules` 中的模块，才必须来自最终高风险模块列表。

如果攻击树输出引用了无法匹配最终高风险模块的叶子节点或 `related_high_risk_modules`，流程会抛出 `ArtifactConsistencyError`，避免产出无法关联的最终攻击树。

## 主要代码位置

- `src/threat_analysis_harness/pipeline.py`：整体流程编排。
- `src/threat_analysis_harness/stages/value_assets.py`：价值资产分类识别和程序合并。
- `src/threat_analysis_harness/stages/high_risk_modules.py`：高风险模块分类 map 和 merge 任务构造。
- `src/threat_analysis_harness/stages/attack_trees.py`：按资产生成攻击树任务、合并攻击树输出和产物一致性对齐。
- `src/threat_analysis_harness/task_agent_submitter.py`：harness 到 `task_agent.run_opencode_task()` 的同步提交适配器。
- `src/task_agent/`：新的 OpenCode/nga Serve 任务框架，负责模型池、队列、会话、重试和 JSON schema 校验。
- `src/threat_analysis_harness/skills/`：各阶段 agent 使用的 skill 提示词。
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
- `serve`：OpenCode/nga Serve 的工具、端口、超时、环境变量和 OpenCode 原生配置。
- `serve.opencode_config.skills.paths`：OpenCode skill 搜索路径；harness 默认需要配置 `src/threat_analysis_harness/skills/value-assets`、`src/threat_analysis_harness/skills/high-risk-modules` 和 `src/threat_analysis_harness/skills/attack-trees`。
- `model_pool`：可用模型、能力等级、权重、并发和全局并发。

`TaskAgentSubmitter` 会把 harness 的业务任务字典转换成 `run_opencode_task()` 调用。业务任务类型仍保留为 `value_asset_map`、`high_risk_module_map`、`high_risk_module_merge` 和 `attack_tree_by_asset`，提交给 task_agent 时统一使用公开 API 支持的 `task_type="threat_analysis"`；默认 `required_capability="high"`。任务携带的 `output_schema` 会传给 task_agent，由 task_agent 负责 JSON 提取、同会话 JSON 修正和 schema 校验，校验后的 `result.structured` 会写入任务的 `output_path`。

`TaskAgentSubmitter` 不读取或内联 `SKILL.md`。`task_agent` 会把 `serve.opencode_config` 写入 OpenCode 配置并加载其中的 `skills.paths`；适配器只根据 harness 任务的 `skill_name` 在 prompt 开头使用 `/skill-name` 调用已配置的 skill。

## 命令行启动

推荐通过包内入口启动完整威胁分析流程：

```bash
PYTHONPATH=src python3 -m threat_analysis_harness.main \
  --code-path /path/to/repository-or-input \
  --output-path artifacts/demo-run
```

该入口只负责解析命令行参数并调用 `run_threat_analysis()`。Serve 启动、复用、端口和环境变量仍由 task_agent 的配置决定。

如果需要透传暂未消费的产品知识 MCP 或私有攻击模式，可以传入：

```bash
PYTHONPATH=src python3 -m threat_analysis_harness.main \
  --code-path /path/to/repository-or-input \
  --output-path artifacts/demo-run \
  --product-mcp product-mcp \
  --attack-modes '{"attack_mode1": ["introduction", "skill-name"]}'
```

常用参数：

- `--code-path`：代码仓路径，必填。
- `--output-path`：落盘产物路径，必填。
- `--resume`：复用 `--output-path` 下已有任务 JSON 输出。
- `--product-mcp`：产品知识 MCP 名称，当前仅透传给接口。
- `--attack-modes`：私有攻击模式 JSON 字符串，当前仅透传给接口。

命令结束后会输出 `run_threat_analysis()` 返回的 JSON。成功时包含 `value_asset_path`、`high_risk_modules_path` 和 `attack_tree_path`；失败时包含 `reason`。

也可以在代码中直接组装 pipeline：

```python
from threat_analysis_harness import (
    TaskAgentSubmitter,
    ThreatAnalysisLayout,
    ThreatAnalysisPipeline,
)
layout = ThreatAnalysisLayout.for_run("artifacts", "demo-run")
submitter = TaskAgentSubmitter(config_path="task-agent.yaml")

pipeline = ThreatAnalysisPipeline(
    submit_tasks=submitter.submit_tasks,
    layout=layout,
)
result = pipeline.run(
    input_files=["/path/to/repository-or-input"],
    timeout=None,
)
```

`ThreatAnalysisPipeline` 只依赖 `pipeline_submit_tasks(tasks, timeout=None)` 这个函数契约；如果替换 runtime，只需要在装配层传入另一个同签名函数。

`tasks` 和返回值都使用普通 JSON 字典。任务 JSON 字段约定为：

- 必填：`task_id`、`task_type`、`skill_name`、`runtime_prompt`、`output_path`。
- 可选：`input_files`、`output_schema`、`output_schema_path`、`metadata`、`priority`。
- 返回结果至少包含：`task_id`、`task_type`、`status`、`output_path`、`output`；失败时包含 `error`。

适配器每个任务会把完整 prompt 写入 `<output_path>.prompt.txt`，把模型原始文本写入 `<output_path>.raw.txt`，并把简要运行信息写入 `<output_path>.log`。

## Web 查看页

查看页是静态页面，可直接打开：

```text
web/index.html
```

页面可分别导入以下 JSON 产物：

- `<output_path>/value_assets/final/value-assets.json`
- `<output_path>/high_risk_modules/final/high-risk-module-merge.json`
- `<output_path>/attack_trees/final/attack_trees.json`

导入后，页面会以表格展示价值资产和高风险模块，从攻击树中提取内部节点，并用根到叶的树状图展示每棵攻击树及叶子节点匹配的攻击模式标题。

## 测试

当前测试可通过标准库 `unittest` 运行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
