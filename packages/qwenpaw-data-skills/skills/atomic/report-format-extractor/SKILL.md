---
name: report-format-extractor
description: 在数值验收完成后，从原报告提取安全、结构化的视觉规范。
---

# Report Format Extractor

仅在全部 Section 已通过数值和逻辑验收后执行。

本 Skill 由 Agent 直接完成视觉理解和规范落盘，不调用报告格式提取工具，也不得用
简单的“取前几个颜色/第一个字体”代替完整风格分析。

## 输入

1. 读取 `report_manifest.json`，按其中的 `source_path` 定位原始报告。
2. HTML 报告同时检查 DOM、内联样式、样式表和可见交互结构；DOCX/Markdown 报告检查
   标题层级、段落、表格、图片和分页线索。
3. 读取各 Section 的 `source.md` 辅助理解“哪些结构是业务内容，哪些结构是视觉容器”。
4. 只分析原报告；不得从当前通用 BI 模板反推原报告风格。

## 必须提取的规范

将结果写入任务根目录的 `format_spec.json`，至少包含：

- `source`：原报告定位信息。三个子字段都是硬约束，因为 `report_builder.py`
  要靠它找到原报告并透传样式：
  - `path`：相对**任务根目录**的路径，且必须真的能打开；
  - `type`：字面量 `html`。写成别的值（`HTML 报告`、`pdf`、留空）会让生成端
    静默降级为「不注入原报告样式」，报告直接退回通用模板外观；
  - `evidence`：证据列表，精确到 CSS 选择器或 DOM 结构，不要写「参考原文」。
- `source_css`：原报告**全部** `<style>` 块的原文拼接。这是生成端唯一的类名
  事实来源，见下节「source_css」。
- `dom_contract`：结构模式清单，见下节「dom_contract」。
- `page`：页面背景、内容最大宽度、外边距、内边距和整体密度；
- `header`：标题、副标题、周期、摘要区的结构、对齐方式和背景；
- `navigation`：目录、页签、章节卡片或折叠面板的组织与选中态；
- `typography`：字体族、字号层级、字重、行高和正文颜色；
- `palette`：主色、辅助色、背景色、边框色、语义色及各自用途；
- `sections`：Section 容器、卡片、网格、间距、圆角、边框和阴影；
- `tables`：表头、单元格、斑马纹、数值对齐、长表滚动与明细展开方式；
- `charts`：原报告实际使用的图表类型、配色、图例、标签、tooltip 和尺寸；
- `images`：原报告中的静态图片、图标、截图或背景图的用途、尺寸、位置和可替代方式；
- `interactions`：图表/表格切换、页签、折叠、详情展开等安全交互；
- `responsive`：桌面和窄屏下的布局规则；
- `generation_contract`：`bi-report-generation` 必须保留的结构、图表渲染策略、
  图片嵌入策略和允许降级项。

每个关键视觉结论都要在 `source.evidence` 中记录来源，例如 CSS 选择器、DOM 结构、
标题文本或原报告片段；无法确认的值标记为 `unknown`，不得猜测。

### source_css

把原报告所有 `<style>` 块按出现顺序拼成一个字符串写进 `source_css`。顺序就是层叠
优先级（常见是「基础布局」+「主题覆盖」两块，后者用 `!important` 压前者），
不要重排、不要合并、不要「精简掉看着没用的规则」。

允许的加工只有两种：删注释、删危险构造（`@import`、`url()` 指向远程资源、
`expression()`、`behavior`/`-moz-binding`）。删了要在 `source.evidence` 里记一笔。

为什么必须整段搬而不是摘录：原报告的 CSS 通常只有几 KB，而 HTML 有几 MB。以一份
真实原报告实测为例，HTML 4,784,135 字符，3 个 `<style>` 块拼起来只有 9,912 字符
（**0.21%**）。生成端读 `source_css` 一次就拿到全部真实类名；不写这个字段，它就得
自己去几 MB 的 HTML 里采样，而采样必然漏（见下节）。

### dom_contract

