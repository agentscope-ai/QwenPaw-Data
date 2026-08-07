# 排版规范

本文档定义报告 HTML 卡片的排版规则，生成卡片时严格参照执行。

---

## 1. 展示形式与布局

### 展示形式选择

每份数据严格选一种展示形式，禁止重复展示同一份数据。

**快速对照（按数据特征）**:

| 分析场景 | 数据特征 | 推荐可视化 |
|----------|----------|------------|
| 当前状态 | 1-3 个关键指标 | KPI 统计卡（flex 一行，数值 + 环比） |
| 趋势变化 | 波动类 / 时序数据 | 折线图（当前周期 + 上一周期对比） |
| 各部分占比 | 占比 3-6 类 | 饼图 / 环形图 |
| 各部分占比 | 占比 > 6 类 | 取 Top 8 + 环形图，其余合并为「其他」 |
| 排名对比 | 多主体同指标 | 条形图 |
| 转化流失 | 漏斗步骤数据 | 漏斗图 |
| 逐行细节 | 指标按非时间维度拆解 | 表格 |
| 两变量关系 | 双数值变量 | 散点图 |

对于行数 < 4 或列数 < 3 的表格，改用 KPI 统计卡展示。KPI 卡每格至少包含 2 个维度（如数值 + 环比变化），不要只放一个大数字。

**详细规范（按分析任务类型，含双视图要求）**: 标注"✦ 支持切换"的场景需同时生成图表和表格视图：

| 分析任务类型 | 推荐图表 | 使用要点 | 双视图 |
| --- | --- | --- | --- |
| 转化漏斗（注册→激活→付费等多环节） | ECharts 漏斗图（`type: 'funnel'`） | 从大到小排列各环节，标注每步转化率和绝对流失量 | ✦ 表格列：环节名 / 数量 / 转化率 / 流失量 |
| 时间趋势 / 异常检测 | 折线图，叠加 `markPoint` / `markLine` | 标注异常点、骤升骤降，图下小字补充具体数值 | ✦ 表格列：日期 / 当期值 / 环比变化 / 备注 |
| 多维度量级对比（排名、Top N） | 横向条形图（条目 > 8 时必选） | 按值降序排列，Top 10 后聚合为「其他」 | ✦ 表格列：维度名 / 数值 / 占比 |
| 时间 × 维度双向对比（本期 vs 上期） | 分组柱状图或双折线图 | 两组数据用不同颜色，图例置顶 | ✦ 表格列：维度 / 本期 / 上期 / 变化率（颜色标注） |
| 占比分析（3–6 类） | 饼图 / 环形图 | 超 6 类时取 Top 8，其余合并为「其他」 | ✦ 表格列：类别 / 数量 / 占比 |
| 留存 / 队列分析 | 热力色表格（用 `background` 渐变色标注单元格） | 行为入组时间，列为留存周期，颜色深浅表示留存率 | — （本身即表格，无需切换） |
| 多指标综合评估 | 雷达图（`type: 'radar'`） | 适合 5–8 个归一化指标，维度过多时改用表格 | ✦ 表格列：指标名 / 当期值 / 基准值 / 得分 |
| 归因 / 维度贡献拆解 | 瀑布图（堆叠柱状图模拟）或分组表格 | 正贡献绿色，负贡献红色，末行为合计 | ✦ 表格列：归因维度 / 贡献值 / 贡献率 / 方向 |
| 1–3 个关键指标 | KPI 统计卡（flex 一行，数值 + 环比） | 每格至少包含 2 个维度，不只放一个大数字 | — （KPI 卡不切换） |
| 指标按非时间维度拆解（4 列以上） | 表格 | 行数 < 4 或列数 < 3 时改 KPI 卡 | — （本身即表格，无需切换） |

### 布局决策

核心原则：空间利用率最大化，避免不必要的纵向滚动，优先并排展示。

| 布局类型 | 适用条件                                           | CSS 类名                                  |
| -------- | -------------------------------------------------- | ----------------------------------------- |
| 2 个并排 | 两个数据块都较简单（表列数 < 8，图表数据点 ≤ 12） | `grid grid-cols-1 lg:grid-cols-2 gap-4` |
| 3 个并排 | 三个数据块都较简单（表列数 < 6）                   | `grid grid-cols-1 xl:grid-cols-3 gap-4` |
| 顺序堆叠 | 任一数据块较复杂（大表格、多系列图表）             | 每个 `<div class="mb-4">` 包裹          |

