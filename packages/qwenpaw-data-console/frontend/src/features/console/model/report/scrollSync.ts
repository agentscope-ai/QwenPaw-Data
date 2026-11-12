/**
 * Anchor-based scroll sync for the compare panes (VSCode-style position sync).
 *
 * Both panes report the pixel offsets of their headings; headings with matching
 * text become anchor pairs, and a piecewise-linear map between the pairs
 * translates one pane's scrollTop into the other's — so the bottom of a draft
 * lands on the corresponding GT content, not on the bottom of the whole GT
 * report. With no matching headings at all, `mapAnchorScroll` returns `null`
 * and the caller falls back to ratio sync.
 *
 * Deliberately import-free so the unit tests run under `node --test` on the
 * compiled output.
 */

export interface ScrollAnchor {
  /** Heading text as rendered (normalised inside `matchAnchorPairs`). */
  text: string;
  /** Pixel offset of the heading inside its scroll content. */
  y: number;
}

export interface AnchorPair {
  left: number;
  right: number;
}

/** Loose heading identity: drop leading numbering, whitespace and punctuation. */
export function normalizeHeading(text: string): string {
  return text
    .toLowerCase()
    .replace(/^(?:\s*(?:\d+(?:\.\d+)*|[一二三四五六七八九十百千]+)[.、:：)）\-–—\s]*)+/, '')
    .replace(/[\s\p{P}\p{S}]+/gu, '');
}

function headingsSimilar(left: string, right: string): boolean {
  if (left.length < 2 || right.length < 2) return false;
  if (left === right) return true;
  return left.length >= 4 && right.length >= 4 && (left.includes(right) || right.includes(left));
}

/**
 * Pair left/right anchors by heading text. Exact normalised matches win over
 * containment matches, and both sides stay strictly increasing in `y` so the
 * interpolated map never folds back on itself.
 */
export function matchAnchorPairs(left: ScrollAnchor[], right: ScrollAnchor[]): AnchorPair[] {
  const sortedLeft = [...left].sort((a, b) => a.y - b.y);
  const sortedRight = [...right].sort((a, b) => a.y - b.y);
  const normalisedLeft = sortedLeft.map((anchor) => normalizeHeading(anchor.text));
  const used = new Set<number>();
  const pairs: AnchorPair[] = [];
  let minLeftY = -Infinity;
  for (const anchor of sortedRight) {
    const text = normalizeHeading(anchor.text);
    if (text.length < 2) continue;
    let hit = -1;
    for (let index = 0; index < sortedLeft.length; index += 1) {
      if (!used.has(index) && normalisedLeft[index] === text) {
        hit = index;
        break;
      }
    }
    if (hit < 0) {
      for (let index = 0; index < sortedLeft.length; index += 1) {
        if (!used.has(index) && headingsSimilar(normalisedLeft[index], text)) {
          hit = index;
          break;
        }
      }
    }
    if (hit < 0 || sortedLeft[hit].y <= minLeftY) continue;
    used.add(hit);
    minLeftY = sortedLeft[hit].y;
    pairs.push({ left: sortedLeft[hit].y, right: anchor.y });
  }
  return pairs;
}

function segmentSlope(a: AnchorPair, b: AnchorPair, from: 'left' | 'right'): number {
  const srcA = from === 'left' ? a.left : a.right;
  const srcB = from === 'left' ? b.left : b.right;
  const dstA = from === 'left' ? a.right : a.left;
  const dstB = from === 'left' ? b.right : b.left;
  const span = srcB - srcA;
  return span > 0 ? (dstB - dstA) / span : 1;
}

/**
 * Translate a scrollTop between the two panes. Between neighbouring pairs the
 * map interpolates linearly; outside the first/last pair the nearest segment
 * slope is extrapolated, and a single pair keeps a constant offset. The result
 * is clamped at 0 — the caller's own max clamp bounds the other end.
 */
export function mapAnchorScroll(
  pairs: AnchorPair[],
  position: number,
  from: 'left' | 'right',
): number | null {
  if (!pairs.length) return null;
  const sorted = [...pairs].sort((a, b) => (from === 'left' ? a.left - b.left : a.right - b.right));
  const src = (pair: AnchorPair) => (from === 'left' ? pair.left : pair.right);
  const dst = (pair: AnchorPair) => (from === 'left' ? pair.right : pair.left);
  const first = sorted[0];
  const last = sorted[sorted.length - 1];
  let result: number;
  if (sorted.length === 1) {
    result = dst(first) + (position - src(first));
  } else if (position <= src(first)) {
    result = dst(first) - (src(first) - position) * segmentSlope(sorted[0], sorted[1], from);
  } else if (position >= src(last)) {
    result =
      dst(last) + (position - src(last)) * segmentSlope(sorted[sorted.length - 2], last, from);
  } else {
    result = dst(last);
    for (let index = 0; index < sorted.length - 1; index += 1) {
      const a = sorted[index];
      const b = sorted[index + 1];
      if (position <= src(b)) {
        result = dst(a) + (position - src(a)) * segmentSlope(a, b, from);
        break;
      }
    }
  }
  return Math.max(0, result);
}
