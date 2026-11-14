/** 边路由规则：展开边任何折叠态都必须可达（代理路由），intra/ref 边只在两端可见时画。 */
import assert from 'node:assert/strict';
import { routeEdges, type RouteCtx, type NodeVis } from './exploreEdgeRouting';

const P = (x: number, y: number) => ({ x, y });

/** a(w1 种子) b(w2) c(w2) d(w1) — w2 的种子是 a */
function makeCtx(over?: Partial<RouteCtx>): RouteCtx {
  const waves: Record<string, number> = { a: 1, b: 2, c: 2, d: 1 };
  const vis: Record<string, NodeVis> = { a: 'shown', b: 'shown', c: 'shown', d: 'shown' };
  return {
    waveOf: (k) => waves[k] ?? 0,
    visOf: (k) => vis[k] ?? 'shown',
    nodePos: (k) => ({ a: P(10, 10), b: P(50, 50), c: P(60, 60), d: P(20, 20) })[k] || null,
    proxyPos: (w) => (w === 2 ? P(200, 100) : w === 1 ? P(200, 40) : null),
    isSeedOf: (w, k) => w === 2 && k === 'a',
    ...over,
  };
}

// 1. 展开边（from 是该波种子）两端可见 → 单条 expand 边，wave = 边自己的波
{
  const out = routeEdges([{ from: 'a', to: 'b', rel: 'HAS', wave: 2 }], makeCtx());
  assert.equal(out.length, 1);
  assert.equal(out[0].kind, 'expand');
  assert.equal(out[0].wave, 2);
  assert.equal(out[0].count, 1);
  assert.deepEqual(out[0].b, P(50, 50));
}

// 2. 展开边目标被折叠隐藏 → 端点路由到 w2 的代理（+N 超节点）位置
{
  const ctx = makeCtx({ visOf: (k) => (k === 'b' ? 'proxied' : 'shown') });
  const out = routeEdges([{ from: 'a', to: 'b', rel: 'HAS', wave: 2 }], ctx);
  assert.equal(out.length, 1);
  assert.equal(out[0].toProxy, true);
  assert.deepEqual(out[0].b, P(200, 100));
}

// 3. 两条展开边目标都藏进同一个折叠波 → 聚合成一条 count=2
{
  const ctx = makeCtx({ visOf: (k) => (k === 'b' || k === 'c' ? 'proxied' : 'shown') });
  const out = routeEdges(
    [
      { from: 'a', to: 'b', rel: 'HAS', wave: 2 },
      { from: 'a', to: 'c', rel: 'HAS', wave: 2 },
    ],
    ctx,
  );
  assert.equal(out.length, 1);
  assert.equal(out[0].count, 2);
  assert.equal(out[0].toProxy, true);
}

// 4. 折叠波预览内的 intra 边（两端都可见）必须画出来（回归：旧逻辑整波丢边）
{
  const out = routeEdges([{ from: 'b', to: 'c', rel: 'REL', wave: 2 }], makeCtx());
  assert.equal(out.length, 1);
  assert.equal(out[0].kind, 'intra');
}

// 5. intra 边一端被折叠隐藏 → 不画（收进超节点）
{
  const ctx = makeCtx({ visOf: (k) => (k === 'c' ? 'proxied' : 'shown') });
  const out = routeEdges([{ from: 'b', to: 'c', rel: 'REL', wave: 2 }], ctx);
  assert.equal(out.length, 0);
}

// 6. ref（非展开跨波）边一端隐藏 → 不画（由脊柱聚合表达）
{
  const ctx = makeCtx({ visOf: (k) => (k === 'b' ? 'proxied' : 'shown') });
  const out = routeEdges([{ from: 'd', to: 'b', rel: 'X', wave: 2 }], ctx);
  assert.equal(out.length, 0);
}

// 7. 端点尚未逐粒显现（pending）→ 展开边也先不画，显现后自然接上
{
  const ctx = makeCtx({ visOf: (k) => (k === 'b' ? 'pending' : 'shown') });
  const out = routeEdges([{ from: 'a', to: 'b', rel: 'HAS', wave: 2 }], ctx);
  assert.equal(out.length, 0);
}

// 8. 同一对代理端点、不同波次的展开边 → 按波分组，不得跨波合并
{
  const ctx = makeCtx({
    visOf: (k) => (k === 'b' ? 'proxied' : 'shown'),
    isSeedOf: (w, k) => (w === 2 || w === 3) && k === 'a',
  });
  const out = routeEdges(
    [
      { from: 'a', to: 'b', rel: 'HAS', wave: 2 },
      { from: 'a', to: 'b', rel: 'HAS', wave: 3 },
    ],
    ctx,
  );
  assert.equal(out.length, 2);
  assert.deepEqual(out.map((r) => r.wave).sort(), [2, 3]);
  out.forEach((r) => assert.equal(r.count, 1));
}

console.log('exploreEdgeRouting: all assertions passed');
