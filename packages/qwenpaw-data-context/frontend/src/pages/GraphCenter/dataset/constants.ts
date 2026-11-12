import type { LayerId } from '../types';

/** Layer palette — identical to cm-graph-demo styles.css / graph.js. */
export const LAYER_COLOR: Record<LayerId, string> = {
  MG: '#2f6bd6',
  TG: '#7c4dd6',
  KG: '#1e9e6a',
  UG: '#d68a1e',
};

/** Four-layer node shapes (design D7): MG hexagon / TG diamond / KG rect / UG circle. */
export const LAYER_SHAPE: Record<LayerId, string> = {
  MG: 'hexagon',
  TG: 'diamond',
  KG: 'rect',
  UG: 'circle',
};

/** Chinese labels for relation types (full set present in the export). */
export const REL_LABEL: Record<string, string> = {
  DERIVES_FROM: '派生自',
  IN_SOURCE: '来源于数据源',
  HAS: '包含',
  HAS_DOMAIN: '包含数据域',
  EXECUTED_BY: '执行于',
  DECOMPOSES_INTO: '分解为',
  RELATED_TO: '关联',
  HAS_PARENT: '归属于',
  ABOUT: '关于',
  PRODUCES: '产出',
  DERIVED_FROM_STEP: '派生自步骤',
  SURFACE_METRIC: '表出指标',
  HAS_TASK: '包含任务',
  SPAWNS: '派生出',
};

export function relLabel(r: string): string {
  return REL_LABEL[r] || r;
}

export function clipText(s: unknown, n: number): string {
  const v = String(s == null ? '' : s)
    .replace(/\s+/g, ' ')
    .trim();
  return v.length > n ? v.slice(0, n) + '…' : v;
}
