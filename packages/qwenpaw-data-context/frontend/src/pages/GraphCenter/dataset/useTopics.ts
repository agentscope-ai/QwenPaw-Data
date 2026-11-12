/** Topic discovery hook — real backend topics with demo fallback.
 *  - api source: POST /explorer/topics (wait=false kick), poll while computing.
 *  - demo source: synthesize pseudo-topics from the snapshot's domain/task
 *    structure so the lens UI is fully exercisable offline. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  topicsApi,
  type Topic,
  type TopicsDiagnostics,
  type TopicsThresholds,
} from '@/services/cmgraph';
import type { DatasetSource } from './useGraphDataset';
import type { GraphDb } from './graphDb';

export type TopicsState =
  | { phase: 'idle' }
  | { phase: 'loading' }
  | { phase: 'computing' }
  | { phase: 'uninitialized' | 'initializing' }
  | {
      phase: 'ready';
      topics: Topic[];
      diagnostics?: TopicsDiagnostics;
      thresholds?: TopicsThresholds;
    }
  | { phase: 'error'; message: string };

interface DemoTopicSeed {
  title: string;
  summary: string;
  mgContains: string[]; // domain or datasource substring
  kgContains: string[]; // entity label substring
}

const DEMO_SEEDS: Record<'zh' | 'en', DemoTopicSeed[]> = {
  zh: [
    {
      title: '对话应用活跃分析',
      summary: '围绕对话应用活跃指标的口径、血缘与相关知识',
      mgContains: ['活跃', 'chatapp', '对话'],
      kgContains: ['对话', '活跃'],
    },
    {
      title: '商业化收入追踪',
      summary: '收入类指标的表结构与沉淀结论',
      mgContains: ['收入', '商业化', 'gaap'],
      kgContains: ['收入', '商业'],
    },
    {
      title: '口径治理与冲突',
      summary: '口径定义冲突的检测与裁决知识',
      mgContains: ['口径', 'caliber'],
      kgContains: ['口径', '冲突'],
    },
  ],
  en: [
    {
      title: 'Chat app activity analysis',
      summary: 'Caliber, lineage, and related knowledge around chat app activity metrics',
      mgContains: ['活跃', 'chatapp', '对话'],
      kgContains: ['对话', '活跃'],
    },
    {
      title: 'Commercial revenue tracking',
      summary: 'Table structure and distilled findings for revenue metrics',
      mgContains: ['收入', '商业化', 'gaap'],
      kgContains: ['收入', '商业'],
    },
    {
      title: 'Caliber governance and conflicts',
      summary: 'Detection and arbitration of conflicting metric definitions',
      mgContains: ['口径', 'caliber'],
      kgContains: ['口径', '冲突'],
    },
  ],
};

function demoTopics(db: GraphDb, lang: 'zh' | 'en'): Topic[] {
  const stats = db.globalStats();
  void stats;
  const all = db.dataset.nodes;
  const out: Topic[] = [];
  DEMO_SEEDS[lang].forEach((seed, i) => {
    const members: Topic['members'] = [];
    const counts: Topic['layer_counts'] = {};
    const add = (
      key: string,
      layer: 'MG' | 'KG' | 'UG',
      type: string,
      label: string,
      domain: string,
    ) => {
      const node = db.getNode(key);
      const text =
        String((node?.props as Record<string, unknown>)?.definition ?? '') ||
        String((node?.props as Record<string, unknown>)?.description ?? '') ||
        String((node?.props as Record<string, unknown>)?.goal ?? '') ||
        '';
      members.push({ key, layer, type, label, domain, text });
      counts[layer] = (counts[layer] || 0) + 1;
    };
    all.forEach((n) => {
      const hay =
        `${n.label} ${(n.props as Record<string, unknown>)?.goal ?? ''} ${String((n.props as Record<string, unknown>)?.description ?? '')}`.toLowerCase();
      const hit = (arr: string[]) => arr.some((s) => hay.includes(s.toLowerCase()));
      if (n.layer === 'MG' && hit(seed.mgContains)) add(n.id, 'MG', n.type, n.label, '');
      else if (n.layer === 'KG' && hit(seed.kgContains)) add(n.id, 'KG', n.type, n.label, '');
    });
    if (members.length >= 2 && Object.keys(counts).length >= 1) {
      // Demo edges: connect each member to the first member of another layer
      // (semantic affinity placeholder) so the topic view has cross-graph links.
      const edges: Topic['edges'] = [];
      const firstByLayer: Partial<Record<string, string>> = {};
      members.forEach((m) => {
        if (!firstByLayer[m.layer]) firstByLayer[m.layer] = m.key;
      });
      members.forEach((m) => {
        Object.entries(firstByLayer).forEach(([L, k]) => {
          if (L !== m.layer && k && k !== m.key) {
            edges.push({ s: m.key, t: k, rel: '', kind: 'semantic', sim: 0.8 });
          }
        });
      });
      out.push({
        id: `demo-t${i}`,
        title: seed.title,
        summary: seed.summary,
        members,
        edges,
        layer_counts: counts,
        size: members.length,
        cross_layer: Object.keys(counts).length >= 2,
      });
    }
  });
  return out;
}

export function useTopics(
  source: DatasetSource,
  db: GraphDb,
  ownerId: string,
): {
  state: TopicsState;
  recompute: () => void;
  refresh: () => void;
} {
  const { i18n } = useTranslation();
  const lang = i18n.language.startsWith('en') ? 'en' : 'zh';
  const [state, setState] = useState<TopicsState>({ phase: 'idle' });
  const [tick, setTick] = useState(0);
  const recomputeRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(() => setTick((v) => v + 1), []);
  const recompute = useCallback(() => {
    recomputeRef.current = true;
    setTick((v) => v + 1);
  }, []);

  useEffect(() => {
    if (source === 'demo') {
      setState({ phase: 'ready', topics: demoTopics(db, lang) });
      return;
    }
    let cancelled = false;
    setState({ phase: 'loading' });
    const kick = recomputeRef.current;
    recomputeRef.current = false;

    (async () => {
      try {
        const res = await topicsApi.computeTopics({
          wait: false,
          recompute: kick,
          lang,
          ...(ownerId ? { owner_id: ownerId } : {}),
        });
        if (cancelled) return;
        if (res.status === 'ready') {
          setState({
            phase: 'ready',
            topics: res.topics,
            diagnostics: res.diagnostics,
            thresholds: res.thresholds,
          });
          return;
        }
        setState({ phase: 'computing' });
        // Poll every 6s until ready (embedding + clustering take ~10-60s).
        const poll = async () => {
          if (cancelled) return;
          try {
            const st = await topicsApi.getTopics(ownerId, lang);
            if (cancelled) return;
            if (st.status === 'ready') {
              setState({
                phase: 'ready',
                topics: st.topics,
                diagnostics: st.diagnostics,
                thresholds: st.thresholds,
              });
            } else if (st.status === 'uninitialized' || st.status === 'initializing') {
              setState({ phase: st.status });
              // GET reads the cached job result; restart computation to recheck storage.
              timerRef.current = setTimeout(() => setTick((v) => v + 1), 6000);
            } else if (st.status === 'error') {
              setState({ phase: 'error', message: st.error || 'topic computation failed' });
            } else {
              timerRef.current = setTimeout(poll, 6000);
            }
          } catch (e) {
            if (!cancelled)
              setState({ phase: 'error', message: e instanceof Error ? e.message : String(e) });
          }
        };
        timerRef.current = setTimeout(poll, 6000);
      } catch (e) {
        if (!cancelled)
          setState({ phase: 'error', message: e instanceof Error ? e.message : String(e) });
      }
    })();

    return () => {
      cancelled = true;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [source, db, ownerId, tick, lang]);

  const memoState = useMemo(() => state, [state]);
  return { state: memoState, recompute, refresh };
}
