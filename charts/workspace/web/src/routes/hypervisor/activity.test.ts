import { describe, it, expect } from 'vitest';
import { fmtDuration, entryTone, entryLabel, totalErrors, clip } from './activity';
import type { ActivityEntry } from '../../api/hypervisor';

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
    expect(totalErrors({ tool_calls: 3, tool_results: 3, tool_errors: 2, errors: 1, messages: 4 })).toBe(3);
    expect(totalErrors({ tool_calls: 1, tool_results: 1, tool_errors: 0, errors: 0, messages: 1 })).toBe(0);
    expect(totalErrors(null)).toBe(0);
    expect(totalErrors(undefined)).toBe(0);
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
