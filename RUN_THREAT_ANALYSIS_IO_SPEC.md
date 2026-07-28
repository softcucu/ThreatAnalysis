# `run_threat_analysis` 输入输出与兼容实现规范

本文档定义 `run_threat_analysis()` 的公开调用契约、最终产物格式和跨产物一致性要求。适用于：

- 调用仓库现有威胁分析能力的接入方；
- 替换现有模型、运行时或流水线，但仍需保持接口兼容的实现方；
- 独立实现价值资产、高风险模块和攻击树分析，并将结果交给现有调用方或 Web 查看页的实现方。

本文中的“必须”表示兼容要求，“建议”表示不会直接破坏当前接口、但会影响结果质量或可维护性的要求。代码中的最终依据是：

- `src/threat_analysis_harness/threat_analysis.py`：公开函数和返回结果；
- `src/threat_analysis_harness/schemas.py`：最终 JSON 字段 schema；
- `src/threat_analysis_harness/stages/`：合并、规范化和跨产物一致性规则；
- `tests/test_threat_analysis_harness.py`：当前兼容行为。

## 1. 公开函数契约

```python
def run_threat_analysis(
    code_path: str | Path,
    output_path: str | Path,
    is_resume: bool = False,
    product_mcp: str | None = None,
    attack_modes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ...
```

该函数是同步阻塞接口。调用完成后返回可直接执行 `json.dumps()` 的字典，不通过返回值携带 Python 自定义对象。

### 1.1 参数标准

| 参数 | 必填 | 类型 | 当前行为和兼容要求 |
| --- | --- | --- | --- |
| `code_path` | 是 | `str \| pathlib.Path` | 被分析的单个代码仓、目录或输入文件路径。去除首尾空白后不能为空；当前实现会展开 `~`，但不会在入口处预先检查路径是否存在。实际运行时该路径必须存在且对分析进程可读。 |
| `output_path` | 是 | `str \| pathlib.Path` | 本次运行的产物根目录。去除首尾空白后不能为空；允许目录尚不存在，实现必须自动创建所需子目录。分析进程必须具有写权限。 |
| `is_resume` | 否 | `bool` | `False` 表示正常执行，`True` 表示复用同一 `output_path` 中已有的任务 JSON，并继续缺失或不可复用的任务。调用方必须传真实布尔值，不应传 `"false"` 等字符串，因为当前实现使用 `bool(is_resume)`。 |
| `product_mcp` | 否 | `str \| None` | 为产品知识 MCP 预留。当前版本接受但不消费，不得假设它会影响分析结果。兼容实现必须至少能够接收 `None` 或字符串。 |
| `attack_modes` | 否 | `Mapping[str, Any] \| None` | 为私有攻击模式预留。当前版本接受但不消费，不得假设它会影响分析结果。CLI 只接受 JSON 对象；Python 接口应接收映射或 `None`。 |

建议向 `code_path` 和 `output_path` 传绝对路径，避免任务进程和调用进程工作目录不一致造成歧义。

### 1.2 `code_path` 的内容要求

当前入口允许文件或目录。为得到可复核的分析结果，输入至少应包含以下一种内容：

- 可读取的源代码仓，包括源码、路由或接口定义、配置、依赖描述和必要的架构说明；
- 描述系统组件、数据流、外部接口和信任边界的结构化输入文件。

输入应足以回答以下问题：

1. 系统保存、处理或提供了哪些价值资产；
2. 哪些代码模块处理外部输入、敏感数据、安全能力或管理控制接口；
3. 外部暴露模块如何经过内部节点影响价值资产。

分析实现不得修改 `code_path` 中的源文件。若输入缺少代码、接口或数据流证据，应在分析内容中保持保守，不应虚构模块路径或攻击链路。

### 1.3 运行环境要求

使用仓库当前实现时，还必须准备可用的 `task-agent.yaml`、模型资源和以下 skill：

