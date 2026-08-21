# 看板主题规范

本文档定义看板的视觉系统和可复用组件模式。Agent 自由组合这些组件构建看板，不受布局结构约束。

---

## 1. 基础资源

每个看板 HTML 文件头部引入以下资源：

```html
<link rel="stylesheet" href="https://storage.360buyimg.com/pubfree-bucket/ei-data-resource/02f0288/static/tailwind.min.css">
<script src="https://storage.360buyimg.com/pubfree-bucket/ei-data-resource/02f0288/static/echarts.min.js"></script>
<link rel="stylesheet" href="https://storage.360buyimg.com/pubfree-bucket/ei-data-resource/e86f985/static/bootstrap-icons/bootstrap-icons.css">
```

---

## 2. 色彩 Token

| 语义 | 类名 | 用途 |
|------|------|------|
| 主色 | `text-blue-600` / `bg-blue-600` | 标题强调、主按钮、关键数值 |
| 正面 | `text-green-600` | 上升、增长、达标 |
| 负面 | `text-red-500` | 下降、流失、未达标 |
| 中性 | `text-gray-600` | 正文、描述 |
| 次要 | `text-gray-400` | 标注、来源、次要信息 |
| 页面背景 | `bg-gray-50` | body 背景 |
| 卡片背景 | `bg-white` | 内容卡片 |

---

## 3. 组件模式

### 3.1 页面容器

```html
<body class="bg-gray-50 min-h-screen">
  <div class="max-w-7xl mx-auto px-4 py-6">
    <!-- 看板内容 -->
  </div>
</body>
```

### 3.2 标题栏

```html
<div class="bg-gradient-to-r from-blue-600 via-indigo-600 to-purple-600 rounded-xl p-6 mb-6 shadow-lg">
  <h1 class="text-3xl font-bold text-white">看板标题</h1>
  <p class="text-blue-200 text-sm mt-2"><i class="bi bi-clock-history mr-1"></i>生成日期：2025年01月01日</p>
</div>
```

### 3.3 卡片容器

通用卡片，用于包裹任何内容（图表、表格、KPI 等）：

```html
<div class="bg-white rounded-xl shadow-sm p-4 mb-4">
  <h3 class="text-sm font-semibold text-gray-800 flex items-center gap-2 mb-3">
    <i class="bi bi-graph-up-arrow text-blue-500"></i>卡片标题
  </h3>
  <!-- 内容区域 -->
  <p class="text-xs text-gray-400 mt-3">
    数据来源：<a href="path/to/filename.csv" class="hover:underline text-blue-400" target="_blank">filename.csv</a>
  </p>
</div>
```

### 3.4 KPI 格子

```html
<div class="flex flex-wrap gap-3">
  <div class="flex-1 min-w-[140px] bg-gray-50 rounded-lg p-3 text-center">
    <p class="text-2xl font-bold text-gray-800">12,345</p>
    <p class="text-xs text-gray-500 mt-1">指标名称</p>
    <p class="text-xs text-green-600 mt-1">↑ 3.5%</p>
  </div>
  <!-- 更多格子 -->
</div>
```

### 3.5 筛选器

筛选器可放在任何位置。以下是常用模式：

**全局筛选器栏**（放在标题下方）：

```html
<div class="flex flex-wrap items-center gap-3 p-4 bg-white rounded-lg shadow-sm mb-6">
  <span class="text-sm text-gray-500 font-medium"><i class="bi bi-funnel mr-1"></i>筛选</span>
  <div class="flex items-center gap-1">
    <label class="text-sm text-gray-600">渠道</label>
    <select class="text-sm border border-gray-200 rounded-md px-2 py-1.5 bg-white focus:outline-none focus:ring-1 focus:ring-blue-400"
            onchange="applyFilters()">
      <option value="__all__">全部</option>
    </select>
  </div>
</div>
```