混排布局示例：

- 左表（4-6 列）+ 右图（饼图/单线趋势图）：用 `lg:grid-cols-2`，图表高度 200-260px
- 左图 + 中图 + 右表（4 列以内）：用 `xl:grid-cols-3`
> KPI 卡不和其他图表并排展示

### 并列高度协调

并排展示的数据块高度应对齐，确保视觉平衡：

- 并列 ECharts 图表：两个容器设置相同的 `height` 值
- 并列表格：两边行数差距 ≤ 2 行，多出的行用滚动容器截断
- 表格与图表并列：表格外层用 `style="height:Xpx; overflow-y:auto"` 对齐图表高度

### 二级维度数据

对于二级维度下拆的数据，每个一级主体独立展示，配小表格展示其二级维度指标和一句关键结果，在整体下方给出总体的数据描述和解读。

---

## 2. 图表规范

**尺寸与方向**：

- 图表高度按数据点数量设置：≤ 5 个用 `height:200px`，6-12 个用 `height:280px`，13+ 个用 `height:340px`
- 单图不独占全宽超过 `max-w-2xl`，除非数据点 > 20
- 条目 > 8 时使用横向条形图（`yAxis` 为 category，`xAxis` 为 value）

**ECharts 配置**：

- `grid`：`{ top: 30, right: 16, bottom: 40, left: 48 }`，避免内部大量留白
- 图例放在顶部：`legend: { top: 4, textStyle: { fontSize: 11 } }`，图例不得遮挡重叠
- 每个实例的 series 数组只能包含一个对象，多个图表用独立实例
- 容器 id 以章节标题拼音缩写开头，避免 id 冲突
- 仅当有可结构化数值数据时才生成 ECharts 图表，否则用表格或文字
- 饼图样式：`itemStyle: { borderRadius: 0 }`
- 每个实例初始化后注册到全局数组：

```javascript
window.__echartsInstances = window.__echartsInstances || [];
var chart = echarts.init(document.getElementById('your-chart-id'));
window.__echartsInstances.push(chart);
```

---

## 3. 表格规范

表格默认使用 `text-xs` 文字大小和 `px-2 py-1` 紧凑内边距。

**宽度控制**（按列数选择，禁止无条件 `w-full`）：

| 列数   | 宽度控制                                                                             |
| ------ | ------------------------------------------------------------------------------------ |
| 2-3 列 | `<table class="table-auto mx-auto text-sm">`，外层 `overflow-x-auto`，不加宽度类 |
| 4-5 列 | 外层加 `max-w-3xl mx-auto`                                                         |
| 6+ 列  | 外层加 `w-full overflow-x-auto`                                                    |

**长表格处理**：超过 10 行的表格必须用滚动容器包裹，数据完整渲染不截断，禁止用 `<details>` 折叠或拆分为多个子表：

```html
<div style="max-height:360px; overflow-y:auto;" class="rounded border border-gray-100">
  <table class="w-full text-xs">
    <thead>...</thead>
    <tbody><!-- 渲染所有行，不截断 --></tbody>
  </table>
</div>
```

**内容规则**：表格行数较多时，按指标值降序排列，默认展示 Top 10 行，剩余行聚合为一行「其他（N 项）」，聚合行的数值取剩余项的聚合值（例如，合计或均值，视指标类型而定）。

---

## 4. 数据表述与颜色

**文字表述**：

- **单位**：数据文件里的数默认原样加 `%`（贡献度、占比、渗透率、环比/WoW 等）；人数/金额变动不加百分号。`pt` 只用于「率值从 A% 变到 B%」的差值，不要把 `%` 改成 `pt`
- 禁用模糊词（「显著增长」「大幅下降」「略有波动」「基本稳定」等），必须给出具体数值
- 摘要只写方向性总结，不重复详细卡中的具体数字
- 禁止未经证实的发散式推测

**数字颜色标注**：

| 数字类型  | 样式                                        |
| --------- | ------------------------------------------- |
| 关键数值  | `<span class="text-blue-600 font-bold">`  |
| 下降/负面 | `<span class="text-red-500 font-bold">`   |
| 上升/正面 | `<span class="text-green-600 font-bold">` |

**数据来源**：每个展示区域标注数据来源，文件名渲染为超链接：

