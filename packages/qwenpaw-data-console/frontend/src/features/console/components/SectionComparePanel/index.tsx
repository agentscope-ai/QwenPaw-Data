import Markdown from '@agentscope-ai/chat/lib/Markdown';
import { DownOutlined } from '@ant-design/icons';
import { Alert, Empty, Spin, Switch, Tag } from 'antd';
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { useAppI18n } from '@/i18n';
import {
  GT_SCROLL_CHANNEL,
  injectEchartsResizeSupport,
  injectGtScrollBridge,
} from '../../model/artifacts/artifactPreview';
import { dataConsoleApi } from '../../api/client';
import type { ComparisonResult } from '../../api/types';
import {
  readArtifactText,
  readSectionComparison,
  readSourceDocument,
  sectionSourcePath,
  type SectionComparisonView,
  type SourceDocument,
} from '../../model/report/reportArtifacts';
import {
  buildDiffIndex,
  buildRunChecks,
  projectDiffSpans,
  shouldPollComparison,
  splitDraftDiffSection,
  type CompareSelection,
} from '../../model/report/sectionDiff';
import {
  mapAnchorScroll,
  matchAnchorPairs,
  type ScrollAnchor,
} from '../../model/report/scrollSync';
import styles from './index.module.less';
import { highlightComparedHtml } from '../../model/compare/htmlHighlights';
const HighlightedMarkdown = lazy(() => import('./HighlightedMarkdown'));

/**
 * Two-document comparison, rendered inside the artifact preview modal (方案7).
 *
 * The left pane is always the whole reference (GT) document — the old
 * "本 Section 原文 / 整份 GT" segment switch is gone (D3) — and it auto-scrolls
 * to the compared section's title once the GT is ready. The right pane is the
 * reproduced draft: the Agent-written "复现差异与数据缺口说明" block heads the
 * right pane as a collapsible list above the draft body, and only mismatched
 * numbers in the body are highlighted (方案5 D5).
 *
 * `selection.section` non-null means the pair is a reproduction draft against
 * its own GT. Persisted files supply review status and criteria; baseline numeric
 * differences are recomputed against the current section source. Other pairs use
 * the generic `POST /files/compare` endpoint too, without writing files.
 */

const POLL_INTERVAL_MS = 3_000;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function extensionOf(path: string): string {
  const index = path.lastIndexOf('.');
  return index < 0 ? '' : path.slice(index + 1).toLowerCase();
}

function isHtmlPath(path: string | null): boolean {
  if (!path) return false;
  const extension = extensionOf(path);
  return extension === 'html' || extension === 'htm';
}

function statusColor(status?: string): string {
  if (status === 'approved') return 'success';
  if (status === 'failed') return 'error';
  if (status === 'needs_input') return 'warning';
  return 'processing';
}

function statusLabel(status?: string): string {
  if (status === 'approved') return '已通过';
  if (status === 'failed') return '待修订';
  if (status === 'needs_input') return '待补充';
  if (status === 'validating') return '验收中';
  if (status === 'checking') return '校验中';
  if (status === 'running') return '生成中';
  if (status === 'pending') return '待生成';
  return '待生成';
}

/**
 * Ratio fallback for sync scroll, used only when the two panes share no heading
 * anchors (the primary map is pixel-based — see scrollSync.ts). A pane that
 * does not overflow reports `null` and is skipped rather than snapped back to
 * its top.
 */
function scrollRatioOf(node: HTMLElement | null): number | null {
  if (!node) return null;
  const max = node.scrollHeight - node.clientHeight;
  return max > 1 ? node.scrollTop / max : null;
}

function applyScrollRatio(node: HTMLElement | null, ratio: number): void {
  if (!node || !Number.isFinite(ratio)) return;
  const max = node.scrollHeight - node.clientHeight;
  if (max <= 1) return;
  const next = Math.max(0, Math.min(1, ratio)) * max;
  // Skip sub-pixel writes, otherwise the mirrored pane reports a rounded ratio
  // back and the two keep nudging each other.
  if (Math.abs(next - node.scrollTop) < 0.5) return;
  node.scrollTop = next;
}

function applyScrollTop(node: HTMLElement | null, top: number): void {
  if (!node || !Number.isFinite(top)) return;
  const max = node.scrollHeight - node.clientHeight;
  if (max <= 1) return;
  const next = Math.max(0, Math.min(max, top));
  // Same sub-pixel guard as applyScrollRatio.
  if (Math.abs(next - node.scrollTop) < 0.5) return;
  node.scrollTop = next;
}