- `value-asset-map`
- `high-risk-module-map`
- `high-risk-module-merge`
- `attack-tree-by-asset`

这些是当前实现的运行依赖，不属于公开返回 JSON 的字段。替代实现可以不使用这些内部组件，但仍必须满足本文的公开返回值和最终产物标准。

## 2. 执行语义

一次完整分析依次形成三类结果：

1. 识别并合并价值资产；
2. 识别并合并高风险模块；
3. 基于最终价值资产和最终高风险模块生成攻击树。

价值资产和高风险模块的初始识别可以并行，但攻击树必须使用前两个阶段的最终结果。替代实现可以改变内部调度方式，不得改变三个最终文件的含义和引用关系。

### 2.1 Resume 语义

`is_resume=True` 时：

- 必须将 `output_path` 视为同一次分析的产物目录；
- 应复用已成功且与当前格式兼容的中间结果；
- 缺失、损坏或格式不兼容的结果必须重新生成；
- 最终仍须返回完整的成功或失败响应。

当前实现不会为 `code_path`、模型配置或分析规则生成输入指纹。因此，调用方不得使用同一个 `output_path` 恢复另一个代码仓或一组已经变化的输入；否则可能复用陈旧结果。兼容实现建议增加输入指纹校验。

### 2.2 异常和部分产物

当前公开函数会捕获执行中的普通异常并返回失败对象。失败前已经写入的 raw 或 final 文件可能保留，供问题排查或后续 resume 使用。因此：

- 不得将“目录中存在部分 JSON”当作整次分析成功；
- 只有返回对象中的 `result` 严格等于 `True`，并且三个成功路径均存在时，才能按成功处理；
- 失败原因应可定位，但调用方不应把 `reason` 原样暴露给不可信终端用户。

## 3. 返回对象标准

### 3.1 成功

成功时必须返回：

```json
{
  "result": true,
  "value_asset_path": "/output/value_assets/final/value-assets.json",
  "attack_tree_path": "/output/attack_trees/final/attack_trees.json",
  "high_risk_modules_path": "/output/high_risk_modules/final/high-risk-module-merge.json"
}
```

字段要求：

| 字段 | 类型 | 要求 |
| --- | --- | --- |
| `result` | boolean | 必须严格为 `true`。 |
| `value_asset_path` | string | 指向已存在的最终价值资产 JSON。 |
| `high_risk_modules_path` | string | 指向已存在的最终高风险模块 JSON。注意字段名是复数 `modules`。 |
| `attack_tree_path` | string | 指向已存在的最终攻击树 JSON。 |

路径字符串沿用 `output_path` 的绝对或相对形式。实现方不得返回目录路径、尚未写入的预期路径或临时文件路径。

### 3.2 失败

失败时必须返回：

```json
{
  "result": false,
  "reason": "可定位的失败原因"
}
```

字段要求：

| 字段 | 类型 | 要求 |
| --- | --- | --- |
| `result` | boolean | 必须严格为 `false`。 |
| `reason` | string | 非空、可读，并指出主要失败阶段或输入错误。 |

空路径的当前错误形式分别是 `code_path is required` 和 `output_path is required`。失败返回不应伪造三个成功产物路径。

### 3.3 命令行映射

包内 CLI 最终将上述字典以 UTF-8 JSON 输出到标准输出：

- `result=true`：退出码 `0`；
- `result=false`：退出码 `1`；
- 用户中断：退出码 `130`，并返回 `{"result": false, "reason": "Interrupted."}`。

CLI 的 `--attack-modes` 必须是一个 JSON 对象字符串，不能是数组或标量。

## 4. 最终文件和目录标准

成功运行必须形成以下三个最终文件：

```text
<output_path>/
  value_assets/
    final/
      value-assets.json
  high_risk_modules/
    final/
      high-risk-module-merge.json
  attack_trees/
    final/
      attack_trees.json
```

当前流水线还会使用以下内部目录：

