/** 动线二：追溯（实体详情 + 探索关系子图）— port of Pages.detail(). */
import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button } from '@/design';
import { CompassOutlined, LeftOutlined, WarningOutlined } from '@ant-design/icons';
import GraphView from '../components/GraphView';
import LayerTag from '../components/LayerTag';
import LocateButton from '../components/LocateButton';
import { typeLabel, type GraphDb } from '../dataset/graphDb';
import { LAYER_COLOR, clipText } from '../dataset/constants';
import { pushRecent } from '../dataset/recentStore';
import { useGraphCenter } from '../GraphCenterContext';
import type { GraphNode, SubgraphData } from '../types';
import pageMotion from '../components/iosMotion.module.less';
import styles from '../index.module.less';

const DESC_LABEL: Record<string, string> = {
  task: '目标',
  step: '步骤说明',
  event: '事件描述',
  claim: '断言内容',
  memory: '记忆内容',
  entity: '说明',
  toolcall: '工具',
  session: '会话',
};

interface ExploreSpec {
  key: string;
  question: string;
  btnLabel: string;
  hint: string;
  getData: () => SubgraphData;
}

function buildExploreSpecs(db: GraphDb, n: GraphNode): ExploreSpec[] {
  if (n.type === 'metric') {
    return [
      {
        key: 'src',
        question: `「${n.label}」的数据来源是什么？`,
        btnLabel: '展开血缘',
        hint: '血缘：指标 → 数据集 → 数据源（DERIVES_FROM / IN_SOURCE）',
        getData: () => db.subLineage(n.id),
      },
      {
        key: 'imp',
        question: `谁在引用 / 归属「${n.label}」？`,
        btnLabel: '展开引用',
        hint: '上游归属与引用方（数据域 / 实体）',
        getData: () => db.subImpact(n.id),
      },
      {
        key: 'asc',
        question: `「${n.label}」的跨层关联？`,
        btnLabel: '展开关联',
        hint: '跨图层直接关联对象（MG / KG / UG）',
        getData: () => db.subAssoc(n.id),
      },
    ];
  }
  // Insight: impact / path discovery / neighborhood expansion share one intent
  // → non-metrics get neighborhood + cross-layer association.
  return [
    {
      key: 'nb',
      question: `「${n.label}」和什么直接相关？`,
      btnLabel: '展开邻域',
      hint: '一跳邻域（上下游 / 关联）',
      getData: () => db.subNeighborhood(n.id),
    },
    {
      key: 'asc2',
      question: `「${n.label}」的跨层关联？`,
      btnLabel: '展开关联',
      hint: '跨图层直接关联对象',
      getData: () => db.subAssoc(n.id),
    },
  ];
}

interface AccordionProps {
  spec: ExploreSpec;
  focusId: string;
  onGoto: (id: string) => void;
}

/** Accordion that lazy-renders its subgraph on first open. */
function ExploreAccordion({ spec, focusId, onGoto }: AccordionProps) {
  const { db } = useGraphCenter();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<SubgraphData | null>(null);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && !data) setData(spec.getData());
  };

  const height = data ? Math.min(380, Math.max(200, data.nodes.length * 34)) : 300;

  return (
    <div className={`${styles.sketchBox} ${styles.accordion}`}>
      <div className={styles.accordionHead} onClick={toggle}>
        <span>
          <span className={`${styles.caret} ${open ? styles.caretOpen : ''}`}>▸</span>{' '}
          {spec.question}
        </span>
        <Button
          size="small"
          type="dashed"
          onClick={(e) => {
            e.stopPropagation();
            toggle();
          }}
        >
          {spec.btnLabel}
        </Button>
      </div>
      <div className={`${styles.accordionReveal} ${open ? styles.accordionRevealOpen : ''}`}>
        <div className={styles.accordionRevealInner}>
          {data ? (
            <div className={styles.accordionBody}>
              {data.nodes.length <= 1 ? (
                <div className={styles.empty}>暂无此类关系数据</div>
              ) : (
                <div className={styles.subgraphBox}>
                  <GraphView
                    db={db}
                    data={data}
                    focusId={focusId}
                    height={height}
                    layout={data.nodes.length > 6 ? 'force' : 'layered'}
                    onNodeClick={onGoto}
                  />
                  <div className={styles.graphHint}>{spec.hint}</div>
                </div>
              )}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export default function EntityDetailPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { id } = useParams<{ id: string }>();
  const { db, dsNames } = useGraphCenter();
  const node = useMemo(() => (id ? db.getNode(id) : null), [id, db]);

  // Mirror demo behavior: visiting a node pushes it into the recent list.
  useMemo(() => {
    if (node) pushRecent(node.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node?.id]);

  const specs = useMemo(() => (node ? buildExploreSpecs(db, node) : []), [db, node]);

  if (!node) {
    return <div className={styles.empty}>{t('graphCenter.notFound', { id })}</div>;
  }

  const goto = (nid: string) => navigate(`/graph-center/entity/${nid}`);
  const onLocate = (nid: string) =>
    navigate(`/graph-center/global?locate=${encodeURIComponent(nid)}`);

  const sub = [typeLabel(node.type)];
  if (node.domain) sub.push(`${node.domain} 域`);
  if (node.datasource) sub.push(dsNames[node.datasource] || node.datasource);
  if (node.memoryKind) sub.push(node.memoryKind);

  return (
    <div className={pageMotion.pageIn}>
      <span
        className={styles.backLink}
        onClick={() =>
          window.history.length > 1 ? navigate(-1) : navigate('/graph-center/global')
        }
      >
        <LeftOutlined /> {t('graphCenter.backToSearch')}
      </span>

      <div
        className={`${styles.sketchBox} ${styles.detailHead}`}
        style={{ borderLeftColor: LAYER_COLOR[node.layer] }}
      >
        <div className={styles.detailTitleRow}>
          <div className={styles.detailTitle}>
            {node.label} <LayerTag layer={node.layer} />
          </div>
          <LocateButton nodeId={node.id} onLocate={onLocate} />
        </div>
        <div className={styles.detailSub}>{sub.join(' · ')}</div>
        {node.definition ? (
          <div className={styles.detailDef}>
            <b>口径：</b>
            {node.definition}
          </div>
        ) : node.type === 'metric' ? (
          <div className={`${styles.detailDef} ${styles.detailDefWarn}`}>
            <b>
              <WarningOutlined /> 该指标缺少口径定义
            </b>
          </div>
        ) : null}
        {node.desc && node.type !== 'metric' ? (
          <div className={styles.detailDef}>
            <b>{DESC_LABEL[node.type] || '说明'}：</b>
            {clipText(node.desc, 420)}
          </div>
        ) : null}
      </div>

      <div className={styles.sectionH}>
        <CompassOutlined /> {t('graphCenter.exploreTitle')}{' '}
        <span className={`${styles.muted}`} style={{ fontSize: 12 }}>
          {t('graphCenter.exploreSub')}
        </span>
      </div>
      <div className={styles.explore}>
        {specs.map((s) => (
          <ExploreAccordion key={s.key} spec={s} focusId={node.id} onGoto={goto} />
        ))}
      </div>
    </div>
  );
}
