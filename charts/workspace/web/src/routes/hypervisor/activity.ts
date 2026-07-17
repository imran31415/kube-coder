import type { ActivityEntry, ActivityCounts, ActivityCategory } from '../../api/hypervisor';
import type { IconName } from '../../components/Icon';

/** Pure presentation helpers for the hypervisor Activity panel. Kept separate
 *  from the component so the formatting logic is unit-testable without a DOM. */

/** Per-category presentation: the icon + short label used for the pill and the
 *  header badges. Categories mirror the backend classifier. */
export const CATEGORY_META: Record<ActivityCategory, { label: string; icon: IconName }> = {
  build: { label: 'Build', icon: 'play' },
  subagent: { label: 'Sub-agent', icon: 'hypervisor' },
  app: { label: 'App', icon: 'apps' },
  memory: { label: 'Memory', icon: 'memory' },
  task: { label: 'Task', icon: 'tasks' },
  tool: { label: 'Tool', icon: 'terminal' },
};

export function categoryOf(e: ActivityEntry): ActivityCategory {
  return e.category ?? 'tool';
}

/** The primary, human-readable title for a tool row — reads like Claude Code's
 *  working log ("Started build", "Sub-agent · explore", or the raw tool name). */
export function toolTitle(e: ActivityEntry): string {
  switch (e.category) {
    case 'build':
      return 'Started build';
    case 'subagent':
      return e.subagent_type ? `Sub-agent · ${e.subagent_type}` : 'Sub-agent';
    default:
      return e.label || e.tool || 'tool';
  }
}

/** A representative one-line secondary detail for a tool row: the sub-agent's
 *  description, or the most meaningful argument from the tool input. */
export function toolSubtitle(e: ActivityEntry): string {
  if (e.category === 'subagent') return clip(e.description, 120);
  return clip(primaryArg(e.input), 120);
}

/** Best-effort "primary argument" of a tool call for the secondary line —
 *  mirrors what Claude Code echoes in parens after a tool name. */
export function primaryArg(input: unknown): string {
  if (input == null) return '';
  if (typeof input === 'string') return input;
  if (typeof input !== 'object') return String(input);
  const o = input as Record<string, unknown>;
  for (const k of ['command', 'prompt', 'query', 'path', 'file_path', 'name', 'message', 'namespace', 'url', 'port']) {
    const v = o[k];
    if (typeof v === 'string' && v.trim()) return v;
    if (typeof v === 'number') return String(v);
  }
  return '';
}

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

/** Ordered summary badges for the panel header — tools, then the high-signal
 *  categories the maintainer wants tracked (builds, sub-agents), each shown
 *  only when non-zero. Errors are rendered separately. */
export function summaryBadges(
  counts: ActivityCounts | null | undefined,
): { key: string; label: string; icon: IconName }[] {
  if (!counts) return [];
  const out: { key: string; label: string; icon: IconName }[] = [];
  const plural = (n: number, one: string, many = `${one}s`) => (n === 1 ? one : many);
  if (counts.tool_calls) {
    out.push({ key: 'tools', label: `${counts.tool_calls} ${plural(counts.tool_calls, 'tool')}`, icon: CATEGORY_META.tool.icon });
  }
  if (counts.builds) {
    out.push({ key: 'builds', label: `${counts.builds} ${plural(counts.builds, 'build')}`, icon: CATEGORY_META.build.icon });
  }
  if (counts.subagents) {
    out.push({ key: 'subagents', label: `${counts.subagents} ${plural(counts.subagents, 'sub-agent')}`, icon: CATEGORY_META.subagent.icon });
  }
  return out;
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
