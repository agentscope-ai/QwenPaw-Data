# 可沉淀项识别器

从数据分析对话中抽取可沉淀的语义知识（指标口径、维度定义、列含义、数据集用法）。

## 什么该沉淀

应沉淀跨场景可复用、影响取数正确性的语义知识。以用户在对话中新提出、补充或纠正的业务定义、列含义和选表规则为依据，结合 Agent 查询结果、对话上下文和 SQL 补齐相关信息。

**不应沉淀**：

- 一次性分析或临时展示需求
- 业务事件记录（如活动上线、版本发布、服务故障）
- 分析方法论（分析步骤本身，而非指标、维度、列或表的定义）

## 抽取原则

每条沉淀项须满足：**单一主体、单一事实**。即围绕一个主体，仅表述与其直接相关的一件可沉淀知识。

- **单一主体**：每条提取结果只描述一个指标、维度、物理列（含所属表）或数据集。
- **单一事实**：每条提取结果表达一项完整知识（计算口径、维度映射、列含义或场景下的表选型）。同一事实的多段说明合并，不同事实分别提取。
- **字段边界**：每条提取结果仅填写其「字段说明」所允许的字段；不得将应属其它类型的信息写入当前字段。字段名必须使用下文英文 key。
- **字段齐全**：各类型「字段说明」中的字段均须填齐且非空；对话不足以填齐时整条不提取，不得用占位或猜测补全。
- **会话内去重**：相对「本会话已提取过的项」，同一主体的相同内容不再提取；用户补充或纠正的内容仍可提取。
- **相对语义层去重**：对话中 `query_graph` 返回的指标口径、维度定义、列说明或数据集用法属于库中已有知识。若待提取项与这些知识在主体及实质含义上等价，则不提取；存在实质差异者仍可提取。
- **空结果**：无可沉淀项时，返回的 `items` 须为空数组。

## 可沉淀项类型

共 4 类，每条提取结果属于且只属于其中一类：

| type               | 含义       |
| ------------------ | ---------- |
| `metric_caliber` | 指标口径   |
| `dimension_def`  | 维度定义   |
| `column_meaning` | 列含义     |
| `dataset_usage`  | 数据集用法 |

下文按类型说明「何时提取」与各字段怎么填。每条只填该类型列出的字段；`domain` 填写规则见「可用业务域」。

### metric_caliber（指标口径）

**何时提取**：用户定义、补充或纠正了某业务指标的计算方式。主体是指标本身，不是某次分析标题。

**字段说明**：

- **metric_name**：对话中对应指标的名称。补充或纠正已有指标口径时沿用原名，计算规则写入 `caliber`。用户定义新指标时保留其名称。
- **caliber**：用业务语言写清**跨场景可复用**的计算方式。可保留定义性规则（如「排除游客」「退款以负数抵扣」）；**不要**写入本次分析切片或个案条件。
- **table**：口径所依据的物理表（事实表；勿把仅用于本次 JOIN 过滤的维表塞进口径叙事）。
- **formula_sql**：与 `caliber` 同范围的可复用 SQL；须用原始物理表名，不得用 CTE 别名。Agent 给出的完整查询常含本次切片——**必须先剥离不可写条件再写入**，禁止原样照抄整段 `WHERE`。
- **domain**：见「可用业务域」。

**示例对话**：

- Agent 查询得到指标「日活用户数」，已有口径为按 user_id 去重计数。
- 用户纠正："不算游客，要登录过的"
- Agent 更新计算 SQL：`SELECT COUNT(DISTINCT user_id) FROM dws_app.dws_user_login_di WHERE is_guest = 0`

```json
{"type": "metric_caliber", "fields": {"metric_name": "日活用户数", "caliber": "去重登录用户数，排除游客(is_guest=0)", "domain": "some_product", "table": "dws_app.dws_user_login_di", "formula_sql": "SELECT COUNT(DISTINCT user_id) FROM dws_app.dws_user_login_di WHERE is_guest = 0"}}
```

- 用户："成交额按订单实付金额求和，退款以负数抵扣。这次只看华东区，并排除这些测试门店。"
- Agent SQL 含「成交额求和 + 退款为负 + 本次只要华东区、排除某测试门店编码」→ 只留可复用部分

```json
{"type": "metric_caliber", "fields": {"metric_name": "成交额", "caliber": "订单实付金额求和；退款以负数形式自动扣除", "domain": "some_product", "table": "dwd_trade.dwd_order_di", "formula_sql": "SELECT SUM(pay_amount) FROM dwd_trade.dwd_order_di"}}
```

### dimension_def（维度定义）

**何时提取**：用户提出、补充或纠正了分析维度的定义，且对话中有对应的物理列映射。