CSS 只说明「某个类长什么样」，说明不了「谁套在谁里面、哪一层能点、哪个类是运行时
才出现的状态」。这些结构信息必须由 `dom_contract` 显式写出，它是一个数组，每项：

| 字段 | 说明 |
| --- | --- |
| `selector` | **带标签名的选择器**，照抄 CSS 里的写法，如 `h2.sec`、`details.panel` |
| `count` | 该模式在原报告 DOM 中的出现次数（实测值，不是估计） |
| `markup` | 最小可运行 markup 示例，体现必需的父子层级 |
| `semantics` | 这个结构承载什么业务内容 |
| `state` | 可选，`true` 表示该类由交互切换、静态 DOM 中可能为 0 次 |

**`selector` 必须带标签名。** 同一份真实原报告的 CSS 里有 25 组标签限定选择器，
类名对了但标签写错就完全失配：

- `h2.sec` 写成 `<div class="sec">` → 章节标题的左边框消失；
- `details.panel` 写成 `<div class="panel">` → 折叠面板不再可折叠；
- `b.t` 写成 `<span class="t">` → `.blk4-card` 里的标题不再独占一行；
- `tr.drill-uid-row`（原报告 2205 个）写到 `<div>` 上 → 下钻明细整块错位。

**状态类必须单独标出来。** 同一份原报告里，CSS 定义了 62 个类，静态 DOM 只出现其中
51 个；剩下 11 个（`open`、`hide`、`badge`、`empty-state`、`uid-row`、`cid-row`、
`fact`、`insight`、`matrix-note`、`full`、`md`）在 HTML 源码里出现 **0 次**，它们靠交互
或条件分支才出现。例如 `tr.open > td .caret { transform:rotate(90deg) }` 和
`.tabpanel.hide { display:none }`——只看 DOM 会以为这两个类不存在，于是既不写展开
态也不写隐藏态，折叠交互直接失效。这类项 `state` 必须填 `true`。

## 采样规则（硬性禁止）

以下三种取样方式都会系统性漏掉原报告的真实结构，禁止使用：

1. **禁止只扫 `<div>`。** 表格类报告的结构大多挂在 `<tr>`/`<td>`/`<th>`/`<span>`
   上。实测那份原报告：`<td>` 57,128 个元素承载 8 个类、`<span>` 13,843 个承载 8
   个类、`<tr>` 7,484 个承载 4 个类，而 `<div>` 只有 156 个元素、承载 20 个类。
   只看 div 会漏掉 52 个类里的 32 个，包括 `pos`/`neg`/`rate-hl`/`mono`/`sub-tbl`
   /`code-tbl`/`drill-uid-row` 这些决定数值颜色和明细结构的类。
2. **禁止只读文件前若干 KB。** 同一份原报告只扫前 100KB 能采到 48/52 个类，漏 4
   个；而 CSS 已定义、前 100KB 完全采不到的类有 15 个（`appendix`、`badge`、
   `insight`、`matrix-note`、`empty-state`、`rpt-f`、`sig` 等）——附录、页脚、
   异常态全在文件尾部。**必须以 `source_css` 为准反查 DOM，而不是以 DOM 采样为准。**
3. **禁止从通用 BI 模板或经验反推类名。** 类名一律来自 `source_css`；`source_css`
   里没有的类名，就是不存在，不许「按惯例」补一个。

## format_spec.json 骨架

只列出与生成端直接耦合的字段，其余视觉维度按上节要求填写：

