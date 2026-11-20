/** 全局图谱 — port of Pages.globalGraph() from cm-graph-demo/js/pages.js.
 *  Cell-membrane clusters + cross-layer ribbons + BFS chips + Cypher console.
 *  Scope comes from the real logged-in session (GraphCenterContext):
 *  - member: own userspace only (server-enforced);
 *  - admin: whole workspace, optional drilldown into one member. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button } from '@/design';
import {
  BulbOutlined,
  CodeOutlined,
  DatabaseOutlined,
  InfoCircleOutlined,
  LeftOutlined,
  ReloadOutlined,
  RightOutlined,
  RollbackOutlined,
  ShareAltOutlined,
  UndoOutlined,
} from '@ant-design/icons';
import MgSyncBar from '@/components/MgSyncBar';
import MgAppendFloat, { type MgAppendKind } from '@/components/MgAppendFloat';
import MultiGraphView, { type MultiGraphData, type ChipDatum } from '../components/MultiGraphView';
import SearchPanel from '../components/SearchPanel';
import SearchJourneyPanel from '../components/SearchJourneyPanel';
import ExploreStage from '../components/ExploreStage';
import { runJourney } from '../dataset/journeyWalk';
import {
  CypherConsoleProvider,
  CypherConsoleToggle,
  CypherConsoleOverlay,
} from '../components/CypherConsole';
import { CrosshairIcon } from '../components/LocateButton';
import { forceCluster, dirBetween, type BiasMap, type Point } from '../components/forceLayout';
import {
  idsFingerprint,
  layoutCacheKey,
  loadLayout,
  saveLayout,
  type LayoutSnapshot,
} from '../components/layoutCache';
import { useGraphCenter } from '../GraphCenterContext';
import { LAYER_COLOR } from '../dataset/constants';
import { isPrivateNode, nodeOwnerId, type LayerCenter } from '../dataset/graphDb';
import { useTopics } from '../dataset/useTopics';
import TopicView from '../components/TopicView';
import TopicChipRail from '../components/TopicChipRail';
import DatasourceChipRail from '../components/DatasourceChipRail';
import type { LayerId } from '../types';
import FadePresence from '../components/FadePresence';
import styles from '../index.module.less';

const TOPIC_PALETTE = ['#7C4DD6', '#14B8A6', '#DB2777', '#2563EB', '#D97706', '#65A30D'];

const LAYERS: LayerId[] = ['MG', 'KG', 'UG'];
const SEARCH_PANEL_KEY = 'graph-center.global.searchPanelOpen';
const SEARCH_PANEL_NARROW = '(max-width: 1099px)';
const INITIAL_HOP = 1;

function readSearchPanelOpen(): boolean {
  try {
    const stored = sessionStorage.getItem(SEARCH_PANEL_KEY);
    if (stored === '1') return true;
    if (stored === '0') return false;
  } catch {
    /* ignore */
  }
  return typeof window === 'undefined' ? true : !window.matchMedia(SEARCH_PANEL_NARROW).matches;
}
// Minimum gap between layer quadrants (center corridor reserved for ribbons).
const GAP = 340;
const CHIP_LABELS: Record<LayerId, string> = { MG: '元数据', TG: '轨迹', KG: '知识', UG: '用户' };
// Ribbon curvature: adjacent pairs bow outward, diagonals alternate — avoids
// stacking in the center corridor.
const PAIR_CURVE: Record<string, number> = {
  'MG|TG': -70,
  'KG|UG': 70,
  'KG|MG': -70,
  'TG|UG': 70,
  'MG|UG': -90,
  'KG|TG': -90,
};

/** Per-layer BFS state. `seed` is null until the first layerCenter resolves it
 *  (defaults to the highest-degree node in scope); `hop` starts at INITIAL_HOP. */
interface LayerState {
  seed: string | null;
  hop: number;
}

const emptyLayerState = (): LayerState => ({ seed: null, hop: INITIAL_HOP });

interface QueryView {
  q: string;
  ids: Set<string>;
  count: number;
}

interface CrossEdgeInfo {
  source: string;
  target: string;
  relation: string;
  sl: LayerId;
  tl: LayerId;
}

