# 字面量取值发现

用户问题含 **业务字面量**（团队名、产品名、渠道等），而元数据未给出「业务词 → 库内取值」映射时使用。

## 触发

1. `scope` / `question` 含业务词，但元数据 **无**对应列的 `sample_values` / 枚举说明；
2. 维度列已确认，但用户字面量仍无唯一库内值。

**跳过**：元数据已有确定枚举且与用户字面量唯一对应。

## 路径 A — 图谱维度取值示例（优先）

1. 加载 `Skill(skill="cm-skills")`，按其中的检索规则确认 `Dimension`，使用「维度及其绑定字段与取值示例」配方取得字段及边上的映射，**禁止**臆测英文列名。
2. 读取该配方返回的 `value_example`（边 `MAPS_TO_FIELD` 上的取值示例）与绑定字段 `expr`。`value_example` 只是示例，不是完整枚举。
3. 匹配：精确 > 包含 > 别名归一 → 写回 `scope` 为 `列名=库内值` 或 `维度名=库内值`。
4. 多匹配 → 问用户；示例为空、未命中、或只是示例不足以确认库内真实取值 → 路径 B。

## 路径 B — DISTINCT 探针（兜底）

1. 从元数据选定探针表与候选列。
2. 经 **与主查询相同的标准执行通道**（`execute_read`，**同一个 `datasource_id`**）提交探针 SQL；探针不是图谱查询，不要用 `query_graph`。

```sql
SELECT DISTINCT team_name AS v
FROM <project>.dim_user
WHERE ds = MAX_PT('<project>.dim_user')
  AND team_name IS NOT NULL
  AND team_name <> ''
LIMIT 50;
```

- 分区表 **必须** 带分区谓词；`LIMIT` 默认 50。

3. 同样匹配；唯一 → 写回 `scope`；否则换列/表或重拉元数据；**禁止**臆造取值。

## 衔接

- 改写 `scope` 后涉及元数据未覆盖的表/列 → **重拉元数据**。
- 结论可记入取数流程的步骤 yaml（所用路径、映射、是否用户确认）。
