import {
  CodeOutlined,
  DatabaseOutlined,
  DownloadOutlined,
  EyeOutlined,
  FileExcelOutlined,
  FileImageOutlined,
  FileMarkdownOutlined,
  FileOutlined,
  FilePdfOutlined,
  FileZipOutlined,
  LinkOutlined,
  LoadingOutlined,
  MoreOutlined,
} from '@ant-design/icons';
import { Button, Dropdown, Tooltip } from '@agentscope-ai/design';
import { useEffect, useRef, useState } from 'react';
import type { Artifact } from '../../types';
import sharedStyles from '../../index.module.less';
import { mergeStyleModules } from '../../styleModules';
import { useAppI18n } from '@/i18n';
import ownerStyles from './index.module.less';

const styles = mergeStyleModules(ownerStyles, sharedStyles);

interface ArtifactCardProps {
  artifact: Artifact;
  onPreview: () => void;
  onDownload?: () => void;
  onShare?: () => Promise<void>;
  surface?: 'neutral' | 'on-tinted';
  showContextTags?: boolean;
  actionsMode?: 'menu' | 'responsive';
}

const IMAGE_EXTENSIONS = new Set(['avif', 'gif', 'jpeg', 'jpg', 'png', 'svg', 'webp']);
const TABLE_EXTENSIONS = new Set(['csv', 'xls', 'xlsx']);
const CODE_EXTENSIONS = new Set(['css', 'htm', 'html', 'js', 'json', 'py', 'sql', 'ts', 'tsx']);
const ARCHIVE_EXTENSIONS = new Set(['7z', 'gz', 'rar', 'tar', 'zip']);

function getFileExtension(fileName: string) {
  const extension = fileName.split('.').pop()?.toLowerCase();
  return extension && extension !== fileName.toLowerCase() ? extension : '';
}

function getFileIcon(extension: string) {
  if (extension === 'sql') return <DatabaseOutlined />;
  if (TABLE_EXTENSIONS.has(extension)) return <FileExcelOutlined />;
  if (IMAGE_EXTENSIONS.has(extension)) return <FileImageOutlined />;
  if (extension === 'md' || extension === 'mdx') return <FileMarkdownOutlined />;
  if (extension === 'pdf') return <FilePdfOutlined />;
  if (ARCHIVE_EXTENSIONS.has(extension)) return <FileZipOutlined />;
  if (CODE_EXTENSIONS.has(extension)) return <CodeOutlined />;
  return <FileOutlined />;
}

function getFileIconTone(extension: string) {
  if (extension === 'sql') return styles.artifactFileIconSql;
  if (TABLE_EXTENSIONS.has(extension)) return styles.artifactFileIconTable;
  if (IMAGE_EXTENSIONS.has(extension)) return styles.artifactFileIconImage;
  if (extension === 'pdf') return styles.artifactFileIconPdf;
  if (ARCHIVE_EXTENSIONS.has(extension)) return styles.artifactFileIconArchive;
  if (CODE_EXTENSIONS.has(extension) || extension === 'md' || extension === 'mdx') {
    return styles.artifactFileIconCode;
  }
  return styles.artifactFileIconDefault;
}

