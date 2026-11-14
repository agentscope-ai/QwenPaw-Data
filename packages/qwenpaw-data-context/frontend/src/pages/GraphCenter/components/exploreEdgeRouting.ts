/** 探索画布的边路由（纯函数）：
 *  - expand 边（from 是该波种子）永远可见——被折叠隐藏的端点改接所属波的
 *    +N 超节点位置（代理路由），同一对端点的多条隐藏边聚成一条 ×count；
 *  - intra / ref 边只在两端可见时单独画（隐藏的分别由超节点/脊柱聚合表达）；
 *  - pending（逐粒队列还没显现）的端点：边先不画，显现后自然接上。 */
export interface Pt {
  x: number;
  y: number;
}

export type NodeVis = 'shown' | 'pending' | 'proxied';

export interface RouteInput {
  from: string;
  to: string;
  rel: string;
  /** 这条边到达画布的波次。 */
  wave: number;
}

export interface RouteCtx {
  waveOf(key: string): number;
  visOf(key: string): NodeVis;
  nodePos(key: string): Pt | null;
  /** 折叠波 +N 超节点的画布位置；波不存在时返回 null。 */
  proxyPos(wave: number): Pt | null;
  isSeedOf(wave: number, key: string): boolean;
}

export interface RoutedEdge {
  key: string;
  kind: 'intra' | 'expand' | 'ref';
  from: string;
  to: string;
  wf: number;
  wt: number;
  wave: number;
  rel: string;
  /** 聚合的隐藏边条数（1 = 单条独立边）。 */
  count: number;
  a: Pt;
  b: Pt;
  fromProxy: boolean;
  toProxy: boolean;
}

export function routeEdges(input: RouteInput[], ctx: RouteCtx): RoutedEdge[] {
  const out: RoutedEdge[] = [];
  const groups = new Map<string, RoutedEdge>();
  input.forEach((e) => {
    const wf = ctx.waveOf(e.from);
    const wt = ctx.waveOf(e.to);
    const expand = ctx.isSeedOf(e.wave, e.from);
    const kind: RoutedEdge['kind'] = expand ? 'expand' : wf === wt ? 'intra' : 'ref';
    const vf = ctx.visOf(e.from);
    const vt = ctx.visOf(e.to);
    if (vf === 'pending' || vt === 'pending') return;
    if (kind !== 'expand') {
      if (vf !== 'shown' || vt !== 'shown') return;
      const a = ctx.nodePos(e.from);
      const b = ctx.nodePos(e.to);
      if (!a || !b) return;
      out.push({
        key: `${e.from}|${e.rel}|${e.to}`,
        kind,
        from: e.from,
        to: e.to,
        wf,
        wt,
        wave: e.wave,
        rel: e.rel,
        count: 1,
        a,
        b,
        fromProxy: false,
        toProxy: false,
      });
      return;
    }
    const fromProxy = vf === 'proxied';
    const toProxy = vt === 'proxied';
    const a = fromProxy ? ctx.proxyPos(wf) : ctx.nodePos(e.from);
    const b = toProxy ? ctx.proxyPos(wt) : ctx.nodePos(e.to);
    if (!a || !b) return;
    if (!fromProxy && !toProxy) {
      out.push({
        key: `${e.from}|${e.rel}|${e.to}`,
        kind,
        from: e.from,
        to: e.to,
        wf,
        wt,
        wave: e.wave,
        rel: e.rel,
        count: 1,
        a,
        b,
        fromProxy,
        toProxy,
      });
      return;
    }
    // 按边的波次分组：同端点对但不同展开波的边不得合并，否则颜色/步骤牌会误标成首条边的波
    const gk = `${e.wave}|${fromProxy ? `proxy:${wf}` : e.from}→${toProxy ? `proxy:${wt}` : e.to}`;
    const g = groups.get(gk);
    if (g) {
      g.count += 1;
    } else {
      groups.set(gk, {
        key: gk,
        kind,
        from: e.from,
        to: e.to,
        wf,
        wt,
        wave: e.wave,
        rel: e.rel,
        count: 1,
        a,
        b,
        fromProxy,
        toProxy,
      });
    }
  });
  return [...out, ...groups.values()];
}
