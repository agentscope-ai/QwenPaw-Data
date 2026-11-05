import { DownOutlined } from '@ant-design/icons';
import Markdown from '@agentscope-ai/chat/lib/Markdown';
import { Collapse } from '@agentscope-ai/design';
import { useState } from 'react';
import type { Segment, SegmentArtifact } from '../api/types';
import { formatSegmentDuration } from '../model/runtimeProjection';
import { formatSegmentMarkdown } from '../model/segmentMarkdown';
import type { Artifact } from '../types';
import ArtifactCard from './ArtifactCard';
import styles from '../index.module.less';

interface RuntimeSegmentCardProps {
  data: Segment;
  index: number;
  onArtifactOpen?: (artifact: Artifact) => void;
  onArtifactDownload?: (artifact: Artifact) => void;
}

function toDisplayArtifact(segmentId: string, artifact: SegmentArtifact): Artifact {
  const extension = artifact.name.split('.').pop()?.toUpperCase() || 'FILE';
  return {
    id: `${segmentId}:${artifact.relative_path}`,
    name: artifact.name,
    title: artifact.description,
    type: extension,
    meta: artifact.relative_path,
    important: true,
    path: artifact.relative_path,
    access: {
      relativePath: artifact.relative_path,
    },
  };
}

export default function RuntimeSegmentCard({
  data,
  index,
  onArtifactOpen,
  onArtifactDownload,
}: RuntimeSegmentCardProps) {
  const [open, setOpen] = useState(true);
  const duration = formatSegmentDuration(data.started_at, data.ended_at);
  const artifacts = (data.artifact ?? [])
    .map((artifact) => toDisplayArtifact(data.segment_id, artifact));

  return (
    <section className={styles.segmentCard} data-segment-id={data.segment_id}>
      <Collapse
        className={styles.segmentCollapse}
        activeKey={open ? [data.segment_id] : []}
        onChange={(activeKeys) => {
          const keys = Array.isArray(activeKeys) ? activeKeys : [activeKeys];
          setOpen(keys.includes(data.segment_id));
        }}
        items={[
          {
            key: data.segment_id,
            showArrow: false,
            label: (
              <div className={styles.segmentHeader}>
                <span className={styles.segmentIndex} aria-hidden="true">
                  <strong>Segment {index}</strong>
                </span>
                <span className={styles.segmentHeaderCopy}>
                  <strong>{data.title}</strong>
                  {duration ? (
                    <span className={styles.segmentDuration}>{duration}</span>
                  ) : null}
                </span>
                <DownOutlined
                  className={`${styles.collapseChevron} ${open ? styles.collapseChevronOpen : ''}`}
                />
              </div>
            ),
            children: (
              <div className={styles.segmentBody}>
                <div className={styles.segmentSummaryGrid}>
                  <div>
                    <span>输入</span>
                    <Markdown
                      className={styles.segmentMarkdown}
                      content={formatSegmentMarkdown(data.input, '无输入')}
                      baseFontSize={12}
                      baseLineHeight={1.6}
                    />
                  </div>
                </div>
                <div className={styles.segmentBehavior}>
                  <span>执行</span>
                  <Markdown
                    className={styles.segmentMarkdown}
                    content={formatSegmentMarkdown(data.behavior, '无执行内容')}
                    baseFontSize={12}
                    baseLineHeight={1.6}
                  />
                </div>
                <div className={styles.keyResult}>
                  <span>结论</span>
                  <Markdown
                    className={styles.segmentMarkdown}
                    content={formatSegmentMarkdown(data.conclusion, '无结论')}
                    baseFontSize={12}
                    baseLineHeight={1.6}
                  />
                </div>
                {artifacts.length ? (
                  <div className={styles.inlineArtifact}>
                    <span className={styles.artifactGroupTitle}>关键产物</span>
                    <div className={styles.artifactList}>
                      {artifacts.map((artifact) => (
                        <ArtifactCard
                          artifact={artifact}
                          key={artifact.id}
                          surface="on-tinted"
                          onPreview={() => onArtifactOpen?.(artifact)}
                          onDownload={onArtifactDownload
                            ? () => onArtifactDownload(artifact)
                            : undefined}
                        />
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            ),
          },
        ]}
      />
    </section>
  );
}
