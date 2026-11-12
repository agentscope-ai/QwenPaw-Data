/** Force-layout helpers for the global graph's four layer clusters.
 *  Direct port of forceCluster() from cm-graph-demo/js/pages.js — kept
 *  deterministic (golden-angle spiral init + fixed iterations) so repeated
 *  rebuilds produce identical layouts. */
import type { LayerId } from '../types';

export interface Point {
  x: number;
  y: number;
}

/** Direction bias for nodes with cross-layer edges: pulled toward the partner
 *  cluster's membrane so ribbons exit cleanly. */
export interface BiasMap {
  [nodeId: string]: Point;
}

/** 2x2 grid static orientation: MG top-left / TG top-right / KG bottom-left / UG bottom-right. */
const GRID: Record<LayerId, { col: number; row: number }> = {
  MG: { col: 0, row: 0 },
  TG: { col: 1, row: 0 },
  KG: { col: 0, row: 1 },
  UG: { col: 1, row: 1 },
};

export function dirBetween(a: LayerId, b: LayerId): Point {
  const dx = GRID[b].col - GRID[a].col;
  const dy = GRID[b].row - GRID[a].row;
  const d = Math.hypot(dx, dy) || 1;
  return { x: dx / d, y: dy / d };
}

/** Cluster force layout: spring links + truncated repulsion + collision pass.
 *  Pass ``prev`` (origin-centered positions from the last build) to warm-start
 *  and cut iterations — hop expand/shrink then settles in tens of ms instead
 *  of a full 200–500 iteration cold solve. */
