---
name: bi-report-generation
description: 将 BI 数据分析结果组织成可视化 HTML 报告。当分析完成、需要生成报告时调用。
---
# bi-report-generation

将分析结论和数据文件转化为读者友好的 HTML 报告。支持单主题和多主题场景。

报告的读者是数据从业者，他们只关心**关键数字、表格和关键结论**。报告生成遵循四个核心原则：

- **准确**：报告中的所有数字和结论必须来自分析产出的数据文件，不得编造或推测数据
- **干净**：只写读者做判断需要的内容，过程、版本、校验类信息一律不进报告（见下节）
- **紧凑**：充分利用页面空间，优先将数据块并排布局，减少纵向滚动
- **高密度**：每张卡片尽量承载多个相关结论，避免一个指标独占一张卡片（单个指标放进 KPI 带）

## 报告写什么、不写什么

**写**：

- 核心指标：数值 + 相对基期的变化
- 支撑结论的图表和表格
- 关键结论、归因和（有依据时的）建议，结论先行、带具体数字
- 口径：仅当不说明就会误读数字时，在对应指标旁写一行小字（≤30 字），例如
  「活跃 = 当周净 GAAP > 0 的 uid」

**不写**（这些内容需要交代时，放进聊天回复，不进报告）：

| 类别 | 例子 |
| --- | --- |
| 版本与修订历史 | v1→v2 改了什么、「本版新增 / 修正」、较上一版变化、修订记录、更新日志 |
| 计算与取数过程 | SQL、表名与字段链路、处理步骤、公式推导、脚本 / 文件路径、运行参数、分区探测 |
| 校验与自检 | QC / PASS / FAIL、对账结果、逐行复算、md5、外链数、图表渲染验证、「已通过自检」 |
| 工具与环境 | ECharts 版本与内联方式、离线可用性、渲染保真度、生成方式 |
| 展示规则说明 | 小数位、负零处理、颜色含义等的长篇解释 |
| 套话与重复 | 方法论科普、免责声明、「以下为…」类过渡语、复述卡片内容的总结 |

- 不设「口径说明」「方法说明」「附录」「数据说明」之类的独立章节；数据缺失或口径差异
  只在**影响结论**时，用一句话标在对应结论旁。
- 用户明确要求某类说明时按用户要求写，但仍集中放在报告末尾、尽量简短。
- 报告复现模式以原报告为准：原报告有的说明照样复现，原报告没有的不新增。

## 前置检查

开始前确认以下事项已就绪，任一缺失应先完成前置分析流程：

- 分析主题明确（可能包含多个子主题）
- 各主题的分析步骤已执行完毕，结论已产出
- 支撑结论的数据文件已生成且可访问

### 报告复现模式

如果当前任务来自 `report-reproduction`：

- 必须先读取任务根目录的 `report_manifest.json` 和 `format_spec.json`；
- `format_spec.json` 必须由 `report-format-extractor` 基于原报告生成，缺失或字段不完整时
  停止并先执行该 Skill；
- 基线报告只读取各 Section 的 `baseline/draft.md` 和对应数据；
- 新周期只读取各 Section 的 `runs/{period}/draft.md` 和对应 `data/`，禁止回退到基线；
- 按 manifest 中的 Section 顺序生成页面，并以 `format_spec.json` 为最高优先级复现
  页面层级、色板、字体、导航、卡片、表格、图表与交互；
- **类名只能取自 `format_spec.json` 的 `source_css` / `dom_contract`**，不得自创。原报告
  的 CSS 会被原文透传进页面，自创的类名在 `source_css` 里没有对应规则，样式一条也
  命中不了；同时也不得用 Tailwind 工具类去替代原报告已经定义的类（例如原报告用
  `.grid2` 做双栅，就不要改成 `lg:grid-cols-2`）；
- **结构要带对标签名**。`dom_contract` 里写 `h2.sec`、`details.panel`、`tr.drill-uid-row`
  这类标签限定选择器时，类名挂错标签就完全失配；
- **交互必须有出路**。写 `onclick` / `data-*` 驱动的行为时，对应的目标元素和切换的
  状态类（如 `dom_contract` 里 `state: true` 的项）必须同时存在于本片段里；做不到
  就不写这个交互，改成静态展开。**禁止产出点了没反应的按钮**；
