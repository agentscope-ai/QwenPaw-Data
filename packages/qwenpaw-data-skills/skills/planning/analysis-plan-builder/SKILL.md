---
name: analysis-plan-builder
description: 将用户的数据分析需求转化为结构化分析计划，并调用 PlanCreate 落盘。已有未完成计划且仍是同一分析时不要调用。用户提出 BI 业务分析、数据探索、统计建模等需要规划的分析任务时使用。简单数据查询、公式明确的定量计算等无需规划的任务不使用。
---
# analysis-plan-builder

---

## 设计原则：行动优先

用户发出分析请求后最差的体验是"agent 一连串提问、什么都没做"。本 skill 要求：先看 `<current_plan>`。已有未完成任务且仍是同一分析时，续做或 `PlanUpdate`，本 skill 到此结束。没有可用计划或目标变了时，先收集上下文，再生成计划草稿，信息充分时必须调用 `PlanCreate` 落盘，再进入执行；仅当关键信息缺失且无法用工具消歧时，才用 `ask_user_question` 反问。  
“行动优先”不等于跳过计划：需要新计划时，`PlanCreate` 是进入执行前的必经动作。禁止只输出 YAML/正文计划后直接取数或分析。反问不得写成助手正文清单。

---

## 前置条件

开始规划前，确认以下信息已就绪：

- 用户问题的任务类型（如 BI 业务分析、数据探索、统计建模等）

## Step 1: 上下文构建（Phase 1 — Context Gathering）

收集和补充构造分析计划所需的信息。**本步骤应尽早执行，在调用 `ask_user_question` 之前先通过图谱 / 探查工具获取尽可能多的上下文。**

### 1.1 可用资源确认

确认当前可用的资源：

- 用户提供的数据文件或表
- 数据获取工具 / API
- 静态的域知识包
- 可获取业务领域模型的接口（后文称"语义层"），即能查询指标、维度及其关系的服务或工具

已知参考文件路径时直接用 Read，长文件按需分段读取。Grep 启动失败时改用 Read 查看目标文件；工具失败不等于文件不存在或内容无匹配。

**如果工具列表中包含 Context Manager 图谱工具**，加载 `Skill(skill="cm-skills")`，按其规则获取规划所需的指标定义、完整口径、数据集及维度关系。需要业务域全貌时使用其中的「业务域列表」配方。

**实时向用户同步发现**：将图谱返回的关键信息（指标名、口径定义、可用维度、数据表）以简洁文本输出到 assistant 消息。用户可随时打断纠偏。

如果没有图谱工具可用 → 跳过语义层调用，直接进入 1.2。

### 1.2 按任务类型补充上下文

- **BI 业务分析** → 继续 1.3 ~ 1.5
- **其他任务类型**（数据探索、统计建模等）→ 继续 1.6

---

### BI 业务分析

若用户已明确指定了分析的指标（如"分析 DAU 变化"），可跳过 1.3 和 1.4，直接到 1.5 确认指标角色。

#### 1.3 业务上下文识别

判定业务类型（ToB / ToC / ToD / Mixed）和业务子类型（如ToC 下的工具类 / 内容社区 / 电商 / 游戏），判定依据参考 `references/bi-business-type-guide.md`

#### 1.4 分析范围确认

1. 根据业务类型和子类型，读取 `references/modules` 下对应的分析目录文件：

| 业务类型 + 子类型 | 文件                 |
| ----------------- | -------------------- |
| ToC 工具类        | `toc-tool.md`      |
| ToC 内容社区类    | `toc-content.md`   |
| ToC 电商交易类    | `toc-ecommerce.md` |
| ToC 游戏类        | `toc-game.md`      |
| ToB 产品类        | `tob-product.md`   |
| ToD 开发者社区    | `tod-developer.md` |

2. 根据用户问题匹配具体的分析模块和分析内容：

- **命中分析内容条目**（如"用户留存"）→ 仅匹配该条目
- **命中分析模块**（如"用户趋势与规模"）→ 匹配该模块全量条目
- **语义意图匹配**（如"增长分析"）→ 匹配相关模块的相关条目
- **宽泛问题**（如"分析 3 月数据"）→ 匹配全量模块和条目

#### 1.5 指标细化

