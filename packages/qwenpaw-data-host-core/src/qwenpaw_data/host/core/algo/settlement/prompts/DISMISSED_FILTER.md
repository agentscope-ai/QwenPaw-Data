# 已拒绝卡片去重

用户曾拒绝过一些推荐卡片。你的任务是：判断新一批待推荐项中，哪些与已拒绝卡片语义等价，不应再次推荐。

输出：等价项在待推荐列表中的下标，填入 `dismissed_indices`。

## 判断标准

**原则**：先看类型与核心实体，再看口径/含义/用法是否实质一致；名称字面是否相同只作辅助。

### 判定

**等价**（加入 `dismissed_indices`，不再推荐）

- 类型相同
- 核心实体相同（允许缩写、别称、中英文差异）
- 口径/含义/用法实质一致；仅措辞或格式不同仍算等价

**不等价**（不加入 `dismissed_indices`，仍可推荐）——以下任一成立即可：

- 类型不同
- 核心实体不同
- 同名但口径/含义有实质变化（更正或补充了新条件）

## 示例

### 1. 别名相同、口径一致 → **等价**

- **待推荐**：`[0] type=metric_caliber fields={"metric_name": "日活用户数", "caliber": "去重登录用户，排除游客"}`
- **已拒绝**：`type=metric_caliber fields={"metric_name": "DAU", "caliber": "COUNT(DISTINCT user_id) WHERE is_guest=0"}`
- **结果**：`dismissed_indices=[0]`
- **理由**：同一指标，口径一致

### 2. 同名但补充了过滤条件 → **不等价**

- **待推荐**：`[0] type=metric_caliber fields={"metric_name": "日活用户数", "caliber": "去重登录用户，排除游客且排除内部员工"}`
- **已拒绝**：`type=metric_caliber fields={"metric_name": "DAU", "caliber": "COUNT(DISTINCT user_id) WHERE is_guest=0"}`
- **结果**：`dismissed_indices=[]`
- **理由**：补充了新条件，口径有实质差异

### 3. 含义一致、仅格式不同 → **等价**

- **待推荐**：`[0] type=column_meaning fields={"column_name": "status", "meaning": "3=已发货, 5=已签收"}`
- **已拒绝**：`type=column_meaning fields={"column_name": "status", "meaning": "订单状态：3已发货5已签收"}`
- **结果**：`dismissed_indices=[0]`
- **理由**：含义一致，仅表述格式不同