```text
<output_path>/
  value_assets/raw/
  high_risk_modules/raw/
  attack_trees/raw/
```

最终文件必须：

- 使用 UTF-8 编码；
- 是独立、合法的 JSON 文档，不能包含 Markdown 代码围栏或解释文字；
- 保留中文字段名原文；
- 不得出现 `NaN`、`Infinity` 等非标准 JSON 值；
- 在返回 `result=true` 前完成写入。

raw 目录不是外部消费者的必需输入。若替换的只是当前 runtime、submitter 或模型，仍需遵循任务下发时携带的动态 schema 和 raw 输出路径，以保持 resume 能力。

## 5. 最终价值资产文件

文件：`value_assets/final/value-assets.json`

顶层必须是数组。允许空数组。每个数组项必须只包含下列字段，不允许额外字段。

| 字段 | JSON 类型 | 约束 |
| --- | --- | --- |
| `资产名` | string | 必填，非空。应是稳定、可识别的资产名称。 |
| `资产类别` | string | 必填，只能是 `数据资产`、`软件资产`、`硬件资产`、`服务资产` 之一。 |
| `资产描述` | string | 必填，非空。说明资产内容、用途或所在边界。 |
| `攻击损失` | string | 必填，非空。说明机密性、完整性、可用性、隐私、合规或业务影响。 |
| `判断为价值资产的原因` | string | 必填，非空。应包含来自输入仓或文档的判断依据。 |

当前流水线按“资产类别 + 去空白且不区分大小写的资产名”去重。同一资产的描述、损失和判断原因会合并。因此，独立实现也应避免仅因空白或大小写不同产生重复资产。

示例：

```json
[
  {
    "资产名": "用户个人数据",
    "资产类别": "数据资产",
    "资产描述": "系统处理和保存的用户身份信息。",
    "攻击损失": "数据泄露可能导致隐私和合规风险。",
    "判断为价值资产的原因": "用户资料代码和查询接口体现系统保存并处理用户身份信息。"
  }
]
```

## 6. 最终高风险模块文件

文件：`high_risk_modules/final/high-risk-module-merge.json`

顶层必须是数组。允许空数组。每个数组项必须只包含下列字段，不允许额外字段。

| 字段 | JSON 类型 | 约束 |
| --- | --- | --- |
| `模块名称` | string | 必填，非空。多个阶段引用该模块时必须使用同一规范名称。 |
| `代码目录` | string 或 string[] | 必填。数组中的每个字符串必须非空；语义上至少应提供一个可定位的代码路径。 |
| `面临威胁` | string | 必填，非空。 |
| `是否涉及设备或系统对外提供管理和控制接口相关的代码` | string | 必填，只能是 `是` 或 `否`。 |
| `是否涉及对不可信来源数据进行解析或处理的代码` | string | 必填，只能是 `是` 或 `否`。 |
| `是否涉及安全相关类代码(如，认证、授权、接入控制、加解密、密钥管理、日志审计、软件完整性保护等模块)` | string | 必填，只能是 `是` 或 `否`。字段名中的标点必须完全一致。 |
| `是否涉及个人数据或者敏感数据的代码` | string | 必填，只能是 `是` 或 `否`。 |
| `是否涉及web相关处理` | string | 必填，只能是 `是` 或 `否`。注意字段名中的 `web` 为小写。 |
| `是否外部暴露面` | string | 必填，只能是 `是` 或 `否`。该字段决定模块能否作为攻击树叶子节点。 |
| `判断为高风险模块的原因` | string | 必填，非空，应给出代码路径、接口或数据处理证据。 |

“是否外部暴露面”必须依据外部接口、外部输入或可达路径判断，不能因为模块风险高就自动填写“是”。

示例：

```json
[
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
    "判断为高风险模块的原因": "src/auth 处理外部登录请求、用户凭据和令牌签发。"
  }
]
```