```json
{
  "source": {
    "path": "source/downloaded-report.html",
    "type": "html",
    "evidence": [
      "3 个 <style> 块，拼接后 9912 字符；块 2/3 为主题覆盖，带 !important",
      "h2.sec 左边框；details.panel 折叠；.grid2 = auto-fit minmax(430px,1fr)",
      "删除 0 条 @import、0 个远程 url()"
    ]
  },
  "source_css": ":root{--line:#ebedf1} .page{...} h2.sec{...} ...",
  "dom_contract": [
    {
      "selector": "h2.sec",
      "count": 5,
      "markup": "<h2 class=\"sec\">一、总体概况</h2>",
      "semantics": "一级章节标题，左侧色条"
    },
    {
      "selector": "details.panel > summary",
      "count": 5,
      "markup": "<details class=\"panel\"><summary>明细（点击展开）</summary><div class=\"tblwrap\"><table class=\"tbl\">...</table></div></details>",
      "semantics": "长明细表的折叠容器，内层用 .tblwrap 做横向滚动"
    },
    {
      "selector": "tr.drill-uid-row",
      "count": 2205,
      "markup": "<tr class=\"drill-uid-row\"><td><span class=\"caret\">▸</span>名称</td><td class=\"r\">123</td></tr>",
      "semantics": "二级下钻明细行；.r 为右对齐数值列"
    },
    {
      "selector": "tr.open",
      "count": 0,
      "state": true,
      "markup": "<tr class=\"drill-uid-row open\">...",
      "semantics": "展开态；驱动 tr.open > td .caret 旋转 90°，静态 DOM 中不出现"
    },
    {
      "selector": ".tabpanel.hide",
      "count": 0,
      "state": true,
      "markup": "<div class=\"tabpanel hide\">...",
      "semantics": "页签隐藏态；非当前页签的 .tabpanel 必须带 hide"
    }
  ],
  "generation_contract": {
    "must_preserve": ["h2.sec 章节标题", "details.panel 折叠", ".grid2 双栏"],
    "class_name_source": "source_css",
    "chart_strategy": "...",
    "allowed_degradations": ["..."]
  }
}
```

`generation_contract.must_preserve` 里引用结构时**必须写带标签名的选择器**（`h2.sec`
而不是「章节标题带左边框」）。散文描述会让生成端自己编类名，而编出的类名一旦不在
`source_css` 里，对应的样式一条也命中不了。

## 安全限制

- 不复制原报告中的脚本、事件处理器、iframe、表单或远程跟踪代码。
- 不直接复用原报告 HTML 作为最终报告。
- 仅允许把可信的样式 token、布局规则和交互行为说明写入 `format_spec.json`。
- `source_css` 是上面「不复制脚本」的唯一例外：它只含样式声明，且必须先删掉
  `@import`、远程 `url()`、`expression()`、`behavior`/`-moz-binding`。原报告的
  `<script>` 一律不进 `format_spec.json`——交互要在 `interactions` 里用文字描述，
  由 `bi-report-generation` 重新实现。
- 原报告依赖外部样式或脚本时，只记录其可见效果，不复制资源地址。
- 原报告依赖静态图片时，只记录图片的可见作用；最终是否嵌入由
  `bi-report-generation` 基于本地可信资产或内联 SVG/data URI 决定。
- 新报告中的数字和业务结论仍以已通过的 Section 草稿及数据证据为准。

## 交付与验收

写入 `format_spec.json` 后重新读取并检查：

1. 文件是合法 JSON，且上述一级字段齐全；
2. `source.type` 是字面量 `html`，`source.path` 从任务根目录拼出来确实是个存在的
   文件（用 `read` 验证一次，不要目测）；
3. `source_css` 非空，且包含原报告全部 `<style>` 块；其中不含 `@import`、
   `expression(`、`behavior:`、`-moz-binding`，`url(` 只指向 `data:image/` 或
   `https://`；
4. `dom_contract` 每项的 `selector` 里的类名**全部**能在 `source_css` 中找到；
   找不到的就是自创类名，必须删掉；
5. `dom_contract` 里 `count` 为 0 的项必须带 `state: true`；既为 0 又不是状态类的，
   说明这个模式不存在，删掉；
6. 页面、标题、导航、Section、表格、图表和图片均有明确规则；
7. 颜色记录了用途，而不是只保存无语义的颜色列表；
8. `generation_contract` 能指导 `bi-report-generation` 还原原报告，并说明图表在
   CDN/脚本不可用时如何展示 fallback；
9. 不含可执行脚本、远程资源或原报告业务数据副本。

完成后加载 `bi-report-generation`。由它读取 `format_spec.json` 和已通过的 Section
产物，直接生成最终 HTML；不得运行 `report-reproduction` 的 `compile-report` 子命令
二次渲染。