**字段说明**：

- **dimension_name**：对话中对应维度的名称。补充或纠正已有维度定义时沿用原名，取值说明写入 `value_samples`。用户定义新维度时保留其名称。
- **bind_column**：映射到的物理列名。
- **value_samples**：主要枚举值即可，不要求穷尽。
- **table**：该维度所在物理表。
- **domain**：见「可用业务域」。

**示例对话**：

- 用户："客户端维度用 platform 字段，取值是 Android、iOS、Web"
- Agent：`GROUP BY platform`

```json
{"type": "dimension_def", "fields": {"dimension_name": "客户端", "bind_column": "platform", "value_samples": "Android, iOS, Web", "domain": "some_product", "table": "dws_app.dws_user_login_di"}}
```

### column_meaning（列含义）

**何时提取**：用户补充或纠正了某一物理列的业务含义、枚举值或派生/转换逻辑，且对话中尚未说明相同内容。

**字段说明**：

- **column_name**：物理列名。
- **meaning**：该列的业务含义、枚举/编码释义，或如何从源字段派生。解释具体取值时，先标出值（如值 \`some_val\`），再写含义，勿把取值直接埋进叙述句中间。
- **table**：该列所在物理表。
- **domain**：见「可用业务域」。

**示例对话**：

- 用户："status=3 是已发货，=5 是已签收"

```json
{"type": "column_meaning", "fields": {"column_name": "status", "meaning": "值 `3` 代表已发货；值 `5` 代表已签收", "domain": "some_product", "table": "ods_logistics.ods_parcel_df"}}
```

### dataset_usage（数据集用法）

**何时提取**：用户明确了某类分析应使用哪张表，且对话中尚未说明相同用法。

**字段说明**：

- **use_case**：适用的分析/业务场景。
- **recommended_dataset**：表名或数据集标识。
- **domain**：见「可用业务域」。

**示例对话**：

- 用户："看留存别用明细表，太慢了，用汇总表"

```json
{"type": "dataset_usage", "fields": {"use_case": "用户留存分析", "recommended_dataset": "ads_app.ads_user_retain_summary", "domain": "some_product"}}
```

## 可用业务域

用户输入会附带「可用业务域」列表：语义层里已有的域名，供填写 `domain`。

- 每条提取结果的 `domain` **必须从中选一个**，与列表原文完全一致，禁止缩写/意译/自造。

## 常见错误（反例）

下列模式违反抽取原则。各条先述规则，再给对照示例；正确形态亦可参照各类型「示例对话」。

### 1. 补充口径时自行改名

用户补充「活跃店铺数不含测试店」的口径时，应沿用「活跃店铺数」，将新增规则写入 `caliber`。

- **错误**：`metric_name = "活跃店铺数(不含测试店)"`
- **正确**：`metric_name = "活跃店铺数"`，不含测试店写入 `caliber`

若用户明确另行定义「有效店铺数」，则保留「有效店铺数」，不因计算规则相关而改回「活跃店铺数」。

### 2. 字段越界

将本类型不应承载的信息写入当前字段 → 应拆分为独立项，或改写入对应类型，或整条不提取。

- **错误**（`dataset_usage`）：`recommended_dataset` 中夹带粒度限制或过滤条件。
- **正确**：`recommended_dataset` 仅填表名或数据集标识；粒度与过滤条件拆至其它类型。

### 3. 类型误判

将列的枚举取值或业务编码填入 `recommended_dataset` → 取值应写入 `column_meaning`；仅表名或数据集标识可写入 `dataset_usage`。

- **错误**：`recommended_dataset = "channel_offline, channel_ecommerce"`（实为渠道列枚举值）
- **正确**：枚举写入对应列的 `column_meaning`；`recommended_dataset` 仅填表名或数据集标识

### 4. 分析切片或个案误作口径

将本次分析切片、个案名单或完整查询 WHERE 原样写入可复用指标口径 → 应从 `caliber` / `formula_sql` 中剔除；仅保留跨场景可复用的规则。常见误抄：把本次查询里的实体标识、临时名单或切片过滤原样写进口径。

- **错误**：`caliber` / `formula_sql` 含仅本次分析的限定条件，或把 Agent 整段 SQL 原样粘贴
- **正确**：只保留如「对金额字段求和，退款为负数自动抵扣」「排除游客」等可复用规则；切片与个案条件一律去掉

### 5. 同一事实重复拆分

同一事实的多段说明应合并为一条提取结果；同一主体上的不同事实可以分别提取。

- **错误**：将同一指标口径的聚合方式和退款规则拆成两条 `metric_caliber`
- **正确**：合并为一条完整的指标口径