## 7. 最终攻击树文件

文件：`attack_trees/final/attack_trees.json`

顶层必须是一个对象，并且只能包含 `attack_trees`：

```json
{
  "attack_trees": []
}
```

正常的单资产攻击树任务至少产生一棵树；最终合并文件在以下情况下允许 `attack_trees` 为空：

- 最终价值资产数组为空；
- 所有攻击路径都因引用非外部暴露叶子模块而被裁剪。

只要存在树，每棵树都必须满足以下结构。

### 7.1 攻击树对象

| 字段 | JSON 类型 | 约束 |
| --- | --- | --- |
| `tree_id` | string | 必填，非空；在文件内应唯一。 |
| `value_asset` | object | 必填，结构见下表。 |
| `nodes` | array | 必填，至少 2 项。 |
| `edges` | array | 必填，至少 1 项。 |
| `attack_paths` | array | 必填，至少 1 项。 |

`value_asset` 只能包含：

| 字段 | JSON 类型 | 约束 |
| --- | --- | --- |
| `asset_name` | string | 非空，必须等于最终价值资产中的 `资产名`。 |
| `asset_category` | string | 必须等于对应资产的 `资产类别`，且属于四个合法枚举值。 |
| `asset_description` | string | 非空，必须等于对应资产的 `资产描述`。 |
| `attack_loss` | string | 非空，必须等于对应资产的 `攻击损失`。 |

### 7.2 节点 `nodes`

每个节点只能包含：

| 字段 | JSON 类型 | 约束 |
| --- | --- | --- |
| `node_id` | string | 必填，非空；同一棵树内必须唯一。 |
| `node_type` | string | 必须是 `根节点`、`内部节点`、`叶子节点` 之一。 |
| `node_name` | string | 必填，非空。 |
| `description` | string | 必填，非空。 |
| `module_name` | string 或 null | 关联高风险模块时填写规范模块名；根节点必须为 `null`。 |
| `is_high_risk_module` | boolean | 是否对应最终高风险模块。 |
| `external_exposure` | boolean | 是否为外部暴露模块；必须与高风险模块文件一致。 |
| `external_interface_description` | string 或 null | 外部暴露节点应说明接口或外部输入；不适用时填 `null`。 |

节点语义必须满足：

- 每棵树应有且仅有一个根节点；
- 根节点名称必须是 `攻击价值资产：<资产名>`，且 `module_name=null`、`is_high_risk_module=false`、`external_exposure=false`、`external_interface_description=null`；
- 叶子节点必须对应最终高风险模块，且该模块的 `是否外部暴露面` 必须为 `是`；
- 属于高风险模块的内部节点，其 `node_name` 和 `module_name` 必须使用最终高风险模块的规范 `模块名称`；
- 不属于最终高风险模块的普通内部节点必须使用 `is_high_risk_module=false` 和 `external_exposure=false`。

最终公开文件中不得包含内部字段 `module_id`。

### 7.3 边 `edges`

每条边只能包含：

| 字段 | JSON 类型 | 约束 |
| --- | --- | --- |
| `edge_id` | string | 必填，非空；同一棵树内必须唯一。 |
| `source_node_id` | string | 必填，非空，必须引用当前树中的节点。 |
| `target_node_id` | string | 必填，非空，必须引用当前树中的节点。 |
| `influence_type` | string | 必须是 `调用`、`数据传递`、`消息传递`、`控制`、`依赖`、`直接影响` 之一。 |
| `description` | string | 必填，非空，说明影响关系。 |

边的方向应从外部入口逐步指向受影响的内部节点和价值资产根节点。

### 7.4 攻击路径 `attack_paths`

每条路径只能包含：

