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
  MoreOutlined,
} from '@ant-design/icons';
import { Button, Dropdown, Tooltip } from '@agentscope-ai/design';
import { useState } from 'react';
import type { Artifact } from '../types';
import styles from '../index.module.less';
import OverflowTooltip from './OverflowTooltip';

interface ArtifactCardProps {
  artifact: Artifact;
  onPreview: () => void;
  onDownload?: () => void;
  surface?: 'neutral' | 'on-tinted';
  showMoreMenu?: boolean;
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
  surface = 'neutral',
  showMoreMenu = true,
}: ArtifactCardProps) {
  const [linkCopied, setLinkCopied] = useState(false);
  const extension = getFileExtension(artifact.name);
  const description = [artifact.title, artifact.meta].filter(Boolean).join(' · ');

  const copyArtifactLink = async () => {
    const artifactUrl = `${window.location.origin}${window.location.pathname}#artifact=${artifact.id}`;
    try {
      await navigator.clipboard?.writeText(artifactUrl);
      setLinkCopied(true);
      window.setTimeout(() => setLinkCopied(false), 1200);
    } catch {
      setLinkCopied(false);
    }
  };

  const runMenuAction = (key: string) => {
    if (key === 'copy') {
      void copyArtifactLink();
    } else if (key === 'preview') {
      onPreview();
    } else if (key === 'download') {
      onDownload?.();
    }
  };

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
        title={`预览 ${artifact.name}`}
        onClick={onPreview}
      >
        <OverflowTooltip title={artifact.name}>
          <strong>{artifact.name}</strong>
        </OverflowTooltip>
        <OverflowTooltip title={description}>
          <span>{description}</span>
        </OverflowTooltip>
      </button>
      <div
        className={`${styles.sharedArtifactActions} ${
          showMoreMenu ? '' : styles.sharedArtifactActionsAlwaysInline
        }`}
        aria-label={`${artifact.name} 操作`}
      >
        <span className={styles.sharedArtifactInlineActions}>
          <Tooltip title={linkCopied ? '链接已复制' : '复制链接'}>
            <Button
              className={styles.sharedArtifactActionButton}
              type="text"
              icon={<LinkOutlined />}
              aria-label={linkCopied ? '链接已复制' : `复制 ${artifact.name} 链接`}
              onClick={() => void copyArtifactLink()}
            />
          </Tooltip>
          <Tooltip title="预览">
            <Button
              className={styles.sharedArtifactActionButton}
              type="text"
              icon={<EyeOutlined />}
              aria-label={`预览 ${artifact.name}`}
              onClick={onPreview}
            />
          </Tooltip>
          <Tooltip title={onDownload ? '下载' : '当前产物不可下载'}>
            <span>
              <Button
                className={styles.sharedArtifactActionButton}
                type="text"
                icon={<DownloadOutlined />}
                aria-label={`下载 ${artifact.name}`}
                disabled={!onDownload}
                onClick={onDownload}
              />
            </span>
          </Tooltip>
        </span>
        {showMoreMenu ? (
          <Dropdown
            placement="bottomRight"
            trigger={['click']}
            menu={{
              onClick: ({ key }: { key: string }) => runMenuAction(key),
              items: [
                {
                  key: 'copy',
                  icon: <LinkOutlined />,
                  label: linkCopied ? '链接已复制' : '复制链接',
                },
                { key: 'preview', icon: <EyeOutlined />, label: '预览' },
                {
                  key: 'download',
                  icon: <DownloadOutlined />,
                  label: '下载',
                  disabled: !onDownload,
                },
              ],
            }}
          >
            <Button
              className={`${styles.sharedArtifactActionButton} ${styles.sharedArtifactMoreButton}`}
              type="text"
              icon={<MoreOutlined />}
              aria-label={`${artifact.name} 更多操作`}
            />
          </Dropdown>
        ) : null}
      </div>
    </div>
  );
}
