import { useCardExpansionAnchor } from '../useCardExpansionAnchor';
import { DownOutlined } from '@ant-design/icons';
import { Clock } from 'lucide-react';
import Markdown from '@agentscope-ai/chat/lib/Markdown';
import { Button, Collapse } from '@agentscope-ai/design';
import { Badge, Tooltip } from 'antd';
import VirtualList from 'rc-virtual-list';
import type { ListRef } from 'rc-virtual-list';
import { forwardRef, memo, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { dataConsoleApi } from '../../api/client';
import type { BizEvent, Segment, SegmentArtifact } from '../../api/types';
import { eventDurationSeconds } from '../../model/runtime/runtimeProjection';
import {
  formatExecutionSeconds,
  otherDurationSeconds,
  segmentDurationParts,
} from '../../model/runtime/executionTiming';
import { readSegmentConfidence } from '../../model/runtime/segmentConfidence';
import { formatSegmentMarkdown } from '../../model/artifacts/segmentMarkdown';
import type { Artifact } from '../../types';
import { useAppI18n } from '@/i18n';
import ArtifactCard from '../ArtifactCard/index';
import RuntimeBizEventCard from '../RuntimeBizEventCard/index';
import sharedStyles from '../../index.module.less';
import { mergeStyleModules } from '../../styleModules';
import ownerStyles from './index.module.less';

const styles = mergeStyleModules(ownerStyles, sharedStyles);

const ARTIFACT_BATCH_SIZE = 3;
const ARTIFACT_CARD_HEIGHT = 60;
const ARTIFACT_CARD_GAP = 6;
const ARTIFACT_ITEM_HEIGHT = ARTIFACT_CARD_HEIGHT + ARTIFACT_CARD_GAP;
const ARTIFACT_LIST_HEIGHT = ARTIFACT_ITEM_HEIGHT * ARTIFACT_BATCH_SIZE;
const ARTIFACT_VIRTUAL_THRESHOLD = 50;
const EMPTY_BIZ_EVENTS: BizEvent[] = [];
const EMPTY_SEGMENT_ARTIFACTS: SegmentArtifact[] = [];

interface RuntimeSegmentCardProps {
  data: Segment;
  index: number;
  events?: BizEvent[];
  showConfidence?: boolean;
  showTerminalEvent?: boolean;
  defaultOpen?: boolean;
  sessionId?: string | null;
  chatId?: string | null;
  onArtifactOpen?: (artifact: Artifact) => void;
  onArtifactDownload?: (artifact: Artifact) => void;
  onArtifactShare?: (artifact: Artifact) => Promise<void>;
}

function toDisplayArtifact(segmentId: string, artifact: SegmentArtifact): Artifact {
  const extension = artifact.name.split('.').pop()?.toUpperCase() || 'FILE';
  return {
    id: `${segmentId}:${artifact.relative_path}`,
    name: artifact.name,
    title: artifact.description,
    type: extension,
    meta: artifact.relative_path,
    path: artifact.relative_path,
    access: {
      relativePath: artifact.relative_path,
    },
  };
}

interface ArtifactListItemProps {
  artifact: Artifact;
  onArtifactOpen?: (artifact: Artifact) => void;
  onArtifactDownload?: (artifact: Artifact) => void;
  onArtifactShare?: (artifact: Artifact) => Promise<void>;
}

const ArtifactListItem = memo(
  forwardRef<HTMLDivElement, ArtifactListItemProps>(function ArtifactListItem(
    { artifact, onArtifactOpen, onArtifactDownload, onArtifactShare },
    ref,
  ) {
    return (
      <div ref={ref} className={styles.artifactVirtualItem}>
        <ArtifactCard
          artifact={artifact}
          surface="on-tinted"
          onPreview={() => onArtifactOpen?.(artifact)}
          onDownload={onArtifactDownload ? () => onArtifactDownload(artifact) : undefined}
          onShare={onArtifactShare ? () => onArtifactShare(artifact) : undefined}
        />
      </div>
    );
  }),
);

export default function RuntimeSegmentCard({
  data,
  index,
  events = EMPTY_BIZ_EVENTS,
  showConfidence = true,
  showTerminalEvent = false,
  defaultOpen = true,
  sessionId,
  chatId,
  onArtifactOpen,
  onArtifactDownload,
  onArtifactShare,
}: RuntimeSegmentCardProps) {
  const { t } = useAppI18n();
  const artifactListRef = useRef<ListRef>(null);
  const artifactScrollAnimationRef = useRef<number | null>(null);
  const pendingArtifactScrollRef = useRef(false);
  const [open, setOpen] = useState(defaultOpen);
  const { cardRef, preserveHeader } = useCardExpansionAnchor();
  const { cardRef: trajectoryRef, preserveHeader: preserveTrajectoryHeader } =
    useCardExpansionAnchor<HTMLDivElement>();
  const [visibleArtifactLimit, setVisibleArtifactLimit] = useState(ARTIFACT_BATCH_SIZE);
  const trajectoryId = useId();
  const [trajectoryOpen, setTrajectoryOpen] = useState(false);
  const duration =
    data.workedForSeconds === undefined ? eventDurationSeconds(events) : data.workedForSeconds;
  const otherDuration = otherDurationSeconds(duration, data.timingTotals);
  const durationParts = duration === null ? null : segmentDurationParts(duration);
  const confidence = showConfidence ? readSegmentConfidence(data.confidence) : null;
  const workedLabel =
    durationParts === null
      ? t('runtime.businessTrajectory')
      : t(
          durationParts.minutes === 0 ? 'runtime.workedForSeconds' : 'runtime.workedFor',
          durationParts,
        );
  const terminalEvent = showTerminalEvent ? events[events.length - 1] : undefined;
  const sourceArtifacts = data.artifact ?? EMPTY_SEGMENT_ARTIFACTS;
  const visibleArtifacts = useMemo(
    () =>
      sourceArtifacts
        .slice(0, visibleArtifactLimit)
        .map((artifact) => toDisplayArtifact(data.segment_id, artifact)),
    [data.segment_id, sourceArtifacts, visibleArtifactLimit],
  );
  const hasMoreArtifacts = visibleArtifacts.length < sourceArtifacts.length;
  const shouldVirtualizeArtifacts = visibleArtifacts.length > ARTIFACT_VIRTUAL_THRESHOLD;
  const artifactListHeight = Math.min(
    ARTIFACT_LIST_HEIGHT,
    visibleArtifacts.length * ARTIFACT_ITEM_HEIGHT,
  );

  useLayoutEffect(() => {
    if (!pendingArtifactScrollRef.current || !visibleArtifacts.length) return;
    pendingArtifactScrollRef.current = false;
    const list = artifactListRef.current;
    if (!list) return;
    const holder = list.nativeElement.querySelector<HTMLElement>('.rc-virtual-list-holder');
    if (!holder) {
      list.scrollTo({ index: visibleArtifacts.length - 1, align: 'bottom' });
      return;
    }
    const targetTop = holder.scrollHeight - holder.clientHeight;
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduceMotion) {
      list.scrollTo(targetTop);
      return;
    }

    const startTop = holder.scrollTop;
    const startedAt = performance.now();
    const animationDuration = 360;
    const animateScroll = (now: number) => {
      const progress = Math.min((now - startedAt) / animationDuration, 1);
      const easedProgress = progress < 0.5 ? 4 * progress ** 3 : 1 - (-2 * progress + 2) ** 3 / 2;
      list.scrollTo(startTop + (targetTop - startTop) * easedProgress);
      if (progress < 1) {
        artifactScrollAnimationRef.current = window.requestAnimationFrame(animateScroll);
      } else {
        artifactScrollAnimationRef.current = null;
      }
    };
    artifactScrollAnimationRef.current = window.requestAnimationFrame(animateScroll);

    return () => {
      if (artifactScrollAnimationRef.current !== null) {
        window.cancelAnimationFrame(artifactScrollAnimationRef.current);
        artifactScrollAnimationRef.current = null;
      }
    };
  }, [visibleArtifacts.length]);

  const handleLoadMoreArtifacts = () => {
    pendingArtifactScrollRef.current = true;
    setVisibleArtifactLimit((current) =>
      Math.min(current + ARTIFACT_BATCH_SIZE, sourceArtifacts.length),
    );
  };

  const segmentCard = (
    <section ref={cardRef} className={styles.segmentCardSurface} data-segment-id={data.segment_id}>
      <Collapse
        className={styles.segmentCollapse}
        activeKey={open ? [data.segment_id] : []}
        onChange={(activeKeys) => {
          preserveHeader();
          const keys = Array.isArray(activeKeys) ? activeKeys : [activeKeys];
          const nextOpen = keys.includes(data.segment_id);
          if (nextOpen !== open && sessionId && chatId) {
            void dataConsoleApi
              .reportBehaviorEvent({
                kind: 'segment_toggle',
                session_id: sessionId,
                chat_id: chatId,
                target_id: data.segment_id,
                payload: { open: nextOpen },
              })
              .catch(() => undefined);
          }
          setOpen(nextOpen);
        }}
        items={[
          {
            key: data.segment_id,
            showArrow: false,
            label: (
              <div className={styles.segmentHeader}>
                <span className={styles.segmentHeaderCopy}>
                  <strong>{data.title}</strong>
                </span>
                <DownOutlined
                  aria-hidden="true"
                  className={`${styles.collapseChevron} ${open ? styles.collapseChevronOpen : ''}`}
                />
              </div>
            ),
            children: (
              <div className={styles.segmentBody}>
                <div className={styles.segmentSummaryGrid}>
                  <div>
                    <span>{t('runtime.input')}</span>
                    <Markdown
                      className={styles.segmentMarkdown}
                      content={formatSegmentMarkdown(data.input, t('runtime.noInput'))}
                      baseFontSize={12}
                      baseLineHeight={1.6}
                    />
                  </div>
                </div>
                <div className={styles.segmentBehavior}>
                  <span>{t('runtime.execution')}</span>
                  <Markdown
                    className={styles.segmentMarkdown}
                    content={formatSegmentMarkdown(data.behavior, t('runtime.noExecution'))}
                    baseFontSize={12}
                    baseLineHeight={1.6}
                  />
                </div>
                <div className={styles.keyResult}>
                  <div className={styles.keyResultHeader}>
                    <span>{t('runtime.conclusion')}</span>
                    {confidence ? (
                      <Tooltip
                        title={
                          <span className={styles.confidenceSummary}>{confidence.summary}</span>
                        }
                        trigger={['hover', 'focus']}
                        placement="top"
                        classNames={{
                          root: 'qwenpaw-data-overflow-tooltip qwenpaw-data-overflow-tooltip-multiline',
                          body: 'qwenpaw-data-popup-multiline',
                        }}
                      >
                        <button
                          type="button"
                          className={styles.confidenceBadge}
                          data-confidence-level={confidence.level}
                        >
                          {t(`runtime.confidence.${confidence.level}`)}
                        </button>
                      </Tooltip>
                    ) : null}
                  </div>
                  <Markdown
                    className={styles.segmentMarkdown}
                    content={formatSegmentMarkdown(data.conclusion, t('runtime.noConclusion'))}
                    baseFontSize={12}
                    baseLineHeight={1.6}
                  />
                </div>
                {sourceArtifacts.length ? (
                  <div className={styles.inlineArtifact}>
                    <span className={styles.artifactGroupTitle}>{t('runtime.keyArtifacts')}</span>
                    <VirtualList
                      ref={artifactListRef}
                      className={styles.artifactVirtualList}
                      data={visibleArtifacts}
                      height={artifactListHeight}
                      itemHeight={ARTIFACT_ITEM_HEIGHT}
                      itemKey="id"
                      showScrollBar="optional"
                      virtual={shouldVirtualizeArtifacts}
                    >
                      {(artifact) => (
                        <ArtifactListItem
                          artifact={artifact}
                          onArtifactOpen={onArtifactOpen}
                          onArtifactDownload={onArtifactDownload}
                          onArtifactShare={onArtifactShare}
                        />
                      )}
                    </VirtualList>
                    {hasMoreArtifacts ? (
                      <Button
                        className={styles.artifactLoadMore}
                        type="link"
                        size="small"
                        onClick={handleLoadMoreArtifacts}
                      >
                        {t('runtime.loadMoreArtifacts')}
                        <DownOutlined aria-hidden="true" />
                      </Button>
                    ) : null}
                  </div>
                ) : null}
              </div>
            ),
          },
        ]}
      />
    </section>
  );

  return (
    <div className={styles.segmentGroup}>
      <div ref={trajectoryRef} className={styles.segmentTrajectory}>
        <button
          type="button"
          className={styles.trajectoryToggle}
          aria-expanded={trajectoryOpen}
          aria-controls={trajectoryId}
          onClick={() => {
            preserveTrajectoryHeader();
            setTrajectoryOpen((value) => !value);
          }}
        >
          <Clock className={styles.trajectoryClock} aria-hidden="true" />
          <Tooltip
            trigger={['hover', 'focus']}
            title={
              duration === null ? null : (
                <div className={styles.timingTotals}>
                  <span>{t('runtime.totalModelDuration')}</span>
                  <strong>
                    {data.timingTotals
                      ? t('runtime.durationSeconds', {
                          seconds: formatExecutionSeconds(data.timingTotals.modelSeconds),
                        })
                      : t('runtime.timingUnavailable')}
                  </strong>
                  <span>{t('runtime.totalToolDuration')}</span>
                  <strong>
                    {data.timingTotals
                      ? t('runtime.durationSeconds', {
                          seconds: formatExecutionSeconds(data.timingTotals.toolSeconds),
                        })
                      : t('runtime.timingUnavailable')}
                  </strong>
                  <span>{t('runtime.otherDuration')}</span>
                  <strong>
                    {otherDuration === null
                      ? t('runtime.timingUnavailable')
                      : t('runtime.durationSeconds', {
                          seconds: formatExecutionSeconds(otherDuration),
                        })}
                  </strong>
                </div>
              )
            }
          >
            <span tabIndex={duration === null ? undefined : 0}>{workedLabel}</span>
          </Tooltip>
          {duration !== null ? (
            <span className={styles.trajectoryHint}>{t('runtime.businessTrajectory')}</span>
          ) : null}
          <DownOutlined
            aria-hidden="true"
            className={`${styles.trajectoryChevron} ${trajectoryOpen ? styles.trajectoryChevronOpen : ''}`}
          />
        </button>
        <div id={trajectoryId} hidden={!trajectoryOpen}>
          {trajectoryOpen ? (
            <div className={styles.trajectoryEvents}>
              {events.length ? (
                events.map((event) => <RuntimeBizEventCard data={event} key={event.event_id} />)
              ) : (
                <span className={styles.trajectoryEmpty}>{t('runtime.noTrajectory')}</span>
              )}
            </div>
          ) : null}
        </div>
      </div>
      <Badge.Ribbon
        className={styles.segmentRibbon}
        color="#1677ff"
        placement="end"
        rootClassName={styles.segmentCard}
        text={`Seg ${index}`}
      >
        {segmentCard}
      </Badge.Ribbon>
      {terminalEvent ? (
        <div className={styles.segmentTerminalEvent}>
          <RuntimeBizEventCard data={terminalEvent} defaultOpen key={terminalEvent.event_id} />
        </div>
      ) : null}
    </div>
  );
}