| 字段 | JSON 类型 | 约束 |
| --- | --- | --- |
| `path_id` | string | 必填，非空；同一棵树内应唯一。 |
| `path_name` | string | 必填，非空。 |
| `node_ids` | string[] | 必填，至少 2 项；所有 ID 必须存在于当前树的 `nodes`。 |
| `edge_ids` | string[] | 必填，至少 1 项；所有 ID 必须存在于当前树的 `edges`。 |
| `path_description` | string | 必填，非空，应按攻击传播顺序描述。 |
| `related_high_risk_modules` | array | 必填，至少 1 项。 |
| `attack_patterns` | array | 必填，允许空数组。 |

`node_ids` 和 `edge_ids` 应按外部入口到价值资产根节点的顺序排列，并形成连续路径。

每个 `related_high_risk_modules` 项只能包含：

| 字段 | JSON 类型 | 约束 |
| --- | --- | --- |
| `module_name` | string | 必填，非空，必须等于最终高风险模块的规范名称。 |
| `node_id` | string | 必填，非空，必须同时出现在当前路径的 `node_ids` 中。 |
| `external_exposure` | boolean | 必须与最终高风险模块的 `是否外部暴露面` 一致。 |
| `path_role` | string | 必须是 `外部攻击入口`、`内部影响模块`、`直接资产影响模块` 之一。 |
| `association_description` | string | 必填，非空。 |

同一路径中同一个高风险模块只应出现一次。最终公开文件的关联对象中同样不得包含 `module_id`。

每个 `attack_patterns` 项只能包含：

| 字段 | JSON 类型 | 约束 |
| --- | --- | --- |
| `pattern_id` | string | 必填；建议非空并使用稳定编号。 |
| `pattern_name` | string | 必填；建议非空。 |
| `association_description` | string | 必填，非空，说明模式与当前路径的对应关系。 |

### 7.5 完整最小示例

```json
{
  "attack_trees": [
    {
      "tree_id": "AT-001",
      "value_asset": {
        "asset_name": "用户个人数据",
        "asset_category": "数据资产",
        "asset_description": "系统处理和保存的用户身份信息。",
        "attack_loss": "数据泄露可能导致隐私和合规风险。"
      },
      "nodes": [
        {
          "node_id": "L-001",
          "node_type": "叶子节点",
          "node_name": "用户认证模块",
          "description": "处理外部登录请求和用户凭据。",
          "module_name": "用户认证模块",
          "is_high_risk_module": true,
          "external_exposure": true,
          "external_interface_description": "登录接口接收外部 HTTP 请求。"
        },
        {
          "node_id": "R-001",
          "node_type": "根节点",
          "node_name": "攻击价值资产：用户个人数据",
          "description": "导致用户个人数据泄露或被越权使用。",
          "module_name": null,
          "is_high_risk_module": false,
          "external_exposure": false,
          "external_interface_description": null
        }
      ],
      "edges": [
        {
          "edge_id": "E-001",
          "source_node_id": "L-001",
          "target_node_id": "R-001",
          "influence_type": "直接影响",
          "description": "认证绕过会直接造成用户数据越权访问。"
        }
      ],
      "attack_paths": [
        {
          "path_id": "AP-001",
          "path_name": "认证入口影响用户个人数据",
          "node_ids": ["L-001", "R-001"],
          "edge_ids": ["E-001"],
          "path_description": "用户认证模块 -> 攻击价值资产：用户个人数据",
          "related_high_risk_modules": [
            {
              "module_name": "用户认证模块",
              "node_id": "L-001",
              "external_exposure": true,
              "path_role": "外部攻击入口",
              "association_description": "该模块是接收外部请求的登录入口。"
            }
          ],
          "attack_patterns": []
        }
      ]
    }
  ]
}
```

## 8. 三类产物的跨文件一致性

仅满足单文件 JSON 类型还不够。兼容实现必须同时满足：