export function forceCluster(
  ids: string[],
  edges: { source: string; target: string }[],
  cx: number,
  cy: number,
  seed: string | null,
  biasMap: BiasMap,
  colRadius: Record<string, number>,
  prev?: Record<string, Point>,
): Record<string, Point> {
  const n = ids.length;
  const out: Record<string, Point> = {};
  if (n === 0) return out;
  if (n === 1) {
    out[ids[0]] = { x: cx, y: cy };
    return out;
  }
  if (n <= 3) {
    const arr0 = ids.slice();
    const si0 = seed ? arr0.indexOf(seed) : -1;
    if (seed && si0 > 0) {
      arr0.splice(si0, 1);
      arr0.unshift(seed);
    }
    const spread = Math.max(100, 60 * n);
    arr0.forEach((id, i) => {
      const angle = (2 * Math.PI * i) / n - Math.PI / 2;
      out[id] = { x: cx + spread * Math.cos(angle), y: cy + spread * Math.sin(angle) };
    });
    return out;
  }
  const degById: Record<string, number> = {};
  edges.forEach((e) => {
    degById[e.source] = (degById[e.source] || 0) + 1;
    degById[e.target] = (degById[e.target] || 0) + 1;
  });
  const arr = ids.slice().sort((a, b) => (degById[b] || 0) - (degById[a] || 0));
  const si = seed ? arr.indexOf(seed) : -1;
  if (seed && si > 0) {
    arr.splice(si, 1);
    arr.unshift(seed);
  }
  const idx: Record<string, number> = {};
  arr.forEach((id, i) => {
    idx[id] = i;
  });
  const E: [number, number][] = [];
  const adjCount = arr.map(() => 0);
  edges.forEach((e) => {
    if (idx[e.source] != null && idx[e.target] != null && idx[e.source] !== idx[e.target]) {
      E.push([idx[e.source], idx[e.target]]);
      adjCount[idx[e.source]]++;
      adjCount[idx[e.target]]++;
    }
  });
  const maxDeg = Math.max(...adjCount) || 1;
  const avgDeg = (E.length * 2) / n;
  const linearity =
    Math.max(0, Math.min(1, 1 - avgDeg / 3)) * Math.max(0, Math.min(1, 1 - maxDeg / (n * 0.4)));
  const aspect = 1 + linearity * 1.8;
  const area = Math.max(140, 70 + 40 * Math.sqrt(n));
  const Rx = area * Math.sqrt(aspect);
  const Ry = area / Math.sqrt(aspect);
  const kLink = Math.max(80, Math.min(150, 0.95 * Math.sqrt((Math.PI * Rx * Ry) / n)));
  const CR = arr.map((id) => colRadius[id] || 36);
  let reused = 0;
  let prevCx = 0;
  let prevCy = 0;
  if (prev) {
    arr.forEach((id) => {
      const p = prev[id];
      if (!p) return;
      prevCx += p.x;
      prevCy += p.y;
      reused++;
    });
    if (reused) {
      prevCx /= reused;
      prevCy /= reused;
    }
  }
  const warm = reused >= Math.max(3, Math.ceil(n * 0.55));
  const P = arr.map((id, i): Point => {
    const cached = prev?.[id];
    if (cached) {
      return { x: cx + cached.x - prevCx, y: cy + cached.y - prevCy };
    }
    if (i === 0) return { x: cx, y: cy };
    const a = i * 2.399963229;
    const rNorm = Math.sqrt(i / n) * 0.9;
    return { x: cx + Rx * rNorm * Math.cos(a), y: cy + Ry * rNorm * Math.sin(a) };
  });
  const iters = warm
    ? Math.max(36, Math.min(80, Math.round(24 + 0.4 * n)))
    : Math.max(220, Math.min(520, Math.round(120 + 10 * n)));
  const cut = kLink * 2.4;
  for (let it = 0; it < iters; it++) {
    const cool = 1 - it / iters;
    const step = 2 + 18 * cool * cool;
    const disp = P.map(() => ({ x: 0, y: 0 }));
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const dx = P[i].x - P[j].x;
        const dy = P[i].y - P[j].y;
        const d = Math.hypot(dx, dy) || 0.01;
        if (d >= cut) continue;
        const f = ((kLink * kLink) / (d * d)) * 1.5;
        const ux = dx / d;
        const uy = dy / d;
        disp[i].x += ux * f;
        disp[i].y += uy * f;
        disp[j].x -= ux * f;
        disp[j].y -= uy * f;
      }
    }
    for (const [u, v] of E) {
      const dx2 = P[v].x - P[u].x;
      const dy2 = P[v].y - P[u].y;
      const d2 = Math.hypot(dx2, dy2) || 0.01;
      const f2 = (d2 - kLink) * 0.08;
      const ux2 = dx2 / d2;
      const uy2 = dy2 / d2;
      disp[u].x += ux2 * f2;
      disp[u].y += uy2 * f2;
      disp[v].x -= ux2 * f2;
      disp[v].y -= uy2 * f2;
    }
    for (let i = 1; i < n; i++) {
      disp[i].x += (cx - P[i].x) * 0.015;
      disp[i].y += (cy - P[i].y) * 0.015;
      const b = biasMap[arr[i]];
      if (b) {
        disp[i].x += (cx + b.x * Rx * 0.72 - P[i].x) * 0.05;
        disp[i].y += (cy + b.y * Ry * 0.72 - P[i].y) * 0.05;
      }
      const dl = Math.hypot(disp[i].x, disp[i].y) || 0.01;
      const lim = Math.min(dl, step);
      P[i].x += (disp[i].x / dl) * lim;
      P[i].y += (disp[i].y / dl) * lim;
      const ex = (P[i].x - cx) / Rx;
      const ey = (P[i].y - cy) / Ry;
      const er = Math.hypot(ex, ey);
      if (er > 1) {
        P[i].x = cx + ((P[i].x - cx) * (1 + (er - 1) * 0.5)) / er;
        P[i].y = cy + ((P[i].y - cy) * (1 + (er - 1) * 0.5)) / er;
      }
    }
  }
  for (let pass = 0; pass < 36; pass++) {
    let moved = false;
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const dx3 = P[j].x - P[i].x;
        const dy3 = P[j].y - P[i].y;
        const d3 = Math.hypot(dx3, dy3) || 0.01;
        const need = CR[i] + CR[j];
        if (d3 >= need) continue;
        const ux3 = dx3 / d3;
        const uy3 = dy3 / d3;
        const overlap = need - d3;
        if (i === 0) {
          P[j].x += ux3 * overlap;
          P[j].y += uy3 * overlap;
        } else {
          P[i].x -= (ux3 * overlap) / 2;
          P[i].y -= (uy3 * overlap) / 2;
          P[j].x += (ux3 * overlap) / 2;
          P[j].y += (uy3 * overlap) / 2;
        }
        moved = true;
      }
    }
    if (!moved) break;
  }
  arr.forEach((id, i) => {
    out[id] = P[i];
  });
  return out;
}