/** Heading offsets of a rendered Markdown pane, in scroll-content pixels. */
function collectDomAnchors(
  container: HTMLElement | null,
  scope: HTMLElement | null = container,
): ScrollAnchor[] {
  if (!container || !scope) return [];
  const base = container.getBoundingClientRect().top - container.scrollTop;
  return Array.from(scope.querySelectorAll('h1, h2, h3, h4, h5, h6')).map((element) => ({
    text: element.textContent ?? '',
    y: element.getBoundingClientRect().top - base,
  }));
}

function postScrollRatio(frame: HTMLIFrameElement | null, ratio: number): void {
  if (!frame || !Number.isFinite(ratio)) return;
  try {
    frame.contentWindow?.postMessage({ channel: GT_SCROLL_CHANNEL, kind: 'apply', ratio }, '*');
  } catch {
    // An opaque-origin frame may refuse the message; sync then just degrades.
  }
}

function postScrollTop(frame: HTMLIFrameElement | null, top: number): void {
  if (!frame || !Number.isFinite(top)) return;
  try {
    frame.contentWindow?.postMessage({ channel: GT_SCROLL_CHANNEL, kind: 'apply', top }, '*');
  } catch {
    // An opaque-origin frame may refuse the message; sync then just degrades.
  }
}

/** Ask the GT frame to re-measure and re-report its heading anchors. */
function postGtCollect(frame: HTMLIFrameElement | null): void {
  if (!frame) return;
  try {
    frame.contentWindow?.postMessage({ channel: GT_SCROLL_CHANNEL, kind: 'collect' }, '*');
  } catch {
    // Same best-effort story as postScrollTop.
  }
}

/** Ask the sandboxed GT frame to scroll to the first node containing `text`. */
function postGtLocate(frame: HTMLIFrameElement | null, text: string): void {
  if (!frame || !text) return;
  try {
    frame.contentWindow?.postMessage({ channel: GT_SCROLL_CHANNEL, kind: 'locate', text }, '*');
  } catch {
    // Locating is best-effort; a refusal just leaves the GT at its top.
  }
}

interface GtFrameMessage {
  kind: 'scroll' | 'anchors' | 'position';
  top: number | null;
  ratio: number | null;
  anchors: ScrollAnchor[];
}

function gtFrameMessageFromEvent(event: MessageEvent): GtFrameMessage | null {
  const data = event.data as {
    channel?: unknown;
    kind?: unknown;
    ratio?: unknown;
    top?: unknown;
    items?: unknown;
  } | null;
  if (!data || data.channel !== GT_SCROLL_CHANNEL) return null;
  if (data.kind === 'anchors') {
    const items = Array.isArray(data.items) ? data.items : [];
    return {
      kind: 'anchors',
      top: null,
      ratio: null,
      anchors: items
        .map((item) => {
          const raw = item as { t?: unknown; y?: unknown };
          return { text: typeof raw.t === 'string' ? raw.t : '', y: Number(raw.y) };
        })
        .filter((anchor) => anchor.text && Number.isFinite(anchor.y)),
    };
  }
  if (data.kind === 'scroll' || data.kind === 'ratio' || data.kind === 'position') {
    const top = typeof data.top === 'number' && Number.isFinite(data.top) ? data.top : null;
    const ratio = typeof data.ratio === 'number' && Number.isFinite(data.ratio) ? data.ratio : null;
    if (top === null && ratio === null) return null;
    return { kind: data.kind === 'position' ? 'position' : 'scroll', top, ratio, anchors: [] };
  }
  return null;
}

interface SectionComparePanelProps {
  sessionId: string | null;
  selection: CompareSelection;
}

