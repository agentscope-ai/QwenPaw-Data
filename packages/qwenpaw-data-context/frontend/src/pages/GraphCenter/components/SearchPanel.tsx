/** Left search panel on the global graph (design 17).
 *  Corpus = the currently loaded viewMode dataset (`GraphDb.search`).
 *  Row / card / recent click locates; 「详情」opens the entity page.
 *  Typing / Enter / sample chips only refresh the result list — the
 *  search suffix action is the ONLY way to start the exploration. */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button, Input } from '@/design';
import { AppstoreOutlined, FireOutlined, HistoryOutlined, SearchOutlined } from '@ant-design/icons';
import LayerTag from './LayerTag';
import { typeLabel } from '../dataset/graphDb';
import { LAYER_COLOR } from '../dataset/constants';
import { getRecent } from '../dataset/recentStore';
import { useGraphCenter } from '../GraphCenterContext';
import type { GraphNode, LayerId } from '../types';
import pageMotion from './iosMotion.module.less';
import styles from '../index.module.less';

const SAMPLE_CHIPS = ['DAU', 'MAU', 'ChatApp', '首访用户数', '一级意图', 'GAAP'];

interface ResultRowProps {
  node: GraphNode;
  active: boolean;
  rowRef?: (el: HTMLElement | null) => void;
  onLocate: (id: string) => void;
  onOpenDetail: (id: string) => void;
}

function ResultRow({ node, active, rowRef, onLocate, onOpenDetail }: ResultRowProps) {
  const { t } = useTranslation();
  const { db, dsNames } = useGraphCenter();
  const meta = [typeLabel(node.type)];
  if (node.domain) meta.push(`${node.domain} 域`);
  if (node.datasource) meta.push(dsNames[node.datasource] || node.datasource);
  if (node.memoryKind) meta.push(node.memoryKind);
  if (node.type === 'domain') {
    const s = db.domainStats().find((d) => d.label === node.label);
    if (s) meta.push(t('graphCenter.metricDsCount', { num: s.metricCount, ds: s.dsCount }));
  } else {
    meta.push(t('graphCenter.relationCount', { num: db.degree(node.id) }));
  }
  return (
    <div
      ref={rowRef}
      className={`${styles.resultRow} ${active ? styles.resultRowActive : ''}`}
      onClick={() => onLocate(node.id)}
    >
      <LayerTag layer={node.layer} />
      <div className={styles.resultMain}>
        <div className={styles.resultName}>{node.label}</div>
        <div className={styles.resultMeta}>{meta.join(' · ')}</div>
      </div>
      <button
        type="button"
        className={styles.resultDetail}
        onClick={(e) => {
          e.stopPropagation();
          onOpenDetail(node.id);
        }}
      >
        {t('graphCenter.openDetail')}
      </button>
    </div>
  );
}

export interface SearchPanelProps {
  activeId?: string | null;
  locateError?: string | null;
  onLocate: (id: string) => void;
  onOpenDetail: (id: string) => void;
}