```html
<p class="text-xs text-gray-400 mt-2">
  数据来源：<a href="path/to/{文件名}" class="hover:underline text-blue-400" target="_blank">{文件名}</a>
</p>
```

---

## 5. 卡片模板

针对每个主题产出的 HTML 卡片是片段形式，最终会被嵌入完整的报告页面中，因此只输出卡片内容，不包含 `<html>`、`<head>`、`<body>` 等页面标签。报告页面已引入 Tailwind CSS、ECharts 和 Bootstrap Icons，卡片中直接使用即可。避免输出空 DOM 节点。

### 路径 A：下钻/拆解/归因分析

```html
<div class="bg-white rounded-xl shadow-md p-4 mb-4 fade-in">
  <!-- 分析标题（含异动/波动/归因等关键词） -->
  <h2 class="text-lg font-bold mb-3 flex items-center">
    <i class="bi bi-graph-up-arrow text-blue-500 mr-2"></i>分析标题
  </h2>

  <!-- 核心结论摘要（≤60字，禁用模糊词，必须包含具体数值） -->
  <div class="p-3 bg-blue-50 border-l-4 border-blue-500 rounded-r-lg mb-4">
    <p class="text-sm font-medium text-gray-800 leading-relaxed">
      <!--
        结论模板：[指标][关键数值]，环比[变化描述]，主要受[因素1, 因素2, ...]影响

        颜色标注：
        - 关键数值用蓝色加粗
        - 下降/负面变化用红色加粗
        - 上升/正面变化用绿色加粗
      -->
    </p>
  </div>

  <!--
    数据展示区域（可重复多个）
    布局策略：
    - 1 个单元：<div class="mb-4">...</div>
    - 2 个单元：<div class="grid grid-cols-1 lg:grid-cols-2 gap-4">...</div>
    - 3 个单元：<div class="grid grid-cols-1 xl:grid-cols-3 gap-4">...</div>
    - ...
  -->
  <div class="mb-4">
    <!-- 图表/表格/KPI -->

    <!-- 每个数据展示区域各自配一组现象与分析 -->
    <p class="text-sm text-gray-600 mt-2">
      <span class="font-medium">现象：</span>具体描述，包含时间/维度、涨跌方向、具体量级
    </p>
    <p class="text-sm text-gray-600 mt-1">
      <span class="font-medium">分析：</span>点出 1-2 个关键驱动因素，简要说明影响逻辑
    </p>
    <p class="text-xs text-gray-400 mt-2">
      数据来源：<a href="path/to/xxx.csv" class="hover:underline text-blue-400" target="_blank">xxx.csv</a>
    </p>
  </div>
</div>
```

### 路径 B：基础数据观测

```html
<div class="bg-white rounded-xl shadow-md p-4 mb-4 fade-in">
  <!-- 分析标题 -->
  <h2 class="text-lg font-bold mb-3 flex items-center">
    <i class="bi bi-clipboard-data text-gray-500 mr-2"></i>分析标题
  </h2>

  <!-- 执行摘要（≤60字，禁用模糊词） -->
  <div class="p-3 bg-blue-50 border-l-4 border-blue-500 rounded-r-lg mb-4">
    <p class="text-sm font-medium text-gray-800 leading-relaxed">
      <!-- 模板：[主链路指标][当前值]，各项指标变动均在正常范围内 -->
    </p>
  </div>
  <hr class="border-gray-100 mb-3">
  <!-- 极简 KPI 展示（每格至少含 2 个维度：数值 + 环比） -->
  <h2 class="text-base font-bold mb-3 flex items-center">
    <i class="bi bi-clipboard-data text-gray-500 mr-2"></i>指标概览
  </h2>
  <div class="flex flex-wrap gap-3">
    <div class="flex-1 min-w-[120px] bg-gray-50 rounded-lg p-3 text-center">
      <p class="text-xl font-bold text-gray-800">数值</p>
      <p class="text-xs text-gray-500">指标名称</p>
      <p class="text-xs text-gray-400">环比 +X.XX%</p>
    </div>
    <!-- 更多 KPI 格子（与上方格子并排在同一行） -->
  </div>
  <p class="text-xs text-gray-400 mt-2">各项指标变动均在正常范围内</p>
  <p class="text-xs text-gray-400 mt-2">
    数据来源：<a href="path/to/xxx.csv" class="hover:underline text-blue-400" target="_blank">xxx.csv</a>
  </p>
</div>
```