根据匹配的分析内容条目，参照 `references/bi-metric-specification.md`，确定每个条目涉及的具体指标。

### 非 BI 任务（数据探索、统计建模等）

#### 1.6 数据探查

对用户提供的数据进行初步探查，建立对数据的基本理解：

- 读取数据 schema（字段名、类型、数量）
- 查看数据规模（行数、时间跨度等）
- 采样查看数据内容，识别数据特征（缺失值、异常值、分布形态等）
- 结合用户问题，判断哪些字段/特征与分析目标相关

---

## Step 2: 生成分析计划

将 Step 1 的结果组织为结构化的分析计划。

### BI 业务分析的计划

包含以下内容：

- **意图**：目标、分析对象、问题类型
- **已知条件**：时间范围、数据来源、用户提到的指标/维度、约束条件
- **额外上下文**：可用数据接口（取数或者语义层工具/API）、域知识约束（如有）
- **缺失信息**：已识别但未解决的信息缺口
- **分析内容**：每个分析模块的名称、分析条目、指标（名称、角色）

示例 — 用户："分析 产品A 3 月的访问趋势和对话情况"

```yaml

task_type: bi_analysis

intent:
  goal: "分析访问趋势和对话情况"
  subject: "产品A"
  question_type: what

known_conditions:
  period: { start: "2026-03-01", end: "2026-03-31" }
  metric_mentions: ["访问用户数", "对话次数"]

context:
  business: { type: "ToC", sub_type: "工具类", domain: "product_a" }
  data: { semantic_layer: available, apis: ["semantic-layer", "fetch-data"] }
  domain_constraints: ["率值变动使用 pt 表达", "归因贡献度正/负各取 Top 3"]

scope:
  modules:
    - module_name: "用户趋势与规模"
      analysis_items: ["用户规模", "增长趋势", "增长归因"]
      	metrics:
        - name: "访问用户数"
          roles: [north_star]
        - name: "新增用户数"
          roles: [display]
    - module_name: "使用行为及体验"
      analysis_items: ["功能使用分析"]
      metrics:
        - name: "人均对话次数"
          roles: [north_star]
        - name: "对话次数"
          roles: [display]
```

### 非 BI 任务的计划

非 BI 任务没有固定的模块体系，需要根据用户问题与给定数据明确*数据范围*、*条件约束*以及识别*未解决的信息缺口*等前置信息，随后将用户问题分解为一系列可执行的原子子任务。每个子任务应当是具体的、可执行的操作（数据清洗、计算、检验、可视化等），而非笼统的分析方向。

未解决的信息缺口主要关注：

- **数据范围的歧义**。例如，无法从问题和数据模式中准确推断出需要分析的数据子集。
- **语义的歧义**。例如，数据中包含的数据列的含义不明确。

问题分解需要依照以下原则：

- 子任务必须包含可执行的数据检查、数据转换或计算操作；
- 子任务能够通过使用数据分析或数据科学库（如 pandas、numpy、scipy）执行 Python 代码来实现；
- 子任务考虑必要的数据清洗和预处理步骤（例如，缺失值的预处理/插补）；

最终，生成结构化的分析计划，计划包含：

- **意图**：目标、分析对象、现象、问题类型
- **已知条件**：数据来源、约束条件
- **数据概况**：数据探查发现的关键信息（schema、数据量、数据质量等）
- **子任务序列**：按执行顺序排列的原子任务，需考虑：
  - 必要的数据清洗和预处理（缺失值处理、类型转换、异常值过滤等）
  - 具体的计算或变换（分组统计、分布计算、相关性分析等）
  - 可执行的检验或判断（阈值检查、统计检验等）
- **缺失信息**：已识别但未解决的信息缺口

示例 — 用户："帮我看看 user_behavior.csv，为什么 age 列分布这么不均匀？"

```yaml
task_type: data_exploration

intent:
  goal: "分析 age 列分布不均的原因"
  subject: "user_behavior.csv 的 age 列"
  phenomenon: "分布不均"
  question_type: why

known_conditions:
  data_source: "user_behavior.csv"

data_profile:
  rows: 50000
  columns: 12
  age_column: { type: int, missing_rate: 3.2%, range: "0-120", median: 28 }

tasks:
  - "过滤 age 列的缺失值和明显异常值（<=0 或 >120），记录过滤比例"
  - "计算 age 列的分布直方图（bin=5），识别峰值和异常聚集区间"
  - "按 source 字段分组，分别计算各渠道的 age 均值和分布，对比差异"
  - "计算 age 与 registration_date 的相关性，判断是否存在注册批次效应"
  - "对分布不均的区间（如 age=0 或 age>100），抽样查看原始记录，判断数据质量问题"

missing_info:
  - "不清楚 age 列的业务含义（用户年龄？账号年龄？）"
```