- 通用 `layout-spec.md` **只用于 `format_spec.json` 未定义的部分**；两者冲突时一律以
  `format_spec.json` 为准，不得用默认主题（`resources/theme.css`）覆盖原风格；
- 最终 HTML 只写基线 `report.html` 或新周期 `runs/{period}/report.html`；
  不得再运行 `report-reproduction` 的 `compile-report` 子命令，也不得拷贝到 Session
  产物根目录或另存 `report_{period}.html`。
- **必须经 `scripts/report_builder.py` 生成，禁止用一次性脚本 / heredoc 手写
  `report.html`**。手搓 HTML 会绕过样式透传和体量对账，是“外壳像、里面空”的根因；
  report_builder 会在 `</head>` 前盖 `qwenpaw-data-report-builder` 生成章，缺失即未走正道。
- **`html_fragment` 必须把已验收草稿的完整数据 materialize 出来**——所有实体行、下钻
  明细行、涨跌色单元格都要落成真实 DOM，不得用几行占位样本代替。report_builder 会拿
  `format_spec.json` 的 `dom_contract.count` 对账生成结果：数据密集结构（实体行、下钻行、
  `td.pos`/`td.neg`、栅格、KPI、tab）实测数量低于原报告约 1/3 即判塌陷、以退出码 2 失败。
  塌陷时回去补全 `sections_*.json` 的数据，而不是加 `--allow-dom-mismatch` 绕过（该开关
  只留给新周期数据量确实合理缩水的场景）。
- **每个图表容器都必须有渲染出路，禁止只写空 `<div id="chart_*">`**。要么在片段里配套一段
  初始化脚本（`echarts.init(document.getElementById('chart_x')).setOption({...})`）按 id
  初始化，要么直接内联同一份数据渲染的 `<svg>`/表格。report_builder 会审计所有图表容器：
  空 `div` 既无 `<svg>`/`<canvas>`、又没有任何脚本按 id 初始化，即判图表空转、以退出码 2
  失败——绝不允许在正文里用文字声称已完成 SSR 渲染、已通过自检来代替真实的图表产物。

## 执行步骤

### 1：展示布局规划

基于已完成的分析过程，梳理各主题涉及的关键指标、分析维度和数据特征，为每个主题规划展示形式：

1. **根据分析路径选择展示方式**：主题涉及多个核心指标时，建议先用一张概况卡（KPI）给出整体情况，再按分析路径展开；只有一个结论的主题直接给结论和数据：

   | 分析路径 | 展示方式 |
   | --- | --- |
   | 基础数据观测 | KPI 卡展示核心数字，表格/图表展示数据细节 |
   | 下钻/拆解/归因 | 按照「结论 → 数据佐证」展示，只保留支撑结论的拆解 |
   | 归因类分析（含外部事件） | 按关键数据点或异常区间分块，每块列出归因 |

   规划时只保留能支撑结论的数据块；探索过程中的中间结果、试过但没结论的拆解不进报告。

2. **选择图表类型**：按 `references/layout-spec.md §1` 中的图表类型表匹配，命中即停。

3. 列出每个主题需要读取的数据文件。

4. 确定报告的总标题（概括报告整体范围），显示在页面顶部的标题区；标题下不写总摘要，核心结论写在第一章的 `conclusion` 块里。全报告最重要的 1–4 个指标可作为一条 KPI 带一并放进标题区（`"type": "hero"` 的 section）。

**对每个主题，依次执行步骤 2 和步骤 3：**

### 2：数据准备

根据步骤 1 确定的文件列表，读取该主题所需数据：

1. 读取数据文件
2. 列筛选：数据表可能包含与当前主题不直接相关的列，只保留相关列用于展示，剔除无关列（包括 id、分区、中间计算列等读者不需要的技术字段）

### 3：写 blocks 规格（声明式生成卡片）

根据步骤 1 的布局规划，为该主题写一个 section：**只描述「展示什么」并指向数据文件，
HTML、表格行、涨跌色、下钻开关和图表初始化脚本都由 `report_builder.py` 生成**。
不要手写 `<tr>`、`<td>` 和 `echarts.init(...)`。

- 每个章节必须包含：章标题、核心结论（一两句、带具体数字）、数据展示（表/图/KPI）。
- 解读文字按需写：图表或表格本身已能说明问题时不配文字；需要时在数据块下写一句
  「现象 / 原因」即可，不为每个数据块强配「现象 + 分析」两段，也不复述图表里已有的数字。