1. 每棵攻击树的 `value_asset` 必须来自最终价值资产文件，四个映射字段完全一致；
2. 攻击树叶子节点必须能匹配最终高风险模块，且只能匹配 `是否外部暴露面="是"` 的模块；
3. 标记为高风险模块的内部节点必须能匹配最终高风险模块；
4. `related_high_risk_modules.module_name` 必须使用最终高风险模块的规范名称；
5. `related_high_risk_modules.node_id` 必须属于当前攻击路径；
6. 路径引用的节点和边必须存在于同一棵树；
7. 根节点、叶子节点、边和路径必须共同构成从外部攻击入口到价值资产的可解释链路；
8. 最终公开攻击树不得泄露仅用于内部对齐的 `module_id`。

当前流水线会自动规范化资产名称、根节点和高风险模块名称；无法匹配的叶子节点或关联模块会导致一致性错误。叶子节点引用非外部暴露模块时，当前实现会先在原会话中修正一次；仍未修正的路径会被裁剪。

## 9. 内部任务 `module_id` 与公开输出的区别

当前攻击树 raw 任务使用动态 schema，要求节点和 `related_high_risk_modules` 携带程序生成的 `module_id`，用于消除模块同名或模型别名造成的歧义：

- 根节点和普通内部节点使用 `module_id=null`；
- 叶子节点及高风险内部节点使用任务提示中给定的 `module_id`；
- 关联高风险模块也使用同一个 `module_id`；
- 最终文件写入前必须移除全部 `module_id`。

因此：

- 实现当前 pipeline 的 runtime/submitter 时，必须服从每个任务实际携带的动态 `output_schema`；
- 只实现本文公开最终产物时，不应自行向最终 JSON 添加 `module_id`；
- 旧的、不含内部 `module_id` 的 raw 攻击树不能直接用于当前版本的 resume。

## 10. 实现验收清单

提交兼容实现前，至少验证以下场景：

- 空 `code_path` 和空 `output_path` 返回 `result=false` 及非空 `reason`；
- 正常运行返回 `result=true` 和三个路径，且路径指向实际存在的 UTF-8 JSON 文件；
- 三个最终文件的顶层类型、必填字段、枚举值和“禁止额外字段”要求全部通过；
- 价值资产和高风险模块允许合法空数组；
- 最终攻击树允许合法空集合；存在树时，每棵树至少有 2 个节点、1 条边和 1 条攻击路径；
- 所有节点、边、路径 ID 引用有效且在各自作用域内唯一；
- 攻击树资产、高风险节点和关联模块均能与前两个最终文件对齐；
- 非外部暴露模块不会成为叶子节点；
- 最终攻击树中不存在 `module_id`；
- `is_resume=True` 能在同一输入和同一 `output_path` 上复用已有结果，并补齐缺失结果；
- 任一阶段失败时返回失败对象，不把部分产物误报为成功；
- 返回字典和三个文件均可由标准 JSON 解析器解析。

仓库当前 schema 可用于自动校验非空攻击树及单项结构：

```python
import json
from pathlib import Path

from threat_analysis_harness.output_validation import validate_json_schema
from threat_analysis_harness.schemas import (
    ATTACK_TREE_SCHEMA,
    HIGH_RISK_MODULES_SCHEMA,
    VALUE_ASSETS_SCHEMA,
)

value_assets = json.loads(Path(value_asset_path).read_text(encoding="utf-8"))
high_risk_modules = json.loads(
    Path(high_risk_modules_path).read_text(encoding="utf-8")
)
attack_trees = json.loads(Path(attack_tree_path).read_text(encoding="utf-8"))

validate_json_schema(value_assets, VALUE_ASSETS_SCHEMA)
validate_json_schema(high_risk_modules, HIGH_RISK_MODULES_SCHEMA)

# ATTACK_TREE_SCHEMA 要求 attack_trees 至少一项；最终合法空集合需单独接受。
if attack_trees.get("attack_trees"):
    validate_json_schema(attack_trees, ATTACK_TREE_SCHEMA)
elif attack_trees != {"attack_trees": []}:
    raise ValueError("invalid empty attack tree payload")
```
