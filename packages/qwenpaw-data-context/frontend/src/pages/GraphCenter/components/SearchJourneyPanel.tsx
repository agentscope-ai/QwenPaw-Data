/** Path-steered reasoning journey renderer (search-journey visual language).
 *  Replays a Journey (dataset/journeyWalk) phase by phase: anchor → fork →
 *  eval → select → collect → conclusion. Renders either in the search panel
 *  (compact) or as the main-canvas stage (stage = true). */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CompassOutlined, RadarChartOutlined, SearchOutlined } from '@ant-design/icons';
import type { Journey, JourneyBranch, JourneyNodeRef } from '../dataset/journeyWalk';
import { buildLocalLoginUrl } from '@/store/authStore';
import styles from './searchJourney.module.less';

interface SceneNode extends JourneyNodeRef {
  x: number;
  y: number;
  branchId: string;
  appearAt: number;
  r: number;
}

interface Schedule {
  W: number;
  H: number;
  anchorX: number;
  anchorAt: number;
  nodes: SceneNode[];
  evalAt: number;
  selectAt: number;
  collectAt: number[];
  conclAt: number;
  total: number;
  stops: { at: number; text: string }[];
}

const BR_COLORS: Record<string, string> = { A: '#10b981', B: '#f59e0b', C: '#ef4444' };

function buildSchedule(j: Journey, W: number, H: number): Schedule {
  const rows = Math.max(2, j.branches.length);
  const anchorX = Math.round(W * 0.16);
  const anchorAt = 450;
  const stepX = Math.max(54, W * 0.13);
  const startX = W * 0.4;
  const nodes: SceneNode[] = [];
  let t = 950;
  j.branches.forEach((b, bi) => {
    const baseY = ((bi + 1) * H) / (rows + 1);
    b.nodes.forEach((n, ni) => {
      nodes.push({
        ...n,
        branchId: b.id,
        x: Math.min(W - 56, startX + ni * stepX),
        y: baseY + (ni % 2 === 0 ? -10 : 10),
        appearAt: t,
        r: W > 500 ? 12 : 10,
      });
      t += 130;
    });
    t += 160;
  });
  const evalAt = t + 350;
  const selectAt = evalAt + 950;
  const collectAt = j.context.map((_, i) => selectAt + 500 + i * 260);
  const conclAt = (collectAt[collectAt.length - 1] || selectAt + 600) + 700;
  const total = conclAt + 1400;
  const stops = j.narrations.map((text, i) => {
    let at: number;
    if (i === 0) at = 0;
    else if (i === 1) at = anchorAt - 150;
    else if (i === 2) at = 900;
    else if (i === 3) at = evalAt - 100;
    else if (i === 4) at = selectAt - 100;
    else if (i === 5) at = selectAt + 350;
    else if (i === 6) at = (collectAt[collectAt.length - 1] || selectAt) + 150;
    else at = conclAt - 100;
    return { at, text };
  });
  return { W, H, anchorX, anchorAt, nodes, evalAt, selectAt, collectAt, conclAt, total, stops };
}

function useClock(total: number) {
  const [t, setT] = useState(0);
  const [run, setRun] = useState(0);
  useEffect(() => {
    setT(0);
    const started = Date.now();
    const iv = setInterval(() => {
      const el = Date.now() - started;
      if (el >= total) {
        setT(total);
        clearInterval(iv);
      } else setT(el);
    }, 50);
    return () => clearInterval(iv);
  }, [total, run]);
  return { t, replay: () => setRun((r) => r + 1) };
}

export interface SearchJourneyPanelProps {
  journey: Journey;
  /** Render as the main-canvas stage (larger scene, roomier ctx grid). */
  stage?: boolean;
  onLocate: (id: string) => void;
  onOpenDetail: (id: string) => void;
  onBack: () => void;
  /** Refill the search with another query (noHit suggestion chips) —
   *  search-list only, never auto-starts an exploration. */
  onTryQuery?: (q: string) => void;
  /** Loaded-graph diagnostics shown on miss states. */
  graphMeta?: { source: 'api' | 'demo'; nodes: number };
}