---

## 产出（强制）

Step 2 的 YAML 只是**内部草稿**，不是交付终点。本 skill 的硬性出口：

1. 完成信息充分性检查（见下）
2. 需要新计划时 **必须调用** `PlanCreate`，把草稿收敛为工具入参；同一分析续做不要调用
3. 再进入 Phase 3

禁止：只把 YAML/要点写在助手正文里就去调 MCP 取数、跑分析 skill，或结束本轮。

### 收敛为 `PlanCreate` 入参

- `tasks`：将分析内容拆成 `{id, subject, description, blocked_by}` 对象；`id`
  在当前 Plan 内唯一且稳定，`blocked_by` 只引用同一批 Task ID。
  目标、范围、关键条件和必要上下文写入各 Task 的 `description`，不再单独传
  Plan 级 `name` / `description`。
- BI：按分析条目 / 取数与计算步骤拆 Task（取数、指标观测、维度下拆、归因等可分可合，但须可执行）。
- 非 BI：把草稿中的子任务序列改写成上述对象结构。

不要把草稿中的 `task_type`、`intent`、`known_conditions`、`context`、
`data_profile`、`scope` 或 `missing_info` 作为额外参数传给 `PlanCreate`。

### BI → `PlanCreate` 示例

用户："产品A 2025 年 12 月的访问趋势分析，访问指标只看 DAU，维度下拆只看国家。"  
（用户已给出对象 / 时间 / 指标 / 下拆维度 → `missing_info` 应为空，直接 `PlanCreate`。）

```json
{
  "tasks": [
    {
      "id": "fetch_dau",
      "subject": "取数：产品A 2025-12 DAU（含国家维度）",
      "description": "按 fetch-data 拉取 产品A 在 2025-12-01～2025-12-31 的 DAU 日序列，并带上国家维度字段；落盘到当前 Session 产物目录。",
      "blocked_by": []
    },
    {
      "id": "observe_trend",
      "subject": "观测：DAU 整体趋势",
      "description": "基于已取数，对全站/合计 DAU 做时间趋势观测（水平、波动、异常点），回答 12 月访问趋势。",
      "blocked_by": ["fetch_dau"]
    },
    {
      "id": "drill_country",
      "subject": "下拆：按国家看 DAU",
      "description": "仅按国家维度下拆 DAU，比较各国贡献与变化；不做其他维度下拆。",
      "blocked_by": ["fetch_dau"]
    }
  ]
}
```

### 信息充分性检查（强制）

交付草稿并调用 `PlanCreate` 前，检查 `missing_info`：

- **`missing_info` 为空** → **必须立即调用 `PlanCreate`**
- **`missing_info` 不为空** → **不应 `PlanCreate`**，先 `ask_user_question` 反问（按系统提示的反问规则）；禁止正文清单代问

约束：

- 当且仅当缺失项**无法通过工具自行获取**时才写入 `missing_info` 并反问。图谱搜索或 `query_graph` 可消歧时先调工具，不要停下来问。
- 用户已明确给出的指标、维度、时间范围、业务对象**不得**再当作 `missing_info` 去反问。
- 不要为了“看起来谨慎”而编造缺失项，从而跳过 `PlanCreate`。

### Phase 3 — PlanCreate 后的行为（强制）

`PlanCreate` 完成后，根据系统提示「人机交互」第2类：

- **第2类未触发**（默认）→ 输出 1-2 句 plan 概览，**立即** `TaskStateUpdate` 将第一个待执行 Task 设为 `in_progress`。
- **第2类触发** → 输出详细计划，再 `ask_user_question` 确认是否开始执行，等待工具结果。

进入执行阶段后，按系统提示 `<qwenpaw-data-analysis-environment>` 落盘，再按计划节点加载对应 workflow / atomic skill。
