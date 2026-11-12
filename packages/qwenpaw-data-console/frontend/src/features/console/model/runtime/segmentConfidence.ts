import type { SegmentConfidence } from '../../api/types';

/** Hide unavailable or malformed scoring without changing the Segment itself. */
export function readSegmentConfidence(value: unknown): SegmentConfidence | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const candidate = value as Partial<SegmentConfidence>;
  if (candidate.level !== 'high' && candidate.level !== 'medium' && candidate.level !== 'low') {
    return null;
  }
  if (typeof candidate.summary !== 'string' || !candidate.summary.trim()) return null;
  return candidate as SegmentConfidence;
}