export default function SearchJourneyPanel({
  journey,
  stage,
  onLocate,
  onOpenDetail,
  onBack,
  onTryQuery,
  graphMeta,
}: SearchJourneyPanelProps) {
  const { t } = useTranslation();
  const W = stage ? 760 : 360;
  const H = stage ? 430 : 300;
  const sched = useMemo(() => buildSchedule(journey, W, H), [journey, W, H]);
  const { t: clock, replay } = useClock(sched.total);
  const ctxListRef = useRef<HTMLDivElement>(null);

  const narr = useMemo(() => {
    let cur = sched.stops[0]?.text || '';
    sched.stops.forEach((s) => {
      if (clock >= s.at) cur = s.text;
    });
    return cur;
  }, [clock, sched]);

  const phase =
    clock < sched.anchorAt
      ? t('graphCenter.journeyPhaseQuery')
      : clock < sched.evalAt
        ? t('graphCenter.journeyPhaseFork')
        : clock < sched.selectAt
          ? t('graphCenter.journeyPhaseEval')
          : clock < (sched.collectAt[0] || sched.selectAt + 500)
            ? t('graphCenter.journeyPhaseSelect')
            : clock < sched.conclAt
              ? t('graphCenter.journeyPhaseCollect')
              : t('graphCenter.journeyPhaseDone');

  useEffect(() => {
    const el = ctxListRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [clock]);

  const anchorVisible = clock >= sched.anchorAt;
  const evalVisible = clock >= sched.evalAt;
  const selected = clock >= sched.selectAt;
  const anchorX = sched.anchorX;

  const branchEdgePaths = (b: JourneyBranch) => {
    const paths: { d: string; key: string }[] = [];
    const bnodes = sched.nodes
      .filter((n) => n.branchId === b.id)
      .sort((a, c) => a.appearAt - c.appearAt);
    const first = bnodes[0];
    if (journey.anchor && first) {
      paths.push({ d: curve(anchorX, H / 2, first.x, first.y), key: b.id + ':a' });
    }
    for (let i = 0; i < bnodes.length - 1; i++) {
      paths.push({
        d: curve(bnodes[i].x, bnodes[i].y, bnodes[i + 1].x, bnodes[i + 1].y),
        key: b.id + ':' + i,
      });
    }
    return paths;
  };

  function curve(x1: number, y1: number, x2: number, y2: number): string {
    const mx = (x1 + x2) / 2;
    return 'M' + x1 + ',' + y1 + ' C' + mx + ',' + y1 + ' ' + mx + ',' + y2 + ' ' + x2 + ',' + y2;
  }

  if (journey.noHit) {
    return (
      <div className={styles.wrap + (stage ? ' ' + styles.stageWrap : '')}>
        <div className={styles.noData}>
          <div className={styles.noDataIco}>
            <SearchOutlined />
          </div>
          <h4>
            {journey.anchor
              ? t('graphCenter.journeyIsolatedTitle', { label: journey.anchor.label })
              : t('graphCenter.journeyNoHitTitle', { q: journey.query })}
          </h4>
          <p>
            {journey.anchor
              ? t('graphCenter.journeyIsolatedHint')
              : t('graphCenter.journeyNoHitHint')}
          </p>
          {graphMeta ? (
            <div className={styles.suggLabel}>
              {t('graphCenter.journeyGraphMeta', {
                source:
                  graphMeta.source === 'demo'
                    ? t('graphCenter.journeySourceDemo')
                    : t('graphCenter.journeySourceApi'),
                num: graphMeta.nodes,
              })}
            </div>
          ) : null}
          {journey.suggestions.length > 0 && onTryQuery ? (
            <>
              <div className={styles.suggLabel}>{t('graphCenter.journeyTryThese')}</div>
              <div className={styles.suggRow}>
                {journey.suggestions.map((s) => (
                  <button
                    key={s}
                    type="button"
                    className={styles.suggChip}
                    onClick={() => onTryQuery(s)}
                  >
                    {s}
                  </button>
                ))}
              </div>
            </>
          ) : null}
          <div className={styles.noDataBtns}>
            <button type="button" className={styles.miniBtn} onClick={onBack}>
              {t('graphCenter.journeyBack')}
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (journey.noData) {
    return (
      <div className={styles.wrap + (stage ? ' ' + styles.stageWrap : '')}>
        <div className={styles.noData}>
          <div className={styles.noDataIco}>
            <RadarChartOutlined />
          </div>
          <h4>{t('graphCenter.journeyNoDataTitle')}</h4>
          <p>{t('graphCenter.journeyNoDataHint')}</p>
          <div className={styles.noDataBtns}>
            <button
              type="button"
              className={styles.miniBtn}
              onClick={() => window.location.assign(buildLocalLoginUrl())}
            >
              {t('graphCenter.journeyLogin')}
            </button>
            <button type="button" className={styles.miniBtn} onClick={onBack}>
              {t('graphCenter.journeyBack')}
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.wrap + (stage ? ' ' + styles.stageWrap : '')}>
      <div className={styles.head}>
        <span className={styles.phaseTag}>
          <CompassOutlined /> {phase}
        </span>
        {journey.demoStory ? (
          <span className={styles.demoTag}>{t('graphCenter.journeyDemoTag')}</span>
        ) : null}
        <span className={styles.headBtns}>
          <button type="button" className={styles.miniBtn} onClick={replay}>
            {t('graphCenter.journeyReplay')}
          </button>
          <button type="button" className={styles.miniBtn} onClick={onBack}>
            {t('graphCenter.journeyBack')}
          </button>
        </span>
      </div>

      <div className={styles.sceneWrap}>
        <svg viewBox={'0 0 ' + W + ' ' + H} className={styles.scene}>
          {/* query node */}
          <g opacity={clock > 0 ? 1 : 0.4}>
            <circle cx={anchorX} cy={H / 2} r="17" fill="#8b5cf6" opacity=".15" />
            <circle cx={anchorX} cy={H / 2} r="5.5" fill="#8b5cf6" />
            <text x={anchorX} y={H / 2 + 34} textAnchor="middle" className={styles.nodeLabel}>
              {t('graphCenter.journeyQueryNode')}
            </text>
          </g>
          {/* anchor */}
          {journey.anchor ? (
            <g className={anchorVisible ? styles.pop : undefined} opacity={anchorVisible ? 1 : 0}>
              <circle cx={anchorX} cy={H / 2} r="27" fill="#0ea5e9" opacity=".12" />
              <circle
                cx={anchorX}
                cy={H / 2}
                r="16"
                fill="#fff"
                stroke="#0ea5e9"
                strokeWidth="2.6"
                className={styles.anchorGlow}
              />
              <text x={anchorX} y={H / 2 + 3.5} textAnchor="middle" className={styles.anchorTxt}>
                {journey.anchor.label.slice(0, 4)}
              </text>
              <text x={anchorX} y={H / 2 - 26} textAnchor="middle" className={styles.anchorName}>
                {journey.anchor.label}
              </text>
            </g>
          ) : null}
          {/* branches */}
          {journey.branches.map((b) => {
            const dim = selected && b.verdict === 'rejected';
            const hot = selected && b.verdict === 'chosen';
            const bnodes = sched.nodes
              .filter((n) => n.branchId === b.id)
              .sort((a, c) => a.appearAt - c.appearAt);
            return (
              <g key={b.id} opacity={dim ? 0.22 : 1}>
                {branchEdgePaths(b).map((p, pi) => {
                  const target = pi === 0 ? bnodes[0] : bnodes[pi];
                  const visible = target ? clock >= target.appearAt : clock >= 950;
                  return (
                    <path
                      key={p.key}
                      d={p.d}
                      fill="none"
                      stroke={BR_COLORS[b.id] || b.color}
                      strokeWidth={hot ? 2.4 : 1.6}
                      strokeDasharray={
                        hot && selected ? '7 5' : b.verdict === 'rejected' ? '4 3' : undefined
                      }
                      className={
                        (hot && selected ? styles.flow : '') +
                        ' ' +
                        (visible ? styles.edgeIn : styles.edgeHidden)
                      }
                    />
                  );
                })}
                {bnodes.map((n) => {
                  const vis = clock >= n.appearAt;
                  const collected =
                    hot &&
                    clock >= (sched.collectAt[0] || Infinity) &&
                    journey.context.some((c) => c.nodeId === n.id);
                  return (
                    <g key={n.id} opacity={vis ? 1 : 0} className={vis ? styles.pop : undefined}>
                      {collected ? (
                        <circle
                          cx={n.x}
                          cy={n.y}
                          r={n.r + 5}
                          fill="none"
                          stroke={BR_COLORS[b.id]}
                          strokeWidth="1.5"
                          className={styles.collectRing}
                        />
                      ) : null}
                      <circle
                        cx={n.x}
                        cy={n.y}
                        r={n.r + 4}
                        fill={BR_COLORS[b.id] || b.color}
                        opacity=".14"
                      />
                      <circle
                        cx={n.x}
                        cy={n.y}
                        r={n.r}
                        fill="#fff"
                        stroke={BR_COLORS[b.id] || b.color}
                        strokeWidth={hot ? 2.6 : 1.8}
                      />
                      <text
                        x={n.x}
                        y={n.y + n.r + 12}
                        textAnchor="middle"
                        className={styles.nodeLabel}
                      >
                        {n.label.length > 9 ? n.label.slice(0, 9) + '…' : n.label}
                      </text>
                    </g>
                  );
                })}
                {evalVisible && bnodes.length > 0 ? (
                  <g className={styles.tagIn}>
                    <EvalTag b={b} x={Math.min(bnodes[0].x, W - 90)} y={bnodes[0].y - 26} />
                  </g>
                ) : null}
              </g>
            );
          })}
        </svg>
      </div>

      <div className={styles.ctxHead}>
        <span>{t('graphCenter.journeyCtxTitle')}</span>
        <span className={styles.ctxCount}>
          {journey.context.filter((_, i) => clock >= (sched.collectAt[i] || Infinity)).length}/
          {journey.context.length}
        </span>
      </div>
      <div className={styles.ctxList + (stage ? ' ' + styles.ctxGrid : '')} ref={ctxListRef}>
        {journey.context.map((c, i) => {
          const vis = clock >= (sched.collectAt[i] || Infinity);
          if (!vis) return null;
          const clickable = c.nodeId && !c.nodeId.startsWith('demo:');
          return (
            <div
              key={c.kind + c.label}
              className={styles.ctxItem}
              style={{ borderColor: c.color + '55' }}
              onClick={clickable ? () => onLocate(c.nodeId as string) : undefined}
            >
              <span className={styles.ctxDot} style={{ background: c.color }} />
              <span className={styles.ctxKind} style={{ color: c.color }}>
                {c.kind}
              </span>
              <span className={styles.ctxLabel}>{c.label}</span>
              {clickable ? (
                <button
                  type="button"
                  className={styles.ctxGo}
                  onClick={(e) => {
                    e.stopPropagation();
                    onOpenDetail(c.nodeId as string);
                  }}
                >
                  {t('graphCenter.openDetail')}
                </button>
              ) : null}
            </div>
          );
        })}
      </div>

      <div className={styles.concl} style={{ opacity: clock >= sched.conclAt ? 1 : 0 }}>
        ✓ {journey.conclusion}
      </div>

      <div className={styles.narr} dangerouslySetInnerHTML={{ __html: narr }} />
    </div>
  );
}

function EvalTag({ b, x, y }: { b: JourneyBranch; x: number; y: number }) {
  const ok = b.verdict === 'chosen';
  return (
    <g>
      <rect
        x={x - 4}
        y={y - 12}
        rx="7"
        width="70"
        height="15"
        fill={ok ? 'rgba(16,185,129,.12)' : 'rgba(148,163,184,.14)'}
        stroke={ok ? '#10b981' : '#cbd5e1'}
        strokeWidth=".8"
      />
      <text
        x={x + 31}
        y={y - 1}
        textAnchor="middle"
        fontSize="8.5"
        fontWeight="700"
        fill={ok ? '#047857' : '#64748b'}
        fontFamily="ui-monospace, Menlo, monospace"
      >
        {b.score.toFixed(2)} · {ok ? '✓选中' : '×拒'}
      </text>
    </g>
  );
}