export default function SectionComparePanel({ sessionId, selection }: SectionComparePanelProps) {
  const { t } = useAppI18n();
  const [comparison, setComparison] = useState<SectionComparisonView | null>(null);
  const [adhocComparison, setAdhocComparison] = useState<ComparisonResult | null>(null);
  const [adhocTarget, setAdhocTarget] = useState('');
  const [referenceDoc, setReferenceDoc] = useState<SourceDocument | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [referenceError, setReferenceError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [leftRatio, setLeftRatio] = useState(50);
  const [isResizing, setIsResizing] = useState(false);
  const [activeDiffId, setActiveDiffId] = useState<string | null>(null);
  const [isSyncScroll, setIsSyncScroll] = useState(true);
  const [isTopDiffOpen, setIsTopDiffOpen] = useState(false);
  const topDiffRef = useRef<HTMLDivElement>(null);

  const columnsRef = useRef<HTMLDivElement>(null);
  const leftScrollRef = useRef<HTMLDivElement>(null);
  const leftFrameRef = useRef<HTMLIFrameElement>(null);
  const rightFrameRef = useRef<HTMLIFrameElement>(null);
  const rightFramePosition = useRef<{ top: number; ratio: number | null }>({ top: 0, ratio: 0 });
  const rightScrollRef = useRef<HTMLDivElement>(null);
  // Scopes the right pane's heading anchors to the Markdown body.
  const rightBodyRef = useRef<HTMLDivElement>(null);
  // Heading pixel offsets per pane — the anchor-sync map's raw material. The
  // Markdown side is read off the DOM; the HTML frame pushes its own.
  const leftAnchorsRef = useRef<ScrollAnchor[]>([]);
  const rightAnchorsRef = useRef<ScrollAnchor[]>([]);
  // Scroll handlers stay identity-stable, so they read the toggle off a ref
  // instead of re-binding on every flip.
  const syncScrollRef = useRef(isSyncScroll);
  syncScrollRef.current = isSyncScroll;
  const scrollLockRef = useRef(false);
  // Auto-locate runs once per selection, not on every poll-driven DOM rebuild.
  const didLocateRef = useRef(false);
  // Set the first time the user scrolls either pane, so the settling-phase
  // re-locates below stop and never fight a manual scroll position.
  const userScrolledRef = useRef(false);
  const resizeStateRef = useRef<{
    pointerId: number;
    startX: number;
    startRatio: number;
    containerWidth: number;
  } | null>(null);
  const resizeCleanupRef = useRef<(() => void) | null>(null);

  // `selection` may be a fresh object per render, so key the effects on primitives.
  const sectionId = selection.section?.sectionId ?? null;
  const period = selection.section?.period ?? null;
  const sectionMode = Boolean(sectionId);
  const referencePath = selection.referencePath;
  const targetPath = selection.targetPath;
  const targetIsHtml = isHtmlPath(targetPath);

  const isRunMode = sectionMode && (Boolean(period) || comparison?.mode === 'run');

  // Reset whenever a different pair is compared without unmounting.
  useEffect(() => {
    setComparison(null);
    setAdhocComparison(null);
    setAdhocTarget('');
    setError(null);
    setReferenceError(null);
    leftAnchorsRef.current = [];
    rightAnchorsRef.current = [];
    rightFramePosition.current = { top: 0, ratio: 0 };
    setActiveDiffId(null);
    setLeftRatio(50);
    setIsTopDiffOpen(false);
    didLocateRef.current = false;
    userScrolledRef.current = false;
  }, [sessionId, sectionId, period, referencePath, targetPath]);

  // Poll only the reproduction case, so `validating -> approved/failed` refreshes
  // the verdict; an ad-hoc pair is a one-shot recompute and need not be re-run.
  useEffect(() => {
    if (!sessionId || !shouldPollComparison(sectionMode, isRunMode, comparison?.status))
      return undefined;
    const interval = window.setInterval(() => {
      if (document.visibilityState === 'visible') setRefreshKey((key) => key + 1);
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [sessionId, sectionMode, sectionId, isRunMode, comparison?.status]);

  // One poll reads the pair's verdict: the reproduction case reads the files the
  // skill CLI owns off disk, the ad-hoc case recomputes through the generic
  // endpoint (never written to disk) and reads the target text alongside it.
  useEffect(() => {
    if (!sessionId) return undefined;
    let disposed = false;
    setLoading(true);
    if (sectionMode && sectionId) {
      void readSectionComparison(sessionId, sectionId, period, targetPath)
        .then(async (next) => {
          if (disposed) return;
          setComparison(next);
          setAdhocComparison(null);
          setAdhocTarget('');
          setError(null);
          // Persisted verdicts may be missing, predate offsets, or describe an
          // earlier draft. Compare the current section text, not the whole GT.
          if (next?.mode === 'baseline' && next.source && next.draft) {
            const computed = await dataConsoleApi.compareWorkspaceFiles(sessionId, {
              reference_path: sectionSourcePath(sectionId),
              target_path: targetPath,
            });
            if (!disposed)
              setAdhocComparison({
                ...computed,
                criteria_analysis: next.comparison?.criteria_analysis ?? [],
              });
          }
        })
        .catch((err) => {
          if (!disposed) setError(errorMessage(err));
        })
        .finally(() => {
          if (!disposed) setLoading(false);
        });
    } else if (targetPath) {
      void Promise.all([
        referencePath
          ? dataConsoleApi.compareWorkspaceFiles(sessionId, {
              target_path: targetPath,
              reference_path: referencePath,
            })
          : Promise.resolve(null),
        readArtifactText(sessionId, targetPath).then((text) => {
          if (!disposed) setAdhocTarget(text ?? '');
          return text;
        }),
      ])
        .then(([computed, text]) => {
          if (disposed) return;
          setAdhocComparison(computed);
          setAdhocTarget(text ?? '');
          setComparison(null);
          setError(null);
        })
        .catch((err) => {
          if (!disposed) setError(errorMessage(err));
        })
        .finally(() => {
          if (!disposed) setLoading(false);
        });
    } else {
      setLoading(false);
    }
    return () => {
      disposed = true;
    };
  }, [refreshKey, sessionId, sectionMode, sectionId, period, referencePath, targetPath]);

  // Left pane reference document: the whole GT for a reproduction, or the raw
  // user-picked file (md/txt/html only — see 方案7 Q2) for an ad-hoc pair.
  useEffect(() => {
    if (!sessionId) return undefined;
    let disposed = false;
    setReferenceDoc(null);
    setReferenceError(null);
    if (sectionMode) {
      void readSourceDocument(sessionId)
        .then((doc) => {
          if (!disposed) {
            setReferenceDoc(doc);
            setReferenceError(null);
          }
        })
        .catch((err) => {
          if (!disposed) setReferenceError(errorMessage(err));
        });
    } else if (referencePath) {
      void readArtifactText(sessionId, referencePath)
        .then((text) => {
          if (disposed) return;
          setReferenceError(null);
          if (text === null) {
            setReferenceDoc(null);
            return;
          }
          const html = isHtmlPath(referencePath);
          setReferenceDoc({
            kind: html ? 'html' : 'markdown',
            content: text,
            path: html ? referencePath : null,
          });
        })
        .catch((err) => {
          if (!disposed) {
            setReferenceDoc(null);
            setReferenceError(errorMessage(err));
          }
        });
    } else {
      setReferenceDoc(null);
    }
    return () => {
      disposed = true;
    };
  }, [sessionId, sectionMode, referencePath]);

  useEffect(
    () => () => {
      resizeCleanupRef.current?.();
    },
    [],
  );

  const targetText = sectionMode ? (comparison?.draft ?? '') : adhocTarget;
  const machineComparison = sectionMode
    ? (adhocComparison ?? comparison?.comparison ?? null)
    : adhocComparison;

  const diffIndex = useMemo(
    () => (isRunMode ? buildDiffIndex(null) : buildDiffIndex(machineComparison)),
    [machineComparison, isRunMode],
  );
  const runChecks = useMemo(
    () => (isRunMode ? buildRunChecks(comparison?.runChecks) : null),
    [comparison?.runChecks, isRunMode],
  );
  const criteria = useMemo(
    () => (!isRunMode ? (machineComparison?.criteria_analysis ?? []) : []),
    [machineComparison, isRunMode],
  );
  const draftSplit = useMemo(
    () =>
      targetIsHtml
        ? { diff: null, body: targetText, retained: [] }
        : splitDraftDiffSection(targetText),
    [targetText, targetIsHtml],
  );
  const highlightSpans = useMemo(
    () =>
      isRunMode || targetIsHtml
        ? []
        : projectDiffSpans(targetText, diffIndex.rows, draftSplit.retained),
    [targetText, diffIndex.rows, draftSplit.retained, isRunMode, targetIsHtml],
  );
  const fullTargetHtml = useMemo(
    () =>
      targetIsHtml && targetText
        ? injectGtScrollBridge(
            injectEchartsResizeSupport(
              highlightComparedHtml(targetText, machineComparison?.target_html_spans),
            ),
          )
        : null,
    [targetIsHtml, targetText, machineComparison?.target_html_spans],
  );

  // Memoised because the reproduction case polls every few seconds and rebuilding
  // a multi-megabyte GT string per poll would also churn the locate effect below.
  const fullGtHtml = useMemo(
    () =>
      referenceDoc?.kind === 'html'
        ? injectGtScrollBridge(injectEchartsResizeSupport(referenceDoc.content))
        : null,
    [referenceDoc],
  );

  // D3: locate the compared section inside the whole GT once its content is ready.
  const gtTitle = sectionMode ? (comparison?.title ?? '') : '';
  useEffect(() => {
    if (didLocateRef.current || !gtTitle) return;
    if (referenceDoc?.kind === 'html') {
      if (!fullGtHtml) return;
      didLocateRef.current = true;
      // The frame needs a tick to parse srcDoc before it can answer a locate,
      // and it keeps re-laying-out for a few seconds after that — async charts
      // and the fallback renderer push the located section around. Re-locate on
      // a widening schedule so the GT settles back onto the compared section,
      // but bail out the instant the user scrolls either pane so we never fight
      // a manual position.
      const timers = [300, 1400, 2800].map((delay) =>
        window.setTimeout(() => {
          if (userScrolledRef.current) return;
          postGtLocate(leftFrameRef.current, gtTitle);
        }, delay),
      );
      return () => timers.forEach((id) => window.clearTimeout(id));
    }
    if (referenceDoc?.kind === 'markdown') {
      didLocateRef.current = true;
      window.requestAnimationFrame(() => {
        const container = leftScrollRef.current;
        if (!container) return;
        const headings = container.querySelectorAll('h1, h2, h3, h4, h5, h6');
        for (const heading of Array.from(headings)) {
          if ((heading.textContent ?? '').includes(gtTitle)) {
            heading.scrollIntoView({ block: 'start' });
            break;
          }
        }
      });
    }
  }, [gtTitle, isRunMode, referenceDoc, fullGtHtml]);

  // Heading anchors for the sync map. Markdown panes are readable directly; the
  // HTML frame pushes its own over the bridge and is re-asked on every resize.
  useEffect(() => {
    const collect = () => {
      if (rightScrollRef.current)
        rightAnchorsRef.current = collectDomAnchors(rightScrollRef.current, rightBodyRef.current);
      if (leftScrollRef.current) {
        leftAnchorsRef.current = collectDomAnchors(leftScrollRef.current);
      }
    };
    const frame = window.requestAnimationFrame(collect);
    // Images and fonts shift heading offsets after first paint; re-measure once.
    const timer = window.setTimeout(collect, 500);
    return () => {
      window.cancelAnimationFrame(frame);
      window.clearTimeout(timer);
    };
  }, [draftSplit.body, referenceDoc, leftRatio]);

  useEffect(() => {
    if (referenceDoc?.kind !== 'html' && !targetIsHtml) return undefined;
    // The GT frame keeps re-laying-out for a while after first paint (async
    // charts, the fallback renderer, fonts), and each shift moves every heading
    // below it — so a single early anchor read goes stale and the whole sync map
    // drifts. Ask the frame to re-report on a widening schedule that outlasts
    // the chart settle; the frame also self-reports (incl. a ResizeObserver).
    const timers = [250, 1200, 2600, 4200].map((delay) =>
      window.setTimeout(() => {
        postGtCollect(leftFrameRef.current);
        postGtCollect(rightFrameRef.current);
      }, delay),
    );
    return () => timers.forEach((id) => window.clearTimeout(id));
  }, [leftRatio, fullGtHtml, fullTargetHtml, referenceDoc?.kind, targetIsHtml]);

  const releaseScrollLock = useCallback(() => {
    // A programmatic `scrollTop` write dispatches its scroll event during the
    // next frame's scroll steps, so releasing one frame later still swallows it.
    window.requestAnimationFrame(() => {
      window.setTimeout(() => {
        scrollLockRef.current = false;
      }, 0);
    });
  }, []);

  const anchorPairsNow = useCallback(() => {
    const pairs = matchAnchorPairs(leftAnchorsRef.current, rightAnchorsRef.current);
    // A lone matched heading only yields a constant-offset map, which drifts
    // badly when the panes differ in length (whole GT vs one reproduced section).
    // Require at least two anchors; otherwise return none so the callers fall
    // back to proportional (ratio) sync — what actually keeps the panes together.
    return pairs.length >= 2 ? pairs : [];
  }, []);

  // Right → left: anchor mapping first, ratio as the no-anchor fallback.
  const syncRightToLeft = useCallback(() => {
    const right = rightScrollRef.current;
    const position = right
      ? { top: right.scrollTop, ratio: scrollRatioOf(right) }
      : rightFramePosition.current;
    const mapped = mapAnchorScroll(anchorPairsNow(), position.top, 'right');
    if (mapped !== null) {
      // Exactly one of the two left-hand renders is mounted at a time.
      if (leftScrollRef.current) applyScrollTop(leftScrollRef.current, mapped);
      else postScrollTop(leftFrameRef.current, mapped);
      return;
    }
    const ratio = position.ratio;
    if (ratio === null) return;
    if (leftScrollRef.current) applyScrollRatio(leftScrollRef.current, ratio);
    else postScrollRatio(leftFrameRef.current, ratio);
  }, [anchorPairsNow]);

  const syncLeftToRight = useCallback(
    (leftTop: number, ratioFallback: number | null) => {
      const right = rightScrollRef.current;
      const mapped = mapAnchorScroll(anchorPairsNow(), leftTop, 'left');
      if (mapped !== null) {
        if (right) applyScrollTop(right, mapped);
        else postScrollTop(rightFrameRef.current, mapped);
      } else if (ratioFallback !== null) {
        if (right) applyScrollRatio(right, ratioFallback);
        else postScrollRatio(rightFrameRef.current, ratioFallback);
      }
    },
    [anchorPairsNow],
  );

  const handleLeftScroll = useCallback(() => {
    if (!syncScrollRef.current || scrollLockRef.current) return;
    const left = leftScrollRef.current;
    if (!left) return;
    userScrolledRef.current = true;
    scrollLockRef.current = true;
    syncLeftToRight(left.scrollTop, scrollRatioOf(left));
    releaseScrollLock();
  }, [releaseScrollLock, syncLeftToRight]);

  const handleRightScroll = useCallback(() => {
    if (!syncScrollRef.current || scrollLockRef.current) return;
    // User drove the draft pane; freeze the GT auto re-locate from here on.
    userScrolledRef.current = true;
    scrollLockRef.current = true;
    syncRightToLeft();
    releaseScrollLock();
  }, [releaseScrollLock, syncRightToLeft]);

  // A highlight click expands the top gap block and scrolls it into view; the
  // machine rows it summarises live there, not in a toolbar dropdown (D4).
  const focusDiffRow = useCallback((id: string) => {
    setActiveDiffId(id);
    setIsTopDiffOpen(true);
    window.requestAnimationFrame(() => {
      topDiffRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
  }, []);

  // Opaque-origin HTML frames are trusted only by their exact window identity.
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const side =
        leftFrameRef.current?.contentWindow === event.source
          ? 'left'
          : rightFrameRef.current?.contentWindow === event.source
            ? 'right'
            : null;
      if (!side || !event.source) return;
      if (
        side === 'right' &&
        event.data?.channel === GT_SCROLL_CHANNEL &&
        event.data?.kind === 'diff-select' &&
        /^diff-\d+$/.test(event.data.id ?? '')
      ) {
        focusDiffRow(event.data.id);
        return;
      }
      const message = gtFrameMessageFromEvent(event);
      if (!message) return;
      if (message.kind === 'anchors') {
        if (side === 'left') leftAnchorsRef.current = message.anchors;
        else rightAnchorsRef.current = message.anchors;
        return;
      }
      if (message.top === null) return;
      if (side === 'right') rightFramePosition.current = { top: message.top, ratio: message.ratio };
      if (message.kind === 'position') return;
      if (!syncScrollRef.current || scrollLockRef.current) return;
      userScrolledRef.current = true;
      scrollLockRef.current = true;
      if (side === 'left') syncLeftToRight(message.top, message.ratio);
      else syncRightToLeft();
      releaseScrollLock();
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [syncLeftToRight, syncRightToLeft, releaseScrollLock, focusDiffRow]);

  const handleSyncScrollChange = useCallback(
    (next: boolean) => {
      setIsSyncScroll(next);
      syncScrollRef.current = next;
      if (!next) return;
      // Align immediately so re-enabling sync does not wait for the next gesture.
      scrollLockRef.current = true;
      syncRightToLeft();
      releaseScrollLock();
    },
    [syncRightToLeft, releaseScrollLock],
  );

  const handleResizeStart = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      const container = columnsRef.current;
      if (!container) return;
      event.preventDefault();
      resizeStateRef.current = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startRatio: leftRatio,
        containerWidth: container.getBoundingClientRect().width,
      };
      setIsResizing(true);

      const handleMove = (moveEvent: globalThis.PointerEvent) => {
        const state = resizeStateRef.current;
        if (!state || state.pointerId !== moveEvent.pointerId) return;
        const deltaPct = ((moveEvent.clientX - state.startX) / state.containerWidth) * 100;
        setLeftRatio(Math.min(80, Math.max(20, state.startRatio + deltaPct)));
      };
      const finishResize = (finishEvent: globalThis.PointerEvent) => {
        const state = resizeStateRef.current;
        if (!state || state.pointerId !== finishEvent.pointerId) return;
        resizeStateRef.current = null;
        setIsResizing(false);
        resizeCleanupRef.current?.();
      };

      resizeCleanupRef.current?.();
      resizeCleanupRef.current = () => {
        window.removeEventListener('pointermove', handleMove);
        window.removeEventListener('pointerup', finishResize);
        window.removeEventListener('pointercancel', finishResize);
      };
      window.addEventListener('pointermove', handleMove);
      window.addEventListener('pointerup', finishResize);
      window.addEventListener('pointercancel', finishResize);
    },
    [leftRatio],
  );

  // Narrative and machine results remain independently visible in the same disclosure.
  const narrative = draftSplit.diff;
  const narrativeCount = narrative
    ? narrative.split('\n').filter((line) => /^\s*[-*]\s+/.test(line)).length
    : 0;
  const topTitle = narrative
    ? t('console.compareDiffBlockTitle')
    : isRunMode
      ? '本期检查项'
      : '差异清单';
  const hasMachineRows = Boolean(
    diffIndex.rows.length ||
    diffIndex.structure.length ||
    criteria.length ||
    diffIndex.notes ||
    diffIndex.coverage !== null ||
    machineComparison,
  );
  const hasTopContent = Boolean(narrative) || isRunMode || hasMachineRows;

  const leftPane =
    referenceDoc?.kind === 'html' ? (
      fullGtHtml ? (
        <iframe
          className={styles.sourceFrame}
          ref={leftFrameRef}
          srcDoc={fullGtHtml}
          title={selection.referenceLabel || 'GT 文档'}
          sandbox="allow-scripts"
          referrerPolicy="no-referrer"
        />
      ) : (
        <div className={styles.paneEmpty}>
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="GT 文档暂不可预览" />
        </div>
      )
    ) : referenceDoc?.kind === 'markdown' ? (
      <div className={styles.paneDocument} onScroll={handleLeftScroll} ref={leftScrollRef}>
        <Markdown content={referenceDoc.content} baseFontSize={13} baseLineHeight={1.7} />
      </div>
    ) : (
      <div className={styles.paneEmpty}>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={sectionMode ? '原报告尚未解析' : '未选择源文档'}
        />
      </div>
    );

  // The gap/diff block heads the reproduced (right) pane: every row in it
  // describes the right document, so it lives inside that pane, under its
  // header, collapsed by default.
  const diffListBlock = (
    <div className={styles.diffList} ref={topDiffRef}>
      <div
        aria-expanded={isTopDiffOpen}
        className={styles.diffListHeader}
        role="button"
        tabIndex={0}
        onClick={() => setIsTopDiffOpen((open) => !open)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            setIsTopDiffOpen((open) => !open);
          }
        }}
      >
        <DownOutlined
          aria-hidden="true"
          className={`${styles.collapseChevron} ${isTopDiffOpen ? styles.collapseChevronOpen : ''}`}
        />
        <h4>{topTitle}</h4>
        <span className={styles.diffListAside}>
          {narrative ? (
            <span className={styles.diffListStat}>
              {narrativeCount > 0 ? t('console.compareDiffCount', { count: narrativeCount }) : null}
            </span>
          ) : isRunMode ? (
            runChecks ? (
              <Tag color={runChecks.passed ? 'success' : 'error'}>
                {runChecks.passed ? '检查通过' : '存在未通过项'}
              </Tag>
            ) : (
              <span className={styles.diffListStat}>尚未生成检查结果</span>
            )
          ) : diffIndex.structure.length ? (
            <span className={styles.diffListStat}>
              {diffIndex.structure.length}
              {' 处结构差异'}
            </span>
          ) : (
            <span className={styles.diffListStat}>暂无差异记录</span>
          )}
        </span>
      </div>

      {isTopDiffOpen ? (
        <div className={styles.diffListBody}>
          {narrative ? (
            <div className={styles.diffNarrative}>
              <Markdown content={narrative} baseFontSize={13} baseLineHeight={1.7} />
            </div>
          ) : null}
          {isRunMode ? (
            runChecks?.checks.length ? (
              <ul className={styles.checkList}>
                {runChecks.checks.map((check) => (
                  <li key={check.name} className={styles.checkItem}>
                    <Tag color={check.passed ? 'success' : 'error'}>
                      {check.passed ? '通过' : '未通过'}
                    </Tag>
                    <div>
                      <strong>{check.name}</strong>
                      {check.details ? <p>{check.details}</p> : null}
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未生成本期检查结果" />
            )
          ) : hasMachineRows ? (
            <>
              {diffIndex.structure.length ? (
                <ul className={styles.structureList}>
                  {diffIndex.structure.map((item, index) => (
                    // The payload carries no stable id, so the index is the only key available.
                    <li key={`${item.kind}-${index}`}>
                      <Tag
                        color={
                          item.severity === 'error'
                            ? 'error'
                            : item.severity === 'warning'
                              ? 'warning'
                              : 'default'
                        }
                      >
                        {item.kind}
                      </Tag>
                      <span>{item.description}</span>
                    </li>
                  ))}
                </ul>
              ) : null}
            </>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无差异记录" />
          )}

          {/* A cross-period run has no GT to diff against, so its `notes` are the
              only 口径 disclosure it carries — always rendered verbatim. */}
          {runChecks?.notes ? <p className={styles.diffNotes}>{runChecks.notes}</p> : null}
        </div>
      ) : null}
    </div>
  );

  return (
    <div className={styles.panel}>
      <div className={styles.toolbar}>
        <div className={styles.toolbarSync}>
          <Switch
            aria-label={t('console.compareSyncScroll')}
            checked={isSyncScroll}
            onChange={handleSyncScrollChange}
            size="small"
          />
          <span>同步滚动</span>
        </div>
        <div className={styles.toolbarMeta}>
          <strong title={selection.targetLabel || gtTitle || ''}>
            {selection.targetLabel || gtTitle || '文档对照'}
          </strong>
          {comparison?.status ? (
            <Tag color={statusColor(comparison.status)}>{statusLabel(comparison.status)}</Tag>
          ) : null}
          {isRunMode ? <Tag color="blue">{period ?? runChecks?.period ?? '跨周期'}</Tag> : null}
        </div>
        {referenceDoc?.kind !== 'html' && !targetIsHtml ? (
          <span className={styles.toolbarHint}>{t('console.compareHintDefault')}</span>
        ) : null}
      </div>

      {isRunMode ? (
        <Alert
          className={styles.banner}
          type="info"
          showIcon
          message="跨周期复跑：左栏为原报告基线口径，右栏为本期复现结果，两者数值不应相等"
        />
      ) : null}

      {referenceError ? (
        <Alert className={styles.banner} type="error" showIcon message={referenceError} />
      ) : null}
      {error ? <Alert className={styles.banner} type="error" showIcon message={error} /> : null}

      <Spin spinning={loading && !hasTopContent && !targetText}>
        <div
          className={styles.columns}
          ref={columnsRef}
          style={
            {
              '--left-ratio': `${leftRatio}fr`,
              '--right-ratio': `${100 - leftRatio}fr`,
            } as CSSProperties
          }
        >
          <article className={styles.pane}>
            <div className={styles.paneHeader}>
              <span className={styles.paneEyebrow}>GT</span>
              <h4>{selection.referenceLabel || '整份原报告'}</h4>
            </div>
            {leftPane}
          </article>

          <div
            className={`${styles.resizer} ${isResizing ? styles.resizerActive : ''}`}
            role="separator"
            aria-orientation="vertical"
            aria-label={t('console.compareResize')}
            tabIndex={0}
            aria-valuemin={20}
            aria-valuemax={80}
            aria-valuenow={Math.round(leftRatio)}
            onKeyDown={(event) => {
              if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
                event.preventDefault();
                setLeftRatio((value) =>
                  Math.min(80, Math.max(20, value + (event.key === 'ArrowLeft' ? -2 : 2))),
                );
              }
            }}
            onPointerDown={handleResizeStart}
          />

          <article className={styles.pane}>
            <div className={styles.paneHeader}>
              <span className={styles.paneEyebrow}>TARGET</span>
              <h4>{selection.targetLabel || '比较文档'}</h4>
            </div>
            {fullTargetHtml ? (
              <>
                {/* An HTML target surfaces every mismatch as an inline highlight in
                    the pane, so the collapsible diff list adds nothing and is hidden
                    for any reference type — a markdown reference no longer gets it. */}
                <iframe
                  className={styles.sourceFrame}
                  ref={rightFrameRef}
                  srcDoc={fullTargetHtml}
                  onLoad={() => {
                    postScrollTop(rightFrameRef.current, rightFramePosition.current.top);
                    postGtCollect(rightFrameRef.current);
                  }}
                  title={selection.targetLabel || '当前文档'}
                  sandbox="allow-scripts"
                  referrerPolicy="no-referrer"
                />
              </>
            ) : draftSplit.body ? (
              <div
                className={styles.paneDocument}
                onScroll={handleRightScroll}
                ref={rightScrollRef}
              >
                {/* The diff block flows inside the scroll container so opening it
                    pushes the report down and scrolls away with it, instead of
                    hanging fixed over the pane and eating its height. */}
                {diffListBlock}
                <div ref={rightBodyRef}>
                  <Suspense fallback={<Spin size="small" />}>
                    <HighlightedMarkdown
                      content={draftSplit.body}
                      spans={highlightSpans}
                      activeId={activeDiffId}
                      onSelect={focusDiffRow}
                    />
                  </Suspense>
                </div>
              </div>
            ) : (
              <>
                {diffListBlock}
                <div className={styles.paneEmpty}>
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description="当前文档尚未生成或为空"
                  />
                </div>
              </>
            )}
          </article>
        </div>
      </Spin>
    </div>
  );
}