export default function ArtifactCard({
  artifact,
  onPreview,
  onDownload,
  onShare,
  surface = 'neutral',
  showContextTags = false,
  actionsMode = 'responsive',
}: ArtifactCardProps) {
  const { t } = useAppI18n();
  const [linkCopied, setLinkCopied] = useState(false);
  const [sharing, setSharing] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const copiedTimerRef = useRef<number | null>(null);
  const extension = getFileExtension(artifact.name);
  const description = [artifact.title, artifact.meta].filter(Boolean).join(' · ');
  const segmentContexts = (artifact.segments ?? []).map((segment) => ({
    ...segment,
    title: segment.title.trim() || t('artifact.unnamedSegment'),
  }));
  const showSegmentTag = showContextTags && segmentContexts.length > 0;

  useEffect(
    () => () => {
      if (copiedTimerRef.current !== null) {
        window.clearTimeout(copiedTimerRef.current);
      }
    },
    [],
  );

  const copyArtifactLink = async () => {
    if (!onShare || sharing) return;
    setSharing(true);
    try {
      await onShare();
      setLinkCopied(true);
      if (copiedTimerRef.current !== null) {
        window.clearTimeout(copiedTimerRef.current);
      }
      copiedTimerRef.current = window.setTimeout(() => {
        setLinkCopied(false);
        copiedTimerRef.current = null;
      }, 1200);
    } catch {
      setLinkCopied(false);
    } finally {
      setSharing(false);
    }
  };

  const runMenuAction = (key: string) => {
    setMenuOpen(false);
    if (key === 'share') {
      void copyArtifactLink();
    } else if (key === 'preview') {
      onPreview();
    } else if (key === 'download') {
      onDownload?.();
    }
  };
  const menu = {
    onClick: ({ key }: { key: string }) => runMenuAction(key),
    items: [
      ...(onShare
        ? [
            {
              key: 'share',
              icon: sharing ? <LoadingOutlined spin /> : <LinkOutlined />,
              label: sharing
                ? t('artifact.sharing')
                : linkCopied
                  ? t('artifact.linkCopied')
                  : t('artifact.copyLink'),
              disabled: sharing,
            },
          ]
        : []),
      { key: 'preview', icon: <EyeOutlined />, label: t('artifact.preview') },
      {
        key: 'download',
        icon: <DownloadOutlined />,
        label: t('artifact.download'),
        disabled: !onDownload,
      },
    ],
  };
  const menuButton = (
    <Button
      className={`${styles.sharedArtifactActionButton} ${
        actionsMode === 'menu' ? styles.sharedArtifactMenuButton : styles.sharedArtifactMoreButton
      }`}
      type="text"
      icon={<MoreOutlined />}
      aria-label={t('artifact.moreActions', { name: artifact.name })}
      onClick={actionsMode === 'menu' && !menuOpen ? () => setMenuOpen(true) : undefined}
    />
  );

  return (
    <div
      className={`${styles.sharedArtifactCard} ${
        surface === 'on-tinted' ? styles.sharedArtifactCardOnTinted : ''
      }`}
      data-artifact-card={artifact.id}
      data-file-extension={extension || 'unknown'}
      data-surface={surface}
    >
      <span
        className={`${styles.artifactFileIcon} ${getFileIconTone(extension)}`}
        aria-hidden="true"
      >
        {getFileIcon(extension)}
      </span>
      <button
        className={styles.sharedArtifactMain}
        type="button"
        title={t('artifact.previewNamed', { name: artifact.name })}
        onClick={onPreview}
      >
        <div className={styles.artifactTitleRow}>
          <strong>{artifact.name}</strong>
        </div>
        <span>{description}</span>
      </button>
      <div
        className={styles.sharedArtifactActions}
        aria-label={t('artifact.actions', { name: artifact.name })}
      >
        {actionsMode === 'responsive' ? (
          <span className={styles.sharedArtifactInlineActions}>
            {onShare ? (
              <Tooltip
                title={
                  sharing
                    ? t('artifact.sharing')
                    : linkCopied
                      ? t('artifact.linkCopied')
                      : t('artifact.copyLink')
                }
              >
                <Button
                  className={styles.sharedArtifactActionButton}
                  type="text"
                  icon={<LinkOutlined />}
                  loading={sharing}
                  aria-label={
                    linkCopied
                      ? t('artifact.linkCopied')
                      : t('artifact.copyNamedLink', { name: artifact.name })
                  }
                  onClick={() => void copyArtifactLink()}
                />
              </Tooltip>
            ) : null}
            <Tooltip title={t('artifact.preview')}>
              <Button
                className={styles.sharedArtifactActionButton}
                type="text"
                icon={<EyeOutlined />}
                aria-label={t('artifact.previewNamed', { name: artifact.name })}
                onClick={onPreview}
              />
            </Tooltip>
            <Tooltip
              title={onDownload ? t('artifact.download') : t('artifact.downloadUnavailable')}
            >
              <span>
                <Button
                  className={styles.sharedArtifactActionButton}
                  type="text"
                  icon={<DownloadOutlined />}
                  aria-label={t('artifact.downloadNamed', { name: artifact.name })}
                  disabled={!onDownload}
                  onClick={onDownload}
                />
              </span>
            </Tooltip>
          </span>
        ) : null}
        {actionsMode === 'menu' && !menuOpen ? (
          menuButton
        ) : (
          <Dropdown
            placement="bottomRight"
            trigger={['click']}
            menu={menu}
            open={actionsMode === 'menu' ? menuOpen : undefined}
            onOpenChange={actionsMode === 'menu' ? setMenuOpen : undefined}
          >
            {menuButton}
          </Dropdown>
        )}
      </div>
      {showSegmentTag ? (
        <div className={styles.artifactSegmentRow}>
          {segmentContexts.map((segment) => (
            <div
              className={styles.artifactSegmentItem}
              key={segment.id}
              aria-label={t('artifact.segmentLocation', {
                segments:
                  segment.number === undefined
                    ? segment.title
                    : `Segment ${segment.number} ${segment.title}`,
              })}
            >
              {segment.number !== undefined ? (
                <strong className={styles.artifactSegmentLabel}>Segment {segment.number}</strong>
              ) : null}
              <span className={styles.artifactSegmentTitle}>{segment.title}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