export default function SearchPanel({
  activeId,
  locateError,
  onLocate,
  onOpenDetail,
}: SearchPanelProps) {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const { db, dataset, loading, error, reload } = useGraphCenter();
  const q = searchParams.get('q') || '';
  const inputRef = useRef<HTMLInputElement>(null);
  const activeElRef = useRef<HTMLElement | null>(null);
  const setQ = (value: string) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set('q', value);
        else next.delete('q');
        // 打字换词：左侧结果实时刷新，同时停掉上一轮探索（避免边打字边重播）。
        next.delete('journey');
        return next;
      },
      { replace: true },
    );
  };
  // IME-safe input: the URL stays the source of truth for search/journey,
  // but the box keeps a local value while typing and never re-renders
  // mid-composition (which used to break Chinese input and feed half-typed
  // queries to the search). Commits are debounced; composition end and
  // Enter/blur flush immediately.
  const [inputVal, setInputVal] = useState(q);
  const composingRef = useRef(false);
  const focusedRef = useRef(false);
  const debounceRef = useRef<number | null>(null);
  useEffect(() => {
    // external q changes (chip click, back/forward) — don't clobber typing
    if (!focusedRef.current) setInputVal(q);
  }, [q]);
  useEffect(
    () => () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current);
    },
    [],
  );
  const commitQ = (value: string) => {
    if (debounceRef.current) {
      window.clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
    // 值没变（典型：搜索后点击画布导致 blur 的兜底 flush）不能重写 URL——
    // setQ 会清 journey=1，把正在播放的探索舞台踢回全局图谱。
    if (value === q) return;
    setQ(value);
  };
  const scheduleCommit = (value: string) => {
    if (debounceRef.current) window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => commitQ(value), 240);
  };
  /** 搜索框右侧图标 = 唯一的探索入口：提交 q 并启动模型自主探索。
   *  必须在一次 setSearchParams 里原子完成 —— 同一 tick 内连发两次函数式
   *  更新时，第二次拿到的是旧参数，会把第一次的 q 覆盖掉。 */
  const runExplore = () => {
    const value = inputVal.trim();
    if (!value) return;
    if (debounceRef.current) {
      window.clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set('q', value);
        next.set('journey', '1');
        return next;
      },
      { replace: true },
    );
  };
  const onInputChange = (value: string) => {
    setInputVal(value);
    if (composingRef.current) return;
    scheduleCommit(value);
  };

  const domains = useMemo(() => db.domainStats(), [db]);
  const hotNodes = useMemo(
    () =>
      dataset.nodes
        .filter((n) => n.type === 'metric' || n.type === 'entity' || n.type === 'task')
        .sort((a, b) => db.degree(b.id) - db.degree(a.id))
        .slice(0, 6),
    [db, dataset],
  );
  const recentItems = useMemo(
    () =>
      getRecent().map((id) => {
        const node = db.getNode(id);
        return { id, node, label: node?.label || id, layer: node?.layer as LayerId | undefined };
      }),
    [db, q],
  );
  const results = useMemo(() => (q.trim() ? db.search(q) : []), [q, db]);
  // Sample chips come from the loaded graph itself so they always hit;
  // hardcoded fallback only while the dataset is empty/offline.
  const sampleChips = useMemo(() => {
    const top = dataset.nodes
      .filter((n) => n.type === 'metric' || n.type === 'entity' || n.type === 'table')
      .sort((a, b) => db.degree(b.id) - db.degree(a.id))
      .slice(0, 6)
      .map((n) => n.label);
    return top.length >= 3 ? top : SAMPLE_CHIPS;
  }, [db, dataset]);

  useEffect(() => {
    activeElRef.current?.scrollIntoView({ block: 'nearest' });
  }, [activeId, q, db, results]);

  const bindActive = (id: string) => (el: HTMLElement | null) => {
    if (id === activeId) activeElRef.current = el;
  };

  const datasetBlocked = Boolean(error || loading);

  return (
    <aside className={styles.searchPanel} aria-label={t('graphCenter.tabSearch')}>
      <div className={styles.searchHero}>
        <div className={styles.searchRow}>
          <Input
            ref={inputRef as never}
            size="middle"
            suffix={
              <button
                type="button"
                className={styles.searchSubmit}
                disabled={!inputVal.trim()}
                title={t('graphCenter.exploreBtnTip')}
                aria-label={t('graphCenter.exploreBtn')}
                onMouseDown={(event) => event.preventDefault()}
                onClick={runExplore}
              >
                <SearchOutlined />
              </button>
            }
            placeholder={t('graphCenter.searchPlaceholder')}
            value={inputVal}
            onChange={(e) => onInputChange(e.target.value)}
            onCompositionStart={() => {
              composingRef.current = true;
            }}
            onCompositionEnd={(e) => {
              composingRef.current = false;
              const v = (e.target as HTMLInputElement).value;
              setInputVal(v);
              scheduleCommit(v);
            }}
            onFocus={() => {
              focusedRef.current = true;
            }}
            onBlur={() => {
              focusedRef.current = false;
              if (!composingRef.current) commitQ(inputVal);
            }}
            onPressEnter={() => {
              // 回车只提交搜索（刷新左侧结果列表），不启动探索。
              if (!composingRef.current) commitQ(inputVal);
            }}
          />
        </div>
        <div className={[styles.chips, styles.recommendations].join(' ')}>
          <span className={styles.recommendLabel}>{t('graphCenter.recommendedSearch')}:</span>
          {sampleChips.map((s) => (
            <button
              type="button"
              key={s}
              className={[styles.chip, styles.recommendationChip].join(' ')}
              onClick={() => {
                setInputVal(s);
                commitQ(s);
                inputRef.current?.focus();
              }}
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      {locateError ? <div className={styles.locateError}>{locateError}</div> : null}

      {datasetBlocked ? (
        <div className={styles.panelStatus}>
          {loading ? (
            <span>{t('graphCenter.usageLoading')}</span>
          ) : (
            <>
              <span>{t('graphCenter.usageFailed')}</span>
              <Button size="small" type="primary" onClick={() => reload()}>
                {t('graphCenter.usageRetry')}
              </Button>
            </>
          )}
        </div>
      ) : (
        <div className={styles.searchBody}>
          {recentItems.length > 0 && (
            <>
              <div className={styles.sectionH}>
                <HistoryOutlined />
                {t('graphCenter.recentTitle')}
              </div>
              <div className={styles.chips} style={{ marginTop: 0 }}>
                {recentItems.map((item) => (
                  <span
                    key={item.id}
                    ref={bindActive(item.id)}
                    className={`${styles.chip} ${item.id === activeId ? styles.chipActive : ''}`}
                    onClick={() => onLocate(item.id)}
                  >
                    {item.layer ? (
                      <b style={{ color: LAYER_COLOR[item.layer] }}>{item.layer}</b>
                    ) : null}{' '}
                    {item.label}
                  </span>
                ))}
              </div>
            </>
          )}

          <div key={q.trim() ? 'results' : 'browse'} className={pageMotion.pageIn}>
            {!q.trim() ? (
              <>
                {domains.length > 0 ? (
                  <>
                    <div className={styles.sectionH}>
                      <AppstoreOutlined />
                      {t('graphCenter.domainTitle')}
                    </div>
                    <div className={styles.cards}>
                      {domains.map((d) => (
                        <div
                          key={d.id}
                          ref={bindActive(d.id)}
                          className={`${styles.sketchBox} ${styles.domainCard} ${d.id === activeId ? styles.resultRowActive : ''}`}
                          onClick={() => onLocate(d.id)}
                        >
                          <div className={styles.dcName}>{d.label}</div>
                          <div className={styles.dcStat}>
                            {t('graphCenter.metricDsCount', { num: d.metricCount, ds: d.dsCount })}
                          </div>
                        </div>
                      ))}
                    </div>
                  </>
                ) : null}
                {hotNodes.length > 0 ? (
                  <>
                    <div className={styles.sectionH}>
                      <FireOutlined />
                      {t('graphCenter.hotTitle')}
                    </div>
                    <div className={`${styles.sketchBox} ${styles.resultList}`}>
                      {hotNodes.map((n) => (
                        <ResultRow
                          key={n.id}
                          node={n}
                          active={n.id === activeId}
                          rowRef={bindActive(n.id)}
                          onLocate={onLocate}
                          onOpenDetail={onOpenDetail}
                        />
                      ))}
                    </div>
                  </>
                ) : null}
              </>
            ) : (
              <>
                <div className={styles.sectionH}>
                  <SearchOutlined />
                  {t('graphCenter.resultsTitle', { num: results.length })}
                </div>
                {results.length === 0 ? (
                  <div className={styles.empty}>{t('graphCenter.noResult', { q: q.trim() })}</div>
                ) : (
                  <div className={`${styles.sketchBox} ${styles.resultList}`}>
                    {results.map((n) => (
                      <ResultRow
                        key={n.id}
                        node={n}
                        active={n.id === activeId}
                        rowRef={bindActive(n.id)}
                        onLocate={onLocate}
                        onOpenDetail={onOpenDetail}
                      />
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </aside>
  );
}
