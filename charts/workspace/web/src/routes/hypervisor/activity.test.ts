import { describe, it, expect } from 'vitest';
import {
  fmtDuration, entryTone, entryLabel, totalErrors, clip,
  toolTitle, toolSubtitle, primaryArg, summaryBadges, categoryOf,
} from './activity';
import type { ActivityEntry, ActivityCounts } from '../../api/hypervisor';

const counts = (p: Partial<ActivityCounts>): ActivityCounts => ({
  tool_calls: 0, tool_results: 0, tool_errors: 0, errors: 0, messages: 0,
  builds: 0, subagents: 0, ...p,
});

describe('fmtDuration', () => {
  it('formats sub-second, seconds, and minutes', () => {
    expect(fmtDuration(0)).toBe('0ms');
    expect(fmtDuration(820)).toBe('820ms');
    expect(fmtDuration(3400)).toBe('3.4s');
    expect(fmtDuration(45000)).toBe('45s');
    expect(fmtDuration(65000)).toBe('1m 05s');
  });

  it('returns empty for null/negative/non-finite', () => {
    expect(fmtDuration(null)).toBe('');
    expect(fmtDuration(undefined)).toBe('');
    expect(fmtDuration(-5)).toBe('');
    expect(fmtDuration(Number.NaN)).toBe('');
  });
});

describe('entryTone', () => {
  const tool = (status: string): ActivityEntry =>
    ({ kind: 'tool', seq: 1, ts: 1, status } as ActivityEntry);
  it('maps tool status to tone', () => {
    expect(entryTone(tool('ok'))).toBe('ok');
    expect(entryTone(tool('error'))).toBe('error');
    expect(entryTone(tool('pending'))).toBe('pending');
  });
  it('maps error/status/orphan kinds', () => {
    expect(entryTone({ kind: 'error', seq: 1, ts: 1 } as ActivityEntry)).toBe('error');
    expect(entryTone({ kind: 'status', seq: 1, ts: 1 } as ActivityEntry)).toBe('muted');
    expect(entryTone({ kind: 'tool_result_orphan', seq: 1, ts: 1, status: 'error' } as ActivityEntry)).toBe('error');
    expect(entryTone({ kind: 'tool_result_orphan', seq: 1, ts: 1, status: 'ok' } as ActivityEntry)).toBe('ok');
  });
});

describe('entryLabel', () => {
  it('labels each kind', () => {
    expect(entryLabel({ kind: 'tool', seq: 1, ts: 1, tool: 'get_metrics' } as ActivityEntry)).toBe('get_metrics');
    expect(entryLabel({ kind: 'tool', seq: 1, ts: 1 } as ActivityEntry)).toBe('tool');
    expect(entryLabel({ kind: 'tool_result_orphan', seq: 1, ts: 1 } as ActivityEntry)).toBe('result');
    expect(entryLabel({ kind: 'error', seq: 1, ts: 1 } as ActivityEntry)).toBe('error');
    expect(entryLabel({ kind: 'status', seq: 1, ts: 1, status: 'idle' } as ActivityEntry)).toBe('status → idle');
  });
});

describe('totalErrors', () => {
  it('sums tool errors and hard errors', () => {
    expect(totalErrors(counts({ tool_errors: 2, errors: 1 }))).toBe(3);
    expect(totalErrors(counts({}))).toBe(0);
    expect(totalErrors(null)).toBe(0);
    expect(totalErrors(undefined)).toBe(0);
  });
});

describe('category helpers', () => {
  const tool = (p: Partial<ActivityEntry>): ActivityEntry =>
    ({ kind: 'tool', seq: 1, ts: 1, status: 'ok', ...p } as ActivityEntry);

  it('categoryOf defaults to tool', () => {
    expect(categoryOf(tool({}))).toBe('tool');
    expect(categoryOf(tool({ category: 'build' }))).toBe('build');
  });

  it('toolTitle reads like a working log per category', () => {
    expect(toolTitle(tool({ category: 'build' }))).toBe('Started build');
    expect(toolTitle(tool({ category: 'subagent', subagent_type: 'explore' }))).toBe('Sub-agent · explore');
    expect(toolTitle(tool({ category: 'subagent' }))).toBe('Sub-agent');
    expect(toolTitle(tool({ category: 'tool', label: 'Bash' }))).toBe('Bash');
  });

  it('toolSubtitle prefers sub-agent description, else a primary arg', () => {
    expect(toolSubtitle(tool({ category: 'subagent', description: 'map the codebase' }))).toBe('map the codebase');
    expect(toolSubtitle(tool({ category: 'tool', input: { command: 'npm run build' } }))).toBe('npm run build');
    expect(toolSubtitle(tool({ category: 'tool', input: {} }))).toBe('');
  });

  it('primaryArg picks the most meaningful field', () => {
    expect(primaryArg({ command: 'ls -la' })).toBe('ls -la');
    expect(primaryArg({ prompt: 'do a thing', extra: 1 })).toBe('do a thing');
    expect(primaryArg({ port: 8080 })).toBe('8080');
    expect(primaryArg('raw string')).toBe('raw string');
    expect(primaryArg(null)).toBe('');
    expect(primaryArg({ nothing: true })).toBe('');
  });

  it('summaryBadges lists tools + non-zero high-signal categories', () => {
    const b = summaryBadges(counts({ tool_calls: 5, builds: 1, subagents: 2 }));
    expect(b.map((x) => x.key)).toEqual(['tools', 'builds', 'subagents']);
    expect(b.map((x) => x.label)).toEqual(['5 tools', '1 build', '2 sub-agents']);
  });

  it('summaryBadges omits zero categories and handles null', () => {
    expect(summaryBadges(counts({ tool_calls: 1 })).map((x) => x.key)).toEqual(['tools']);
    expect(summaryBadges(null)).toEqual([]);
  });
});

describe('clip', () => {
  it('collapses whitespace and truncates', () => {
    expect(clip('  hello   world \n')).toBe('hello world');
    expect(clip('')).toBe('');
    expect(clip(null)).toBe('');
    expect(clip('x'.repeat(200), 10)).toBe(`${'x'.repeat(9)}…`);
  });
});
