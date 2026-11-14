import type { Artifact } from '../../types';
import { DIFF_FLASH_KEYFRAMES, DIFF_FLASH_OPTIONS } from '../compare/highlightFlash.js';

export type ArtifactPreviewKind =
  | 'audio'
  | 'csv'
  | 'html'
  | 'image'
  | 'json'
  | 'markdown'
  | 'pdf'
  | 'text'
  | 'unsupported'
  | 'video';

export const MAX_INLINE_TEXT_BYTES = 2 * 1024 * 1024;

const COMMENTABLE_PREVIEW_KINDS = new Set<ArtifactPreviewKind>([
  'csv',
  'html',
  'json',
  'markdown',
  'text',
]);

const IMAGE_EXTENSIONS = new Set(['avif', 'bmp', 'gif', 'jpeg', 'jpg', 'png', 'webp']);
const TEXT_EXTENSIONS = new Set([
  'css',
  'log',
  'py',
  'sql',
  'text',
  'txt',
  'ts',
  'tsx',
  'js',
  'jsx',
  'xml',
  'yaml',
  'yml',
]);
const AUDIO_EXTENSIONS = new Set(['aac', 'flac', 'm4a', 'mp3', 'ogg', 'wav']);
const VIDEO_EXTENSIONS = new Set(['m4v', 'mov', 'mp4', 'ogg', 'webm']);
const ECHARTS_INIT_PATTERN = /\becharts\s*\.\s*init\s*\(/i;
const ECHARTS_RESIZE_SUPPORT_MARKER = 'data-qwenpaw-data-echarts-resize-support';
const ECHARTS_RESIZE_SUPPORT = `<script ${ECHARTS_RESIZE_SUPPORT_MARKER}>
(function () {
  var instances = window.__echartsInstances = window.__echartsInstances || [];
  window.CHART_OPTS = window.CHART_OPTS || {};
  var palette = ['#1C54E3', '#7347FF', '#00CEC9', '#fb6a3c', '#60a5fa', '#047857'];
  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }
  function toNumber(value) {
    var number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }
  function dataValue(item) {
    if (Array.isArray(item)) return toNumber(item[item.length - 1]);
    if (item && typeof item === 'object') return toNumber(item.value);
    return toNumber(item);
  }
  function dataName(item, index) {
    if (item && typeof item === 'object' && !Array.isArray(item) && item.name) return item.name;
    return '项目 ' + (index + 1);
  }
  function seriesColor(series, index) {
    return series && series.itemStyle && series.itemStyle.color
      ? series.itemStyle.color
      : palette[index % palette.length];
  }
  function optionSeries(option) {
    if (!option || !option.series) return [];
    return Array.isArray(option.series) ? option.series : [option.series];
  }
  function renderBarFallback(option) {
    var series = optionSeries(option).filter(function (item) { return item && item.type === 'bar'; });
    if (!series.length) return '';
    var categories = option.xAxis && option.xAxis.data ? option.xAxis.data : [];
    var rowCount = Math.max(categories.length, series.reduce(function (max, item) {
      return Math.max(max, (item.data || []).length);
    }, 0));
    var totals = [];
    for (var index = 0; index < rowCount; index += 1) {
      totals[index] = series.reduce(function (sum, item) {
        return sum + Math.max(0, dataValue((item.data || [])[index]));
      }, 0);
    }
    var maxTotal = Math.max.apply(null, totals.concat([1]));
    var rows = '';
    for (var rowIndex = 0; rowIndex < rowCount; rowIndex += 1) {
      var label = categories[rowIndex] || dataName((series[0].data || [])[rowIndex], rowIndex);
      var bars = series.map(function (item, seriesIndex) {
        var value = Math.max(0, dataValue((item.data || [])[rowIndex]));
        var width = Math.max(2, value / maxTotal * 100);
        var title = (item.name || ('系列 ' + (seriesIndex + 1))) + ': ' + value.toLocaleString();
        return '<span title="' + escapeHtml(title) + '" style="display:inline-block;height:12px;width:' + width + '%;max-width:100%;background:' + escapeHtml(seriesColor(item, seriesIndex)) + ';border-radius:999px;margin-right:3px;"></span>';
      }).join('');
      rows += '<div style="display:grid;grid-template-columns:minmax(96px,160px) 1fr;gap:10px;align-items:center;margin:7px 0;">'
        + '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#475569;">' + escapeHtml(label) + '</span>'
        + '<span style="display:flex;align-items:center;gap:3px;min-width:0;">' + bars + '</span>'
        + '</div>';
    }
    return rows;
  }
  function renderPieFallback(option) {
    var series = optionSeries(option).find(function (item) { return item && item.type === 'pie'; });
    if (!series) return '';
    var data = series.data || [];
    var total = data.reduce(function (sum, item) { return sum + Math.max(0, dataValue(item)); }, 0) || 1;
    return data.map(function (item, index) {
      var value = Math.max(0, dataValue(item));
      var percent = value / total * 100;
      return '<div style="display:grid;grid-template-columns:minmax(80px,140px) 1fr auto;gap:10px;align-items:center;margin:8px 0;">'
        + '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#475569;">' + escapeHtml(dataName(item, index)) + '</span>'
        + '<span style="height:12px;background:#e2e8f0;border-radius:999px;overflow:hidden;"><span style="display:block;height:100%;width:' + percent + '%;background:' + escapeHtml(seriesColor(series, index)) + ';"></span></span>'
        + '<span style="color:#64748b;font-variant-numeric:tabular-nums;">' + percent.toFixed(1) + '%</span>'
        + '</div>';
    }).join('');
  }
  function renderStaticChart(option) {
    if (!option || !optionSeries(option).length) return '';
    var body = renderPieFallback(option) || renderBarFallback(option);
    if (!body) return '';
    var title = option.title && option.title.text ? option.title.text : '图表静态预览';
    return '<div style="min-height:220px;border:1px solid #dbe4f0;border-radius:12px;background:#fff;color:#334155;padding:14px 16px;font:12px/1.6 -apple-system,BlinkMacSystemFont,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif;">'
      + '<div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px;">'
      + '<strong style="font-size:13px;color:#172033;">' + escapeHtml(title) + '</strong>'
      + '<span style="color:#94a3b8;">ECharts 未加载，显示静态预览</span>'
      + '</div>'
      + body
      + '</div>';
  }
  function renderPlaceholder() {
    return '<div style="min-height:220px;border:1px dashed #cbd5e1;border-radius:12px;background:#f8fafc;color:#64748b;display:flex;align-items:center;justify-content:center;padding:16px;text-align:center;">图表运行库未加载，已保留同卡片的数据表/指标作为 fallback。</div>';
  }
  function installFallbackEcharts() {
    if (window.echarts && typeof window.echarts.init === 'function') return false;
    if (window.__qwenpawDataFallbackEchartsInstalled) return true;
    window.__qwenpawDataFallbackEchartsInstalled = true;
    window.echarts = {
      __qwenpawDataFallback: true,
      init: function (dom) {
        return {
          setOption: function (option) {
            if (!dom) return;
            if (dom.id) window.CHART_OPTS[dom.id] = option;
            if (dom.dataset.qwenpawDataFallbackRendered === 'true') return;
            dom.dataset.qwenpawDataFallbackRendered = 'true';
            dom.innerHTML = renderStaticChart(option) || renderPlaceholder();
          },
          resize: function () {},
          dispose: function () {}
        };
      },
      getInstanceByDom: function () { return null; }
    };
    return true;
  }
  function trackEcharts() {
    if (!window.echarts || typeof window.echarts.init !== 'function' || window.echarts.__qwenpawDataFallback) return false;
    var originalInit = window.echarts.init;
    if (originalInit.__qwenpawDataTracked) return true;
    function trackedInit() {
      var instance = originalInit.apply(this, arguments);
      if (instances.indexOf(instance) < 0) instances.push(instance);
      return instance;
    }
    trackedInit.__qwenpawDataTracked = true;
    window.echarts.init = trackedInit;
    return true;
  }
  function resizeCharts() {
    window.requestAnimationFrame(function () {
      instances.forEach(function (instance) {
        if (instance && typeof instance.resize === 'function') instance.resize();
      });
    });
  }
  function chartContainers() {
    return document.querySelectorAll('.chart-container, .chart-box > div[id], div[id^="chart-"], div[id^="chart_"]');
  }
  function hasChartContent(container) {
    return container.children.length > 0 || container.textContent.trim();
  }
  function isRenderable(container) {
    var rect = container.getBoundingClientRect ? container.getBoundingClientRect() : null;
    return !!((rect && rect.width > 0 && rect.height > 0) || (container.offsetWidth > 0 && container.offsetHeight > 0));
  }
  function renderMissingChartFallback() {
    var hasRealEcharts = trackEcharts();
    if (!hasRealEcharts) installFallbackEcharts();
    chartContainers().forEach(function (container) {
      if (container.dataset.qwenpawDataFallbackRendered === 'true') return;
      if (hasChartContent(container)) return;
      if (hasRealEcharts && !isRenderable(container)) return;
      container.dataset.qwenpawDataFallbackRendered = 'true';
      var option = container.id && window.CHART_OPTS ? window.CHART_OPTS[container.id] : null;
      container.innerHTML = renderStaticChart(option) || renderPlaceholder();
    });
  }

  // 同步装桩：真 echarts 缺失时立即装降级桩，赶在图表内联脚本执行前捕获 option
  if (!trackEcharts()) installFallbackEcharts();

  [0, 120, 600, 1200].forEach(function (delay) {
    setTimeout(function () {
      trackEcharts();
      resizeCharts();
    }, delay);
  });
  document.addEventListener('DOMContentLoaded', function () {
    trackEcharts();
    resizeCharts();
    setTimeout(renderMissingChartFallback, 1200);
  });
  window.addEventListener('load', function () {
    trackEcharts();
    resizeCharts();
    setTimeout(renderMissingChartFallback, 300);
  });
  document.addEventListener('click', function () {
    trackEcharts();
    setTimeout(resizeCharts, 0);
    setTimeout(resizeCharts, 120);
    setTimeout(renderMissingChartFallback, 1200);
  });
  window.addEventListener('resize', function () {
    trackEcharts();
    resizeCharts();
  });
})();
</script>`;

function getExtension(fileName: string) {
  const extension = fileName.split('.').pop()?.toLowerCase();
  return extension && extension !== fileName.toLowerCase() ? extension : '';
}

export function getArtifactPreviewKind(artifact: Artifact): ArtifactPreviewKind {
  const extension = getExtension(artifact.name);
  const mimeType = artifact.mimeType?.split(';', 1)[0]?.trim().toLowerCase() ?? '';
  const artifactType = artifact.type.toLowerCase();

  if (mimeType === 'application/pdf' || extension === 'pdf') return 'pdf';
  if (
    mimeType === 'text/html' ||
    artifactType === 'html' ||
    extension === 'html' ||
    extension === 'htm'
  ) {
    return 'html';
  }
  if (
    mimeType === 'text/csv' ||
    artifactType === 'csv' ||
    extension === 'csv' ||
    extension === 'tsv'
  ) {
    return 'csv';
  }
  if (mimeType === 'application/json' || extension === 'json') return 'json';
  if (
    mimeType === 'text/markdown' ||
    artifactType === 'markdown' ||
    extension === 'md' ||
    extension === 'mdx'
  ) {
    return 'markdown';
  }
  if (
    (mimeType.startsWith('image/') && mimeType !== 'image/svg+xml') ||
    IMAGE_EXTENSIONS.has(extension)
  ) {
    return 'image';
  }
  if (mimeType.startsWith('audio/') || AUDIO_EXTENSIONS.has(extension)) return 'audio';
  if (mimeType.startsWith('video/') || VIDEO_EXTENSIONS.has(extension)) return 'video';
  if (
    mimeType.startsWith('text/') ||
    TEXT_EXTENSIONS.has(extension) ||
    ['python', 'sql'].includes(artifactType)
  ) {
    return 'text';
  }
  return 'unsupported';
}

export function isCommentablePreviewKind(kind: ArtifactPreviewKind): boolean {
  return COMMENTABLE_PREVIEW_KINDS.has(kind);
}

export interface CsvPreview {
  rows: string[][];
  truncated: boolean;
}

export function parseCsvPreview(
  source: string,
  delimiter = ',',
  maxRows = 200,
  maxColumns = 50,
): CsvPreview {
  const rows: string[][] = [];
  let row: string[] = [];
  let value = '';
  let quoted = false;
  let index = 0;
  let truncated = false;

  const pushValue = () => {
    if (row.length < maxColumns) row.push(value);
    value = '';
  };
  const pushRow = () => {
    pushValue();
    rows.push(row);
    row = [];
  };

  while (index < source.length) {
    const char = source[index];
    if (quoted) {
      if (char === '"' && source[index + 1] === '"') {
        value += '"';
        index += 2;
        continue;
      }
      if (char === '"') {
        quoted = false;
      } else {
        value += char;
      }
    } else if (char === '"' && value.length === 0) {
      quoted = true;
    } else if (char === delimiter) {
      pushValue();
    } else if (char === '\n') {
      pushRow();
      if (rows.length >= maxRows) {
        truncated = index + 1 < source.length;
        break;
      }
    } else if (char !== '\r') {
      value += char;
    }
    index += 1;
  }

  if (!truncated && (value.length > 0 || row.length > 0)) pushRow();
  return { rows, truncated };
}

export function formatJsonPreview(source: string) {
  try {
    return JSON.stringify(JSON.parse(source), null, 2);
  } catch {
    return source;
  }
}

export function htmlUsesEcharts(source: string): boolean {
  return ECHARTS_INIT_PATTERN.test(source);
}

export function injectEchartsResizeSupport(source: string): string {
  if (!htmlUsesEcharts(source) || source.includes(ECHARTS_RESIZE_SUPPORT_MARKER)) {
    return source;
  }

  const closingHeadIndex = source.search(/<\/head\s*>/i);
  if (closingHeadIndex >= 0) {
    return `${source.slice(0, closingHeadIndex)}${ECHARTS_RESIZE_SUPPORT}\n${source.slice(closingHeadIndex)}`;
  }

  const openingBodyIndex = source.search(/<body\b/i);
  if (openingBodyIndex >= 0) {
    return `${source.slice(0, openingBodyIndex)}<head>${ECHARTS_RESIZE_SUPPORT}</head>\n${source.slice(openingBodyIndex)}`;
  }

  return `${ECHARTS_RESIZE_SUPPORT}\n${source}`;
}

/**
 * Scroll bridge for the whole-GT iframe, used by the compare panel's sync scroll.
 *
 * That frame is rendered with `sandbox="allow-scripts"` and deliberately without
 * `allow-same-origin`, so it gets an opaque origin and `contentDocument` is
 * unreachable from the host. `postMessage` still crosses that boundary: the frame
 * reports its own scroll ratio and accepts one back. Adding `allow-same-origin`
 * instead would let the GT document reach the host DOM, which is not a trade
 * worth making for a scroll position.
 *
 * Ratios travel alongside pixel offsets: the compare panel maps positions
 * between the two documents through matching heading anchors, which needs the
 * frame's scrollTop in pixels, and keeps the ratio only as its no-anchor
 * fallback. The frame also reports its heading offsets (`anchors`) on load and
 * on demand (`collect`), so the panel can build that map across the origin
 * boundary.
 */
export const GT_SCROLL_CHANNEL = 'qwenpaw-data-report-gt-scroll';

const SCROLL_BRIDGE_MARKER = 'data-qwenpaw-data-scroll-bridge';
const SCROLL_BRIDGE = `<script ${SCROLL_BRIDGE_MARKER}>
(function () {
  var CHANNEL = '${GT_SCROLL_CHANNEL}';
  var locked = false;
  var active = null;
  function scroller() {
    if (active && active.isConnected && active.scrollHeight - active.clientHeight > 1) return active;
    var root = document.scrollingElement || document.documentElement;
    if (root.scrollHeight - root.clientHeight > 1) return root;
    // Many exported reports scroll a height:100vh main/div with body overflow
    // hidden. Discover it before the first user scroll, including the peer pane.
    var candidates = document.querySelectorAll('body *');
    var area = 0;
    for (var i = 0; i < candidates.length; i++) {
      var el = candidates[i];
      if (el.clientHeight <= 0 || el.scrollHeight - el.clientHeight <= 1) continue;
      var overflow = window.getComputedStyle(el).overflowY;
      if (overflow !== 'auto' && overflow !== 'scroll') continue;
      var size = el.clientWidth * el.clientHeight;
      if (size > area) { active = el; area = size; }
    }
    return active || root;
  }
  function release() {
    window.requestAnimationFrame(function () {
      window.setTimeout(function () { locked = false; }, 0);
    });
  }
  function isDocumentScroller(node) {
    return node === document.scrollingElement
      || node === document.documentElement
      || node === document.body;
  }
  function contentTop(el, node) {
    var rect = el.getBoundingClientRect();
    if (isDocumentScroller(node)) {
      return rect.top + (window.pageYOffset || 0);
    }
    return rect.top - node.getBoundingClientRect().top + node.scrollTop;
  }
  function report(node) {
    if (locked || !node) return;
    var max = node.scrollHeight - node.clientHeight;
    if (max <= 1) return;
    try {
      window.parent.postMessage(
        {
          channel: CHANNEL,
          kind: 'scroll',
          ratio: node.scrollTop / max,
          top: node.scrollTop,
        },
        '*'
      );
    } catch (error) {
      /* the compare pane may already be unmounted */
    }
  }
  // Report programmatic position without treating it as a user scroll.
  function reportPosition() {
    var node = scroller();
    if (!node) return;
    var max = node.scrollHeight - node.clientHeight;
    window.parent.postMessage({ channel: CHANNEL, kind: 'position',
      top: node.scrollTop, ratio: max > 1 ? node.scrollTop / max : 0 }, '*');
  }
  function reportAnchors() {
    var node = scroller();
    if (!node || !document.body) return;
    var items = [];
    var headings = document.querySelectorAll('h1,h2,h3,h4,h5,h6');
    for (var i = 0; i < headings.length; i++) {
      items.push({
        t: headings[i].textContent || '',
        y: contentTop(headings[i], node),
      });
    }
    try {
      window.parent.postMessage(
        { channel: CHANNEL, kind: 'anchors', items: items },
        '*'
      );
    } catch (error) {
      /* the compare pane may already be unmounted */
    }
  }
  function locate(text) {
    if (!text || !document.body) return;
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    var node;
    var target = null;
    while ((node = walker.nextNode())) {
      if (node.nodeValue && node.nodeValue.indexOf(text) >= 0) {
        target = node.parentElement;
        break;
      }
    }
    if (!target || typeof target.scrollIntoView !== 'function') return;
    locked = true;
    target.scrollIntoView({ block: 'center' });
    release();
  }
  window.addEventListener('message', function (event) {
    if (event.source !== window.parent) return;
    var data = event.data;
    if (!data || data.channel !== CHANNEL) return;
    if (data.kind === 'diff-focus' && /^diff-[0-9]+$/.test(data.id || '')) {
      var hit = document.querySelector('[data-qwenpaw-data-diff-id="' + data.id + '"]');
      if (hit) {
        locked = true;
        hit.scrollIntoView({ block: 'center' });
        hit.getAnimations().forEach(function (animation) {
          if (animation.id === '${DIFF_FLASH_OPTIONS.id}') animation.cancel();
        });
        hit.animate(${JSON.stringify(DIFF_FLASH_KEYFRAMES)}, ${JSON.stringify(DIFF_FLASH_OPTIONS)});
        reportPosition();
        release();
      }
      return;
    }
    if (data.kind === 'locate') { locate(data.text); return; }
    if (data.kind === 'collect') { reportAnchors(); reportPosition(); return; }
    if (data.kind !== 'apply') return;
    var node = scroller();
    if (!node) return;
    var max = node.scrollHeight - node.clientHeight;
    if (max <= 1) return;
    var top = Number(data.top);
    if (!isFinite(top)) {
      var ratio = Number(data.ratio);
      if (!isFinite(ratio)) return;
      top = Math.max(0, Math.min(1, ratio)) * max;
    }
    locked = true;
    node.scrollTop = Math.max(0, Math.min(max, top));
    reportPosition();
    release();
  });
  document.addEventListener('scroll', function (event) {
    var target = event.target;
    if (target && target.nodeType === 1
        && target.scrollHeight - target.clientHeight > 1) {
      if (active !== target) { active = target; reportAnchors(); }
    }
    report(scroller());
  }, true);
  document.addEventListener('click', function (event) {
    var hit = event.target && event.target.closest ? event.target.closest('[data-qwenpaw-data-diff-id]') : null;
    if (hit) window.parent.postMessage({ channel: CHANNEL, kind: 'diff-select', id: hit.getAttribute('data-qwenpaw-data-diff-id') }, '*');
  });
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', reportAnchors);
  } else {
    reportAnchors();
  }
  // Chart scripts resize their containers after first paint, and the fallback
  // renderer swaps placeholders for static charts as late as ~1.2s — each of
  // those shifts every heading below it. Re-measure on a widening schedule that
  // outlasts the chart settle, so the host's anchor map tracks the final layout
  // instead of a half-rendered one.
  window.addEventListener('load', function () {
    [400, 1200, 2400, 4000].forEach(function (delay) {
      window.setTimeout(reportAnchors, delay);
    });
  });
  // Belt and braces: a debounced ResizeObserver catches any late height change
  // (async charts, web fonts, images) that the fixed schedule above misses.
  if (typeof window.ResizeObserver === 'function') {
    var pending = null;
    var observer = new window.ResizeObserver(function () {
      if (pending) return;
      pending = window.setTimeout(function () {
        pending = null;
        reportAnchors();
      }, 300);
    });
    function observeBody() {
      if (document.body) observer.observe(document.body);
    }
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', observeBody);
    } else {
      observeBody();
    }
  }
})();
</script>`;

export function injectGtScrollBridge(source: string): string {
  if (source.includes(SCROLL_BRIDGE_MARKER)) {
    return source;
  }

  // Inject before the LAST </body>, never the first. A big inline bundle can
  // carry the literal text "</body>" inside a <script> — echarts' saveAsImage
  // builds a '<body>…</body>' string — and the first match lands inside that
  // script. Writing our own <script>…</script> there injects a "</script>" that
  // closes the bundle early, so the rest of the library renders as visible page
  // text. The document's real </body> is always the last one, after every script.
  const closingBodyPattern = /<\/body\s*>/gi;
  let closingBodyIndex = -1;
  let match: RegExpExecArray | null = closingBodyPattern.exec(source);
  while (match !== null) {
    closingBodyIndex = match.index;
    match = closingBodyPattern.exec(source);
  }
  if (closingBodyIndex >= 0) {
    return `${source.slice(0, closingBodyIndex)}${SCROLL_BRIDGE}\n${source.slice(closingBodyIndex)}`;
  }

  return `${source}\n${SCROLL_BRIDGE}`;
}