- 卡片里不写数据来源文件链接、计算过程和校验信息（见「报告写什么、不写什么」）。
- 排版规范按下列优先级取：
  1. **项目 / 用户指定的渲染器或页面契约**：如果用户或项目已经提供了渲染脚本、模板或页面
     结构约定，直接用它，本 Skill 的样式规则只作默认值，不要另写一套渲染器；
  2. **报告复现模式**：`format_spec.json` 的 `source_css` / `dom_contract` 最高优先，
     `references/layout-spec.md` 只补它未定义的部分；
  3. **普通 BI 分析**：用 blocks，样式已内置，其数值是默认值，按数据实际情况调整即可。
- 卡片写入 `sections_{xxx}.json` 文件（使用唯一标识区分不同报告），每条 section：

  | 字段 | 类型 | 说明 |
  | --- | --- | --- |
  | `title` | string | 章标题，页面上显示为带编号（01、02…）的章节标题 |
  | `subtitle` | string（可选） | 章标题右侧的一行灰色短注（周期、范围），可省略 |
  | `type` | string（可选） | 填 `"hero"` 表示这一节的块（通常是一条 KPI 带）放进顶部标题区、不编号；不填为普通章节 |
  | `blocks` | array | 声明式块列表（推荐），见下 |
  | `html_fragment` | string | 手写 HTML，仅用于 blocks 表达不了的版式，或报告复现模式 |

  一个 section 里 `blocks` 与 `html_fragment` 二选一。章节按顺序纵向排列、全部可见。
  表 / 图 / 构成条 / 时间线各自成一张白底卡片（卡头是块的 `title` / `subtitle`），
  `conclusion`、`kpi`、`note` 直接放在章节里；要把几个块收进同一张卡片时用 `card`。
  用 `grid` 并排的块共用同一高度。视觉规范见 `references/layout-spec.md §0`。

**紧凑示例**（标题区 KPI 带 + 结论 + 双栏「图 + 构成」+ 明细表 + 下钻表，数据路径相对
sections 文件所在目录，或用 `--data-root` 指定）：

```json
[
  {"type": "hero", "blocks": [
    {"type": "kpi", "data": "data/kpi.csv", "items": [
      {"label": "净 GAAP", "field": "gaap", "fmt": "num2", "unit": "万元", "delta_field": "gaap_wow"},
      {"label": "活跃 uid", "field": "uid", "fmt": "int", "unit": "个", "delta_field": "uid_wow"}
    ]}
  ]},
  {
    "title": "访问趋势分析",
    "subtitle": "近 8 周",
    "blocks": [
      {"type": "conclusion", "text": "W43 净 GAAP **30,390.05** 万元，环比 {-1.66%}，新客 {+12.5%}"},
      {"type": "grid", "cols": "2:1", "blocks": [
        {"type": "chart", "kind": "bar", "title": "周趋势", "subtitle": "柱：净 GAAP（万元）· 线：环比",
         "data": "data/trend.csv", "x": "week", "highlight": "last", "legend": true, "toggle": true,
         "series": [{"field": "gaap", "name": "净GAAP", "fmt": "num2"},
                    {"field": "wow", "name": "环比", "fmt": "pct1", "type": "line", "axis": "right"}]},
        {"type": "stackbar", "title": "客群构成", "data": "data/seg.csv", "x": "seg", "value": "uid", "unit": "个"}
      ]},
      {"type": "table", "title": "客户明细", "data": "data/cust.csv", "sort": "-w43", "rank": true, "search": true,
       "columns": [{"field": "name", "label": "客户"}, {"field": "industry", "label": "行业", "tag": true},
                   {"field": "w43", "fmt": "num2"}, {"field": "delta", "fmt": "pct2+", "color": "sign"}]},
      {"type": "drill_table", "title": "客户榜单", "data": "data/boards.json", "select": "boards.rows",
       "key": "cid", "columns": [{"field": "name", "label": "客户"}, {"field": "w43", "fmt": "num2"}],
       "children": {"data": "data/uid.csv", "join": "cid",
                    "columns": [{"field": "uid"}, {"field": "w43", "fmt": "num2"}]}}
    ]
  },
  {"title": "业务事件时间线", "blocks": [
    {"type": "timeline", "events": [
      {"date": "2026-10-01", "title": "模型升级", "kind": "product"},
      {"date": "2026-10-05", "title": "活动上线", "desc": "满减", "kind": "promo"}]}
  ]}
]
```

