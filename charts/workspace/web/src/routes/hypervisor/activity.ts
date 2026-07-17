import type { ActivityEntry, ActivityCounts } from '../../api/hypervisor';

/** Pure presentation helpers for the hypervisor Activity panel. Kept separate
 *  from the component so the formatting logic is unit-testable without a DOM. */

/** Human-readable duration for a tool run: "820ms", "3.4s", "1m 05s". */
export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  const m = Math.floor(s / 60);
  const rem = Math.round(s % 60);
  return `${m}m ${String(rem).padStart(2, '0')}s`;
}

/** The status dot class-suffix for a timeline entry. */
export function entryTone(e: ActivityEntry): 'ok' | 'error' | 'pending' | 'muted' {
  if (e.kind === 'error') return 'error';
  if (e.kind === 'status') return 'muted';
  if (e.kind === 'tool') {
    if (e.status === 'error') return 'error';
    if (e.status === 'pending') return 'pending';
    return 'ok';
  }
  // orphan result
  return e.status === 'error' ? 'error' : 'ok';
}

/** Total error count surfaced in the header badge (tool failures + hard errors). */
export function totalErrors(counts: ActivityCounts | null | undefined): number {
  if (!counts) return 0;
  return (counts.tool_errors || 0) + (counts.errors || 0);
}

/** A short, single-line label for a timeline entry. */
export function entryLabel(e: ActivityEntry): string {
  switch (e.kind) {
    case 'tool':
      return e.tool || 'tool';
    case 'tool_result_orphan':
      return 'result';
    case 'error':
      return 'error';
    case 'status':
      return `status → ${e.status ?? '?'}`;
    default:
      return 'event';
  }
}

/** Collapse whitespace and clip long result/error text for a one-line preview. */
export function clip(text: string | null | undefined, max = 140): string {
  if (!text) return '';
  const t = text.replace(/\s+/g, ' ').trim();
  return t.length > max ? `${t.slice(0, max - 1)}…` : t;
}