**卡片内嵌筛选器**（放在卡片标题右侧）：

```html
<div class="bg-white rounded-xl shadow-sm p-4 mb-4">
  <div class="flex items-center justify-between mb-3">
    <h3 class="text-sm font-semibold text-gray-800">卡片标题</h3>
    <select class="text-xs border border-gray-200 rounded px-2 py-1 bg-white">
      <option>近7天</option>
      <option>近30天</option>
    </select>
  </div>
  <!-- 内容 -->
</div>
```

### 3.6 ECharts 图表容器

```html
<div id="unique-chart-id" style="width:100%; height:280px;"></div>
<script>
(function() {
  var chart = echarts.init(document.getElementById('unique-chart-id'));
  window.__echartsInstances = window.__echartsInstances || [];
  window.__echartsInstances.push(chart);
  chart.setOption({ /* ECharts 配置 */ });
})();
</script>
```

图表高度参考：
- 数据点 ≤ 5：`200px`
- 数据点 6-12：`280px`
- 数据点 13+：`340px`
- 漏斗图 3-5 步：`280px`，6+ 步：`360px`

### 3.7 表格

```html
<div class="overflow-x-auto">
  <table class="w-full text-xs">
    <thead class="bg-gray-50">
      <tr>
        <th class="px-2 py-1.5 text-left font-medium text-gray-600">列名</th>
      </tr>
    </thead>
    <tbody>
      <tr class="border-t border-gray-50">
        <td class="px-2 py-1">数据</td>
      </tr>
    </tbody>
  </table>
</div>
```

长表格（> 10 行）加滚动容器：

```html
<div style="max-height:360px; overflow-y:auto;" class="rounded border border-gray-100">
  <table>...</table>
</div>
```

### 3.8 空状态

```html
<p class="text-gray-400 text-sm text-center py-8">暂无数据</p>
```

### 3.9 数据来源标注

```html
<p class="text-xs text-gray-400 mt-2">
  数据来源：<a href="path/to/file.csv" class="hover:underline text-blue-400" target="_blank">file.csv</a>
</p>
```

---

## 4. 数据嵌入模式

将 CSV 数据以 JSON 嵌入 HTML：

```html
<script>
  window.__data = {
    "funnel": [
      {"step": "访问", "count": 10000},
      {"step": "注册", "count": 6500}
    ],
    "trend": [
      {"date": "2025-01-01", "dau": 22000, "channel": "自然流量"}
    ]
  };
</script>
```

---

## 5. 筛选联动模式

以下是推荐的筛选联动 JS 模式，Agent 可直接使用或按需调整：

```html
<script>
function applyFilters() {
  // 1. 收集所有筛选器状态
  var filters = {};
  document.querySelectorAll('[data-filter]').forEach(function(el) {
    filters[el.dataset.filter] = el.value;
  });

  // 2. 对数据做客户端 filter
  var filtered = window.__data["datasetKey"].filter(function(row) {
    return Object.keys(filters).every(function(key) {
      return filters[key] === '__all__' || String(row[key]) === filters[key];
    });
  });

  // 3. 重新渲染受影响的图表/表格
  renderChart(filtered);
}
</script>
```

筛选器控件添加 `data-filter` 属性和 `onchange="applyFilters()"` 即可接入联动。

---

## 6. 全局脚本

页面底部统一添加 ECharts resize 处理：

```html
<script>
window.addEventListener('resize', function() {
  (window.__echartsInstances || []).forEach(function(c) { c && c.resize(); });
});
</script>
```

---

## 7. 响应式

- 使用 Tailwind 断点：`lg:` (1024px+) 和 `xl:` (1280px+)
- 移动端自动单列堆叠
- 筛选器栏用 `flex-wrap` 窄屏自动换行

---

## 8. 动画

```css
.fade-in { animation: fadeIn 0.3s ease-in; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
```

卡片容器添加 `fade-in` 类即可启用入场动画。