**块类型与字段**

| `type` | 字段 |
| --- | --- |
| `conclusion` | `text`：核心结论，支持标记 `**x**`（加粗强调）、`{+x}`（绿色 +x，上升）、`{-x}`（蓝色 -x，下降），其余内容一律转义 |
| `kpi` | `items[]`：`label`、`value` 或 `field`、`fmt`、`unit`（数值后的小字单位）、`delta` 或 `delta_field`、`delta_fmt`（默认 `pct2+`）、`delta_label`（默认「环比」）、`delta_inverse`（降为好时置 true）、`foot`（可选的一句短注，如「占名单 83.9%」）；可选顶层 `data` 取第一行、`compact`（小号，放进 `card` 时用） |
| `table` | `data`、`columns[]`、`sort`（`"-uid"` 降序）、`top_n`（余项自动并成「其他」，`other_row:false` 关闭）、`total`（合计行，列 `agg`: sum/mean/none）、`rank`（序号列）、`search`（搜索框）、`sortable`（表头点击排序，超过 10 行默认开）、`header`（`dark`/`light`，超过 10 行默认 `dark`）、`max_height` |
| `drill_table` | 同 `table`，加 `key` 与 `children`（`data`、`join`、`columns`，可再嵌套 `children`，最多 3 层）；展开开关与 id 自动生成并配对 |
| `heatmap_table` | `data`、`label_field`、`value_fields[]` 或 `row`/`col`/`value` 长表、`fmt`、`scale_by`、`col_order`、`row_label` |
| `chart` | `kind`、`data`、`x`、`series[]`（字符串或 `{field,name,fmt,color,type,axis}`，`axis: "right"` 放到右轴）、`height`、`toggle`（图/表切换，按钮在卡头）、`highlight`（`last`/`max`/类目名：只高亮这根柱）、`labels`（数据标签，默认小图开）、`y_label`/`y2_label`（轴名）、`mark`（`max`/`min`/`avg`/`points[]`/`lines[]`）、`stack`、`sort`、`top_n`、`legend` |
| `stackbar` | `data`、`x`（分类字段）、`value`（数值字段）、`fmt`、`unit`、`sort`：一个总量的 2–6 个组成部分画成一根百分比条 + 图例（值与占比），超过 6 类自动合并「其他」 |
| `grid` | `cols`（`2`、`3` 或 `"2:1"` 左宽右窄）、`blocks[]`；块并排，共用高度，KPI 不要放进 grid |
| `note` / `caption` | `text`；`note` 是 ≤30 字的口径小字，`caption` 是图表下的一行解读 |
| `timeline` | `events[]`：`date`、`title`、`desc`、`kind`（`major`/`promo`/`product`），自动按日期升序 |
| `card` | `title`、`subtitle`、`blocks[]`：把几个块收进同一张卡片（子块不再各自成卡） |
| `html` | `html`：单个块内的手写 HTML，应急用 |

表 / 图 / 构成条 / 时间线块还可带 `title`、`subtitle`（卡头）和 `caption`、`note`（卡内底部）。
`columns[]` 每项可为字段名或 `{field, label, fmt, color, align, agg, tag, title_field}`：`tag`
为 `true` 时按值自动配色显示成药丸标签，也可写 `{"值": "teal"}` 指定色调（`blue`/`teal`/
`purple`/`amber`/`gray`/`line`）；`title_field` 指定悬停提示取哪个字段。`data` 可为 csv / json
（`select` 用点路径取子树，`where` 过滤，也接受 `{columns, rows}`）或直接内联 `rows`。

**`chart.kind`**：`line` 趋势 / `bar` 单维对比 / `grouped_bar` 多指标对比 / `hbar` 排名（Top N）/
`pie` 占比（超过 6 类自动合并为「其他」）/ `funnel` 转化 / `scatter` 相关性 / `radar` 多维评估；
选型规则见 `references/layout-spec.md §1`。图表自带 `window.echarts` guard 和同数据的
fallback 表格，不用再写。

**`fmt`**：`auto`、`text`、`int`、`num1`、`num2`、`money2`、`wan1`、`wan2`、`yi2`、
`pct0/1/2`（数据已是百分点）、`ratio0/1/2`（数据是小数，×100 后加 %）；末尾加 `+` 表示正数带 `+` 号
（如 `pct2+`）。四舍五入按 ROUND_HALF_UP；显示为 0.00 的值不染色，不出现「-0.00」。
**`color`**：`sign`（上升绿、下降蓝）、`sign_inv`（上升红、下降绿，成本类）、`key`（深色加粗强调）。

