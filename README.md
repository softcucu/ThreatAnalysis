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

每个任务的 runtime prompt 只允许识别当前类别，例如“当前只识别数据类价值资产”，并要求“不允许输出json文件，直接返回json结果”。任务输出必须符合价值资产 JSON schema。

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
- `src/agent_runtime/runner.py`：agent runner 实现，包括函数 runner、外部命令 runner 和 opencode server runner。
- `skills/threat-analysis-harness/`：各阶段 agent 使用的 skill 提示词。
- `web/index.html`：威胁分析产物查看页，提供价值资产、高风险模块、内部节点和攻击树四个页签。

## 模型与 opencode 运行方式

模型按 `task_type` 配置，但并发主要按模型资源池限制。可以复制示例配置：

```bash
cp agent-runtime.example.json agent-runtime.json
```

配置格式：

```json
{
  "models": {
    "value_asset_map": [
      {
        "model": "openai/gpt-5-mini",
        "resource": "fast-model-pool"
      },
      {
        "model": "anthropic/claude-sonnet-4",
        "resource": "backup-model-pool"
      }
    ],
    "high_risk_module_map": {
      "model": "openai/gpt-5-mini",
      "resource": "fast-model-pool"
    },
    "high_risk_module_merge": {
      "model": "openai/gpt-5",
      "resource": "reasoning-model-pool"
    },
    "attack_tree_by_asset": [
      {
        "model": "openai/gpt-5",
        "resource": "reasoning-model-pool"
      },
      {
        "model": "anthropic/claude-opus-4",
        "resource": "reasoning-model-pool"
      }
    ]
  },
  "model_resources": {
    "fast-model-pool": {
      "concurrency": 4
    },
    "backup-model-pool": {
      "concurrency": 2
    },
    "reasoning-model-pool": {
      "concurrency": 1
    }
  },
  "concurrency": {
    "global": 7
  },
  "retry": {
    "max_retries": 3
  },
  "progress": {
    "enabled": true
  }
}
```

说明：

- `models` 的 key 是任务类型。
- 同一个任务类型可以配置一个模型，也可以配置多个模型；多个模型按顺序作为候选，前面的模型资源池满了之后会使用后面的候选。
- 使用 `OpenCodeAgentRunner` 时，`model` 推荐写成 `provider/model`，例如 `openai/gpt-5-mini`。runner 会把该模型作为 opencode message 的 `model` 参数发送，不会写入运行时 prompt。
- 如果模型名不能写成 `provider/model`，可以在模型配置里显式传入 `parameters.opencode_model`：

```json
{
  "model": "fast-value-model",
  "resource": "fast-model-pool",
  "parameters": {
    "opencode_model": {
      "providerID": "openai",
      "modelID": "gpt-5-mini"
    }
  }
}
```

- `resource` 是模型资源池名称；未配置时默认使用 `model` 字符串作为资源池名称。
- `model_resources` 配置每个资源池的并发度，这是主要限流方式。
- `concurrency.global` 是 scheduler worker 总数，通常设置为各模型资源池并发度之和或略高。
- `concurrency.by_task_type` 仍可作为兼容性的额外限制，但默认不需要配置。
- `retry.max_retries` 是任务失败后的最大重试次数，未配置时默认 3 次；设置为 `0` 可关闭重试。
- `progress.enabled` 是全局进度打印开关。开启后会向 stderr 输出 opencode 连接检查、pipeline 阶段、任务排队、任务开始、任务完成和失败信息；关闭后只保留最终 JSON 输出。

opencode 推荐通过 `opencode serve` 作为后台 HTTP server 运行。框架中的 `OpenCodeAgentRunner` 会在启动或连接 server 前，把配置的所有 skills 同步到 opencode 项目目录的 `.opencode/skills/<skill-name>/`，并在该目录的 `opencode.json` 中配置 `skills.paths` 指向同一个 `.opencode/skills` 目录；每个 `AgentTask` 会创建独立 opencode session，并通过 `/session/{id}/message` 发送 `/<skill-name>` 开头的提示词调用该 skill。运行时 prompt 只包含当前任务说明、输入文件和 JSON schema，不内联 skill 正文、模型配置或输出路径；prompt 会要求模型“不允许输出json文件，直接返回json结果”。每次 OpenCode 输出因 JSON 解析或 JSON schema 校验失败时，scheduler 会在该次 attempt 的 session 中继续追问“不要写文件，按照正确的JSON Schema直接输出”，并再次校验追问返回的 JSON；如果追问结果仍不合法，再进入原有重试流程。

## 命令行启动

推荐使用脚本启动完整威胁分析流程：

```bash
python3 scripts/run_threat_analysis.py \
  --config agent-runtime.json \
  --input /path/to/repository-or-input \
  --artifacts-root artifacts \
  --run-id demo-run
```

默认情况下，脚本会为本次运行选择一个未占用的随机端口，并执行：