export default function GlobalGraphPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const {
    scope,
    db,
    dataset,
    source,
    memberId,
    setMemberId,
    datasourceId,
    setDatasourceId,
    reload,
    viewMode,
    setViewMode,
    loading,
    error,
    memberOptions,
    membersLoaded,
    dsNames,
  } = useGraphCenter();
  const isBiz = viewMode === 'biz';
  // Auth disabled → no session, treat as the global (admin) viewpoint.
  const isAdminView = scope.isAdmin || !scope.authEnabled;
  // Member default view is their userspace; admin default is the global MG.
  // The "我的图" chip sets memberId to self — that is the only admin view
  // that may pull the global MG into a personal mirror.
  const isOwnUserspaceView =
    !scope.authEnabled || (!scope.isAdmin && !memberId) || memberId === scope.userId;

  // 🧭 自主探索舞台：唯一入口是左侧搜索框右侧图标（设置 journey=1），
  // 输入 / 回车 / 示例词只刷新左侧结果列表。返回列表 / locating a
  // result exits back to the graph.
  const qParam = (searchParams.get('q') || '').trim();
  const journeyOn = searchParams.get('journey') === '1' && Boolean(qParam);
  const journey = useMemo(
    () => (journeyOn && qParam ? runJourney(db, qParam) : null),
    [journeyOn, qParam, db],
  );
  const patchJourneyParams = useCallback(
    (mutate: (next: URLSearchParams) => void) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          mutate(next);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );
  const exitJourney = useCallback(() => {
    patchJourneyParams((next) => {
      next.delete('journey');
    });
  }, [patchJourneyParams]);
  /** noHit 建议词：只回填搜索并退出舞台，不自动重启探索。 */
  const trySearchQuery = useCallback(
    (q: string) => {
      patchJourneyParams((next) => {
        next.set('q', q);
        next.delete('journey');
      });
    },
    [patchJourneyParams],
  );

  const locateParam = searchParams.get('locate');
  const locateNode = useMemo(
    () => (locateParam ? db.getNode(locateParam) : null),
    [locateParam, db],
  );
  const locateId = locateNode ? locateNode.id : null;
  const clearLocateParam = () => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete('locate');
        return next;
      },
      { replace: true },
    );
  };

  const [panelOpen, setPanelOpen] = useState(readSearchPanelOpen);
  const userExpandedPanel = useRef(false);
  useEffect(() => {
    try {
      userExpandedPanel.current = sessionStorage.getItem(SEARCH_PANEL_KEY) === '1';
    } catch {
      /* ignore */
    }
    const mq = window.matchMedia(SEARCH_PANEL_NARROW);
    const onNarrow = () => {
      if (mq.matches && !userExpandedPanel.current) setPanelOpen(false);
    };
    onNarrow();
    mq.addEventListener('change', onNarrow);
    return () => mq.removeEventListener('change', onNarrow);
  }, []);
  const toggleSearchPanel = () => {
    setPanelOpen((prev) => {
      const next = !prev;
      userExpandedPanel.current = next;
      try {
        sessionStorage.setItem(SEARCH_PANEL_KEY, next ? '1' : '0');
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  const [appendKind, setAppendKind] = useState<MgAppendKind | null>(null);
  const [showCrossEdges, setShowCrossEdges] = useState(true);
  const [expandedPairs, setExpandedPairs] = useState<Set<string>>(new Set());
  const [queryView, setQueryView] = useState<QueryView | null>(null);
  const [layerStates, setLayerStates] = useState<Record<LayerId, LayerState>>({
    MG: emptyLayerState(),
    TG: emptyLayerState(),
    KG: emptyLayerState(),
    UG: emptyLayerState(),
  });
  /** Bumps on explicit reset so the canvas re-fits once; hop/reload keep the camera. */
  const [cameraNonce, setCameraNonce] = useState(0);
  const layoutMem = useRef<LayoutSnapshot | null>(null);
  // Admin drilldown options come from the shell context (auth member
  // universe + UG supplement) so the chips and the locate auto-drilldown
  // share one list.

  // ── Topic discovery lens ────────────────────────────────────────────
  const { state: topicsState, recompute: recomputeTopics } = useTopics(source, db, memberId);
  const [activeTopicId, setActiveTopicId] = useState<string>('');
  useEffect(() => {
    setActiveTopicId('');
  }, [memberId]);
  // Topic lens is a business-view affordance; leaving biz drops the lens.
  useEffect(() => {
    if (!isBiz) setActiveTopicId('');
  }, [isBiz]);
  const topics = useMemo(
    () => (topicsState.phase === 'ready' ? topicsState.topics : []),
    [topicsState],
  );
  const topicsDiag = topicsState.phase === 'ready' ? topicsState.diagnostics : undefined;
  const topicsThresholds = topicsState.phase === 'ready' ? topicsState.thresholds : undefined;
  // Reason-aware empty text: the backend diagnoses WHY nothing clustered
  // (global-MG-only scope / single-layer data / no or weak cross links).
  const topicsEmptyText = useMemo(() => {
    switch (topicsDiag?.reason_code) {
      case 'global_mg_scope':
        return t('graphCenter.topicsEmptyGlobalMg');
      case 'single_layer_candidates':
        return t('graphCenter.topicsEmptySingleLayer', {
          layers: Object.keys(topicsDiag.layer_counts).join('/') || '—',
        });
      case 'no_cross_links':
        return t('graphCenter.topicsEmptyNoCrossLinks');
      case 'cross_links_too_weak':
        return t('graphCenter.topicsEmptyWeakCross');
      default:
        return t('graphCenter.topicsEmpty');
    }
  }, [topicsDiag, t]);
  const topicsEmptyDetail = topicsDiag
    ? t('graphCenter.topicsEmptyDetail', {
        layers:
          Object.entries(topicsDiag.layer_counts)
            .map(([k, v]) => `${k}·${v}`)
            .join(' ') || '—',
        sim: topicsDiag.max_cross_sim.toFixed(2),
        floor: (topicsThresholds?.cross_floor ?? 0.6).toFixed(2),
        hard: topicsDiag.cross_hard_edges,
      })
    : undefined;
  const activeTopic = topics.find((tp) => tp.id === activeTopicId) || null;
  const topicColorOf = useMemo(() => {
    const m = new Map<string, string>();
    topics.forEach((tp, i) => m.set(tp.id, TOPIC_PALETTE[i % TOPIC_PALETTE.length]));
    return m;
  }, [topics]);

  /** D8: at most one auto-drilldown per locate id. */
  const drilledOnceRef = useRef<string | null>(null);
  useEffect(() => {
    if (!locateParam) drilledOnceRef.current = null;
  }, [locateParam]);

  // Locate miss order (17-D7/D8/D9/D10/D17/D18): biz miss first, then ds, then
  // one member drilldown, then Dev/ACL failure. Never auto-switch viewMode.
  useEffect(() => {
    if (!locateParam || loading) return;
    if (!locateNode) return;

    if (!db.scopeHas(locateNode.id, datasourceId)) {
      setDatasourceId(db.dsForNode(locateNode.id));
      return;
    }
    if (datasourceId === 'all') {
      const d = db.dsForNode(locateNode.id);
      if (d !== 'all') setDatasourceId(d);
    }

    if (!isAdminView || !membersLoaded) return;
    if (drilledOnceRef.current === locateNode.id) return;
    const owner = nodeOwnerId(locateNode);
    if (!owner || owner === '__global__' || owner === memberId) return;
    const known = owner === scope.userId || memberOptions.some((m) => m.id === owner);
    if (!known) return;
    drilledOnceRef.current = locateNode.id;
    setMemberId(owner);
  }, [
    locateParam,
    locateNode,
    loading,
    db,
    datasourceId,
    setDatasourceId,
    isAdminView,
    membersLoaded,
    memberId,
    memberOptions,
    scope.userId,
    setMemberId,
  ]);

  const writeLocate = useCallback(
    (id: string) => {
      drilledOnceRef.current = null;
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set('locate', id);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const dsList = useMemo(() => ['all', ...db.datasourceList()], [db]);
  const dsItems = useMemo(
    () =>
      dsList.map((d) => ({
        id: d,
        label: d === 'all' ? t('graphCenter.dsAll') : dsNames[d] || d,
      })),
    [dsList, dsNames, t],
  );

  const skipScopeFit = useRef(true);
  useEffect(() => {
    if (skipScopeFit.current) {
      skipScopeFit.current = false;
      return;
    }
    layoutMem.current = null;
    setCameraNonce((n) => n + 1);
  }, [datasourceId, memberId, viewMode]);

  useEffect(() => {
    if (!isBiz) return;
    setQueryView(null);
  }, [isBiz]);

  // Drop a chip that vanished with the previous (demo / other-scope) dataset.
  useEffect(() => {
    if (datasourceId !== 'all' && !dsList.includes(datasourceId)) {
      setDatasourceId('all');
    }
  }, [dsList, datasourceId, setDatasourceId]);

  /** Reset per-layer BFS state + transient views (scope/ds change, reset). */
  const resetScope = () => {
    setLayerStates({
      MG: emptyLayerState(),
      TG: emptyLayerState(),
      KG: emptyLayerState(),
      UG: emptyLayerState(),
    });
    setExpandedPairs(new Set());
    setQueryView(null);
    layoutMem.current = null;
    setCameraNonce((n) => n + 1);
  };

  /** The full rebuild: computes the multi-graph datum from current state.
   *  Mirrors rebuild() from the demo line-for-line. */
  const built = useMemo(() => {
    const states: Record<LayerId, LayerState> = { ...layerStates };
    const centers = {} as Record<LayerId, LayerCenter>;
    const sets = {} as Record<LayerId, Set<string>>;

    if (queryView) {
      LAYERS.forEach((L) => {
        sets[L] = new Set();
      });
      queryView.ids.forEach((id) => {
        const n = db.getNode(id);
        if (n && sets[n.layer]) sets[n.layer].add(id);
      });
    } else {
      LAYERS.forEach((L) => {
        // Seed stays stable across hop changes: layerCenter resolves it
        // deterministically (highest-degree node) when not pinned yet.
        const seed = locateNode && locateNode.layer === L ? locateId : states[L].seed;
        const c = db.layerCenter(L, datasourceId, states[L].hop, seed);
        centers[L] = c;
        sets[L] = c.ids;
      });
    }

    // Cross-layer edges aggregated by layer pair within scope (ribbon ×N badge).
    const layerScope = {} as Record<LayerId, Set<string>>;
    LAYERS.forEach((L) => {
      layerScope[L] = new Set(db.layerScopeIds(L, datasourceId));
    });
    const pairEdges: Record<string, CrossEdgeInfo[]> = {};
    if (!queryView) {
      dataset.edges.forEach((e) => {
        const a = db.getNode(e.source);
        const b = db.getNode(e.target);
        if (!a || !b || a.layer === b.layer) return;
        if (!layerScope[a.layer] || !layerScope[b.layer]) return;
        if (!layerScope[a.layer].has(e.source) || !layerScope[b.layer].has(e.target)) return;
        const key = [a.layer, b.layer].sort().join('|');
        (pairEdges[key] = pairEdges[key] || []).push({
          source: e.source,
          target: e.target,
          relation: e.relation,
          sl: a.layer,
          tl: b.layer,
        });
      });
    }
    // Drop expanded pairs that no longer exist in scope.
    const validExpanded = new Set([...expandedPairs].filter((key) => pairEdges[key]));
    const focusMode = !queryView && showCrossEdges && validExpanded.size > 0;

    // Expanded state: pull real cross-edge endpoints into the shown sets (hot).
    const hotIds = new Set<string>();
    if (!queryView && showCrossEdges) {
      validExpanded.forEach((key) => {
        (pairEdges[key] || []).forEach((e) => {
          sets[e.sl].add(e.source);
          sets[e.tl].add(e.target);
          hotIds.add(e.source);
          hotIds.add(e.target);
        });
      });
    }
    const allShown = new Set<string>();
    LAYERS.forEach((L) => sets[L].forEach((i) => allShown.add(i)));

    // Query view: cross edges between hit nodes (direct gradient links).
    const qCross: CrossEdgeInfo[] = [];
    if (queryView) {
      dataset.edges.forEach((e) => {
        if (!allShown.has(e.source) || !allShown.has(e.target)) return;
        const a = db.getNode(e.source);
        const b = db.getNode(e.target);
        if (!a || !b || a.layer === b.layer) return;
        qCross.push({
          source: e.source,
          target: e.target,
          relation: e.relation,
          sl: a.layer,
          tl: b.layer,
        });
      });
    }

    // Direction bias: nodes with cross-layer links lean toward the partner
    // cluster's membrane so ribbons exit cleanly.
    const biasMap: BiasMap = {};
    const addBias = (id: string, L: LayerId, partner: LayerId) => {
      if (!sets[L].has(id)) return;
      const u = dirBetween(L, partner);
      const b = (biasMap[id] = biasMap[id] || { x: 0, y: 0 });
      b.x += u.x;
      b.y += u.y;
    };
    Object.keys(pairEdges).forEach((key) => {
      pairEdges[key].forEach((e) => {
        addBias(e.source, e.sl, e.tl);
        addBias(e.target, e.tl, e.sl);
      });
    });
    qCross.forEach((e) => {
      addBias(e.source, e.sl, e.tl);
      addBias(e.target, e.tl, e.sl);
    });
    Object.keys(biasMap).forEach((id) => {
      const b = biasMap[id];
      const d = Math.hypot(b.x, b.y) || 1;
      b.x /= d;
      b.y /= d;
    });

    // Pre-computed positions: per-layer force layout centered on origin, then
    // laid out into three layer clusters. Exact session-cache hits skip the
    // solver; otherwise warm-start from the last in-memory positions. BBoxes
    // only grow (never shrink) so hop changes don't reflow the other clusters.
    const fingerprint = idsFingerprint(sets);
    const hopKey = queryView
      ? `q:${[...queryView.ids].sort().join(',')}`
      : LAYERS.map((L) => String(states[L].hop)).join('-');
    const persistKey = layoutCacheKey(scope.workspaceId, datasourceId, hopKey, viewMode);
    const cached =
      layoutMem.current?.fingerprint === fingerprint
        ? layoutMem.current
        : (() => {
            const stored = loadLayout(persistKey);
            return stored?.fingerprint === fingerprint ? stored : null;
          })();
    const localPos = {} as Record<LayerId, Record<string, Point>>;
    const bbox = {} as Record<LayerId, { w: number; h: number }>;
    LAYERS.forEach((L) => {
      const ids = [...sets[L]];
      if (cached?.localPos[L] && cached.fingerprint === fingerprint) {
        localPos[L] = cached.localPos[L];
      } else {
        const sg = db.subFromIds(sets[L]);
        const degL: Record<string, number> = {};
        sg.edges.forEach((e) => {
          degL[e.source] = (degL[e.source] || 0) + 1;
          degL[e.target] = (degL[e.target] || 0) + 1;
        });
        const colR: Record<string, number> = {};
        ids.forEach((id) => {
          const base = Math.max(32, Math.min(56, 32 + (degL[id] || 0) * 3));
          colR[id] = base / 2 + (L === 'KG' ? 8 : 0) + 18;
        });
        const prevLocal = layoutMem.current?.localPos[L] || cached?.localPos[L];
        localPos[L] = forceCluster(ids, sg.edges, 0, 0, states[L].seed, biasMap, colR, prevLocal);
      }
      let minX = Infinity;
      let maxX = -Infinity;
      let minY = Infinity;
      let maxY = -Infinity;
      ids.forEach((id) => {
        const p = localPos[L][id];
        if (!p) return;
        if (p.x < minX) minX = p.x;
        if (p.x > maxX) maxX = p.x;
        if (p.y < minY) minY = p.y;
        if (p.y > maxY) maxY = p.y;
      });
      // Visual margin: outer hull glow padding(46) + max node radius(28) + label.
      const margin = 64;
      const rawW = ids.length > 1 ? maxX - minX : 0;
      const rawH = ids.length > 1 ? maxY - minY : 0;
      const next = { w: Math.max(220, rawW + margin * 2), h: Math.max(220, rawH + margin * 2) };
      const prevBox = layoutMem.current?.bbox[L];
      // Keep the last cell size unless the user explicitly reset (layoutMem cleared).
      bbox[L] = prevBox ? { w: Math.max(prevBox.w, next.w), h: Math.max(prevBox.h, next.h) } : next;
    });
    const colW0 = Math.max(bbox.MG.w, bbox.UG.w);
    const colW1 = bbox.KG.w;
    const rowH0 = Math.max(bbox.MG.h, bbox.KG.h);
    const QUAD: Record<LayerId, Point> = {
      MG: { x: colW0 / 2 + GAP, y: rowH0 / 2 + GAP },
      KG: { x: colW0 + GAP * 2 + colW1 / 2, y: rowH0 / 2 + GAP },
      UG: { x: (colW0 + colW1 + GAP) / 2 + GAP, y: rowH0 + GAP * 2 + bbox.UG.h / 2 },
      TG: { x: 0, y: 0 },
    };
    const pos: Record<string, Point> = {};
    LAYERS.forEach((L) => {
      const cx = QUAD[L].x;
      const cy = QUAD[L].y;
      Object.keys(localPos[L]).forEach((id) => {
        pos[id] = { x: localPos[L][id].x + cx, y: localPos[L][id].y + cy };
      });
    });

    // Port anchors: on the member support point along the centroid line + padding.
    const centroid = {} as Record<LayerId, Point>;
    LAYERS.forEach((L) => {
      const ids = [...sets[L]];
      if (!ids.length) {
        centroid[L] = { x: QUAD[L].x, y: QUAD[L].y };
        return;
      }
      let sx = 0;
      let sy = 0;
      ids.forEach((id) => {
        sx += pos[id].x;
        sy += pos[id].y;
      });
      centroid[L] = { x: sx / ids.length, y: sy / ids.length };
    });
    const portPos = (L: LayerId, partner: LayerId): Point => {
      const c = centroid[L];
      const tp = centroid[partner];
      let ux = tp.x - c.x;
      let uy = tp.y - c.y;
      const d = Math.hypot(ux, uy) || 1;
      ux /= d;
      uy /= d;
      let support = 0;
      sets[L].forEach((id) => {
        support = Math.max(support, (pos[id].x - c.x) * ux + (pos[id].y - c.y) * uy);
      });
      return { x: c.x + ux * (support + 24), y: c.y + uy * (support + 24) };
    };

    // Assemble MultiGraphData.
    const nodes = [...allShown].map((id) => {
      const n = db.getNode(id)!;
      const p = pos[id];
      return {
        id,
        label: n.label,
        type: n.type,
        layer: n.layer,
        x: p.x,
        y: p.y,
        focus: !queryView && (id === centers[n.layer]?.seed || id === locateId),
        hot: hotIds.has(id),
        dim: focusMode && !hotIds.has(id),
        priv: isPrivateNode(n), // owner-scoped mirror-add (owner != __global__) → dashed outline
      };
    });
    const sub = db.subFromIds(allShown);
    const intra = sub.edges
      .filter((e) => {
        const a = db.getNode(e.source);
        const b = db.getNode(e.target);
        return a && b && a.layer === b.layer;
      })
      .map((e) => ({ source: e.source, target: e.target, relation: e.relation, dim: focusMode }));

    const ports: MultiGraphData['ports'] = [];
    const ribbons: MultiGraphData['ribbons'] = [];
    const cross: MultiGraphData['cross'] = [];
    if (queryView) {
      qCross.forEach((e, i) => {
        cross.push({
          source: e.source,
          target: e.target,
          relation: e.relation,
          colorA: LAYER_COLOR[e.sl],
          colorB: LAYER_COLOR[e.tl],
          curveOffset: (i % 2 ? 1 : -1) * (14 + (i % 3) * 10),
          sx: pos[e.source].x,
          sy: pos[e.source].y,
          tx: pos[e.target].x,
          ty: pos[e.target].y,
        });
      });
    } else if (showCrossEdges) {
      Object.keys(pairEdges)
        .sort()
        .forEach((key) => {
          const [A, B] = key.split('|') as [LayerId, LayerId];
          if (!sets[A].size || !sets[B].size) return;
          const pa = portPos(A, B);
          const pb = portPos(B, A);
          const expanded = validExpanded.has(key);
          const dimmed = focusMode && !expanded;
          ports.push({ id: `port:${key}:${A}`, layer: A, x: pa.x, y: pa.y, dim: dimmed });
          ports.push({ id: `port:${key}:${B}`, layer: B, x: pb.x, y: pb.y, dim: dimmed });
          ribbons.push({
            pair: key,
            source: `port:${key}:${A}`,
            target: `port:${key}:${B}`,
            count: pairEdges[key].length,
            expanded,
            dim: dimmed,
            colorA: LAYER_COLOR[A],
            colorB: LAYER_COLOR[B],
            curveOffset: PAIR_CURVE[key] || 60,
            sx: pa.x,
            sy: pa.y,
            tx: pb.x,
            ty: pb.y,
          });
          if (expanded) {
            pairEdges[key].forEach((e, i) => {
              cross.push({
                source: e.source,
                target: e.target,
                relation: e.relation,
                colorA: LAYER_COLOR[e.sl],
                colorB: LAYER_COLOR[e.tl],
                curveOffset: (i % 2 ? 1 : -1) * (14 + (i % 3) * 10),
                sx: pos[e.source].x,
                sy: pos[e.source].y,
                tx: pos[e.target].x,
                ty: pos[e.target].y,
              });
            });
          }
        });
    }

    const members: Partial<Record<LayerId, string[]>> = {};
    LAYERS.forEach((L) => {
      members[L] = [...sets[L]];
    });

    const chips: ChipDatum[] = LAYERS.map((L) => {
      if (queryView) {
        return {
          layer: L,
          label: CHIP_LABELS[L],
          shown: sets[L].size,
          total: sets[L].size,
          done: true,
          shrinkDone: true,
        };
      }
      const c = centers[L];
      return {
        layer: L,
        label: CHIP_LABELS[L],
        shown: sets[L].size,
        total: c.total,
        done: sets[L].size >= c.total,
        shrinkDone: states[L].hop <= 0,
      };
    });

    const layout: LayoutSnapshot = { fingerprint, localPos, bbox };
    return {
      data: { nodes, ports, intra, ribbons, cross } as MultiGraphData,
      members,
      chips,
      layout,
      persistKey,
    };
  }, [
    db,
    dataset,
    datasourceId,
    locateNode,
    locateId,
    showCrossEdges,
    expandedPairs,
    queryView,
    layerStates,
    scope.workspaceId,
    viewMode,
  ]);

  useEffect(() => {
    if (!built.data.nodes.length) return;
    layoutMem.current = built.layout;
    saveLayout(built.persistKey, built.layout);
  }, [built]);

  const onGrow = (L: LayerId) => {
    if (queryView) return;
    setLayerStates((prev) => ({ ...prev, [L]: { ...prev[L], hop: prev[L].hop + 1 } }));
  };
  const onShrink = (L: LayerId) => {
    if (queryView) return;
    setLayerStates((prev) =>
      prev[L].hop <= 0 ? prev : { ...prev, [L]: { ...prev[L], hop: prev[L].hop - 1 } },
    );
  };

  const exitQueryView = () => {
    setQueryView(null);
  };

  const handleLocate = (id: string) => {
    exitQueryView();
    // 定位说明用户想看真实画布上的节点 —— 收起路径推演舞台。
    if (journeyOn) exitJourney();
    writeLocate(id);
  };

  const handleOpenDetail = (id: string) => {
    navigate(`/graph-center/entity/${encodeURIComponent(id)}`);
  };

  const handleNodeClick = (nid: string) => handleLocate(nid);

  const st = db.globalStats();
  const drillUser = memberOptions.find((m) => m.id === memberId);
  const viewerLabel = isAdminView
    ? memberId
      ? memberId === scope.userId
        ? t('graphCenter.viewerMyGraph', { name: scope.username || memberId.slice(0, 10) })
        : t('graphCenter.viewerMember', { name: drillUser?.username || memberId.slice(0, 10) })
      : t('graphCenter.viewerAdmin')
    : t('graphCenter.viewerMember', { name: scope.username || scope.userId });
  const xc = db.crossLayerCounts(datasourceId);
  const xcStr = Object.keys(xc).length
    ? Object.keys(xc)
        .map((k) => `${k} ${xc[k]} 条`)
        .join(' · ')
    : t('graphCenter.noCrossLinks');
  const hintText = queryView
    ? t('graphCenter.hintQuery', {
        viewer: viewerLabel,
        num: queryView.count,
        nodes: st.nodeCount,
        edges: st.edgeCount,
      })
    : t('graphCenter.hintMain', {
        viewer: viewerLabel,
        cross: showCrossEdges
          ? t('graphCenter.hintRibbons', { xc: xcStr })
          : t('graphCenter.hintRibbonsHidden'),
        nodes: st.nodeCount,
        edges: st.edgeCount,
      });
  const locateDenied = Boolean(locateParam && !locateNode && !loading && !error);
  const bannerKind = queryView && !isBiz ? 'query' : locateNode ? 'locate' : '';

  return (
    <CypherConsoleProvider onQueryView={setQueryView}>
      <div className={styles.globalSplit}>
        <div className={styles.globalMain}>
          {/* Search + canvas share one row so the panel bottom tracks the
          canvas column (hint sits under the canvas, aligned to its left
          border). */}
          <div className={styles.canvasRow}>
            {panelOpen && (
              <SearchPanel
                activeId={locateParam}
                locateError={locateDenied ? t('graphCenter.locateDenied') : null}
                onLocate={handleLocate}
                onOpenDetail={handleOpenDetail}
              />
            )}
            <button
              type="button"
              className={styles.searchPanelHandle}
              title={
                panelOpen
                  ? t('graphCenter.searchPanelCollapse')
                  : t('graphCenter.searchPanelExpand')
              }
              aria-label={
                panelOpen
                  ? t('graphCenter.searchPanelCollapse')
                  : t('graphCenter.searchPanelExpand')
              }
              onClick={toggleSearchPanel}
            >
              {panelOpen ? <LeftOutlined /> : <RightOutlined />}
            </button>
            <div className={styles.canvasCol}>
              {/* One compact toolbar: real-session viewpoint + datasource filters on
          the left, actions right-aligned; MG sync folded into a status chip. */}
              <div className={styles.toolbar}>
                {/* Viewpoint switching lives in the shell-level ViewerSwitcher
            (above the tabs); the toolbar keeps datasource + topic lens. */}
                <span
                  className={`${styles.toolbarGroupDs} ${isBiz ? styles.toolbarGroupDsCapped : ''}`}
                >
                  <span className={styles.chipsLabel}>
                    <DatabaseOutlined />
                    {t('graphCenter.dsLabel')}
                  </span>
                  <DatasourceChipRail
                    items={dsItems}
                    activeId={datasourceId}
                    onSelect={(d) => {
                      setDatasourceId(d);
                      if (locateId && !db.scopeHas(locateId, d)) clearLocateParam();
                      resetScope();
                    }}
                  />
                </span>

                {/* Topic lens is a business-view affordance — hidden in Dev. */}
                {isBiz && (
                  <>
                    <span className={styles.toolbarSep} />

                    {/* Topic lens chips: leftover width budget; overflow →「⋯ 更多」。 */}
                    <span className={styles.toolbarGroupTopics}>
                      <span className={styles.chipsLabel}>
                        <BulbOutlined />
                        {t('graphCenter.topicsLabel')}
                      </span>
                      {topicsState.phase === 'computing' || topicsState.phase === 'loading' ? (
                        <span className={styles.topicStatusChip}>
                          {t('graphCenter.topicsComputing')}
                        </span>
                      ) : topicsState.phase === 'uninitialized' ||
                        topicsState.phase === 'initializing' ? (
                        <span className={styles.topicStatusChip}>
                          {t(`kgBrowser.mgSync.${topicsState.phase}`)}
                        </span>
                      ) : topicsState.phase === 'error' ? (
                        <span className={styles.topicStatusChip} title={topicsState.message}>
                          {t('graphCenter.topicsError')}
                        </span>
                      ) : topics.length === 0 ? (
                        <span className={styles.topicStatusChip} title={topicsEmptyDetail}>
                          {topicsEmptyText}
                        </span>
                      ) : (
                        <TopicChipRail
                          topics={topics}
                          activeTopicId={activeTopicId}
                          topicColorOf={topicColorOf}
                          fallbackColor={TOPIC_PALETTE[0]}
                          onToggle={(id) => setActiveTopicId((prev) => (prev === id ? '' : id))}
                        />
                      )}
                      {topics.length > 0 && topicsDiag?.reason_code === 'global_mg_scope' && (
                        <span
                          className={styles.topicScopeHint}
                          title={t('graphCenter.topicsGlobalMgHintTip')}
                        >
                          <InfoCircleOutlined />
                          {t('graphCenter.topicsGlobalMgHint')}
                        </span>
                      )}
                      <button
                        className={styles.topicRecompute}
                        title={t('graphCenter.topicsRecomputeTip')}
                        disabled={
                          topicsState.phase === 'computing' || topicsState.phase === 'loading'
                        }
                        onClick={() => recomputeTopics()}
                      >
                        <ReloadOutlined />
                        {t('graphCenter.topicsRecompute')}
                      </button>
                    </span>
                  </>
                )}

                <span className={styles.toolbarRight}>
                  {/* Userspace MG sync — own-userspace view only, every role incl.
              admins (我的图). Shown in both biz/dev modes; permission is
              checked by MgSyncBar against session capabilities; failed status
              queries keep the retry action available. */}
                  {source === 'api' && isOwnUserspaceView && (
                    <MgSyncBar compact onGraphChanged={reload} onOpenAppend={setAppendKind} />
                  )}
                  <Button
                    size="small"
                    type={showCrossEdges ? 'primary' : 'default'}
                    ghost={showCrossEdges}
                    icon={<ShareAltOutlined />}
                    onClick={() => {
                      setShowCrossEdges((v) => {
                        if (v) setExpandedPairs(new Set());
                        return !v;
                      });
                    }}
                  >
                    {t('graphCenter.ribbonsToggle')}
                  </Button>
                  {expandedPairs.size > 0 && (
                    <Button size="small" onClick={() => setExpandedPairs(new Set())}>
                      {t('graphCenter.collapsePairs', { num: expandedPairs.size })}
                    </Button>
                  )}
                  <Button
                    size="small"
                    type="dashed"
                    icon={<UndoOutlined />}
                    onClick={() => {
                      resetScope();
                    }}
                  >
                    {t('graphCenter.resetView')}
                  </Button>
                  <span
                    className={styles.modeSwitch}
                    role="group"
                    aria-label={t('graphCenter.modeLabel')}
                  >
                    <button
                      type="button"
                      className={`${styles.modeBtn} ${isBiz ? styles.modeBtnActive : ''}`}
                      onClick={() => {
                        if (!isBiz) {
                          setViewMode('biz');
                          resetScope();
                        }
                      }}
                    >
                      {t('graphCenter.modeBiz')}
                    </button>
                    <button
                      type="button"
                      className={`${styles.modeBtn} ${!isBiz ? styles.modeBtnActive : ''}`}
                      onClick={() => {
                        if (isBiz) {
                          setViewMode('dev');
                          resetScope();
                        }
                      }}
                    >
                      {t('graphCenter.modeDev')}
                    </button>
                  </span>
                  {!isBiz && <CypherConsoleToggle />}
                </span>
              </div>

              {/* Banner: Cypher query view or Locate */}
              <div className={styles.banner}>
                <FadePresence
                  key={bannerKind || 'idle'}
                  open={Boolean(bannerKind)}
                  className={styles.locateBanner}
                >
                  {bannerKind === 'query' && queryView ? (
                    <>
                      <CodeOutlined />
                      <span>
                        {t('graphCenter.queryBanner', { num: queryView.count })}{' '}
                        <code>{queryView.q.slice(0, 64)}</code> {t('graphCenter.queryBannerSub')}
                      </span>
                      <Button
                        size="small"
                        type="primary"
                        icon={<RollbackOutlined />}
                        onClick={exitQueryView}
                      >
                        {t('graphCenter.backToFullGraph')}
                      </Button>
                    </>
                  ) : locateNode ? (
                    <>
                      <CrosshairIcon size={16} />
                      <span>
                        {t('graphCenter.locateBannerPre')} <b>{locateNode.label}</b>（
                        {locateNode.layer} · {locateNode.type}）{t('graphCenter.locateBannerPost')}
                      </span>
                      <Button size="small" type="dashed" onClick={clearLocateParam}>
                        {t('graphCenter.clearLocate')}
                      </Button>
                    </>
                  ) : null}
                </FadePresence>
              </div>
              <div className={styles.globalDark}>
                {journey ? (
                  <ExploreStage
                    query={qParam}
                    onBack={exitJourney}
                    fallback={
                      <div className={styles.journeyStage}>
                        <SearchJourneyPanel
                          stage
                          journey={journey}
                          onLocate={(id) => {
                            exitJourney();
                            handleLocate(id);
                          }}
                          onOpenDetail={handleOpenDetail}
                          onBack={exitJourney}
                          onTryQuery={trySearchQuery}
                          graphMeta={{ source, nodes: dataset.nodes.length }}
                        />
                      </div>
                    }
                  />
                ) : isBiz && activeTopic ? (
                  <TopicView
                    topics={topics}
                    topic={activeTopic}
                    color={topicColorOf.get(activeTopic.id) || TOPIC_PALETTE[0]}
                    colorOf={(id) => topicColorOf.get(id) || TOPIC_PALETTE[0]}
                    onSelectTopic={(id) => setActiveTopicId(id)}
                    onNodeClick={(key) =>
                      navigate(`/graph-center/entity/${encodeURIComponent(key)}`)
                    }
                    onExit={() => setActiveTopicId('')}
                  />
                ) : (
                  <>
                    <div className={`${styles.mgraph} ${loading ? styles.canvasDim : ''}`}>
                      <MultiGraphView
                        db={db}
                        data={built.data}
                        members={built.members}
                        chips={built.chips}
                        cameraNonce={cameraNonce}
                        locateId={locateId}
                        onNodeClick={handleNodeClick}
                        onNodeDblClick={handleOpenDetail}
                        onRibbonClick={(pair) => {
                          setExpandedPairs((prev) => {
                            const next = new Set(prev);
                            if (next.has(pair)) next.delete(pair);
                            else next.add(pair);
                            return next;
                          });
                        }}
                        onGrow={onGrow}
                        onShrink={onShrink}
                      />
                    </div>
                    <FadePresence
                      open={Boolean(error || loading)}
                      kind="veil"
                      className={styles.usageEmpty}
                    >
                      {loading ? (
                        <span>{t('graphCenter.usageLoading')}</span>
                      ) : (
                        <>
                          <span>
                            {t(
                              error === 'user_space_uninitialized'
                                ? 'kgBrowser.mgSync.uninitialized'
                                : error === 'user_space_initializing'
                                  ? 'kgBrowser.mgSync.initializing'
                                  : 'graphCenter.usageFailed',
                            )}
                          </span>
                          <Button size="small" type="primary" onClick={() => reload()}>
                            {t('graphCenter.usageRetry')}
                          </Button>
                        </>
                      )}
                    </FadePresence>
                    {!isBiz && <CypherConsoleOverlay />}
                    <FadePresence open={Boolean(queryView && !isBiz)} className={styles.qvExit}>
                      <Button
                        size="small"
                        shape="round"
                        icon={<RollbackOutlined />}
                        onClick={exitQueryView}
                        title={t('graphCenter.backToFullGraph')}
                      >
                        {t('graphCenter.fullGraph')}
                      </Button>
                    </FadePresence>
                  </>
                )}
                <MgAppendFloat
                  kind={appendKind}
                  onClose={() => setAppendKind(null)}
                  onSuccess={reload}
                />
              </div>
              <div className={styles.hintLine}>{hintText}</div>
            </div>
          </div>
        </div>
      </div>
    </CypherConsoleProvider>
  );
}