**`html_fragment` 兜底**：仅当 blocks 无法表达（如自定义交互、非标准版式）时才写；写的话
沿用 `references/layout-spec.md §5` 的 `rp-*` 类名，不要在片段里重复章标题，并保证每个图表
容器都有渲染出路。

**时间线 section**：涉及业务事件（促销、模型上线、策略变更）时，在所有章节之后追加一个
只含 `timeline` 块的 section。

### 4：生成完整报告（一次运行，审计即质检）

所有主题的 sections 写好后，调用 `<skill-dir>/scripts/report_builder.py` **运行一次**，
将各 JSON 拼接成完整 HTML。脚本的内置审计就是质检：不要另写渲染脚本、自建 QC 脚本或
反复手工校对 HTML，除非用户或项目明确提供了这样的工具。

产物**完全离线**：报告主题（`resources/theme.css`）、按需裁剪的 Tailwind 工具类和
Bootstrap Icons（`resources/report.css`、`resources/icons.css`）都内联进页面，ECharts 也是
内联，没有任何远程请求。
若新增了 `resources/` 里没有的 Tailwind 类，用 `scripts/build_report_css.py` 重新生成。

**自动审计**（结果写入 `</head>` 前的 `qwenpaw-data-report-builder` 生成章，失败时 stdout 给出明细）：

| 审计 | 失败条件 / 处理 |
| --- | --- |
| `dom_contract` 体量对账（复现模式） | 数据密集结构塌陷到原报告约 1/3 以下，退出码 2 |
| 图表渲染 | 空转的图表容器，退出码 2 |
| 外链 | 出现远程 `<link>` / `<script src>` / `@import` / `url()`，退出码 2；确需外链时加 `--allow-external` |
| 下钻 id | 下钻开关与目标行对不上，退出码 2 |
| 类名覆盖 | 类名没有任何样式规则，警告（复现模式下用于发现自创类名） |
| 内容 lint | 正文出现 QC / PASS / 版本 / 路径等过程信息，警告（复现模式不检查） |

任一失败都应回到 sections 修数据 / 规格后重跑，不得改小对账阈值或靠 `--allow-dom-mismatch`
绕过。

报告复现模式必须传入 `--format-spec`；脚本会把原报告的 `<style>` 原文清洗后注入最终
页面（优先取 `format_spec.json` 的 `source_css`，没有则按 `source.path` 现读现抽），注入
位置在 `</head>` 之前，因此层叠优先级高于模板自带样式。若原报告存在通用骨架无法表达的
导航或布局，应先依据 `format_spec.json` 生成安全的 Jinja 模板，再通过 `--template` 传入；
模板只能包含展示逻辑，不得复制原报告脚本。

**模板的外部依赖**：

| 依赖 | 引入方式 | 离线时 |
| --- | --- | --- |
| 报告主题 | `resources/theme.css`（`rp-*` 类）内联进 `<style>` | 不受影响 |
| Tailwind CSS | 裁剪后的 `resources/report.css` 内联进 `<style>` | 不受影响 |
| Bootstrap Icons | `resources/icons.css`（字体 base64 内联） | 不受影响 |
| ECharts | `build_echarts_loader()` 内联本地 `resources/echarts.min.js` | 不受影响 |
| 图表兜底运行时 / 下钻与图表切换脚本 | `resources/chart_fallback.html`、`resources/bi_helpers.html`，均无外链 | 不受影响 |

- 复现模式下原报告 CSS 整段透传进 `<style>`，应当用原报告类名；不要用 Tailwind 工具类去替代
  原报告已定义的类。
- 自定义 `--template` 必须保留 `</head>`，否则脚本无法安全注入样式而直接报错；
  若模板里没有图表兜底、主题样式或交互脚本，而页面用到了它们，`report_builder.py` 会自动
  补上。若自定义模板仍要用内置离线样式，保留 `{{ report_css }}` / `{{ icons_css }}` /
  `{{ theme_css }}` / `{{ bi_helpers }}`；章节列表用 `body_sections`（`hero_sections` 为标题区）。

**参数**：