```bash
opencode serve --hostname 127.0.0.1 --port <auto-port>
```

然后通过对应的 `http://127.0.0.1:<auto-port>` 连接 opencode server，避免复用旧的 `4096` 进程。

如果 opencode server 已经手动启动：

```bash
python3 scripts/run_threat_analysis.py \
  --config agent-runtime.json \
  --input /path/to/repository-or-input \
  --no-start-opencode
```

如果需要指定多个输入文件或目录，可以重复传入 `--input`：

```bash
python3 scripts/run_threat_analysis.py \
  --config agent-runtime.json \
  --input product.md \
  --input src \
  --input deploy
```

如果高风险模块识别需要独立 batch，可以重复传入 `--high-risk-batch`：

```bash
python3 scripts/run_threat_analysis.py \
  --config agent-runtime.json \
  --input product.md \
  --high-risk-batch src/api src/auth \
  --high-risk-batch src/parser src/protocol
```

常用参数：

- `--opencode-base-url`：opencode server 地址；未传且自动启动 opencode 时使用随机未占用端口。
- `--opencode-command`：启动 opencode serve 的命令字符串；未传 `--opencode-base-url` 时，其中的 `--port` 会被本次运行的随机端口覆盖。
- `--opencode-directory`：opencode 运行的项目目录；runner 会把威胁分析所需的所有 skills 安装到该目录下的 `.opencode/skills/`，并把该路径写入此目录的 `opencode.json` 中的 `skills.paths`。
- `--opencode-password`：basic auth 密码；未传时读取 `OPENCODE_PASSWORD`。
- `--opencode-agent`：发送给 opencode 的 agent 名称。
- `--timeout`：等待每批 agent 任务的超时时间。
- `--delete-session`：任务完成后删除对应 opencode session。
- `--print-progress` / `--no-print-progress`：覆盖配置文件中的 `progress.enabled`，控制是否打印关键步骤进度。

脚本结束后会输出本次 run ID、产物数量和最终 JSON 路径。

也可以在代码中直接组装 pipeline：

```python
from agent_runtime import AgentScheduler, AgentSubmitter, ModelRouter, OpenCodeAgentRunner, load_runtime_config
from threat_analysis_harness.skills import default_skill_paths

config = load_runtime_config("agent-runtime.json")
skill_paths = default_skill_paths()

runner = OpenCodeAgentRunner(
    start_command=("opencode", "serve", "--hostname", "127.0.0.1", "--port", "4096"),
    skill_paths=(
        skill_paths.value_asset_map,
        skill_paths.high_risk_module_map,
        skill_paths.high_risk_module_merge,
        skill_paths.attack_tree_by_asset,
    ),
)

with runner:
    scheduler = AgentScheduler(
        runner=runner,
        model_router=ModelRouter(config),
    )
    with scheduler:
        submitter = AgentSubmitter(scheduler)
        # 将 submitter 传给 ThreatAnalysisPipeline
```

如果用户已经手动启动了 opencode server，可以只连接已有 server：

```python
runner = OpenCodeAgentRunner(
    base_url="http://127.0.0.1:4096",
    start_command=None,
    skill_paths=(
        skill_paths.value_asset_map,
        skill_paths.high_risk_module_map,
        skill_paths.high_risk_module_merge,
        skill_paths.attack_tree_by_asset,
    ),
)
```

未显式传入 `base_url` 时，runner 会把 `--port` 改写为本次运行选出的随机未占用端口。

如果启用了 opencode server basic auth：

```python
runner = OpenCodeAgentRunner(
    base_url="http://127.0.0.1:4096",
    username="opencode",
    password="your-password",
    skill_paths=(
        skill_paths.value_asset_map,
        skill_paths.high_risk_module_map,
        skill_paths.high_risk_module_merge,
        skill_paths.attack_tree_by_asset,
    ),
)
```

runner 每个任务会把完整 prompt 写入 `<output_path>.prompt.txt`。使用 opencode runner 时，程序会通过 `/session/{id}/message` 发送 message 并读取返回的 assistant 文本；如果返回内容不是 assistant 消息，会回查 `/session/{id}/message` 的消息列表。最终 assistant 文本会写入 `<output_path>.raw.txt`；运行框架随后从该最终文本中提取 JSON，完成 JSON schema 校验后，再由程序将规范化 JSON 写入 `output_path`。

## Web 查看页

查看页是静态页面，可直接打开：

```text
web/index.html
```

页面可分别导入以下 JSON 产物：

- `runs/<run_id>/value_assets/final/value-assets.json`
- `runs/<run_id>/high_risk_modules/final/high-risk-module-merge.json`
- `runs/<run_id>/attack_trees/final/attack_trees.json`

导入后，页面会以表格展示价值资产和高风险模块，从攻击树中提取内部节点，并用可展开/收起的树状图展示每棵攻击树。

## 测试

当前测试可通过标准库 `unittest` 运行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