| 参数              | 说明                                     |
| ----------------- | ---------------------------------------- |
| --sections        | 主题卡片 JSON 文件路径，支持多个（必填） |
| --output / -o     | 输出路径；须为新文件（`<交付物名>_vN.html`，见系统提示 `<qwenpaw-data-analysis-environment>` 的交付物版本规则），禁止覆盖已有报告 |
| --report-title    | 报告总标题，显示在顶部标题区             |
| --data-root       | blocks 里相对路径的数据根目录；默认取各 sections 文件所在目录 |
| --format-spec     | `report-format-extractor` 生成的 JSON（报告复现模式必填） |
| --allow-external  | 允许外链资源（外链审计降为警告，仅限用户明确要求引用外部资源） |
| --allow-dom-mismatch | `dom_contract` 体量对账或图表渲染审计塌陷时不失败退出（仅限新周期数据合理缩水，复现基线不应使用；图表空转不得靠它绕过） |

**调用示例**：

```bash
# 多主题
python <skill-dir>/scripts/report_builder.py \
    --sections sections_001.json sections_002.json \
    --output report_v1.html \
    --report-title "XXX 产品 2025 年 12 月数据分析报告"

# 报告复现的新周期
python <skill-dir>/scripts/report_builder.py \
  --sections runs/202603/sections_*.json \
  --format-spec format_spec.json \
  --output runs/202603/report.html \
  --report-title "MaaS 模型(sfm) 客户月报" \
  --date "202603"

# 单主题（不传 report-title 且没有 hero section 时，不显示标题区）
python <skill-dir>/scripts/report_builder.py \
    --sections sections_001.json \
    --output report_v1.html
```

### 5：生成后自检

builder 已自动检查：图表空转、外链、下钻 id、类名缺失、过程信息，以及复现模式的
`dom_contract`。**不必**再手工逐项复查这些；只需确认下列内容与语义相关的项：

**内容**：

- [ ] 每张卡片结论先行、带具体数字；没有复述图表数字的文字和套话
- [ ] 没有版本 / 修订历史、计算与取数过程、校验与自检信息、工具 / 环境说明、
      「口径说明」「附录」类独立章节（用户明确要求的除外；builder 的内容 lint 只能抓常见词）
- [ ] 所有数字来自读取的数据文件，不得编造或推测

**展示**：

- [ ] 图表类型与数据匹配（`chart.kind` 与 `references/layout-spec.md §1`）
- [ ] 明细/清单类长表格逐行完整，不聚合、不删行（不要对明细表设 `top_n`）
- [ ] 多个简单数据块已用 `grid` 并排，没有大面积空白
- [ ] 图片（如有）仅使用 `data:image/...`、内联 SVG 或已确认可访问的安全资产，`<img>` 有 `alt`
- [ ] 涉及业务事件时，时间线为末尾独立的 section
- [ ] 标题区、章节副标题、卡片副标题里没有装饰性标语、数据快照或口径说明之类的多余内容

**报告复现模式**：

- [ ] 标题和周期来自当前 Run，而不是基线标题中的旧周期
- [ ] Section 数量与顺序和 manifest 一致
- [ ] 页面层级、色板、字体、导航、卡片、表格、图表和交互逐项符合 `format_spec.json`
- [ ] **`html_fragment` 里每一个 `class` 都能在 `source_css` 中找到**；自创的类名一个也不留
- [ ] **标签名与 `dom_contract` 一致**：`h2.sec` 没写成 `<div class="sec">`，
      `details.panel` 没写成 `<div class="panel">`，`tr.*` 类没挂到非 `<tr>` 上
- [ ] **没有悬空交互**：每个 `onclick` / `data-*` 驱动的行为都有对应的目标元素和状态类；
      做不到出路的已改成静态展开
- [ ] 长明细表逐行完整，未做 Top N 聚合或删行
- [ ] 未使用默认模板覆盖原报告已经明确的风格
- [ ] 最终 HTML 已直接写入标准产物路径，且未再运行 `report-reproduction` 的
      `compile-report` 子命令
- [ ] **最终 HTML 经 `report_builder.py` 生成**（含 `qwenpaw-data-report-builder` 生成章），
      不是一次性脚本 / heredoc 手写的
- [ ] **`dom_contract` 体量对账 PASS**，report_builder 退出码为 0
- [ ] **图表渲染审计 PASS**：生成章 `charts` 段为 PASS
- [ ] `html_fragment` 里是已验收草稿的**完整数据**（逐行实体 / 明细），不是占位样本
